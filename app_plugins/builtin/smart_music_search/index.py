"""Incremental SQLite index; never changes the source music library."""

import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from app_paths import APP_ROOT
from .audio import (
    AUDIO_SUFFIXES, SEGMENT_SECONDS, decode_segment, media_duration, segment_starts,
)

INDEX_ROOT = APP_ROOT / "SmartMusicSearch"


def _name_key(text):
    return "".join(char for char in unicodedata.normalize("NFKC", str(text)).casefold()
                   if char.isalnum())


def discover_music(root, cancelled=lambda: False):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"配乐库目录无法访问：{root}；已有索引保持不变")
    found = []
    for directory, _subdirs, names in os.walk(root):
        if cancelled():
            return None
        for name in names:
            if Path(name).suffix.lower() in AUDIO_SUFFIXES:
                found.append(str(Path(directory) / name))
    return sorted(found, key=str.casefold)


class MusicIndex:
    def __init__(self, root=None, filename="index.sqlite3"):
        self.root = Path(root or INDEX_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        if Path(filename).name != filename:
            raise ValueError("索引文件名不能包含目录")
        self.path = self.root / filename
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tracks (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, duration REAL NOT NULL,
                model_id TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                indexed_at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS segments (
                path TEXT NOT NULL, start REAL NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY (path, start),
                FOREIGN KEY (path) REFERENCES tracks(path) ON DELETE CASCADE
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS tracks_model ON tracks(model_id)")

    @classmethod
    def for_encoder(cls, encoder, root=None):
        from .encoder import MODEL_ID
        # Preserve the original database byte-for-byte when using new models.
        filename = "index.sqlite3" if encoder.model_id == MODEL_ID else (
            f"index-{encoder.model_key}-{encoder.coverage}-v3.sqlite3"
        )
        return cls(root, filename)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def count(self, model_id):
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM tracks WHERE model_id=? AND error=''",
                (model_id,),
            ).fetchone()[0])

    def failures(self, model_id):
        with self._connect() as db:
            return [(row["path"], row["error"]) for row in db.execute(
                "SELECT path,error FROM tracks WHERE model_id=? AND error<>'' "
                "AND error NOT LIKE 'SKIP:%' ORDER BY path",
                (model_id,),
            )]

    def search_filename(self, query, *, seconds=0):
        """Find literal title matches separately; never alter audio similarity scores."""
        query_name = Path(str(query).strip()).name
        if Path(query_name).suffix.lower() in AUDIO_SUFFIXES:
            query_name = Path(query_name).stem
        needle = _name_key(query_name)
        if not needle:
            return []
        with self._connect() as db:
            tracks = db.execute("SELECT path,duration FROM tracks").fetchall()
        matches = []
        for row in tracks:
            path = Path(row["path"])
            title = _name_key(path.stem)
            position = title.find(needle)
            if position < 0 or not path.is_file():
                continue
            duration = float(row["duration"])
            matches.append({
                "path": str(path), "duration": duration, "start": 0.0,
                "score": 0.0, "short": bool(seconds and duration < seconds),
                "match_type": "filename", "_order": (position, len(title), str(path).casefold()),
            })
        matches.sort(key=lambda row: row["_order"])
        for row in matches:
            del row["_order"]
        return matches

    def sync(self, root, encoder, ffmpeg, ffprobe, progress=None, cancelled=None):
        progress = progress or (lambda _done, _total, _msg: None)
        cancelled = cancelled or (lambda: False)
        files = discover_music(root, cancelled)
        if files is None:
            return {"cancelled": True, "updated": 0, "failed": 0,
                    "skipped": 0, "total": 0}
        with self._connect() as db:
            old = {row["path"]: row for row in db.execute(
                "SELECT path,size,mtime_ns,duration,model_id,error FROM tracks"
            )}
        pending = []
        for path in files:
            if cancelled():
                return {"cancelled": True, "updated": 0, "failed": 0,
                        "skipped": 0,
                        "total": len(files)}
            try:
                stat = Path(path).stat()
            except OSError:
                continue
            row = old.get(path)
            if (row and row["size"] == stat.st_size
                    and row["mtime_ns"] == stat.st_mtime_ns
                    and row["model_id"] == encoder.model_id
                    and not (0 < row["duration"] < SEGMENT_SECONDS
                             and row["error"] != "SKIP: too short")):
                continue
            pending.append((path, stat.st_size, stat.st_mtime_ns))
        updated = failed = skipped = 0
        consecutive_model_failures = 0
        total = len(pending)
        if total:
            progress(0, total, "正在加载音频模型…")
            encoder._load_music()
        for path, size, mtime_ns in pending:
            if cancelled():
                break
            duration, vectors, error = 0.0, [], ""
            model_failed = False
            try:
                duration = media_duration(path, ffprobe)
                if duration < SEGMENT_SECONDS:
                    # CLAP indexes ten-second excerpts. Tiny effects are not
                    # useful as full-song candidates and must not abort a scan.
                    error = "SKIP: too short"
                    skipped += 1
                else:
                    starts = (encoder.segment_starts(duration) if hasattr(encoder, "segment_starts")
                              else segment_starts(duration))
                    for start in starts:
                        if cancelled():
                            break
                        samples = decode_segment(path, start, ffmpeg)
                        try:
                            vector = encoder.audio(samples)
                        except Exception:
                            model_failed = True
                            raise
                        vectors.append((start, np.asarray(vector, dtype=np.float16).tobytes()))
                    if cancelled():
                        break
                    if not vectors:
                        raise ValueError("没有得到音频特征")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failed += 1
                if model_failed:
                    consecutive_model_failures += 1
                else:
                    consecutive_model_failures = 0
                if consecutive_model_failures >= 3:
                    raise RuntimeError(
                        "连续 3 首可解码音频在模型推理阶段失败，已停止。"
                        f"最后错误：{error}"
                    ) from exc
            else:
                consecutive_model_failures = 0
            with self._connect() as db:
                db.execute("DELETE FROM segments WHERE path=?", (path,))
                db.execute("""INSERT INTO tracks
                    (path,size,mtime_ns,duration,model_id,error,indexed_at)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,mtime_ns=excluded.mtime_ns,
                    duration=excluded.duration,model_id=excluded.model_id,
                    error=excluded.error,indexed_at=excluded.indexed_at""",
                    (path, size, mtime_ns, duration, encoder.model_id, error, time.time()),
                )
                db.executemany(
                    "INSERT INTO segments(path,start,vector) VALUES (?,?,?)",
                    ((path, start, blob) for start, blob in vectors) if not error else (),
                )
            updated += 1
            progress(updated, total, f"已处理 {updated}/{total}：{Path(path).name}")
        if not cancelled():
            valid = set(files)
            with self._connect() as db:
                db.executemany(
                    "DELETE FROM tracks WHERE path=?",
                    ((path,) for path in old if path not in valid),
                )
        return {"cancelled": bool(cancelled()), "updated": updated,
                "failed": failed, "skipped": skipped, "total": len(files)}

    def search(self, model_id, query_vector, *, seconds=0, limit=50,
               include_short=False, stable=False):
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1).copy()
        norm = float(np.linalg.norm(query))
        if not np.isfinite(query).all() or not np.isfinite(norm) or norm < 1e-8:
            raise ValueError("查询特征无效，不能进行音乐搜索")
        query /= norm
        with self._connect() as db:
            rows = db.execute("""SELECT t.path,t.duration,s.start,s.vector
                FROM tracks AS t JOIN segments AS s ON s.path=t.path
                WHERE t.model_id=? AND t.error='' ORDER BY t.path,s.start""",
                (model_id,),
            ).fetchall()
        parsed = []
        existing = {}
        for row in rows:
            path = row["path"]
            if path not in existing:
                existing[path] = Path(path).is_file()
            if not existing[path]:
                continue
            duration = float(row["duration"])
            vector = np.frombuffer(row["vector"], dtype=np.float16).astype(np.float32)
            norm = float(np.linalg.norm(vector))
            if vector.size != query.size or not np.isfinite(vector).all() or norm < 1e-8:
                continue
            vector /= norm
            parsed.append((path, duration, float(row["start"]), vector))
        if not parsed:
            return []
        grouped = {}
        similarities = np.stack([row[3] for row in parsed]) @ query
        for (path, duration, start, _vector), similarity in zip(parsed, similarities):
            if seconds and duration < seconds and not include_short:
                continue
            grouped.setdefault(path, {"duration": duration, "points": []})["points"].append(
                (start, float(similarity))
            )
        results = []
        for path, info in grouped.items():
            duration, points = info["duration"], info["points"]
            best = None
            window_seconds = seconds or (min(30.0, duration) if stable else 0)
            global_mean = float(np.mean([score for _start, score in points]))
            if stable:
                # Evaluate all candidate windows together; calling percentile
                # in a Python loop makes dense indexes needlessly slow.
                positions, scores = np.asarray(points, dtype=np.float64).T
                candidates = np.unique(np.minimum(positions, max(0., duration-window_seconds)))
                mask = ((positions[None, :] >= candidates[:, None])
                        & (positions[None, :] < candidates[:, None]+window_seconds))
                counts = mask.sum(axis=1)
                valid = counts > 0
                candidates, mask, counts = candidates[valid], mask[valid], counts[valid]
                ordered = np.sort(np.where(mask, scores[None, :], np.inf), axis=1)
                quantiles = (counts-1) * .25
                lower = quantiles.astype(int)
                upper = np.ceil(quantiles).astype(int)
                fraction = quantiles-lower
                row_ids = np.arange(len(candidates))
                low_scores = (ordered[row_ids, lower]*(1-fraction)
                              + ordered[row_ids, upper]*fraction)
                means = np.where(mask, scores[None, :], 0.).sum(axis=1)/counts
                totals = .65*means + .25*low_scores + .10*global_mean
                if len(totals):
                    chosen = int(np.argmax(totals))
                    best = float(totals[chosen]), float(candidates[chosen])
            else:
                for start, point_score in points:
                    candidate = min(start, max(0.0, duration - window_seconds)) if window_seconds else start
                    covered = [score for position, score in points
                               if candidate <= position < candidate + window_seconds] if window_seconds else [point_score]
                    if not covered:
                        covered = [point_score]
                    score = 0.8 * float(np.mean(covered)) + 0.2 * min(covered)
                    if best is None or score > best[0]:
                        best = score, candidate
            if best:
                results.append({"path": path, "duration": duration,
                                "start": best[1], "score": best[0],
                                "short": bool(seconds and duration < seconds)})
        results.sort(key=lambda item: (-item["score"], item["path"].casefold()))
        return results if limit is None else results[:limit]

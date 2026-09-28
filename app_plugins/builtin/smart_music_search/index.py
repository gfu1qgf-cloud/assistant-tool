"""Incremental SQLite index; never changes the source music library."""

import os
import sqlite3
import time
from pathlib import Path

import numpy as np

from app_paths import APP_ROOT
from .audio import AUDIO_SUFFIXES, decode_segment, media_duration, segment_starts

INDEX_ROOT = APP_ROOT / "SmartMusicSearch"


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
    def __init__(self, root=None):
        self.root = Path(root or INDEX_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.sqlite3"
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

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def count(self, model_id):
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM tracks WHERE model_id=? AND error=''",
                (model_id,),
            ).fetchone()[0])

    def failures(self, model_id):
        with self._connect() as db:
            return [(row["path"], row["error"]) for row in db.execute(
                "SELECT path,error FROM tracks WHERE model_id=? AND error<>'' ORDER BY path",
                (model_id,),
            )]

    def sync(self, root, encoder, ffmpeg, ffprobe, progress=None, cancelled=None):
        progress = progress or (lambda _done, _total, _msg: None)
        cancelled = cancelled or (lambda: False)
        files = discover_music(root, cancelled)
        if files is None:
            return {"cancelled": True, "updated": 0, "failed": 0, "total": 0}
        with self._connect() as db:
            old = {row["path"]: row for row in db.execute(
                "SELECT path,size,mtime_ns,model_id FROM tracks"
            )}
        pending = []
        for path in files:
            if cancelled():
                return {"cancelled": True, "updated": 0, "failed": 0,
                        "total": len(files)}
            try:
                stat = Path(path).stat()
            except OSError:
                continue
            row = old.get(path)
            if (row and row["size"] == stat.st_size
                    and row["mtime_ns"] == stat.st_mtime_ns
                    and row["model_id"] == encoder.model_id):
                continue
            pending.append((path, stat.st_size, stat.st_mtime_ns))
        updated = failed = 0
        consecutive_failures = 0
        total = len(pending)
        if total:
            progress(0, total, "正在加载音频模型…")
            encoder._load_music()
        for path, size, mtime_ns in pending:
            if cancelled():
                break
            duration, vectors, error = 0.0, [], ""
            try:
                duration = media_duration(path, ffprobe)
                for start in segment_starts(duration):
                    if cancelled():
                        break
                    samples = decode_segment(path, start, ffmpeg)
                    vectors.append((start, np.asarray(
                        encoder.audio(samples), dtype=np.float16
                    ).tobytes()))
                if cancelled():
                    break
                if not vectors:
                    raise ValueError("没有得到音频特征")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    raise RuntimeError(
                        "连续 3 首音频索引失败，已停止以避免整库失败。"
                        f"最后错误：{error}"
                    ) from exc
            else:
                consecutive_failures = 0
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
                "failed": failed, "total": len(files)}

    def search(self, model_id, query_vector, *, seconds=0, limit=50,
               include_short=False):
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        query /= max(1e-8, float(np.linalg.norm(query)))
        with self._connect() as db:
            rows = db.execute("""SELECT t.path,t.duration,s.start,s.vector
                FROM tracks AS t JOIN segments AS s ON s.path=t.path
                WHERE t.model_id=? AND t.error='' ORDER BY t.path,s.start""",
                (model_id,),
            ).fetchall()
        grouped = {}
        for row in rows:
            path = row["path"]
            if not Path(path).is_file():
                continue
            duration = float(row["duration"])
            if seconds and duration < seconds and not include_short:
                continue
            vector = np.frombuffer(row["vector"], dtype=np.float16).astype(np.float32)
            if vector.size != query.size:
                continue
            similarity = float(np.dot(vector, query) / max(1e-8, np.linalg.norm(vector)))
            grouped.setdefault(path, {"duration": duration, "points": []})["points"].append(
                (float(row["start"]), similarity)
            )
        results = []
        for path, info in grouped.items():
            duration, points = info["duration"], info["points"]
            best = None
            for start, point_score in points:
                candidate = min(start, max(0.0, duration - seconds)) if seconds else start
                covered = [score for position, score in points
                           if candidate <= position < candidate + seconds] if seconds else [point_score]
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
        return results[:limit]

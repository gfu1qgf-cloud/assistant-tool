"""Incremental, local-only feature index for inventory and person images."""

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from app_paths import APP_ROOT
from .encoder import ModelLoadError
from model.InventoryManager import MATERIAL_IMAGE_SUFFIXES


INDEX_ROOT = APP_ROOT / "SmartImageSearch"


def discover_external_groups(roots):
    """Include configured folders without copying files into the inventory."""
    groups = []
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"图片库目录无法访问：{root}；原索引未删除")
        images = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in MATERIAL_IMAGE_SUFFIXES:
                relative = path.parent.relative_to(root)
                label = root.name if relative == Path(".") else f"{root.name}/{relative}"
                images.append({"path": str(path), "source_name": label})
        groups.append({"source_kind": "folder", "name": root.name,
                       "images": images})
    return groups


class ImageSearchIndex:
    def __init__(self, root=None):
        self.root = Path(root or INDEX_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self.thumbnails = self.root / "thumbnails"
        self.thumbnails.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.sqlite3"
        self._lock = threading.RLock()
        self._matrix_cache = None
        self._inactive_indices = set()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    vector BLOB,
                    thumbnail TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    indexed_at REAL NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS images_model ON images(model_id)"
            )

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    def count(self, model_id=None):
        with self._lock, self._connect() as connection:
            if model_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM images WHERE vector IS NOT NULL"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM images WHERE model_id=? AND vector IS NOT NULL",
                    (model_id,),
                ).fetchone()
            return int(row[0])

    def _thumbnail(self, path):
        from PIL import Image, ImageOps
        digest = hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
        target = self.thumbnails / f"{digest}.jpg"
        temporary = target.with_suffix(".tmp")
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((220, 170))
            image.save(temporary, format="JPEG", quality=78)
            image.close()
        os.replace(temporary, target)
        return str(target)

    def sync(self, groups, encoder, progress=None, cancelled=None, batch_size=4):
        """Resume safely: committed batches remain available after cancellation."""
        progress = progress or (lambda _done, _total, _message: None)
        cancelled = cancelled or (lambda: False)
        current = {}
        for group in groups:
            for image in group.get("images", ()):
                path = str(image.get("path") or "")
                if path:
                    current.setdefault(os.path.normcase(os.path.abspath(path)), (
                        path, str(group.get("source_kind") or ""),
                        str(image.get("source_name") or group.get("name") or ""),
                    ))
        with self._lock, self._connect() as connection:
            old = {
                row["path"]: row for row in connection.execute(
                    "SELECT path,size,mtime_ns,model_id,source_kind,source_name FROM images"
                )
            }
        pending = []
        metadata = []
        for key, (path, source_kind, source_name) in current.items():
            try:
                stat = Path(path).stat()
            except OSError:
                continue
            existing = old.get(key)
            if (existing is not None and existing["size"] == stat.st_size
                    and existing["mtime_ns"] == stat.st_mtime_ns
                    and existing["model_id"] == encoder.model_id):
                if (existing["source_kind"] != source_kind
                        or existing["source_name"] != source_name):
                    metadata.append((source_kind, source_name, key))
                continue
            pending.append((key, path, stat.st_size, stat.st_mtime_ns,
                            source_kind, source_name))
        removed = set(old) - set(current)
        total = len(pending)
        done = 0
        failures = 0
        if not cancelled():
            with self._lock, self._connect() as connection:
                connection.executemany(
                    "UPDATE images SET source_kind=?,source_name=? WHERE path=?",
                    metadata,
                )
                if removed:
                    connection.executemany(
                        "DELETE FROM images WHERE path=?",
                        ((path,) for path in removed),
                    )
                if metadata or removed:
                    self._matrix_cache = None
                    self._inactive_indices.clear()
        for offset in range(0, total, max(1, batch_size)):
            if cancelled():
                break
            chunk = pending[offset:offset + max(1, batch_size)]
            try:
                vectors = encoder.images([row[1] for row in chunk])
                if len(vectors) != len(chunk):
                    raise ValueError("模型返回的特征数量与图片数量不一致")
                outcomes = [(row, vector, "") for row, vector in zip(chunk, vectors)]
            except ModelLoadError:
                raise
            except Exception as error:
                # A damaged image must not prevent all other images from indexing.
                outcomes = []
                for row in chunk:
                    if cancelled():
                        break
                    try:
                        outcomes.append((row, encoder.image(row[1]), ""))
                    except Exception as item_error:
                        outcomes.append((row, None, f"{type(item_error).__name__}: {item_error}"))
                if not outcomes and not cancelled():
                    raise error
            updates = []
            for row, vector, error in outcomes:
                key, path, size, mtime_ns, source_kind, source_name = row
                try:
                    thumbnail = self._thumbnail(path) if vector is not None else ""
                except Exception as image_error:
                    thumbnail = ""
                    error = f"缩略图失败：{image_error}"
                blob = None if vector is None else np.asarray(
                    vector, dtype=np.float16
                ).tobytes()
                updates.append((key, size, mtime_ns, source_kind, source_name,
                                encoder.model_id, blob, thumbnail, error, time.time()))
                failures += bool(vector is None)
            with self._lock, self._connect() as connection:
                connection.executemany("""
                    INSERT INTO images (path,size,mtime_ns,source_kind,source_name,
                                        model_id,vector,thumbnail,error,indexed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        size=excluded.size, mtime_ns=excluded.mtime_ns,
                        source_kind=excluded.source_kind,
                        source_name=excluded.source_name,model_id=excluded.model_id,
                        vector=excluded.vector,thumbnail=excluded.thumbnail,
                        error=excluded.error,indexed_at=excluded.indexed_at
                """, updates)
                self._matrix_cache = None
                self._inactive_indices.clear()
            done += len(outcomes)
            progress(done, total, f"已建立索引 {done}/{total}，失败 {failures}")
        return {"new_or_changed": done, "needed": total,
                "removed": len(removed) if not cancelled() else 0,
                "failed": failures, "cancelled": bool(cancelled())}

    def remove_paths(self, paths):
        keys = [os.path.normcase(os.path.abspath(str(path))) for path in paths]
        with self._lock, self._connect() as connection:
            connection.executemany("DELETE FROM images WHERE path=?",
                                   ((key,) for key in keys))
            if self._matrix_cache is not None:
                positions = self._matrix_cache[3]
                self._inactive_indices.update(
                    positions[key] for key in keys if key in positions
                )

    def _matrix(self, model_id):
        with self._lock:
            if self._matrix_cache is not None and self._matrix_cache[0] == model_id:
                return self._matrix_cache[1:3]
            with self._connect() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT path,source_kind,source_name,thumbnail,vector "
                    "FROM images WHERE model_id=? AND vector IS NOT NULL",
                    (model_id,),
                )]
            if not rows:
                result = ([], np.empty((0, 0), dtype=np.float32))
            else:
                dimensions = len(rows[0]["vector"]) // 2
                rows = [row for row in rows if len(row["vector"]) == dimensions * 2]
                vectors = np.stack([
                    np.frombuffer(row.pop("vector"), dtype=np.float16).astype(np.float32)
                    for row in rows
                ])
                result = (rows, vectors)
            positions = {row["path"]: index for index, row in enumerate(result[0])}
            self._matrix_cache = (model_id, *result, positions)
            self._inactive_indices.clear()
            return result

    def search(self, model_id, vector, limit=60, source_kind=""):
        """Return ranked matches; limit=None allows the UI to page every match."""
        rows, matrix = self._matrix(model_id)
        if not rows:
            return []
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        if query.size != matrix.shape[1]:
            raise ValueError("搜索模型与图片索引不匹配，请重新建立索引")
        length = float(np.linalg.norm(query))
        if not length:
            return []
        query /= length
        scores = matrix @ query
        if self._inactive_indices:
            scores[list(self._inactive_indices)] = -np.inf
        if source_kind:
            excluded = [index for index, row in enumerate(rows)
                        if row["source_kind"] != source_kind]
            scores[excluded] = -np.inf
        target = len(rows) if limit is None else max(0, int(limit))
        if not target:
            return []
        result = []
        # Sorting the full score array is cheap for a local library and avoids
        # losing results when many top-ranked files were moved externally.
        for index in np.argsort(-scores):
            row = rows[int(index)]
            if not np.isfinite(scores[index]):
                continue
            if source_kind and row["source_kind"] != source_kind:
                continue
            if not Path(row["path"]).is_file():
                continue
            result.append({**row, "score": float(scores[index])})
            if len(result) >= target:
                break
        return result

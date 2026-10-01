"""Explicit, query-specific user preferences; never infer moods from filenames."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def search_key(query, moods=(), sounds=()):
    return json.dumps({"query": str(query).strip().casefold(),
                       "moods": sorted(set(moods)), "sounds": sorted(set(sounds))},
                      ensure_ascii=False, sort_keys=True)


class MusicFeedback:
    def __init__(self, root):
        self.path = Path(root) / "preferences.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS feedback (
                query_key TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, preference INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(query_key,path)
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _path(path):
        return os.path.normcase(os.path.abspath(path))

    def set(self, query_key, path, preference):
        if not query_key or preference not in {-1, 0, 1}:
            raise ValueError("情绪标记无效")
        path = self._path(path)
        with self._connect() as db:
            if preference == 0:
                db.execute("DELETE FROM feedback WHERE query_key=? AND path=?", (query_key, path))
            else:
                stat = Path(path).stat()
                db.execute("""INSERT INTO feedback VALUES (?,?,?,?,?,?)
                    ON CONFLICT(query_key,path) DO UPDATE SET size=excluded.size,
                    mtime_ns=excluded.mtime_ns,preference=excluded.preference,
                    updated_at=excluded.updated_at""",
                    (query_key, path, stat.st_size, stat.st_mtime_ns, preference, time.time()))

    def apply(self, rows, query_key, enabled=True):
        with self._connect() as db:
            saved = {row[0]: row[1:] for row in db.execute(
                "SELECT path,size,mtime_ns,preference FROM feedback WHERE query_key=?", (query_key,)
            )} if query_key else {}
        results = []
        for source in rows:
            row = dict(source)
            # Never accumulate the bonus when re-sorting an existing result list.
            raw = float(row.get("audio_score", row["score"]))
            row["audio_score"] = raw
            row["preference"] = 0
            entry = saved.get(self._path(row["path"]))
            if entry:
                try:
                    stat = Path(row["path"]).stat()
                    if (stat.st_size, stat.st_mtime_ns) == entry[:2]:
                        row["preference"] = entry[2]
                except OSError:
                    pass
            row["score"] = raw + (0.05 * row["preference"] if enabled else 0.0)
            results.append(row)
        return sorted(results, key=lambda row: (
            row.get("match_type") != "filename", -row["score"], row["path"].casefold()
        ))

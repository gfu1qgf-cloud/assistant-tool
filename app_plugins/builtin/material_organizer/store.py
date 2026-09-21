from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from app_paths import APP_ROOT


DEFAULT_MATERIAL_ORGANIZER_ROOT = APP_ROOT / "MaterialOrganizer"
DEFAULT_MATERIAL_ORGANIZER_DB = DEFAULT_MATERIAL_ORGANIZER_ROOT / "catalog.sqlite3"
DEFAULT_THUMBNAIL_ROOT = DEFAULT_MATERIAL_ORGANIZER_ROOT / "thumbnails"

BUILTIN_CATEGORIES = (
    ("static", "基本静态", 10),
    ("pan_left", "镜头左摇", 20),
    ("pan_right", "镜头右摇", 30),
    ("tilt_up", "镜头上摇", 40),
    ("tilt_down", "镜头下摇", 50),
    ("push_in", "推近 / 放大", 60),
    ("pull_out", "拉远 / 缩小", 70),
    ("rotate", "旋转 / 环绕", 80),
    ("handheld", "手持晃动", 90),
    ("mixed", "混合运镜", 100),
    ("subject_motion", "主体运动", 110),
    ("uncertain", "待人工确认", 120),
    ("damaged", "无法读取", 130),
)
SCHEMA_VERSION = 1


def normalized_file_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


class MaterialCatalogStore:
    """SQLite-backed virtual material catalog.

    The database stores references and classifications only.  No method in
    this class renames, moves, overwrites, or deletes a source video.
    """

    def __init__(self, database_path=None, thumbnail_root=None):
        self.database_path = Path(database_path or DEFAULT_MATERIAL_ORGANIZER_DB)
        self.thumbnail_root = Path(thumbnail_root or DEFAULT_THUMBNAIL_ROOT)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.thumbnail_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            str(self.database_path), timeout=20.0, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        return connection

    def _initialize(self):
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    category_key TEXT UNIQUE,
                    name TEXT NOT NULL,
                    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS categories_parent_name
                    ON categories(COALESCE(parent_id, -1), name COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    normalized_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    fps REAL NOT NULL DEFAULT 0,
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    motion_key TEXT NOT NULL DEFAULT 'uncertain',
                    confidence REAL NOT NULL DEFAULT 0,
                    analysis_json TEXT NOT NULL DEFAULT '{}',
                    analysis_signature TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL,
                    analyzed_at REAL,
                    last_seen_at REAL NOT NULL,
                    exists_now INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS assets_file_name
                    ON assets(file_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS assets_motion_key
                    ON assets(motion_key);

                CREATE TABLE IF NOT EXISTS asset_categories (
                    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(asset_id, category_id)
                );
                CREATE INDEX IF NOT EXISTS asset_categories_category
                    ON asset_categories(category_id, asset_id);

                CREATE TABLE IF NOT EXISTS catalog_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    metadata_value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO catalog_metadata(metadata_key, metadata_value)
                VALUES ('schema_version', ?)
                ON CONFLICT(metadata_key) DO NOTHING
                """,
                (str(SCHEMA_VERSION),),
            )
            now = time.time()
            for key, name, order in BUILTIN_CATEGORIES:
                connection.execute(
                    """
                    INSERT INTO categories(
                        category_key, name, parent_id, builtin, sort_order, created_at
                    ) VALUES (?, ?, NULL, 1, ?, ?)
                    ON CONFLICT(category_key) DO UPDATE SET
                        name=excluded.name,
                        builtin=1,
                        sort_order=excluded.sort_order
                    """,
                    (key, name, order, now),
                )

    def category_by_key(self, category_key):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM categories WHERE category_key = ?",
                (str(category_key),),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_categories(self):
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*, COUNT(ac.asset_id) AS asset_count
                FROM categories AS c
                LEFT JOIN asset_categories AS ac ON ac.category_id = c.id
                GROUP BY c.id
                ORDER BY c.parent_id IS NOT NULL, c.sort_order, c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_category(self, name, parent_id=None):
        name = str(name or "").strip()
        if not name:
            raise ValueError("分类名称不能为空。")
        now = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO categories(name, parent_id, builtin, sort_order, created_at)
                VALUES (?, ?, 0, 500, ?)
                """,
                (name, parent_id, now),
            )
            return int(cursor.lastrowid)

    def rename_category(self, category_id, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("分类名称不能为空。")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE categories SET name = ? WHERE id = ? AND builtin = 0",
                (name, int(category_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("内置分类不能重命名。")

    def delete_category(self, category_id):
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM categories WHERE id = ? AND builtin = 0",
                (int(category_id),),
            )
            if cursor.rowcount != 1:
                raise ValueError("内置分类不能删除。")

    def upsert_pending_asset(self, path):
        source = Path(path).resolve()
        stat = source.stat()
        normalized = normalized_file_path(source)
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                    path, normalized_path, file_name, size, mtime_ns,
                    added_at, last_seen_at, exists_now, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'pending')
                ON CONFLICT(normalized_path) DO UPDATE SET
                    path=excluded.path,
                    file_name=excluded.file_name,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    last_seen_at=excluded.last_seen_at,
                    exists_now=1
                """,
                (
                    str(source), normalized, source.name, int(stat.st_size),
                    int(stat.st_mtime_ns), now, now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM assets WHERE normalized_path = ?", (normalized,)
            ).fetchone()
        return int(row["id"])

    def update_analysis(self, asset_id, result):
        result = dict(result or {})
        category_key = str(result.get("motion_key") or "uncertain")
        analysis = result.get("analysis") or {}
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE assets SET
                    duration=?, width=?, height=?, fps=?, thumbnail_path=?,
                    status=?, motion_key=?, confidence=?, analysis_json=?,
                    analysis_signature=?, analyzed_at=?, last_seen_at=?, exists_now=1
                WHERE id=?
                """,
                (
                    float(result.get("duration") or 0.0),
                    int(result.get("width") or 0),
                    int(result.get("height") or 0),
                    float(result.get("fps") or 0.0),
                    str(result.get("thumbnail_path") or ""),
                    str(result.get("status") or "ready"),
                    category_key,
                    float(result.get("confidence") or 0.0),
                    json.dumps(analysis, ensure_ascii=False, separators=(",", ":")),
                    str(result.get("analysis_signature") or ""),
                    now,
                    now,
                    int(asset_id),
                ),
            )
            category = connection.execute(
                "SELECT id FROM categories WHERE category_key = ?", (category_key,)
            ).fetchone()
            if category is None:
                category = connection.execute(
                    "SELECT id FROM categories WHERE category_key = 'uncertain'"
                ).fetchone()
            connection.execute(
                "DELETE FROM asset_categories WHERE asset_id = ? AND source = 'auto'",
                (int(asset_id),),
            )
            connection.execute(
                """
                INSERT INTO asset_categories(asset_id, category_id, source, created_at)
                VALUES (?, ?, 'auto', ?)
                ON CONFLICT(asset_id, category_id) DO NOTHING
                """,
                (int(asset_id), int(category["id"]), now),
            )

    def asset(self, asset_id):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (int(asset_id),)
            ).fetchone()
        return self._row_to_asset(row) if row is not None else None

    def list_assets(self, category_id=None, query=""):
        values = []
        joins = ""
        conditions = []
        if category_id is not None:
            joins = "JOIN asset_categories ac ON ac.asset_id = a.id"
            conditions.append("ac.category_id = ?")
            values.append(int(category_id))
        query = str(query or "").strip()
        if query:
            conditions.append("(a.file_name LIKE ? OR a.path LIKE ?)")
            pattern = f"%{query}%"
            values.extend((pattern, pattern))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT DISTINCT a.* FROM assets a " + joins + where
            + " ORDER BY a.added_at DESC, a.file_name COLLATE NOCASE"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._row_to_asset(row) for row in rows]

    def _row_to_asset(self, row):
        value = dict(row)
        try:
            value["analysis"] = json.loads(value.pop("analysis_json") or "{}")
        except (TypeError, ValueError):
            value["analysis"] = {}
            value.pop("analysis_json", None)
        return value

    def add_memberships(self, asset_ids, category_id):
        now = time.time()
        with self._lock, self._connect() as connection:
            for asset_id in {int(value) for value in asset_ids or ()}:
                connection.execute(
                    """
                    INSERT INTO asset_categories(asset_id, category_id, source, created_at)
                    VALUES (?, ?, 'manual', ?)
                    ON CONFLICT(asset_id, category_id) DO UPDATE SET source='manual'
                    """,
                    (asset_id, int(category_id), now),
                )

    def remove_memberships(self, asset_ids, category_id):
        identifiers = {int(value) for value in asset_ids or ()}
        if not identifiers:
            return
        with self._lock, self._connect() as connection:
            connection.executemany(
                "DELETE FROM asset_categories WHERE asset_id = ? AND category_id = ?",
                [(asset_id, int(category_id)) for asset_id in identifiers],
            )

    def remove_assets(self, asset_ids):
        identifiers = {int(value) for value in asset_ids or ()}
        if not identifiers:
            return []
        removed = []
        with self._lock, self._connect() as connection:
            for asset_id in identifiers:
                row = connection.execute(
                    "SELECT thumbnail_path FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()
                if row is not None:
                    removed.append(str(row["thumbnail_path"] or ""))
                connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return removed

    def refresh_file_states(self):
        changed = 0
        now = time.time()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, path, exists_now FROM assets"
            ).fetchall()
            for row in rows:
                exists_now = int(Path(row["path"]).is_file())
                if exists_now != int(row["exists_now"]):
                    changed += 1
                connection.execute(
                    """
                    UPDATE assets SET
                        exists_now=?,
                        last_seen_at=CASE WHEN ? = 1 THEN ? ELSE last_seen_at END
                    WHERE id=?
                    """,
                    (exists_now, exists_now, now, row["id"]),
                )
        return changed

    def counts(self):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN exists_now = 0 THEN 1 ELSE 0 END) AS missing,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                FROM assets
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "missing": int(row["missing"] or 0),
            "pending": int(row["pending"] or 0),
        }

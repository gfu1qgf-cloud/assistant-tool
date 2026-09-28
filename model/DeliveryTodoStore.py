"""Durable, user-controlled delivery tasks. Source changes never auto-complete them."""

import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app_paths import APP_ROOT
from model.DailyLinkHistory import daily_task_sheet_failures, history_dates
from model.ReviewSubmissionHistory import canonical_review_link
from model.VideoUploadHistory import normalize_video_identity


DEFAULT_TODO_FILE = APP_ROOT / "DeliveryTodos.sqlite3"
QUADRANTS = (
    "立即处理", "安排时间", "顺手完成", "稍后处理",
)


def _task_id(*parts):
    payload = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def collect_delivery_todos(upload_records, review_items, daily_history):
    """Create stable action IDs without guessing that equal filenames mean equal files."""
    sources = []
    upload_keys = set()
    latest = {}
    for record in upload_records or ():
        if not isinstance(record, dict) or (record.get("replacement") or {}).get("state") == "replaced":
            continue
        key = str(record.get("logical_key") or normalize_video_identity(record.get("file_name"))).strip()
        if not key:
            continue
        if key not in latest or str(record.get("recorded_at") or "") >= str(latest[key].get("recorded_at") or ""):
            latest[key] = record
    for record in latest.values():
        name = str(record.get("file_name") or "未命名视频")
        day = str(record.get("batch_date") or "")
        slot = str(record.get("batch_slot") or "")
        upload_keys.add((day, slot, normalize_video_identity(name)))
        sheet = record.get("task_submission") or {}
        if sheet.get("status") not in {"failed", "not_matched", "pending"}:
            continue
        source = record.get("event_id") or _task_id(
            record.get("drive_file_id"), record.get("recorded_at"), name,
        )
        sources.append({
            "id": "sheet:" + str(source), "kind": "sheet", "quadrant": 2,
            "title": "核对任务提交表 · " + name,
            "detail": str(sheet.get("reason") or "尚未确认写入任务提交表"),
            "admin": str((record.get("task") or {}).get("admin") or ""),
            "link": str(record.get("drive_link") or ""),
            "local_file": str(record.get("local_file") or ""),
            "source_time": str(record.get("recorded_at") or ""),
        })

    for review in review_items or ():
        if not isinstance(review, dict):
            continue
        status = str(review.get("status") or "")
        if status not in {"passed", "needs_changes"}:
            continue
        if review.get("acknowledged_status") == status:
            # Respect reminders the user had already cleared before this board existed.
            continue
        link = str(review.get("link") or "")
        key = str(review.get("key") or canonical_review_link(link) or "")
        if not key:
            continue
        revision = str(review.get("review_revision") or review.get("submitted_at") or "legacy")
        kind = "send" if status == "passed" else "rework"
        name = str(review.get("name") or "未命名视频")
        admin = str(review.get("admin") or "")
        sources.append({
            "id": "review:" + _task_id(key, revision, status),
            "kind": kind, "quadrant": 0 if kind == "send" else 1,
            "title": ("发送给管理员 · " if kind == "send" else "修改并重传 · ") + name,
            "detail": str(review.get("note") or ""), "admin": admin,
            "link": link, "local_file": "",
            "source_time": str(review.get("status_updated_at") or review.get("submitted_at") or ""),
            "review_key": key, "review_revision": revision,
        })

    for day in history_dates(daily_history):
        for failure in daily_task_sheet_failures(daily_history, day):
            name = str(failure.get("file_name") or "")
            slot = str(failure.get("slot") or "")
            if (day, slot, normalize_video_identity(name)) in upload_keys:
                continue
            sources.append({
                "id": "daily:" + _task_id(day, slot, normalize_video_identity(name)),
                "kind": "sheet", "quadrant": 2,
                "title": "核对任务提交表 · " + name,
                "detail": str(failure.get("reason") or ""), "admin": "",
                "link": "", "local_file": "",
                "source_time": str(failure.get("saved_at") or day),
            })
    return sources


class DeliveryTodoStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_TODO_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '', admin TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '', local_file TEXT NOT NULL DEFAULT '',
                source_time TEXT NOT NULL DEFAULT '', review_key TEXT NOT NULL DEFAULT '',
                review_revision TEXT NOT NULL DEFAULT '', stale INTEGER NOT NULL DEFAULT 0,
                quadrant INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL, completed_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS todos_status ON todos(status, quadrant)")
            db.execute("CREATE TABLE IF NOT EXISTS todo_options (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(todos)")}
            if "review_revision" not in columns:
                db.execute("ALTER TABLE todos ADD COLUMN review_revision TEXT NOT NULL DEFAULT ''")
            if "stale" not in columns:
                db.execute("ALTER TABLE todos ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    def add_sources(self, sources):
        added = 0
        with self._connect() as db:
            for source in sources:
                cursor = db.execute("""INSERT OR IGNORE INTO todos
                    (id,kind,title,detail,admin,link,local_file,source_time,review_key,
                     review_revision,quadrant,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)""", (
                    source["id"], source["kind"], source["title"],
                    source.get("detail", ""), source.get("admin", ""),
                    source.get("link", ""), source.get("local_file", ""),
                    source.get("source_time", ""), source.get("review_key", ""),
                    source.get("review_revision", ""),
                    int(source.get("quadrant", 1)), time.time(),
                ))
                added += cursor.rowcount
                if not cursor.rowcount:
                    db.execute("""UPDATE todos SET detail=?, admin=?, link=?, local_file=?
                        WHERE id=? AND status='open'""", (
                        source.get("detail", ""), source.get("admin", ""),
                        source.get("link", ""), source.get("local_file", ""), source["id"],
                    ))
        return added

    def mark_old_review_versions(self, review_items):
        """Keep old unfinished tasks, but visibly warn when their link now has newer content."""
        current = {}
        for item in review_items or ():
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or canonical_review_link(item.get("link")))
            revision = str(item.get("review_revision") or item.get("submitted_at") or "")
            if key and revision:
                current[key] = revision
        changed = 0
        with self._connect() as db:
            for key, revision in current.items():
                changed += db.execute("""UPDATE todos SET stale=1
                    WHERE review_key=? AND review_revision<>? AND stale=0
                    AND kind IN ('send','rework') AND status='open'""",
                    (key, revision)).rowcount
                changed += db.execute("""UPDATE todos SET stale=0
                    WHERE review_key=? AND review_revision=? AND stale=1
                    AND kind IN ('send','rework') AND status='open'""",
                    (key, revision)).rowcount
        return changed

    def add_manual(self, title, detail="", quadrant=1):
        title = str(title).strip()
        if not title:
            raise ValueError("请填写待办标题")
        task_id = "manual:" + uuid.uuid4().hex
        self.add_sources([{ "id": task_id, "kind": "manual", "title": title,
                            "detail": str(detail).strip(), "quadrant": quadrant }])
        return task_id

    def list_tasks(self, status="open"):
        order = "created_at" if status == "open" else "completed_at"
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                f"SELECT * FROM todos WHERE status=? ORDER BY {order} DESC", (status,)
            )]

    def set_quadrant(self, task_id, quadrant):
        if int(quadrant) not in range(4):
            raise ValueError("待办象限无效")
        with self._connect() as db:
            db.execute("UPDATE todos SET quadrant=? WHERE id=? AND status='open'",
                       (int(quadrant), task_id))

    def complete(self, task_id, now=None):
        with self._connect() as db:
            return db.execute("""UPDATE todos SET status='completed', completed_at=?
                WHERE id=? AND status='open'""", (
                    float(time.time() if now is None else now), task_id,
                )).rowcount

    def restore(self, task_id):
        with self._connect() as db:
            return db.execute("""UPDATE todos SET status='open', completed_at=NULL
                WHERE id=? AND status IN ('completed','archived')""", (task_id,)).rowcount

    def retention_days(self):
        with self._connect() as db:
            row = db.execute("SELECT value FROM todo_options WHERE key='completed_days'").fetchone()
            try:
                return max(1, min(30, int(row[0]))) if row else 3
            except (TypeError, ValueError):
                return 3

    def set_retention_days(self, days):
        days = max(1, min(30, int(days)))
        with self._connect() as db:
            db.execute("""INSERT INTO todo_options(key,value) VALUES ('completed_days',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(days),))

    def archive_completed(self, now=None):
        cutoff = float(time.time() if now is None else now) - self.retention_days() * 86400
        with self._connect() as db:
            return db.execute("""UPDATE todos SET status='archived'
                WHERE status='completed' AND completed_at<=?""", (cutoff,)).rowcount

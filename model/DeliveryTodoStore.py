"""Durable, user-controlled delivery tasks. Source changes never auto-complete them."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
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


def _day(value, fallback="未注明日期"):
    text = str(value or "").strip()
    if text[:10].count("-") == 2:
        return text[:10]
    try:
        return datetime.fromtimestamp(float(text)).date().isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return fallback


def _was_not_sent_for_review(record, review_folder_name):
    if record.get("review_routed") is not None:
        return record["review_routed"] is False
    if (record.get("task") or {}).get("review_required") is False:
        return True
    prefix = str(record.get("remote_prefix") or "").replace("\\", "/").strip("/")
    return bool(prefix) and prefix.split("/", 1)[0].casefold() != review_folder_name.casefold()


def collect_delivery_todos(upload_records, review_items, daily_history, review_folder_name="review"):
    """Group actionable videos by date and administrator, retaining per-video links."""
    groups = {}

    def add(kind, day, admin, item):
        admin = str(admin or "").strip()
        key = (kind, day, admin)
        groups.setdefault(key, {})[item["id"]] = item

    upload_keys = set()
    latest = {}
    uploads_by_drive_id = {}
    review_items = [item for item in (review_items or ()) if isinstance(item, dict)]
    reviewed_keys = {
        str(item.get("key") or canonical_review_link(item.get("link")) or "")
        for item in review_items
    }
    for record in upload_records or ():
        if not isinstance(record, dict) or (record.get("replacement") or {}).get("state") == "replaced":
            continue
        key = str(record.get("drive_file_id") or record.get("logical_key")
                  or normalize_video_identity(record.get("file_name"))).strip()
        if not key:
            continue
        if key not in latest or str(record.get("recorded_at") or "") >= str(latest[key].get("recorded_at") or ""):
            latest[key] = record
    for record in latest.values():
        if record.get("drive_file_id"):
            uploads_by_drive_id[str(record["drive_file_id"])] = record
        name = str(record.get("file_name") or "未命名视频")
        day = str(record.get("batch_date") or "")
        slot = str(record.get("batch_slot") or "")
        upload_keys.add((day, slot, normalize_video_identity(name)))
        sheet = record.get("task_submission") or {}
        source = record.get("event_id") or _task_id(
            record.get("drive_file_id"), record.get("recorded_at"), name,
        )
        admin = (record.get("task") or {}).get("admin")
        item = {
            "id": "upload:" + str(source), "name": name,
            "link": str(record.get("drive_link") or ""),
            "local_file": str(record.get("local_file") or ""),
            "note": "", "source_time": str(record.get("recorded_at") or ""),
            "drive_file_id": str(record.get("drive_file_id") or ""),
            "folder_id": str(record.get("remote_parent_id") or ""),
        }
        if sheet.get("status") in {"failed", "not_matched", "pending"}:
            add("sheet", day or _day(record.get("recorded_at")), admin,
                dict(item, id="sheet:" + str(source),
                     note=str(sheet.get("reason") or "尚未确认写入任务提交表")))
        review_key = "google:" + str(record.get("drive_file_id") or "")
        if (
            item["link"] and review_key not in reviewed_keys
            and _was_not_sent_for_review(record, review_folder_name)
        ):
            add("send", day or _day(record.get("recorded_at")), admin, item)

    for review in review_items:
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
        source_time = str(review.get("status_updated_at") or review.get("submitted_at") or "")
        drive_id = key.removeprefix("google:") if key.startswith("google:") else ""
        uploaded = uploads_by_drive_id.get(drive_id, {})
        add(kind, _day(source_time), admin, {
            "id": "review:" + _task_id(key, revision, status), "name": name,
            "link": link, "local_file": str(uploaded.get("local_file") or ""),
            "drive_file_id": drive_id,
            "folder_id": str(uploaded.get("remote_parent_id") or ""),
            "note": str(review.get("note") or ""),
            "source_time": source_time, "review_key": key,
            "review_revision": revision, "review_status": status,
        })

    for day in history_dates(daily_history):
        for failure in daily_task_sheet_failures(daily_history, day):
            name = str(failure.get("file_name") or "")
            slot = str(failure.get("slot") or "")
            if (day, slot, normalize_video_identity(name)) in upload_keys:
                continue
            add("sheet", day, "", {
                "id": "daily:" + _task_id(day, slot, normalize_video_identity(name)),
                "name": name, "link": "", "local_file": "",
                "note": str(failure.get("reason") or ""),
                "source_time": str(failure.get("saved_at") or day),
            })
    sources = []
    labels = {"send": "发送给管理员", "rework": "修改并重传", "sheet": "核对任务提交表"}
    quadrants = {"send": 0, "rework": 1, "sheet": 2}
    for (kind, day, admin), child_map in sorted(groups.items()):
        items = sorted(child_map.values(), key=lambda item: (item["name"], item["id"]))
        display_admin = admin or "未指定管理员"
        sources.append({
            "id": "group:" + _task_id(kind, day, admin),
            "kind": kind, "quadrant": quadrants[kind],
            "title": f"{day} · {labels[kind]} · {display_admin}",
            "detail": f"共 {len(items)} 个视频；右键查看详情和链接",
            "admin": admin, "link": "", "local_file": "",
            "source_time": max((item["source_time"] for item in items), default=day),
            "items": items,
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
                group_key TEXT NOT NULL DEFAULT '', items_json TEXT NOT NULL DEFAULT '[]',
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
            if "group_key" not in columns:
                db.execute("ALTER TABLE todos ADD COLUMN group_key TEXT NOT NULL DEFAULT ''")
            if "items_json" not in columns:
                db.execute("ALTER TABLE todos ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]'")

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
                if source.get("items") is not None:
                    added += self._upsert_group(db, source)
                    continue
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

    def _upsert_group(self, db, source):
        """Keep one open card per admin/day; completed items remain completed."""
        key = source["id"]
        items = list(source.get("items") or ())
        if not items:
            return 0
        rows = db.execute("SELECT * FROM todos WHERE group_key=? OR id=?", (key, key)).fetchall()
        covered = set()
        open_row = None
        for row in rows:
            try:
                row_items = json.loads(row["items_json"] or "[]")
            except (TypeError, ValueError):
                row_items = []
            if row["status"] == "open":
                open_row = row
            elif row["status"] in {"completed", "archived"}:
                covered.update(str(item.get("id")) for item in row_items if isinstance(item, dict))
        child_ids = [str(item["id"]) for item in items]
        if child_ids:
            placeholders = ",".join("?" for _ in child_ids)
            legacy = db.execute(
                f"SELECT id,status FROM todos WHERE id IN ({placeholders})", child_ids
            ).fetchall()
            covered.update(row["id"] for row in legacy if row["status"] in {"completed", "archived"})
        pending = [item for item in items if str(item["id"]) not in covered]
        if not pending:
            return 0
        payload = json.dumps(pending, ensure_ascii=False)
        detail = f"共 {len(pending)} 个视频；右键查看详情和链接"
        if open_row is not None:
            db.execute("""UPDATE todos SET title=?,detail=?,admin=?,items_json=?,
                source_time=? WHERE id=?""", (
                source["title"], detail, source.get("admin", ""), payload,
                source.get("source_time", ""), open_row["id"],
            ))
        else:
            task_id = key if not rows else key + ":" + _task_id(*(item["id"] for item in pending))[:12]
            db.execute("""INSERT INTO todos
                (id,kind,title,detail,admin,link,local_file,source_time,group_key,
                 items_json,quadrant,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)""", (
                task_id, source["kind"], source["title"], detail,
                source.get("admin", ""), "", "", source.get("source_time", ""),
                key, payload, int(source.get("quadrant", 1)), time.time(),
            ))
        # Old per-video cards are retained in SQLite, but hidden after merging.
        if child_ids:
            db.execute(
                f"UPDATE todos SET status='merged' WHERE id IN ({placeholders}) AND status='open'",
                child_ids,
            )
        return int(open_row is None)

    def mark_old_review_versions(self, review_items):
        """Keep old unfinished tasks, but visibly warn when their link now has newer content."""
        current = {}
        for item in review_items or ():
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or canonical_review_link(item.get("link")))
            revision = str(item.get("review_revision") or item.get("submitted_at") or "")
            if key and revision:
                current[key] = (revision, str(item.get("status") or ""))
        changed = 0
        with self._connect() as db:
            for key, (revision, _status) in current.items():
                changed += db.execute("""UPDATE todos SET stale=1
                    WHERE review_key=? AND review_revision<>? AND stale=0
                    AND kind IN ('send','rework') AND status='open'""",
                    (key, revision)).rowcount
                changed += db.execute("""UPDATE todos SET stale=0
                    WHERE review_key=? AND review_revision=? AND stale=1
                    AND kind IN ('send','rework') AND status='open'""",
                    (key, revision)).rowcount
            for row in db.execute("""SELECT id,items_json,stale FROM todos
                WHERE status='open' AND group_key<>''"""):
                try:
                    items = json.loads(row["items_json"] or "[]")
                except (TypeError, ValueError):
                    items = []
                stale = any(
                    item.get("review_key") in current and
                    current[item["review_key"]] != (
                        str(item.get("review_revision") or ""),
                        str(item.get("review_status") or ""),
                    )
                    for item in items if isinstance(item, dict) and item.get("review_key")
                )
                if bool(row["stale"]) != stale:
                    changed += db.execute("UPDATE todos SET stale=? WHERE id=?",
                                          (int(stale), row["id"])).rowcount
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

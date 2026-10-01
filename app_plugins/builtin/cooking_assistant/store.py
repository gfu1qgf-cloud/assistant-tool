"""Private food inventory and bounded recipe cache. No AI access here."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid

from qt_compat import QtCore


def today():
    return QtCore.QDateTime.currentDateTimeUtc().toTimeZone(
        QtCore.QTimeZone(b"Europe/Rome")).date().toPyDate()


def validate_item(raw):
    item = dict(raw)
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("请填写食材名称。")
    quantity = float(item.get("quantity", 0))
    if not math.isfinite(quantity) or quantity < 0:
        raise ValueError("数量必须是非负数。")
    purchased = str(item.get("purchased") or today().isoformat())
    date.fromisoformat(purchased)
    due = str(item.get("due") or "")
    if due:
        date.fromisoformat(due)
    kind = item.get("date_kind", "check")
    if kind not in {"check", "use_by", "best_before"}:
        raise ValueError("日期类型无效。")
    return dict(item, id=str(item.get("id") or uuid.uuid4().hex), name=name[:100],
        quantity=quantity, unit=str(item.get("unit") or "个")[:20],
        storage=str(item.get("storage") or "冷藏")[:30], purchased=purchased, due=due,
        date_kind=kind, spoiled=bool(item.get("spoiled", False)),
        note=str(item.get("note") or "")[:2000],
        updated_at=datetime.now(timezone.utc).isoformat())


def stock_status(item, day=None, warn_days=2):
    day = day or today()
    if item.get("quantity", 0) <= 0:
        return "empty", "已用完"
    if item.get("spoiled"):
        return "spoiled", "已标记变质／不可用"
    if not item.get("due"):
        return "unknown", "未设置检查日期"
    remaining = (date.fromisoformat(item["due"]) - day).days
    if remaining < 0:
        return "overdue", {"use_by": "食用截止日已过，禁止推荐", "best_before": "最佳赏味期已过，请核查",
                           "check": "已过检查日，请核查"}[item.get("date_kind", "check")]
    if remaining == 0:
        return "soon", "今天到期／需检查"
    if remaining <= warn_days:
        return "soon", f"还有 {remaining} 天，优先考虑"
    return "available", f"还有 {remaining} 天"


def eligible_items(items, day=None, warn_days=2):
    return [item for item in items if stock_status(item, day, warn_days)[0]
            not in {"spoiled", "empty", "overdue"}]


class FoodStore:
    def __init__(self, path):
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def connection(self, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            with self._init_lock:
                if not self._initialized:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE IF NOT EXISTS stock (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS recipes (key TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL)")
                    connection.commit()
                    self._initialized = True
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def items(self):
        with self.connection() as connection:
            values = [json.loads(row[0]) for row in connection.execute("SELECT data FROM stock")]
        return sorted(values, key=lambda x: (x.get("due") or "9999", x["name"]))

    def save_item(self, item):
        item = validate_item(item)
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR REPLACE INTO stock VALUES (?, ?)",
                               (item["id"], json.dumps(item, ensure_ascii=False)))
        return item

    def remove(self, identifier):
        with self.connection(write=True) as connection:
            connection.execute("DELETE FROM stock WHERE id=?", (identifier,))

    def consume(self, identifier, amount):
        amount = float(amount)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("使用量必须大于 0。")
        with self.connection(write=True) as connection:
            row = connection.execute("SELECT data FROM stock WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("这条库存已经不存在，请刷新。")
            item = json.loads(row[0])
            if amount > item["quantity"] + 1e-9:
                raise ValueError("使用量超过现有库存。")
            item = validate_item(dict(item, quantity=max(0, round(item["quantity"] - amount, 6))))
            connection.execute("UPDATE stock SET data=? WHERE id=?", (json.dumps(item, ensure_ascii=False), identifier))
        return item

    def get(self, key, default=None):
        with self.connection() as connection:
            row = connection.execute("SELECT data FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, data):
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                               (key, json.dumps(data, ensure_ascii=False)))

    def recipe_cache(self, key):
        with self.connection() as connection:
            row = connection.execute("SELECT data FROM recipes WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_recipes(self, key, value):
        import time
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR REPLACE INTO recipes VALUES (?, ?, ?)",
                               (key, json.dumps(value, ensure_ascii=False), time.time()))
            connection.execute("DELETE FROM recipes WHERE key NOT IN (SELECT key FROM recipes ORDER BY created DESC LIMIT 30)")
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('last_recipes', ?)",
                               (json.dumps(value, ensure_ascii=False),))

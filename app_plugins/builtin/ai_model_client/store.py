"""Private local conversations and immutable, managed image attachments."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from model.AIImages import normalize_image, MAX_SOURCE_BYTES

from model.SensitiveData import redact_sensitive_text
from model.AppDataStorage import data_file, data_directory

ASSET_NAME = re.compile(r"^images/[0-9a-f]{64}\.(?:jpg|png)$")


class ChatStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        data_directory(self.root).ensure()
        self.images = self.root / "images"
        data_directory(self.images).ensure()
        if self.root not in self.images.resolve().parents:
            raise ValueError("聊天附件目录不能指向本地记录目录以外的位置。")
        self.path = self.root / "conversations.sqlite3"
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '{}', draft TEXT NOT NULL DEFAULT '',
                    images TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
                    role TEXT NOT NULL, text TEXT NOT NULL, images TEXT NOT NULL DEFAULT '[]',
                    provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, FOREIGN KEY(conversation) REFERENCES conversations(id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation, id);
            """)
            db.execute("UPDATE messages SET status='interrupted',error='上次程序退出时请求未完成，可手动重试。' WHERE status='pending'")

    @contextmanager
    def connection(self):
        with self.lock:
            db = data_file(self.path).connect(timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        row = dict(row)
        for field in ("options", "images", "metadata"):
            if field in row:
                row[field] = json.loads(row[field])
        return row

    def create(self, provider="gemini", model="", options=None):
        identifier = uuid4().hex
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO conversations(id,title,provider,model,options,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (identifier, "新对话", provider, model, json.dumps(options or {}, ensure_ascii=False), now, now))
        return identifier

    def conversations(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM conversations ORDER BY updated DESC")]

    def get(self, identifier):
        with self.connection() as db:
            row = self.decode(db.execute("SELECT * FROM conversations WHERE id=?", (identifier,)).fetchone())
        if row is None:
            raise ValueError("这段对话已不存在。")
        return row

    def update(self, identifier, **fields):
        if not fields or set(fields) - {"title", "provider", "model", "options", "draft", "images"}:
            raise ValueError("对话更新字段不正确。")
        values = [json.dumps(value, ensure_ascii=False) if field in {"options", "images"} else value
                  for field, value in fields.items()]
        with self.connection() as db:
            db.execute("UPDATE conversations SET " + ",".join(field + "=?" for field in fields) + ",updated=? WHERE id=?",
                       [*values, time.time(), identifier])

    def messages(self, identifier):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM messages WHERE conversation=? ORDER BY id", (identifier,))]

    def begin_request(self, identifier, text, images, provider, model, retry_id=None):
        with self.connection() as db:
            if retry_id is not None:
                row = db.execute("SELECT * FROM messages WHERE id=? AND conversation=?", (retry_id, identifier)).fetchone()
                last = db.execute("SELECT MAX(id) FROM messages WHERE conversation=?", (identifier,)).fetchone()[0]
                if row is None or row["role"] != "user" or row["status"] in {"pending", "done"} or last != retry_id:
                    raise ValueError("只能重试本对话最后一条未成功的请求。")
                db.execute("UPDATE messages SET status='pending',error='',metadata='{}',provider=?,model=? WHERE id=?", (provider, model, retry_id))
                prompt_id = retry_id
            else:
                cursor = db.execute("INSERT INTO messages(conversation,role,text,images,provider,model,status,created) VALUES(?,?,?,?,?,?,'pending',?)",
                    (identifier, "user", text, json.dumps(images, ensure_ascii=False), provider, model, time.time()))
                prompt_id = cursor.lastrowid
                db.execute("UPDATE conversations SET draft='',images='[]',updated=?,title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE id=?",
                           (time.time(), (text.strip().splitlines() or ["图片分析"])[0][:32] or "图片分析", identifier))
        return prompt_id

    def complete_request(self, identifier, prompt_id, result):
        with self.connection() as db:
            row = db.execute("SELECT status FROM messages WHERE id=? AND conversation=?", (prompt_id, identifier)).fetchone()
            if row is None or row["status"] != "pending":
                return False
            db.execute("UPDATE messages SET status='done',error='',metadata='{}' WHERE id=?", (prompt_id,))
            db.execute("INSERT INTO messages(conversation,role,text,provider,model,status,metadata,created) VALUES(?,'assistant',?,?,?,'done',?,?)",
                       (identifier, result["text"], result["provider"], result["model"],
                        json.dumps({key: result[key] for key in ("usage", "elapsed", "warning")}, ensure_ascii=False), time.time()))
            db.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), identifier))
        return True

    def fail_request(self, identifier, prompt_id, error, canceled=False, partial=None):
        metadata = {"partial": partial} if partial and partial.get("text") else {}
        with self.connection() as db:
            db.execute("UPDATE messages SET status=?,error=?,metadata=? WHERE id=? AND conversation=? AND status='pending'",
                       ("canceled" if canceled else "failed", redact_sensitive_text(error)[:1200],
                        json.dumps(metadata, ensure_ascii=False), prompt_id, identifier))

    def request_messages(self, identifier, prompt_id, rounds):
        rows = self.messages(identifier)
        prompt = next((row for row in rows if row["id"] == prompt_id and row["status"] == "pending"), None)
        if prompt is None:
            raise ValueError("找不到当前待发送消息。")
        history = [row for row in rows if row["id"] < prompt_id and row["status"] == "done"]
        history = history[-2 * max(1, min(50, int(rounds))):]
        return [{"role": row["role"], "text": row["text"], "images": [self.encoded_image(image) for image in row["images"]]}
                for row in [*history, prompt]]

    def asset_path(self, image):
        name = image.get("file", "")
        if not ASSET_NAME.fullmatch(name):
            raise ValueError("图片附件记录不正确，拒绝读取目录外文件。")
        path = (self.root / name).resolve()
        if path.parent != self.images.resolve() or not path.is_file():
            raise ValueError("本地图片附件已丢失，请重新添加图片或新建对话。")
        return path

    def encoded_image(self, image):
        path = self.asset_path(image)
        if path.stat().st_size > 3 * 1024 * 1024:
            raise ValueError("图片附件过大或已被修改，请重新添加。")
        return {"mime": "image/png" if path.suffix == ".png" else "image/jpeg",
                "data": base64.b64encode(data_file(path).read_bytes()).decode("ascii")}

    def import_image(self, source, name=None):
        if isinstance(source, bytes):
            name = name or "剪贴板图片.png"
        else:
            name = Path(source).name
        normalized, suffix, mime, width, height = normalize_image(source)
        filename = hashlib.sha256(normalized).hexdigest() + "." + suffix
        target = self.images / filename
        if self.root not in target.resolve().parents:
            raise ValueError("附件保存路径不安全。")
        if not target.exists():
            data_file(target).write_bytes(normalized)
        return {"file": "images/" + filename, "name": str(name), "mime": mime,
                "width": width, "height": height}

    def delete(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT images FROM conversations WHERE id=?", (identifier,)).fetchone()
            candidates = json.loads(row[0]) if row else []
            for row in db.execute("SELECT images FROM messages WHERE conversation=?", (identifier,)):
                candidates.extend(json.loads(row[0]))
            db.execute("DELETE FROM conversations WHERE id=?", (identifier,))
            referenced = set()
            for row in db.execute("SELECT images FROM conversations UNION ALL SELECT images FROM messages"):
                referenced.update(item.get("file") for item in json.loads(row[0]))
            for image in candidates:
                name = image.get("file", "")
                if ASSET_NAME.fullmatch(name) and name not in referenced:
                    target = self.root / name
                    if target.resolve().parent == self.images.resolve() and not target.is_symlink():
                        data_file(target).remove(missing_ok=True)

    def export_text(self, identifier):
        conversation = self.get(identifier)
        lines = [conversation["title"], ""]
        for row in self.messages(identifier):
            lines.append(("你" if row["role"] == "user" else "AI") + " · " + row["provider"] + " / " + row["model"])
            lines.append(row["text"])
            if row["images"]:
                lines.append("[图片：" + "、".join(image["name"] for image in row["images"]) + "]")
            if row["error"]:
                lines.append("[未成功：" + row["error"] + "]")
            partial = row["metadata"].get("partial") or {}
            if partial.get("text"):
                lines.extend(["AI（未完成，不计入后续上下文）", partial["text"]])
            lines.append("")
        return "\n".join(lines)

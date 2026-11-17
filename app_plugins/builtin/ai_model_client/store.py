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
from model.AIChatSources import text_with_sources
from .request_context import select_context

from model.SensitiveData import redact_sensitive_text
from model.AppDataStorage import data_file, data_directory

ASSET_NAME = re.compile(r"^images/[0-9a-f]{64}\.(?:jpg|png)$")


class ChatStore:
    is_private = False

    def __init__(self, root):
        self.root = Path(root).resolve()
        data_directory(self.root).ensure()
        self.images = self.root / "images"
        data_directory(self.images).ensure()
        if self.root not in self.images.resolve().parents:
            raise ValueError("聊天附件目录不能指向本地记录目录以外的位置。")
        self.path = self.root / "conversations.sqlite3"
        self.lock = threading.RLock()
        self._initialize_database()
        self.resume_purges()

    def _initialize_database(self):
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
                CREATE TABLE IF NOT EXISTS chat_purge_queue (
                    id TEXT PRIMARY KEY, assets TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS chat_batches (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL,
                    prompt_id INTEGER NOT NULL UNIQUE, model TEXT NOT NULL,
                    key_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                    file_id TEXT NOT NULL DEFAULT '', batch_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', applied INTEGER NOT NULL DEFAULT 0,
                    body TEXT NOT NULL, created REAL NOT NULL,
                    FOREIGN KEY(conversation) REFERENCES conversations(id) ON DELETE CASCADE);
            """)
            self._migrate_branches(db)
            db.execute("UPDATE messages SET status='interrupted',error='上次程序退出时请求未完成，可手动重试。' WHERE status='pending' AND id NOT IN (SELECT prompt_id FROM chat_batches WHERE applied=0 AND state NOT IN ('failed','expired','cancelled'))")

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

    def _migrate_branches(self, db):
        messages = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        conversations = {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
        if 'parent_id' not in messages or 'active_leaf' not in conversations:
            if db.execute('SELECT 1 FROM messages LIMIT 1').fetchone():
                # Keep the pre-migration copy inside the same protected data
                # directory, through the storage broker. No plaintext export.
                backup_path = self.root / ('conversations.pre-branches-' + str(time.time_ns()) + '.sqlite3')
                backup = data_file(backup_path).connect()
                try:
                    db.backup(backup)
                finally:
                    backup.close()
            db.execute('BEGIN IMMEDIATE')
            if 'parent_id' not in messages:
                db.execute('ALTER TABLE messages ADD COLUMN parent_id INTEGER')
                previous = {}
                for row in db.execute('SELECT id,conversation FROM messages ORDER BY id').fetchall():
                    db.execute('UPDATE messages SET parent_id=? WHERE id=?', (previous.get(row['conversation']), row['id']))
                    previous[row['conversation']] = row['id']
            if 'active_leaf' not in conversations:
                db.execute('ALTER TABLE conversations ADD COLUMN active_leaf INTEGER')
                db.execute('UPDATE conversations SET active_leaf=(SELECT MAX(id) FROM messages WHERE conversation=conversations.id)')
        db.execute('CREATE INDEX IF NOT EXISTS messages_parent ON messages(conversation,parent_id)')

    @staticmethod
    def _path(rows, leaf):
        by_id = {row['id']: row for row in rows}
        path, visited = [], set()
        while leaf is not None:
            if leaf not in by_id or leaf in visited:
                raise ValueError('对话分支记录不完整，未自动修改历史。')
            visited.add(leaf)
            path.append(by_id[leaf])
            leaf = by_id[leaf]['parent_id']
        return list(reversed(path))

    def messages(self, identifier, *, leaf_id=None, all_branches=False):
        with self.connection() as db:
            rows = [self.decode(row) for row in db.execute('SELECT * FROM messages WHERE conversation=? ORDER BY id', (identifier,))]
            if all_branches:
                return rows
            conversation = db.execute('SELECT active_leaf FROM conversations WHERE id=?', (identifier,)).fetchone()
            if conversation is None:
                raise ValueError('这段对话已不存在。')
            return self._path(rows, leaf_id if leaf_id is not None else conversation['active_leaf'])

    def regeneration_prompt(self, identifier, assistant_id):
        rows = {row['id']: row for row in self.messages(identifier, all_branches=True)}
        answer = rows.get(assistant_id)
        prompt = rows.get(answer['parent_id']) if answer else None
        if not answer or answer['role'] != 'assistant' or answer['status'] != 'done' or not prompt or prompt['role'] != 'user':
            raise ValueError('只能为已完成的回答重新生成。')
        return prompt

    def editable_prompt(self, identifier, prompt_id):
        with self.connection() as db:
            row = self.decode(db.execute('SELECT * FROM messages WHERE conversation=? AND id=?', (identifier, prompt_id)).fetchone())
        if row is None or row['role'] != 'user':
            raise ValueError('只能编辑当前对话中自己发送的消息。')
        return row

    def branch_navigation(self, identifier):
        rows = self.messages(identifier, all_branches=True)
        by_id = {row['id']: row for row in rows}
        groups = {}
        for row in rows:
            if row['role'] == 'user':
                groups.setdefault(row['parent_id'], []).append(row['id'])
        result = {}
        for row in rows:
            prompt = row if row['role'] == 'user' else by_id.get(row['metadata'].get('group_run',row['parent_id']))
            if prompt and prompt['role'] == 'user':
                targets = groups[prompt['parent_id']]
                result[row['id']] = dict(targets=targets, index=targets.index(prompt['id']))
        return result

    def select_branch(self, identifier, branch_id):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute('SELECT id,parent_id FROM messages WHERE conversation=? ORDER BY id', (identifier,))]
            if branch_id not in {row['id'] for row in rows}:
                raise ValueError('这个分支不属于当前对话。')
            children = {}
            for row in rows:
                children.setdefault(row['parent_id'], []).append(row['id'])
            leaf, seen = branch_id, set()
            while children.get(leaf):
                if leaf in seen:
                    raise ValueError('分支记录异常，未切换。')
                seen.add(leaf)
                leaf = children[leaf][-1]
            db.execute('UPDATE conversations SET active_leaf=? WHERE id=?', (leaf, identifier))
        return leaf

    def begin_request(self, identifier, text, images, provider, model, retry_id=None, regenerate_id=None, edit_id=None):
        # Read the original prompt before entering the write transaction.
        if sum(value is not None for value in (retry_id, regenerate_id, edit_id)) > 1:
            raise ValueError('一次只能选择重试、重新生成或编辑一种操作。')
        source = self.regeneration_prompt(identifier, regenerate_id) if regenerate_id is not None else None
        edited = self.editable_prompt(identifier, edit_id) if edit_id is not None else None
        with self.connection() as db:
            conversation = db.execute('SELECT active_leaf FROM conversations WHERE id=?', (identifier,)).fetchone()
            if conversation is None:
                raise ValueError('这段对话已不存在。')
            if retry_id is None and source is None and edited is None and conversation['active_leaf'] is not None:
                waiting = db.execute("SELECT 1 FROM messages WHERE id=? AND status='pending'", (conversation['active_leaf'],)).fetchone()
                if waiting:
                    raise ValueError('当前分支还有请求等待回答；请先检查收取Batch结果，或新建对话提交独立问题。')
            if retry_id is not None:
                row = db.execute('SELECT * FROM messages WHERE id=? AND conversation=?', (retry_id, identifier)).fetchone()
                if row is None or row['role'] != 'user' or row['status'] in {'pending', 'done'} or conversation['active_leaf'] != retry_id:
                    raise ValueError('只能重试当前分支最后一条未成功的请求。')
                if db.execute('SELECT 1 FROM chat_batches WHERE prompt_id=?', (retry_id,)).fetchone():
                    cursor = db.execute("INSERT INTO messages(conversation,role,text,images,provider,model,status,created,parent_id) VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (identifier, 'user', row['text'], row['images'], provider, model, time.time(), row['parent_id']))
                    prompt_id = cursor.lastrowid
                else:
                    db.execute("UPDATE messages SET status='pending',error='',metadata='{}',provider=?,model=? WHERE id=?", (provider, model, retry_id))
                    prompt_id = retry_id
            else:
                if source:
                    text, images, parent = source['text'], source['images'], source['parent_id']
                elif edited:
                    images, parent = edited['images'], edited['parent_id']
                else:
                    parent = conversation['active_leaf']
                cursor = db.execute("INSERT INTO messages(conversation,role,text,images,provider,model,status,created,parent_id) VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (identifier, 'user', text, json.dumps(images, ensure_ascii=False), provider, model, time.time(), parent))
                prompt_id = cursor.lastrowid
                if source is None and edited is None:
                    db.execute("UPDATE conversations SET draft='',images='[]',title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE id=?",
                               ((text.strip().splitlines() or ['图片分析'])[0][:32] or '图片分析', identifier))
            db.execute('UPDATE conversations SET active_leaf=?,updated=? WHERE id=?', (prompt_id, time.time(), identifier))
        return prompt_id

    def complete_request(self, identifier, prompt_id, result):
        with self.connection() as db:
            row = db.execute("SELECT status FROM messages WHERE id=? AND conversation=?", (prompt_id, identifier)).fetchone()
            if row is None or row["status"] != "pending":
                return False
            db.execute("UPDATE messages SET status='done',error='',metadata='{}' WHERE id=?", (prompt_id,))
            cursor = db.execute("INSERT INTO messages(conversation,role,text,provider,model,status,metadata,created,parent_id) VALUES(?,'assistant',?,?,?,'done',?,?,?)",
                       (identifier, result["text"], result["provider"], result["model"],
                        json.dumps({key: result[key] for key in ("usage", "elapsed", "warning", "requested_model", "reported_model",
                                    'sources','search_calls','reasoning_tokens','web_enabled','search_queries')
                                    if key in result}, ensure_ascii=False), time.time(), prompt_id))
            db.execute('UPDATE conversations SET active_leaf=CASE WHEN active_leaf=? THEN ? ELSE active_leaf END,updated=? WHERE id=?',
                       (prompt_id, cursor.lastrowid, time.time(), identifier))
        return True

    def fail_request(self, identifier, prompt_id, error, canceled=False, partial=None):
        metadata = {"partial": partial} if partial and partial.get("text") else {}
        with self.connection() as db:
            db.execute("UPDATE messages SET status=?,error=?,metadata=? WHERE id=? AND conversation=? AND status='pending'",
                       ("canceled" if canceled else "failed", redact_sensitive_text(error)[:1200],
                        json.dumps(metadata, ensure_ascii=False), prompt_id, identifier))

    def begin_group(self, identifier, prompt_id, config):
        with self.connection() as db:
            meta = {'group_discussion':dict(state='running',round_limit=config['round_limit'],
                member_count=len(config['members']),calls_started=0,calls_done=0,
                max_calls=config['round_limit']*len(config['members']))}
            if db.execute("UPDATE messages SET metadata=? WHERE conversation=? AND id=? AND role='user' AND status='pending'",
                          (json.dumps(meta,ensure_ascii=False),identifier,prompt_id)).rowcount != 1:
                raise ValueError('找不到群聊的待讨论问题。')

    def claim_group_step(self, identifier, prompt_id, step):
        with self.connection() as db:
            row = db.execute('SELECT metadata,status FROM messages WHERE conversation=? AND id=?',(identifier,prompt_id)).fetchone()
            meta = json.loads(row['metadata']) if row else {}
            group = meta.get('group_discussion') or {}
            if not row or row['status']!='pending' or group.get('state')!='running' or group.get('calls_started')!=step or step>=group.get('max_calls',0):
                raise ValueError('讨论步骤已开始或达到调用上限，不会重复提交。')
            group['calls_started']=step+1
            db.execute('UPDATE messages SET metadata=? WHERE id=?',(json.dumps(meta,ensure_ascii=False),prompt_id))

    def _append_group(self, identifier, prompt_id, parent_id, result, member, round_number, status, error=''):
        with self.connection() as db:
            conversation = db.execute('SELECT active_leaf FROM conversations WHERE id=?',(identifier,)).fetchone()
            root = db.execute('SELECT metadata,status FROM messages WHERE conversation=? AND id=?',(identifier,prompt_id)).fetchone()
            if not conversation or conversation['active_leaf']!=parent_id or not root or root['status']!='pending':
                if status!='done': return False
                raise ValueError('群聊分支已改变，未把回答写入其他分支。')
            meta = {key:result[key] for key in ('usage','elapsed','warning','requested_model','reported_model','sources',
                     'search_calls','reasoning_tokens','web_enabled','search_queries') if key in result}
            meta.update(group_member=member['name'],group_member_id=member['id'],group_round=round_number,group_run=prompt_id)
            cursor = db.execute("INSERT INTO messages(conversation,role,text,provider,model,status,error,metadata,created,parent_id) VALUES(?,'assistant',?,?,?,?,?,?,?,?)",
                (identifier,result.get('text',''),result.get('provider',member['provider']),result.get('model',member['model']),
                 status,redact_sensitive_text(error)[:1200],json.dumps(meta,ensure_ascii=False),time.time(),parent_id))
            root_meta = json.loads(root['metadata'])
            if status=='done': root_meta['group_discussion']['calls_done']+=1
            db.execute('UPDATE messages SET metadata=? WHERE id=?',(json.dumps(root_meta,ensure_ascii=False),prompt_id))
            db.execute('UPDATE conversations SET active_leaf=?,updated=? WHERE id=?',(cursor.lastrowid,time.time(),identifier))
            return cursor.lastrowid

    def append_group_message(self, identifier, prompt_id, parent_id, result, member, round_number):
        return self._append_group(identifier,prompt_id,parent_id,result,member,round_number,'done')

    def append_group_failure(self, identifier, prompt_id, parent_id, member, round_number, error, partial=''):
        return self._append_group(identifier,prompt_id,parent_id,{'text':partial},member,round_number,'failed',error)

    def finish_group(self, identifier, prompt_id, state, error=''):
        with self.connection() as db:
            row = db.execute('SELECT metadata,status FROM messages WHERE conversation=? AND id=?',(identifier,prompt_id)).fetchone()
            meta = json.loads(row['metadata']) if row else {}
            group = meta.get('group_discussion')
            if not group: return
            group.update(state=state,error=redact_sensitive_text(error)[:1200])
            status = 'done' if group.get('calls_done',0)>0 else 'failed'
            db.execute('UPDATE messages SET status=?,error=?,metadata=? WHERE id=?',
                (status,'' if status=='done' else redact_sensitive_text(error or '讨论已暂停。')[:1200],json.dumps(meta,ensure_ascii=False),prompt_id))

    def context_preview(self, identifier, text, images, options, *, before_prompt_id=None):
        rows = self.messages(identifier, leaf_id=before_prompt_id) if identifier is not None else []
        if before_prompt_id is not None:
            rows = rows[:-1]
        _messages, info = select_context(rows, {'text': text, 'images': images},
            rounds=options.get('rounds', 20), text_limit=options.get('context_chars', 0),
            include_history_images=options.get('history_images', True), system=options.get('system', ''))
        return info

    def request_messages(self, identifier, prompt_id, rounds, *, text_limit=0, include_history_images=True, system=''):
        rows = self.messages(identifier, leaf_id=prompt_id)
        prompt = rows[-1] if rows else None
        if prompt is None or prompt['id'] != prompt_id or prompt['status'] != 'pending':
            raise ValueError('找不到当前待发送消息。')
        selected, _info = select_context(rows[:-1], prompt, rounds=rounds, text_limit=text_limit,
            include_history_images=include_history_images, system=system)
        return [{**row, 'images': [self.encoded_image(image) for image in row['images']]} for row in selected]

    def asset_path(self, image):
        name = image.get("file", "")
        if not ASSET_NAME.fullmatch(name):
            raise ValueError("图片附件记录不正确，拒绝读取目录外文件。")
        path = (self.root / name).resolve()
        if path.parent != self.images.resolve() or not path.is_file():
            raise ValueError("本地图片附件已丢失，请重新添加图片或新建对话。")
        return path

    def prepare_batch(self, identifier, prompt_id, key_fingerprint, body):
        with self.connection() as db:
            existing = db.execute('SELECT * FROM chat_batches WHERE prompt_id=?', (prompt_id,)).fetchone()
            if existing:
                return dict(existing)
            request = db.execute("SELECT 1 FROM messages WHERE conversation=? AND id=? AND status='pending'", (identifier, prompt_id)).fetchone()
            if not request:
                raise ValueError('找不到Batch待发送消息。')
            batch_id = uuid4().hex
            db.execute('INSERT INTO chat_batches(id,conversation,prompt_id,model,key_fingerprint,state,body,created) VALUES(?,?,?,?,?,?,?,?)',
                (batch_id, identifier, prompt_id, body['model'], key_fingerprint, 'prepared', json.dumps(body, ensure_ascii=False), time.time()))
            return dict(db.execute('SELECT * FROM chat_batches WHERE id=?', (batch_id,)).fetchone())

    def get_batch(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM chat_batches WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Batch记录不存在。')
            return dict(row)

    def claim_batch(self, identifier):
        with self.connection() as db:
            return db.execute("UPDATE chat_batches SET state='uploading' WHERE id=? AND state='prepared'", (identifier,)).rowcount == 1

    def batches(self, identifier):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM chat_batches WHERE conversation=? ORDER BY created', (identifier,))]

    def update_batch(self, identifier, **fields):
        if not fields or set(fields) - {'state', 'file_id', 'batch_id', 'error', 'applied'}:
            raise ValueError('Batch更新字段不正确。')
        with self.connection() as db:
            db.execute('UPDATE chat_batches SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?', [*fields.values(), identifier])

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
            if db.execute("SELECT 1 FROM chat_batches WHERE conversation=? AND applied=0 AND state NOT IN ('failed','expired','cancelled')", (identifier,)).fetchone():
                raise ValueError('本对话还有未收取的Batch结果，请先检查并收取后再删除。')
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

    def asset_resource(self, image):
        return self.asset_path(image)

    def pending_purges(self):
        with self.connection() as db:
            return [row['id'] for row in db.execute('SELECT id FROM chat_purge_queue')]

    @staticmethod
    def _image_references(db):
        references = set()
        for row in db.execute('SELECT images FROM conversations UNION ALL SELECT images FROM messages'):
            references.update(image.get('file') for image in json.loads(row[0]))
        return references

    @staticmethod
    def _erase_database_conversation(db, identifier):
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA temp_store=MEMORY')
        candidates = set()
        for row in db.execute('SELECT images FROM conversations WHERE id=? UNION ALL SELECT images FROM messages WHERE conversation=?', (identifier,identifier)):
            candidates.update(image.get('file') for image in json.loads(row[0]))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'chat_batches' in tables:
            db.execute('DELETE FROM chat_batches WHERE conversation=?', (identifier,))
        if 'chat_purge_queue' in tables:
            db.execute('DELETE FROM chat_purge_queue WHERE id=?', (identifier,))
        db.execute('DELETE FROM messages WHERE conversation=?', (identifier,))
        db.execute('DELETE FROM conversations WHERE id=?', (identifier,))
        return {name for name in candidates if isinstance(name,str) and ASSET_NAME.fullmatch(name)}

    @staticmethod
    def _compact_database(db):
        db.execute('PRAGMA temp_store=MEMORY')
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            raise ValueError('对话数据库仍被其他操作占用，删除清理尚未全部完成，请稍后重试。')
        db.execute('VACUUM')
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]:
            raise ValueError('对话日志仍被占用，删除清理尚未全部完成，请稍后重试。')

    def purge_conversation(self, identifier):
        """Purge application-managed records; never claim physical disk erasure."""
        with self.lock:
            with self.connection() as db:
                queued = db.execute('SELECT assets FROM chat_purge_queue WHERE id=?', (identifier,)).fetchone()
                candidates = set(json.loads(queued[0])) if queued else set()
                db.execute('PRAGMA secure_delete=ON')
                # Commit this marker with the deletion so a crash cannot leave
                # the automatic backup as an unnoticed recovery copy.
                candidates.update(self._erase_database_conversation(db, identifier))
                db.execute('INSERT OR REPLACE INTO chat_purge_queue(id,assets) VALUES(?,?)',
                           (identifier,json.dumps(sorted(candidates))))
                references = self._image_references(db)
            for backup_path in self.root.glob('conversations.pre-branches-*.sqlite3'):
                if backup_path.is_symlink() or backup_path.resolve().parent != self.root:
                    raise ValueError('对话备份路径异常，未越过当前数据目录进行删除。')
                backup = data_file(backup_path).connect(timeout=5)
                try:
                    backup.execute('PRAGMA journal_mode=DELETE')
                    with backup:
                        candidates.update(self._erase_database_conversation(backup, identifier))
                        with self.connection() as db:
                            db.execute('UPDATE chat_purge_queue SET assets=? WHERE id=?',
                                       (json.dumps(sorted(candidates)),identifier))
                    self._compact_database(backup)
                    references.update(self._image_references(backup))
                finally:
                    backup.close()
                with self.connection() as db:
                    db.execute('UPDATE chat_purge_queue SET assets=? WHERE id=?',
                               (json.dumps(sorted(candidates)),identifier))
            for name in candidates - references:
                target = self.root / name
                if target.is_symlink() or target.resolve().parent != self.images.resolve():
                    raise ValueError('附件路径异常，未删除目录外文件。')
                data_file(target).remove(missing_ok=True)
            with self.connection() as db:
                self._compact_database(db)
                db.execute('PRAGMA secure_delete=ON')
                db.execute('DELETE FROM chat_purge_queue WHERE id=?', (identifier,))
            with self.connection() as db:
                self._compact_database(db)

    def resume_purges(self):
        for identifier in self.pending_purges():
            self.purge_conversation(identifier)

    def export_text(self, identifier):
        conversation = self.get(identifier)
        lines = [conversation["title"], ""]
        for row in self.messages(identifier):
            lines.append(("你" if row["role"] == "user" else "AI") + " · " + row["provider"] + " / " + row["model"])
            lines.append(text_with_sources(row["text"],row['metadata']))
            if row["images"]:
                lines.append("[图片：" + "、".join(image["name"] for image in row["images"]) + "]")
            if row["error"]:
                lines.append("[未成功：" + row["error"] + "]")
            partial = row["metadata"].get("partial") or {}
            if partial.get("text"):
                lines.extend(["AI（未完成，不计入后续上下文）", partial["text"]])
            lines.append("")
        return "\n".join(lines)

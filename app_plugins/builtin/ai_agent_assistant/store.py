"""Private local queue, stable IDs, and crash/timeout-aware submission journal."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from model.AppDataStorage import data_directory
from .tasks import fingerprint


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, root):
        self.root = data_directory(root)
        self.file = self.root.file('queue.sqlite3')

    @contextmanager
    def connect(self):
        self.root.ensure()
        db = self.file.connect()
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1): raise ValueError('AI Agent 队列版本不兼容，未重置数据。')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT UNIQUE, payload TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, destination TEXT NOT NULL DEFAULT "", detail TEXT NOT NULL DEFAULT "", created TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            if not version: db.execute('PRAGMA user_version=1')
            yield db
            db.commit()
        except Exception:
            db.rollback(); raise
        finally: db.close()

    def settings(self):
        if not self.file.exists(): return {'url': '', 'sheet': '口播表格'}
        with self.connect() as db:
            row = db.execute('SELECT value FROM preferences WHERE key="target"').fetchone()
        return json.loads(row[0]) if row else {'url': '', 'sheet': '口播表格'}

    def save_settings(self, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', ('target', json.dumps(value)))

    def stage(self, payloads, *, new_version=False):
        imported, unchanged, blocked = [], [], []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for payload in payloads:
                source = payload['source_key'] + (':' + uuid4().hex if new_version else '')
                digest = fingerprint(payload)
                previous = db.execute('SELECT id,digest,state FROM jobs WHERE source=?', (source,)).fetchone()
                if previous:
                    if previous[1] == digest:
                        unchanged.append(previous[0]); continue
                    if previous[2] not in {'draft', 'failed'}:
                        blocked.append(payload['task_id']); continue
                    db.execute('UPDATE jobs SET payload=?,digest=?,state="draft",detail="" WHERE id=?',
                               (json.dumps(payload, ensure_ascii=False), digest, previous[0]))
                    imported.append(previous[0])
                else:
                    key = uuid4().hex
                    db.execute('INSERT INTO jobs(id,source,payload,digest,state,created) VALUES (?,?,?,?,?,?)',
                               (key, source, json.dumps(payload, ensure_ascii=False), digest, 'draft', now()))
                    imported.append(key)
        return {'imported': imported, 'unchanged': unchanged, 'blocked': blocked}

    def jobs(self, ids=None, limit=1000):
        if not self.file.exists(): return []
        with self.connect() as db:
            rows = db.execute('SELECT id,payload,state,destination,detail,created FROM jobs ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall() if ids is None else [
                db.execute('SELECT id,payload,state,destination,detail,created FROM jobs WHERE id=?', (key,)).fetchone() for key in dict.fromkeys(ids)]
        return [{'id': row[0], 'payload': json.loads(row[1]), 'state': row[2], 'destination': row[3],
                 'detail': row[4], 'created': row[5]} for row in rows if row]

    def mark(self, ids, state, destination, detail=''):
        with self.connect() as db:
            db.executemany('UPDATE jobs SET state=?,destination=?,detail=? WHERE id=?',
                           [(state, destination, detail[:1000], key) for key in ids])

    def begin(self, jobs, destination):
        """Journal BEFORE a possible remote write; another process cannot resend."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for job in jobs:
                row = db.execute('SELECT digest,state FROM jobs WHERE id=?', (job['id'],)).fetchone()
                if not row or row[0] != fingerprint(job['payload']) or row[1] not in {'draft', 'failed'}:
                    raise ValueError('任务已改变或已提交，请先核对提交结果；未重复写入。')
            db.executemany('UPDATE jobs SET state="sending",destination=?,detail="请求结果尚待核对" WHERE id=?',
                           [(destination, job['id']) for job in jobs])

    def remove_drafts(self, ids):
        with self.connect() as db:
            db.executemany('DELETE FROM jobs WHERE id=? AND state IN ("draft","failed")', [(key,) for key in ids])

    def prepare_drafts(self, ids, *, confirm_chinese=False, expected):
        """Prepare exactly the reviewed snapshots, atomically; never rewrite history."""
        from .preparation import prepare_payload
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key in dict.fromkeys(ids):
                row = db.execute('SELECT payload,digest,state FROM jobs WHERE id=?', (key,)).fetchone()
                if not row or row[2] not in {'draft', 'failed'} or row[1] != expected.get(key):
                    raise ValueError('任务在确认期间发生变化，请刷新后重新选择；未覆盖其他进程的修改。')
                before = json.loads(row[0])
                value = prepare_payload(before, confirm_chinese=confirm_chinese)
                if value != before:
                    db.execute('UPDATE jobs SET payload=?,digest=? WHERE id=?',
                               (json.dumps(value, ensure_ascii=False), fingerprint(value), key))

    def edit(self, key, field, value):
        if field not in {'language', 'spoken', 'chinese', 'reference'}: raise ValueError('此字段不可编辑。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,state FROM jobs WHERE id=?', (key,)).fetchone()
            if not row or row[1] not in {'draft', 'failed'}: raise ValueError('已提交或待核对任务不能直接改写，请导入新版本。')
            payload = json.loads(row[0]); payload[field] = str(value).strip()
            if field == 'chinese':
                payload['chinese_source'] = '插件内手动校对'; payload['chinese_confirmed'] = True
            db.execute('UPDATE jobs SET payload=?,digest=? WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), fingerprint(payload), key))

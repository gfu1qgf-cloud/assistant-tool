"""Managed, transactional job state. Keys are never written to this database."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from model.AppDataStorage import data_directory, data_file
from model.AIImages import normalize_image
from .prompts import validate_spec, variant_prompt


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        data_directory(self.root).ensure()
        self.database = data_file(self.root / 'jobs.sqlite3')
        self.lock = threading.RLock()
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError('图片翻版记录来自更新版本，不能覆盖。')
            db.executescript('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);''')
            if version == 0: db.execute('PRAGMA user_version=1')

    @contextmanager
    def connection(self):
        with self.lock:
            db = self.database.connect(timeout=5)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def all(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM jobs ORDER BY created DESC')]

    @contextmanager
    def runtime_lock(self):
        # The host serializes its own workers. This additionally protects two
        # application processes sharing a data volume from double submission.
        import os
        managed = data_file(self.root / '.runtime.lock')
        with managed.open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT value FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('该生成记录已不存在。')
        return json.loads(row[0])

    def save(self, job):
        with self.connection() as db:
            target_version = 3 if job.get('planning') else 2 if job['spec'].get('mode', 'batch') == 'instant' else 1
            if target_version > db.execute('PRAGMA user_version').fetchone()[0]:
                # Old 0.1 clients reject version 2 rather than interpreting a
                # queued instant intent as a new Batch on downgrade.
                db.execute('PRAGMA user_version=' + str(target_version))
            db.execute('INSERT INTO jobs(id,value,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                       (job['id'], json.dumps(job, ensure_ascii=False), job['created']))

    def draft(self, value=None):
        with self.connection() as db:
            if value is not None:
                db.execute('INSERT INTO drafts VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                           (json.dumps(value, ensure_ascii=False),))
            row = db.execute('SELECT value FROM drafts WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def create(self, spec, credential, *, identifier=None, defer_snapshot=False):
        spec = validate_spec(spec)
        if not credential:
            raise ValueError('请先在程序设置的OpenAI密钥列表选择一个Key。')
        output = Path(str(spec.get('output', '')))
        if not output.is_absolute() or not output.is_dir():
            raise ValueError('请选择存在的绝对输出目录。')
        identifier = identifier or uuid4().hex
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('本地作业ID无效。')
        # A repeated GUI delivery must not create another paid job.
        try:
            return self.get(identifier)
        except ValueError:
            pass
        destination = output.resolve() / ('图片翻版_' + time.strftime('%Y%m%d_%H%M%S') + '_' + identifier[:8])
        data_directory(destination).ensure(exist_ok=False)
        refs = []
        assets = data_directory(self.root / identifier / 'references')
        assets.ensure()
        for source in ([] if defer_snapshot else spec['references']):
            raw, suffix, mime, width, height = normalize_image(source)
            filename = hashlib.sha256(raw).hexdigest() + '.' + suffix
            assets.file(filename).write_bytes(raw)
            refs.append({'file': filename, 'mime': mime, 'width': width, 'height': height})
        job = {'schema': 1, 'id': identifier, 'created': time.time(), 'credential': credential,
               'spec': deepcopy(spec), 'references': refs, 'output': str(destination),
               'snapshot_pending': defer_snapshot,
               'status': 'ready', 'error': '', 'attempts': [], 'cancel_requested': False,
               'items': [{'id': f'{identifier}-{i:04d}', 'index': i, 'prompt': '' if defer_snapshot else variant_prompt(spec, i),
                          'status': 'pending', 'error': '', 'path': ''} for i in range(spec['count'])]}
        if spec.get('rewrite_enabled'):
            from .planning import initial_plan
            job['planning'] = initial_plan(spec['count'], spec['rewrite_model'])
            job['status'] = 'planning'
            for item in job['items']: item['prompt'] = ''
        self.save(job)  # The complete input snapshot exists before any API call.
        return job

    def prepare_snapshot(self, job):
        if not job.get('snapshot_pending'):
            return
        refs = []
        assets = data_directory(self.root / job['id'] / 'references')
        assets.ensure()
        for source in job['spec']['references']:
            raw, suffix, mime, width, height = normalize_image(source)
            filename = hashlib.sha256(raw).hexdigest() + '.' + suffix
            assets.file(filename).write_bytes(raw)
            refs.append({'file': filename, 'mime': mime, 'width': width, 'height': height})
        if not job.get('planning'):
            for item in job['items']:
                item['prompt'] = variant_prompt(job['spec'], item['index'])
        job.update(references=refs, snapshot_pending=False)
        self.save(job)

    def asset(self, job, ref):
        filename = ref['file']
        if Path(filename).name != filename or len(filename.split('.')[0]) != 64:
            raise ValueError('参考图片记录无效。')
        managed = data_directory(self.root / job['id'] / 'references').file(filename)
        raw = managed.read_bytes()
        if hashlib.sha256(raw).hexdigest() != filename.split('.')[0]:
            raise ValueError('已保存的人物参考图损坏，不会改用其他图片。')
        return raw

    def request_stop(self, identifier):
        job = self.get(identifier)
        if job['spec'].get('mode', 'batch') != 'instant':
            raise ValueError('停止后续图片仅适用于即时模式。')
        data_directory(self.root / identifier).file('stop.json').write_json({'stop': True})

    def stop_requested(self, identifier):
        return data_directory(self.root / identifier).file('stop.json').exists()

    def clear_stop(self, identifier):
        data_directory(self.root / identifier).file('stop.json').remove(missing_ok=True)

    def delete(self, identifier):
        job = self.get(identifier)
        if job['status'] not in ('done', 'partial', 'failed', 'cancelled'):
            raise ValueError('云端批次尚未结束，请先取消并核对结果，不能丢弃活动批次记录。')
        with self.connection() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (identifier,))
        # User output and reference snapshots are deliberately retained.


def summary(job):
    done = sum(item['status'] == 'done' for item in job['items'])
    failed = sum(item['status'] == 'failed' for item in job['items'])
    counts = (job.get('attempts') or [{}])[-1].get('counts') or {}
    return done, failed, counts

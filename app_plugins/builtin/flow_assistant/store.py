"""Persistent job snapshots. Browser replies never choose destination paths."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from .routing import dispatch_rank
from .credit_policy import balance_ready
from .policy import signature, capable
from .transfer_policy import retry_info, ready_bindings

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
TRANSIENT = {'preparing', 'uploading', 'submitting', 'generating', 'downloading'}
TERMINAL = {'completed', 'cancelled'}
RECORD_DELETABLE = {'queued', 'completed', 'cancelled', 'failed', 'archive_failed', 'download_parked', 'needs_review'}
RECORD_UNRESOLVED = {'uncertain', 'download_failed'}


def safe_name(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip(' .')[:90] or 'image'


def fingerprint(path):
    path = Path(path)
    stat = path.stat()
    if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError('参考图片不存在或格式不支持。')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest.hexdigest()}


def make_jobs(target, images, prompt, *, duration=None, aspect='9:16', resolution='720p', count=1, budget=0):
    directory = Path(target['target_dir']).resolve()
    if not directory.is_dir():
        raise ValueError(f'任务目录不存在：{directory}')
    from model.MediaStorage import require_media_output
    require_media_output(directory)
    if not str(prompt).strip():
        raise ValueError('请填写视频提示词。')
    if duration not in {None, 4, 6, 8, 10} or aspect not in {'9:16', '16:9'} or resolution not in {'720p', '360p'}:
        raise ValueError('生成参数无效。')
    if not 1 <= int(count) <= 20 or not 0 <= int(budget) <= 10000:
        raise ValueError('数量或积分预算无效。')
    task = target.get('task')
    def value(name, fallback=''):
        return task.get(name, fallback) if isinstance(task, dict) else getattr(task, name, fallback)
    result = []
    for image in images:
        image = Path(image).resolve()
        if image.parent != directory:
            raise ValueError('请选择该任务目录中的图片。')
        identity = fingerprint(image)
        for _ in range(int(count)):
            job_id = uuid.uuid4().hex
            result.append({'id': job_id, 'task_id': str(value('task_id')),
                'task_name': str(value('task_name', target.get('label', ''))),
                'task_date': str(value('task_date')), 'task_type': str(value('task_type')),
                'directory': str(directory), 'image': str(image), 'image_identity': identity,
                'prompt': str(prompt).strip(), 'model': 'omni_1_1_flash', 'duration': duration,
                'aspect': aspect, 'resolution': resolution, 'budget': int(budget),
                'copies_per_image': int(count)})
    if not result:
        raise ValueError('至少选择一张图片。')
    return result


class QueueStore:
    def __init__(self, namespace):
        self.file = namespace.file('queue.sqlite3')
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                       'status TEXT NOT NULL, attempt TEXT NOT NULL DEFAULT "", browser TEXT NOT NULL DEFAULT "", '
                       'remote TEXT NOT NULL DEFAULT "{}", error TEXT NOT NULL DEFAULT "", updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS history (job TEXT, at REAL, status TEXT, message TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, label TEXT, created REAL, limit_total REAL NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS charges (attempt TEXT PRIMARY KEY, batch TEXT, job TEXT, cost REAL, source TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, value TEXT, updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, batch TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS deleted_attempts (attempt TEXT, browser TEXT, '
                       'PRIMARY KEY (attempt,browser))')

    @contextmanager
    def connection(self):
        db = self.file.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key="preferences"').fetchone()
        return json.loads(row[0]) if row else {}

    def save_settings(self, value):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES ("preferences", ?)', (json.dumps(value),))

    def enqueue(self, jobs, *, batch_id=None, label='', limit_total=0, request_id=None):
        if not jobs: raise ValueError('请选择至少一条生成任务。')
        batch_id = batch_id or uuid.uuid4().hex
        if type(limit_total) not in (int, float) or not 0 <= limit_total <= 10**8:
            raise ValueError('整批积分上限无效。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                existing = db.execute('SELECT batch FROM submissions WHERE id=?', (request_id,)).fetchone()
                if existing:
                    return existing[0]
            db.execute('INSERT INTO batches VALUES (?,?,?,?)', (batch_id, label or time.strftime('%m-%d %H:%M:%S'), time.time(), limit_total))
            if request_id:
                db.execute('INSERT INTO submissions VALUES (?,?)', (request_id, batch_id))
            used = {json.loads(row[0]).get('output') for row in db.execute('SELECT payload FROM jobs')}
            for original in jobs:
                job = copy.deepcopy(original)
                job.update(batch_id=batch_id, batch_label=label or time.strftime('%m-%d %H:%M:%S'))
                job.setdefault('priority', 0)
                job.setdefault('created_at', time.time())
                # Allocate by submission order, never by completion order.
                sequence = 1
                while True:
                    name = f'Flow_{sequence:03d}_{safe_name(Path(job["image"]).stem)}.mp4'
                    output = str(Path(job['directory']) / name)
                    if output not in used and not Path(output).exists():
                        break
                    sequence += 1
                job['output'] = output
                used.add(output)
                db.execute('INSERT INTO jobs(id,payload,status,updated) VALUES (?,?,"queued",?)',
                           (job['id'], json.dumps(job, ensure_ascii=False), time.time()))
                db.execute('INSERT INTO history VALUES (?,?,"queued","")', (job['id'], time.time()))
        return batch_id

    def batches(self):
        with self.connection() as db:
            rows = db.execute('SELECT b.id,b.label,b.created,b.limit_total,COALESCE(SUM(c.cost),0), '
                              'SUM(CASE WHEN c.source="user_budget" THEN 1 ELSE 0 END) '
                              ',SUM(CASE WHEN c.source="duration_rule" THEN 1 ELSE 0 END) '
                              'FROM batches b LEFT JOIN charges c ON c.batch=b.id GROUP BY b.id').fetchall()
        return {row[0]: dict(id=row[0], label=row[1], created=row[2], limit_total=row[3], reserved=row[4], estimated=row[5], fixed=row[6]) for row in rows}

    def reserve(self, job_id, attempt, cost, source):
        if type(cost) not in (int, float) or not 0 <= cost <= 10000:
            raise ValueError('积分预留无效。')
        row = self.get(job_id)
        batch = row['payload'].get('batch_id', '')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM charges WHERE attempt=?', (attempt,)).fetchone():
                return True
            limit = db.execute('SELECT limit_total FROM batches WHERE id=?', (batch,)).fetchone()
            used = db.execute('SELECT COALESCE(SUM(cost),0) FROM charges WHERE batch=?', (batch,)).fetchone()[0]
            if limit and limit[0] and used + cost > limit[0]:
                return False
            db.execute('INSERT INTO charges VALUES (?,?,?,?,?,?)', (attempt, batch, job_id, cost, source, time.time()))
        return True

    def set_batch_limit(self, batch_id, limit):
        if type(limit) not in (int, float) or not 0 <= limit <= 10**8:
            raise ValueError('整批积分上限无效。')
        with self.connection() as db:
            db.execute('UPDATE batches SET limit_total=? WHERE id=?', (limit, batch_id))

    def submission(self, request_id):
        with self.connection() as db:
            row = db.execute('SELECT batch FROM submissions WHERE id=?', (request_id,)).fetchone()
        return row[0] if row else None

    def duplicates(self, jobs, exclude=()):
        identities = {signature(job) for job in jobs}
        return [row['id'] for row in self.rows() if row['id'] not in exclude
                and row['status'] in TRANSIENT | {'queued'} and signature(row['payload']) in identities]

    def delete_records(self, ids, *, confirmed_finished=False, busy_ids=()):
        """Delete queue/history only; keep media, charge totals and replay keys.

        Recheck in the transaction: a queued item may have started while the
        confirmation dialog was open. Keep such items instead of interrupting
        paid work or rejecting the rest of a multiple selection.
        """
        if not isinstance(ids, (list, tuple, set)) or any(not isinstance(value, str) for value in ids):
            raise ValueError('删除记录需要选择有效的任务。')
        wanted, busy = set(ids), set(busy_ids)
        deleted, skipped = [], []
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,status,attempt,browser FROM jobs ORDER BY rowid').fetchall()
            for job_id, status, attempt, browser in rows:
                if job_id not in wanted: continue
                if job_id in busy or status not in RECORD_DELETABLE | RECORD_UNRESOLVED:
                    skipped.append({'id': job_id, 'status': status, 'reason': '正在执行或保存，已保留'})
                    continue
                if status in RECORD_UNRESOLVED and not confirmed_finished:
                    skipped.append({'id': job_id, 'status': status, 'reason': '尚未确认云端结束，已保留'})
                    continue
                if attempt and browser:
                    db.execute('INSERT OR IGNORE INTO deleted_attempts VALUES (?,?)', (attempt, browser))
                deleted.append(job_id)
            db.executemany('DELETE FROM history WHERE job=?', [(job_id,) for job_id in deleted])
            db.executemany('DELETE FROM jobs WHERE id=?', [(job_id,) for job_id in deleted])
            # Do not delete charges: removing one finished row must not refund
            # its batch budget and enable more paid submissions. Submission
            # keys also remain so a delayed enqueue cannot recreate deleted jobs.
        return {'deleted': deleted, 'skipped': skipped}

    def was_deleted(self, attempt, browser):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM deleted_attempts WHERE attempt=? AND browser=?',
                              (attempt, browser)).fetchone() is not None

    def set_payload(self, job_id, payload):
        self.set_payloads([(job_id, payload)])

    def set_payloads(self, values):
        with self.connection() as db:
            db.executemany('UPDATE jobs SET payload=?,updated=? WHERE id=?',
                [(json.dumps(payload, ensure_ascii=False), time.time(), job_id) for job_id, payload in values])

    def drafts(self):
        with self.connection() as db:
            rows = db.execute('SELECT id,value FROM drafts ORDER BY updated').fetchall()
        return {key: json.loads(value) for key, value in rows}

    def save_draft(self, key, value):
        if not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(value, dict) or len(json.dumps(value)) > 200000:
            raise ValueError('草稿格式或大小无效。')
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO drafts VALUES (?,?,?)', (key, json.dumps(value, ensure_ascii=False), time.time()))

    def rows(self):
        with self.connection() as db:
            rows = db.execute('SELECT id,payload,status,attempt,browser,remote,error,updated FROM jobs ORDER BY rowid').fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row):
        return {'id': row[0], 'payload': json.loads(row[1]), 'status': row[2], 'attempt': row[3],
                'browser': row[4], 'remote': json.loads(row[5]), 'error': row[6], 'updated': row[7]}

    def get(self, job_id):
        with self.connection() as db:
            row = db.execute('SELECT id,payload,status,attempt,browser,remote,error,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
        return self._row(row) if row else None

    def update(self, job_id, status, *, attempt=None, browser=None, remote=None, error=''):
        row = self.get(job_id)
        if row is None:
            raise ValueError('找不到队列任务。')
        with self.connection() as db:
            db.execute('UPDATE jobs SET status=?,attempt=?,browser=?,remote=?,error=?,updated=? WHERE id=?',
                (status, row['attempt'] if attempt is None else attempt,
                 row['browser'] if browser is None else browser,
                 json.dumps(row['remote'] if remote is None else remote, ensure_ascii=False), error, time.time(), job_id))
            db.execute('INSERT INTO history VALUES (?,?,?,?)', (job_id, time.time(), status, error))

    def recover(self):
        with self.connection() as db:
            charged_attempts = {row[0] for row in db.execute('SELECT attempt FROM charges')}
        for row in self.rows():
            upload = retry_info(row, 'upload')
            charged = row['attempt'] in charged_attempts
            if row['status'] == 'uploading' and upload and not charged:
                info = {**upload, 'active': False, 'next_at': time.time(), 'waiting': '等待继续队列后自动恢复上传'}
                self.update(row['id'], 'uploading', remote={**row['remote'], 'transfer_retries': {
                    **row['remote'].get('transfer_retries', {}), 'upload': info}}, error='程序重启，保留原图片上传进度。')
            elif row['status'] == 'downloading' and ready_bindings(row['remote']):
                info = {**retry_info(row, 'download'), 'active': False, 'next_at': time.time() + 5}
                self.update(row['id'], 'download_failed', remote={**row['remote'], 'transfer_retries': {
                    **row['remote'].get('transfer_retries', {}), 'download': info}}, error='程序重启，将自动恢复原视频下载，不重新生成。')
            elif row['status'] in TRANSIENT:
                self.update(row['id'], 'uncertain', error='程序曾中断；等待原浏览器核对，不会自动再次生成。')
            elif row['status'] == 'archiving':
                self.update(row['id'], 'archive_failed', error='归档曾中断，可以仅重试归档。')

    def retry_rejected_batch(self, job_id):
        """Explicit retry only: the audited upstream busy guard precedes import.

        Keep all charge/history rows. Zero only this unsubmitted reservation;
        do not fabricate a website credit refund or reopen ambiguous submissions.
        """
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            raw = db.execute('SELECT id,payload,status,attempt,browser,remote,error,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not raw: return False
            row = self._row(raw)
            if (row['status'] != 'uncertain' or row['error'].strip() != '插件队列正在运行，请先停止'
                    or any(row['remote'].get(key) for key in ('bindings', 'upstream_task', 'generation_response',
                        'download', 'unverified_download', 'page_recovery', 'cloud_finished'))):
                return False
            unused = db.execute('SELECT COALESCE(SUM(cost),0) FROM charges WHERE attempt=? AND job=?',
                                (row['attempt'], job_id)).fetchone()[0]
            db.execute('UPDATE charges SET cost=0,source="rejected_before_submission" WHERE attempt=? AND job=?',
                       (row['attempt'], job_id))
            message = '原批次在网页接收前被拒绝，已撤销本次未提交预算预留；等待网页空闲后重新核对参数。'
            db.execute('UPDATE jobs SET status="queued",attempt="",browser="",remote="{}",error=?,updated=? WHERE id=?',
                       (message, time.time(), job_id))
            db.execute('INSERT INTO history VALUES (?,?,"queued",?)', (job_id, time.time(),
                message + f' 撤销未提交预留：{unused:g}；原执行批次：' + row['attempt']))
        return True

    def retry(self, ids, *, regenerate=False):
        for row in self.rows():
            if row['id'] not in ids or row['status'] in TERMINAL:
                continue
            if (not regenerate and row['status'] == 'uncertain'
                    and row['error'].strip() == '插件队列正在运行，请先停止'
                    and self.retry_rejected_batch(row['id'])):
                continue
            if row['remote'].get('download', {}).get('state') == 'complete':
                self.update(row['id'], 'archive_failed')
            elif row['remote'].get('bindings') and not regenerate:
                self.update(row['id'], 'download_failed', error='保留已有媒体，只重试下载。')
            elif row['status'] == 'failed' or regenerate:
                self.update(row['id'], 'queued', attempt='', browser='', remote={})


def choose_client(clients, rows, limit, job=None, preferences=None, cost=None):
    active = [row for row in rows if row['status'] in TRANSIENT]
    if len(active) >= limit:
        return None
    held = active + [r for r in rows if r['status'] in {'uncertain', 'download_failed'}]
    busy_accounts = {row['remote'].get('account') for row in held} | {
        client.get('account') for client in clients if client.get('batch_running')}
    busy_browsers = {row['browser'] for row in held}
    eligible = [client for client in clients if client.get('account') and client.get('credits') is not None
                and balance_ready(client) and not client.get('blocked') and not client.get('batch_running')
                and client['id'] not in busy_browsers and client['account'] not in busy_accounts]
    if job is None:
        return max(eligible, key=lambda c: c['credits'], default=None)
    preferences = preferences or {}
    ranked = [(dispatch_rank(client, job, preferences), client) for client in eligible if capable(client, job, preferences, cost)]
    ranked = [(rank, client) for rank, client in ranked if rank is not None
              and (cost is None or client['credits'] >= cost)]
    # Prefer a matching lane, then use the smallest sufficient balance when
    # this exact parameter combination has a freshly observed page cost.
    ranked.sort(key=lambda item: (item[0], item[1]['credits'] if cost is not None else -item[1]['credits']))
    return ranked[0][1] if ranked else None

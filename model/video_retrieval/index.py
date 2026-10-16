"""Incremental frame vectors; one search result per original video."""

from model.AppDataStorage import data_io, data_file, data_directory, application_storage
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

import numpy as np
from app_paths import DATA_ROOT as APP_ROOT
from model.image_retrieval.encoder import MODEL_SPECS, ModelLoadError
from .frames import (Canceled, VIDEO_SUFFIXES, video_duration, sample_times,
                     scene_times, extract_frame)

INDEX_ROOT = APP_ROOT / 'SmartVideoSearch'


def discover_videos(roots, canceled=lambda: False):
    """All roots must be accessible before pruning any previously indexed file."""
    paths, seen = [], set()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f'视频库目录无法访问：{root}；旧索引保留。')
        for directory, _dirs, files in os.walk(root, onerror=lambda error: (_ for _ in ()).throw(error)):
            if canceled():
                raise Canceled('已取消目录扫描。')
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() in VIDEO_SUFFIXES:
                    key = os.path.normcase(str(path.resolve()))
                    if key not in seen:
                        paths.append(str(path.resolve()))
                        seen.add(key)
    return sorted(paths, key=str.casefold)


class VideoSearchIndex:
    def __init__(self, root=None, model_key='large336'):
        if model_key not in MODEL_SPECS:
            raise ValueError('不支持的视频索引模型。')
        self.root = application_storage().resolve(root or INDEX_ROOT)
        data_directory(self.root).ensure(parents=True, exist_ok=True)
        self.thumbnails = self.root / 'thumbnails' / model_key
        data_directory(self.thumbnails).ensure(parents=True, exist_ok=True)
        self.path = self.root / f'index-{model_key}.sqlite3'
        self._lock = threading.RLock()
        self._matrix_cache = None
        with closing(self._connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS videos (
                    path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                    duration REAL, model_id TEXT, profile TEXT, indexed_at REAL);
                CREATE TABLE IF NOT EXISTS frames (
                    path TEXT, seconds REAL, vector BLOB NOT NULL, thumbnail TEXT,
                    PRIMARY KEY(path,seconds));
            ''')

    def _connect(self):
        db = data_file(str(self.path)).connect(timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def count(self):
        with self._lock, closing(self._connect()) as db:
            return (db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM frames').fetchone()[0])

    def _forget(self, paths):
        with closing(self._connect()) as db, db:
            for path in paths:
                db.execute('DELETE FROM frames WHERE path=?', (path,))
                db.execute('DELETE FROM videos WHERE path=?', (path,))
        self._matrix_cache = None

    def sync(self, paths, encoder, settings, ffmpeg, ffprobe,
             progress=lambda *_: None, canceled=lambda: False):
        profile = json.dumps({key: settings[key] for key in
                             ('interval', 'max_frames', 'detect_scenes', 'scene_threshold')}, sort_keys=True)
        result = {'updated': 0, 'unchanged': 0, 'removed': 0, 'errors': [], 'canceled': False}
        paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
        seen_paths = set(paths)
        with self._lock:
            try:
                for number, raw in enumerate(paths, 1):
                    if application_storage().protected: application_storage().check(self.root,write=True)
                    if canceled():
                        raise Canceled()
                    path = Path(raw)
                    progress(number - 1, len(paths), f'{number}/{len(paths)} · {path.name}')
                    try:
                        stat = path.stat()
                        with closing(self._connect()) as db:
                            old = db.execute('SELECT * FROM videos WHERE path=?', (raw,)).fetchone()
                        if old and (old['size'], old['mtime_ns'], old['model_id'], old['profile']) == (
                                stat.st_size, stat.st_mtime_ns, encoder.model_id, profile):
                            result['unchanged'] += 1
                            continue
                        duration = video_duration(path, ffprobe, canceled)
                        cuts = scene_times(path, ffmpeg, settings['scene_threshold'], canceled,
                                           settings['max_frames']) if settings['detect_scenes'] else []
                        times = sample_times(duration, settings['interval'], settings['max_frames'], cuts)
                        records = []
                        # Old frame rows are replaced only after the entire new video is ready.
                        with data_io.TemporaryDirectory(prefix='frames-', dir=self.root) as directory:
                            for frame_number, seconds in enumerate(times, 1):
                                if canceled():
                                    raise Canceled()
                                progress(number - 1, len(paths),
                                         f'{path.name} · 画面 {frame_number}/{len(times)} · {seconds:.1f}秒')
                                image = extract_frame(path, seconds, ffmpeg, canceled)
                                if image is None:
                                    continue
                                temporary = Path(directory) / f'{frame_number}.jpg'
                                try:
                                    with data_file(temporary).open('wb') as handle:
                                        image.save(handle, 'JPEG', quality=90)
                                finally:
                                    image.close()
                                vector = np.asarray(encoder.images([temporary])[0], dtype=np.float32).reshape(-1)
                                length = float(np.linalg.norm(vector))
                                if not vector.size or not np.all(np.isfinite(vector)) or length <= 0:
                                    raise ValueError('模型返回无效画面特征；保留旧索引。')
                                key = hashlib.sha256(f'{raw}:{stat.st_size}:{stat.st_mtime_ns}:{profile}:{seconds}'.encode()).hexdigest()
                                target = self.thumbnails / f'{key}.jpg'
                                data_io.replace(temporary, target)
                                records.append((raw, seconds, (vector / length).astype('<f4').tobytes(), str(target)))
                        if not records:
                            raise ValueError('未读到非黑屏画面；保留旧索引。')
                        current = path.stat()
                        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                            raise ValueError('视频仍在写入或已经改变，本次暂不更新。')
                        if canceled():
                            raise Canceled()
                        with closing(self._connect()) as db, db:
                            db.execute('DELETE FROM frames WHERE path=?', (raw,))
                            db.executemany('INSERT INTO frames VALUES (?,?,?,?)', records)
                            db.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?)',
                                       (raw, stat.st_size, stat.st_mtime_ns, duration, encoder.model_id, profile, time.time()))
                        self._matrix_cache = None
                        result['updated'] += 1
                    except (Canceled, ModelLoadError):
                        raise
                    except Exception as error:
                        result['errors'].append(f'{path.name}：{type(error).__name__}: {error}')
                if canceled():
                    raise Canceled()
                with closing(self._connect()) as db:
                    removed = [row[0] for row in db.execute('SELECT path FROM videos') if row[0] not in seen_paths]
                self._forget(removed)
                result['removed'] = len(removed)
            except Canceled:
                result['canceled'] = True
            finally:
                # Remove uncommitted JPEGs too; canceled/failed attempts must not
                # accumulate frame files or delete another model's thumbnails.
                self._cleanup_thumbnails()
            progress(len(paths), len(paths), '视频索引已保存；原视频未修改。')
        return result

    def _cleanup_thumbnails(self):
        with closing(self._connect()) as db:
            used = {str(application_storage().resolve(row[0])) for row in db.execute('SELECT thumbnail FROM frames')}
        for path in self.thumbnails.glob('*.jpg'):
            if str(path) not in used:
                try:
                    data_file(path).unlink()
                except OSError:
                    pass

    def search(self, vector, model_id, minimum_score=-1):
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query))
        if not query.size or not np.all(np.isfinite(query)) or norm <= 0:
            raise ValueError('无效搜索条件。')
        with self._lock:
            if self._matrix_cache is None or self._matrix_cache[0] != model_id:
                with closing(self._connect()) as db:
                    rows = [dict(row) for row in db.execute('''
                        SELECT f.*,v.duration,v.size,v.mtime_ns FROM frames f
                        JOIN videos v ON f.path=v.path WHERE v.model_id=?
                    ''', (model_id,))]
                matrix = np.stack([np.frombuffer(row.pop('vector'), dtype='<f4') for row in rows]) if rows else np.empty((0, query.size), np.float32)
                self._matrix_cache = (model_id, rows, matrix)
            _, rows, matrix = self._matrix_cache
            if matrix.shape[1] != query.size:
                raise ValueError('查询与视频索引模型不一致，请切回原模型或更新对应索引。')
            scores = matrix @ (query / norm)
            matches = {}
            checked = {}
            for position in np.argsort(-scores, kind='stable'):
                score, row = float(scores[position]), rows[position]
                if score < minimum_score:
                    break
                path = row['path']
                if path not in checked:
                    try:
                        stat = Path(path).stat()
                        checked[path] = (stat.st_size, stat.st_mtime_ns) == (row['size'], row['mtime_ns'])
                    except OSError:
                        checked[path] = False
                if not checked[path]:
                    continue
                thumbnail = str(application_storage().resolve(row['thumbnail'])) if row.get('thumbnail') else ''
                hit = {**row, 'thumbnail': thumbnail, 'score': score}
                if path not in matches:
                    matches[path] = {**hit, 'hits': [hit]}
                elif len(matches[path]['hits']) < 4:
                    matches[path]['hits'].append(hit)
            return list(matches.values())

    def release(self):
        self._matrix_cache = None

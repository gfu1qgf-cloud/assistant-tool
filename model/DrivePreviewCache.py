"""Bounded, disposable Drive media cache; never holds URLs, names or tokens."""
from contextlib import contextmanager
from collections import Counter
import hashlib
import os
from pathlib import Path
import re
import threading
import time

from model.AppDataStorage import data_directory, data_file
from model.MediaStorage import private_volume_path, require_media_output, require_media_space

LOCK = threading.RLock()
PINS = Counter()
OWNED = re.compile(r'[0-9a-f]{64}\.(?:jpg|media|part)$')
THUMB_LIMIT = 256 * 1024**2
MEDIA_LIMIT = 2 * 1024**3


def default_root():
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return validate_root(base / 'AssistantTool' / 'DrivePreviewCache')


def validate_root(root):
    root = Path(root).expanduser().absolute()
    if private_volume_path(root):
        raise ValueError('网盘预览缓存不能放在绑定的加密卷，请选择卷外目录。')
    for path in (root, *root.parents):
        if path.is_symlink() or path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('预览缓存目录不能通过符号链接或目录联接跳转。')
    return root.resolve()


class DrivePreviewCache:
    def __init__(self, root=None, *, thumb_limit=THUMB_LIMIT, media_limit=MEDIA_LIMIT):
        self.root = validate_root(root if root is not None else default_root())
        self.limits = {'thumbs': int(thumb_limit), 'media': int(media_limit)}
        if any(value <= 0 or value > 32*1024**3 for value in self.limits.values()):
            raise ValueError('缓存容量设置无效。')

    def path(self, kind, account, item):
        if kind not in self.limits: raise ValueError('未知预览缓存类型。')
        identity = '\0'.join((str(account), str(item['id']), str(item.get('version') or item.get('modifiedTime') or ''), kind))
        name = hashlib.sha256(identity.encode()).hexdigest()
        return self.root / kind / (name + ('.jpg' if kind == 'thumbs' else '.media'))

    def existing(self, path):
        path = self._owned(path)
        with LOCK:
            if path.is_file():
                os.utime(path, None)
                return path
        return None

    def _owned(self, path):
        path = Path(path).absolute()
        if path.parent not in (self.root / 'thumbs', self.root / 'media') or not OWNED.fullmatch(path.name):
            raise ValueError('拒绝操作缓存目录以外的文件。')
        validate_root(path.parent)
        if path.is_symlink(): raise ValueError('缓存文件不能是符号链接。')
        return path

    @contextmanager
    def pin(self, path):
        path = self._owned(path)
        key = str(path)
        with LOCK: PINS[key] += 1
        try: yield path
        finally:
            with LOCK:
                PINS[key] -= 1
                if PINS[key] <= 0: PINS.pop(key, None)

    def prune(self, kind, *, reserve=0, keep=(), clear=False):
        folder = self.root / kind
        validate_root(folder)
        with LOCK:
            protected = set(PINS) | {str(Path(p).absolute()) for p in keep}
            files = [(p.stat().st_mtime, p, p.stat().st_size) for p in folder.glob('*')
                     if p.is_file() and not p.is_symlink() and OWNED.fullmatch(p.name)]
            total = sum(size for _, _, size in files)
            for _, path, size in sorted(files):
                if not clear and total + reserve <= self.limits[kind]: break
                if str(path) in protected: continue
                data_file(path).unlink(missing_ok=True); total -= size
            if not clear and total + reserve > self.limits[kind]:
                raise ValueError('预览缓存空间不足（正在播放的文件不会删除），请停止播放或清理缓存。')
            return total

    @contextmanager
    def writer(self, target, expected=0):
        target = self._owned(target)
        kind = target.parent.name
        if expected > self.limits[kind]:
            raise ValueError('此文件超过预览缓存上限，请在网页中查看，或下载到外部素材目录。')
        pending = target.with_suffix('.part')
        with LOCK:
            if str(pending) in PINS: raise ValueError('同一个预览文件正在准备，请稍候。')
            data_file(pending).unlink(missing_ok=True)
        with self.pin(target), self.pin(pending):
            self.prune(kind, reserve=max(0, expected), keep=(target, pending))
            require_media_output(target)
            require_media_space(target, max(0, expected))
            data_directory(target.parent).ensure()
            try:
                with data_file(pending).open('wb') as stream: yield stream
                with LOCK:
                    self.prune(kind, keep=(pending,), reserve=0)
                    os.replace(pending, target)
            finally:
                data_file(pending).unlink(missing_ok=True)

    def usage(self):
        return {kind: sum(p.stat().st_size for p in (self.root / kind).glob('*')
                          if p.is_file() and not p.is_symlink() and OWNED.fullmatch(p.name))
                for kind in self.limits}

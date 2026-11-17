"""Incognito conversations and normalized image assets, held only in memory."""
import base64
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from model.AIImages import normalize_image
from .store import ChatStore


class _PrivateGeminiKeys:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def report_failure(self, *args, **kwargs):
        kwargs['message'] = '无痕请求失败；未保存接口响应正文。'
        return self.delegate.report_failure(*args, **kwargs)


def private_client(existing):
    from model.AIProviderClient import AIProviderClient
    broker = getattr(existing, 'gemini_keys', None)
    return AIProviderClient(_PrivateGeminiKeys(broker) if broker is not None else None)


class PrivateChatStore(ChatStore):
    is_private = True

    def __init__(self):
        self.root = self.path = None
        self.lock = threading.RLock()
        self._assets = {}
        self.closed = False
        self._memory = sqlite3.connect(':memory:', check_same_thread=False)
        self._memory.row_factory = sqlite3.Row
        self._memory.execute('PRAGMA foreign_keys=ON')
        self._memory.execute('PRAGMA temp_store=MEMORY')
        self._initialize_database()

    @contextmanager
    def connection(self):
        with self.lock:
            if self.closed:
                raise ValueError('无痕对话已经关闭。')
            with self._memory:
                yield self._memory

    def import_image(self, source, name=None):
        normalized, suffix, mime, width, height = normalize_image(source)
        filename = hashlib.sha256(normalized).hexdigest() + '.' + suffix
        key = 'images/' + filename
        with self.lock:
            if self.closed:
                raise ValueError('无痕对话已经关闭。')
            self._assets[key] = normalized
        return dict(file=key,name=name or ('剪贴板图片.png' if isinstance(source,bytes) else Path(source).name),
                    mime=mime,width=width,height=height)

    def asset_resource(self, image):
        with self.lock:
            value = self._assets.get(image.get('file'))
            if value is None:
                raise ValueError('无痕图片已经清除。')
            return value

    def asset_path(self, image):
        raise ValueError('无痕图片只保存在内存，没有本地文件路径。')

    def encoded_image(self, image):
        return dict(mime=image['mime'],data=base64.b64encode(self.asset_resource(image)).decode('ascii'))

    def prepare_batch(self, *args, **kwargs):
        raise ValueError('无痕聊天不支持需要持久化记录的Batch请求。')

    def delete(self, identifier):
        with self.connection() as db:
            db.execute('DELETE FROM conversations WHERE id=?',(identifier,))
            referenced = self._image_references(db)
        with self.lock:
            self._assets = {key:value for key,value in self._assets.items() if key in referenced}

    def purge_conversation(self, identifier):
        self.delete(identifier)

    def close_private(self):
        with self.lock:
            if not self.closed:
                self._assets.clear()
                self._memory.close()
                self.closed = True

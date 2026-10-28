"""Freeze the exact source bytes under an attempt-unique upload filename."""
import os
from pathlib import Path
import re
import shutil
import tempfile
from model.AppDataStorage import data_directory, data_file, data_io

from .store import fingerprint


def freeze_reference(row, downloads_root):
    attempt = row['attempt']
    if not re.fullmatch('[a-f0-9]{32}', attempt): raise ValueError('参考图执行编号无效。')
    source = Path(row['payload']['image'])
    expected = row['payload']['image_identity']['sha256']
    token = 'LZXREF' + attempt
    folder = Path(downloads_root).resolve() / 'FlowBridge' / attempt / 'reference'
    target = folder / (token + source.suffix.lower())
    if target.is_file():
        if fingerprint(target)['sha256'] != expected: raise ValueError('本次参考图副本与原始内容不符，已停止上传。')
    else:
        data_directory(folder).ensure()
        fd, temporary = data_io.mkstemp(prefix='.reference-', suffix=source.suffix.lower(), dir=folder)
        try:
            with os.fdopen(fd, 'wb') as output, data_file(source).open('rb') as incoming:
                shutil.copyfileobj(incoming, output)
                output.flush(); os.fsync(output.fileno())
            if fingerprint(temporary)['sha256'] != expected:
                raise ValueError('参考图在复制期间发生变化，未提交生成。')
            data_io.replace(temporary, target)
        finally:
            if os.path.exists(temporary): data_file(temporary).remove()
    return {'upload_id': attempt, 'path': str(target), 'filename': target.name, 'sha256': expected}


def verified_receipt(reference, receipt):
    return (isinstance(reference, dict) and isinstance(receipt, dict) and
        bool(re.fullmatch('[a-f0-9]{32}', str(reference.get('upload_id') or ''))) and
        bool(re.fullmatch('[a-f0-9]{64}', str(reference.get('sha256') or ''))) and
        Path(str(reference.get('path') or '')).is_absolute() and
        bool(re.fullmatch(r'LZXREF' + reference['upload_id'] + r'\.(?:png|jpe?g|webp|bmp)', str(reference.get('filename') or ''))) and
        Path(reference['path']).name == reference['filename'] and
        all(reference.get(key) == receipt.get(key) for key in ('upload_id', 'path', 'filename', 'sha256')) and
        bool(re.fullmatch('[A-Za-z0-9_-]{1,256}', str(receipt.get('media_id') or ''))) and
        not str(receipt.get('media_id')).startswith('img_') and
        receipt.get('verified') is True)

"""Keep a permanent content ID and a separate attempt-unique upload proof."""
import os
from pathlib import Path
import re
import shutil
import tempfile
from model.AppDataStorage import data_directory, data_file, data_io

from .store import fingerprint


def freeze_reference(row, downloads_root, *, include_image_id=False, stable_image_name=False):
    attempt = row['attempt']
    if not re.fullmatch('[a-f0-9]{32}', attempt): raise ValueError('参考图执行编号无效。')
    source = Path(row['payload']['image'])
    expected = row['payload']['image_identity']['sha256']
    if not re.fullmatch('[a-f0-9]{64}', str(expected)):
        raise ValueError('参考图内容编号无效，未上传图片。')
    token = f'IMG_{expected}' if stable_image_name else f'IMG_{expected}_{attempt}' if include_image_id else 'LZXREF' + attempt
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


def valid_filename(reference):
    filename=reference.get('filename')
    suffix=r'\.(?:png|jpe?g|webp|bmp)'
    return isinstance(filename,str) and bool(re.fullmatch(
        '(?:LZXREF'+re.escape(str(reference.get('upload_id','')))+'|IMG_'+
         re.escape(str(reference.get('sha256','')))+'(?:_'+re.escape(str(reference.get('upload_id','')))+')?)'+suffix,
        filename))


def supports_image_id(client):
    """Use the current document's receipt, not only its extension version."""
    return client.get('reference_sha256_names') is True


def supports_stable_name(client):
    return client.get('reference_stable_sha256_names') is True


def verified_receipt(reference, receipt):
    return (isinstance(reference, dict) and isinstance(receipt, dict) and
        bool(re.fullmatch('[a-f0-9]{32}', str(reference.get('upload_id') or ''))) and
        bool(re.fullmatch('[a-f0-9]{64}', str(reference.get('sha256') or ''))) and
        Path(str(reference.get('path') or '')).is_absolute() and
        valid_filename(reference) and
        Path(reference['path']).name == reference['filename'] and
        (not re.fullmatch(r'IMG_[a-f0-9]{64}\.[^.]+', reference['filename']) or
         Path(reference['path']).parent.name == 'reference' and Path(reference['path']).parent.parent.name == reference['upload_id']) and
        all(reference.get(key) == receipt.get(key) for key in ('upload_id', 'path', 'filename', 'sha256')) and
        bool(re.fullmatch('[A-Za-z0-9_-]{1,256}', str(receipt.get('media_id') or ''))) and
        not str(receipt.get('media_id')).startswith('img_') and
        receipt.get('verified') is True)

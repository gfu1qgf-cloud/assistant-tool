"""Immutable material cutouts and an atomic, private per-source catalogue."""
from collections import OrderedDict
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid

from model.AppDataStorage import data_file, data_directory, data_io

_LOCK = threading.RLock()
_HASHES = OrderedDict()
_PIXELS = OrderedDict()


def identity(path):
    from .store import file_identity
    return file_identity(path)


def fingerprint(path):
    path = data_file(path).path
    before = path.stat()
    token = (identity(path), before.st_size, before.st_mtime_ns)
    with _LOCK:
        digest = _HASHES.get(token)
    if digest is None:
        value = hashlib.sha256()
        with data_file(path).open('rb') as stream:
            while True:
                chunk = stream.read(1024*1024)
                if not chunk: break
                value.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('素材正在改变，请稍后重试：'+path.name)
        digest = value.hexdigest()
        with _LOCK:
            _HASHES[token] = digest
            while len(_HASHES) > 256: _HASHES.popitem(last=False)
    return dict(path=str(path.resolve()), size=before.st_size, mtime_ns=before.st_mtime_ns, sha256=digest)


def default_root():
    from app_paths import DATA_ROOT
    return data_directory(DATA_ROOT/'BatchTextVideo/Matting').path


def image_fingerprint(path):
    from .layers import IMAGE_SUFFIXES, read_image
    if Path(path).suffix.lower() not in IMAGE_SUFFIXES: return None
    from qt_compat import QtCore, QtGui
    info = Path(path).stat(); token = (identity(path), info.st_size, info.st_mtime_ns)
    with _LOCK:
        if token in _PIXELS: return _PIXELS[token]
    # Same orientation correction and 4096 decode bound as pool image import.
    image = read_image(path, QtCore.QSize(4096, 4096)).convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    if max(image.width(), image.height()) > 4096:
        image = image.scaled(4096, 4096, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                             QtCore.Qt.TransformationMode.SmoothTransformation)
    pixels = image.constBits().asstring(image.sizeInBytes())
    result = hashlib.sha256(f'{image.width()}x{image.height()}:'.encode()+pixels).hexdigest()
    with _LOCK:
        _PIXELS[token] = result
        while len(_PIXELS) > 256: _PIXELS.popitem(last=False)
    return result


def verify_file(stored, label):
    if (not isinstance(stored, dict) or not isinstance(stored.get('path'), str)
            or not isinstance(stored.get('sha256'), str) or len(stored['sha256']) != 64):
        raise ValueError('素材抠像记录无效，未自动套用。')
    path = Path(stored['path'])
    if not path.is_file(): raise FileNotFoundError(label+'不存在：'+path.name)
    if fingerprint(path)['sha256'] != stored['sha256']:
        raise ValueError(label+'已改变，请重新抠像或选择使用原素材：'+path.name)
    return path


class Catalogue:
    def __init__(self, root=None):
        self.root = data_directory(root or default_root()).path
        self.path = self.root/'catalogue.json'

    def load(self):
        if not data_file(self.path).exists(): return dict(version=1, revision=0, assets={})
        if self.path.stat().st_size > 8*1024**2: raise ValueError('素材抠像记录过大，未清空原记录。')
        value = data_file(self.path).read_json()
        if (not isinstance(value, dict) or value.get('version') != 1 or type(value.get('revision')) is not int
                or not isinstance(value.get('assets'), dict) or len(value['assets']) > 10000):
            raise ValueError('素材抠像记录损坏，未清空原记录。')
        for entry in value['assets'].values():
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                    or not isinstance(entry.get('parameters'), dict) or type(entry.get('enabled')) is not bool):
                raise ValueError('素材抠像记录损坏，未清空原记录。')
            for field in ('source',) if entry.get('mode') == 'dynamic' else ('source', 'result'):
                stored = entry.get(field)
                if (not isinstance(stored, dict) or not isinstance(stored.get('path'), str)
                        or not isinstance(stored.get('sha256'), str) or len(stored['sha256']) != 64):
                    raise ValueError('素材抠像记录损坏，未清空原记录。')
        return value

    def lookup(self, source):
        with _LOCK:
            assets = self.load()['assets']
        entry = assets.get(identity(source))
        if entry is None and assets and Path(source).is_file():
            digest = fingerprint(source)['sha256']
            candidates = [v for v in assets.values() if isinstance(v, dict) and v.get('enabled', True)
                          and isinstance(v.get('source'), dict)
                          and (v.get('mode') == 'dynamic' or (isinstance(v.get('result'), dict)
                               and isinstance(v['result'].get('path'), str)))]
            candidates.sort(key=lambda v: v.get('created_at', ''), reverse=True)
            entry = next((v for v in candidates if v.get('source', {}).get('sha256') == digest), None)
            if entry is None and any(v.get('source_pixels') for v in candidates):
                pixels = image_fingerprint(source)
                entry = next((v for v in candidates if pixels and v.get('source_pixels') == pixels), None)
            if entry:
                entry = copy.deepcopy(entry); entry['source'] = fingerprint(source)
        if entry is None: return None
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str): raise ValueError('素材抠像记录无效。')
        if not entry.get('enabled', True): return None
        try:
            verify_file(entry['source'], '原素材')
            if entry.get('mode') != 'dynamic': verify_file(entry['result'], '已保存的抠像结果')
        except (KeyError, TypeError) as error:
            raise ValueError('素材抠像记录无效，未自动套用。') from error
        return copy.deepcopy(entry)

    def _write(self, value):
        if len(value['assets']) > 10000: raise ValueError('抠像素材记录已达到上限，未覆盖原记录。')
        encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        if len(encoded.encode('utf-8')) > 8*1024**2: raise ValueError('抠像记录超过8MB，请减少画笔轨迹；原记录保留。')
        data_file(self.path).write_text(encoded, encoding='utf-8')

    def publish(self, source, temporary, parameters, *, expected, model_info=None):
        from .matting_settings import normalize
        parameters = normalize(parameters)
        if fingerprint(source)['sha256'] != expected['sha256']:
            raise ValueError('原素材在抠像期间已改变，未保存关联。')
        temporary = Path(temporary).resolve()
        assets_dir = data_directory(self.root/'assets').ensure()
        assets_dir = Path(assets_dir.path if hasattr(assets_dir, 'path') else self.root/'assets').resolve()
        if temporary.parent != assets_dir or temporary.suffix.lower() not in {'.png', '.mov'}:
            raise ValueError('抠像暂存文件必须位于本工作台管理目录。')
        if not temporary.is_file() or not temporary.stat().st_size: raise ValueError('抠像结果为空。')
        identifier = uuid.uuid4().hex
        target = assets_dir/('cutout-'+identifier+temporary.suffix.lower())
        data_io.replace(temporary, target)
        try:
            record = dict(id=identifier, source=fingerprint(source), result=fingerprint(target),
                          parameters=parameters, enabled=True, model_info=model_info or {},
                          source_pixels=image_fingerprint(source),
                          created_at=datetime.now(timezone.utc).isoformat())
            if fingerprint(source)['sha256'] != expected['sha256'] or record['source']['sha256'] != expected['sha256']:
                raise ValueError('原素材在抠像期间已改变，未保存关联。')
            with _LOCK:
                value = self.load()
                value['assets'][identity(source)] = record
                value['revision'] += 1
                self._write(value)
        except Exception:
            data_file(target).remove(missing_ok=True)
            raise
        return copy.deepcopy(record)

    def publish_dynamic(self, source, parameters, *, expected):
        from .matting_settings import normalize
        parameters = normalize(parameters)
        current = fingerprint(source)
        if current['sha256'] != expected['sha256']:
            raise ValueError('原素材在抠像期间已改变，未保存关联。')
        record = dict(id=uuid.uuid4().hex, mode='dynamic', source=current, parameters=parameters,
                      enabled=True, source_pixels=image_fingerprint(source),
                      created_at=datetime.now(timezone.utc).isoformat())
        if fingerprint(source)['sha256'] != expected['sha256']:
            raise ValueError('原素材在抠像期间已改变，未保存关联。')
        with _LOCK:
            value = self.load()
            value['assets'][identity(source)] = record
            value['revision'] += 1
            self._write(value)
        return copy.deepcopy(record)

    def set_enabled(self, source, enabled):
        if type(enabled) is not bool: raise ValueError('素材结果开关无效。')
        inherited = self.lookup(source) if identity(source) not in self.load()['assets'] else None
        with _LOCK:
            value = self.load(); key = identity(source)
            entry = value['assets'].get(key) or inherited
            if entry is None: return False
            entry['enabled'] = enabled; value['revision'] += 1
            value['assets'][key] = entry
            self._write(value)
        return True

    def alias(self, original, copied):
        entry = self.lookup(original)
        if entry is None or identity(original) == identity(copied): return
        entry['source'] = fingerprint(copied)
        with _LOCK:
            value = self.load()
            if identity(copied) in value['assets']: return
            value['assets'][identity(copied)] = entry
            value['revision'] += 1
            self._write(value)


def apply_saved(layer, catalogue=None):
    inline_ai = layer.get('key_method') == 'ai'
    if layer.get('kind') != 'image' or not (inline_ai or layer.get('use_saved_matte', True)): return layer
    source = layer.get('path', '')
    if not source: return layer
    existing = layer.get('_matting_binding')
    if existing and identity(source) in {identity(existing['source']['path']), identity(existing.get('result', existing['source'])['path'])}:
        verify_record(existing)
        record = existing
    elif '_matting_snapshot' in layer:
        record = layer['_matting_snapshot'].get(identity(source))
        if record and record.get('error'): raise ValueError(record['error'])
        if record:
            verify_record(record)
    else:
        record = (catalogue or Catalogue()).lookup(source)
    if record is None:
        if not inline_ai: return layer
        from .matting_settings import normalize
        source_record = fingerprint(source)
        record = dict(id='inline-ai-'+source_record['sha256'][:16], mode='dynamic', source=source_record, parameters=normalize())
    options = layer.get('_matting_post_options') or {key: value for key, value in layer.items() if key.startswith('key_')}
    from .matting_settings import normalize
    base_parameters = normalize(record.get('base_parameters', record.get('parameters', {})))
    refine_ai = (inline_ai or options.get('key_method') == 'enhanced' or options.get('key_cleanup')
                 or options.get('key_despill') or options.get('key_alpha_low', 0) != 0 or options.get('key_alpha_high', 100) != 100)
    if record.get('mode') == 'dynamic' or refine_ai:
        p = copy.deepcopy(base_parameters)
        if options.get('key_method') in {'ai', 'enhanced'}:
            p['fill_holes'] = bool(options.get('key_protect_inside', True))
        if options.get('key_cleanup'):
            p.update(cleanup=True, cleanup_size=int(options.get('key_cleanup_size', 2)))
        if options.get('key_despill'):
            color = options.get('key_mode')
            p.update(despill=color if color in {'green', 'blue'} else 'green',
                     screen_cleanup=options.get('key_screen_cleanup', True),
                     despill_amount=options.get('key_despill_amount', 100),
                     despill_width=int(options.get('key_despill_width', 4)), despill_luma=options.get('key_despill_luma', 50))
        low, high = p['black_clip'], p['white_clip']
        p.update(black_clip=low+(high-low)*options.get('key_alpha_low', 0)/100,
                 white_clip=low+(high-low)*options.get('key_alpha_high', 100)/100)
        record = dict(id=record['id'], mode='dynamic', source=record['source'],
                      parameters=normalize(p), base_parameters=base_parameters)
    # Keep the source/sequence identities intact; only the resolved rendering
    # path changes, after the successful-export rotation plan has been built.
    result = copy.deepcopy(layer)
    result.update(path=record['source']['path'] if record.get('mode') == 'dynamic' else record['result']['path'],
                  thumbnail='', key_mode='none', key_cleanup=False, key_despill=False,
                  _matting_binding=freeze(record), _matting_post_options=copy.deepcopy(options))
    return result


def freeze(record):
    keys = ('id', 'mode', 'source', 'parameters', 'base_parameters') if record.get('mode') == 'dynamic' else ('id', 'source', 'result', 'parameters')
    return copy.deepcopy({key: record[key] for key in keys if key in record})


def verify_record(record):
    verify_file(record['source'], '原素材')
    if record.get('mode') == 'dynamic':
        from .matting_settings import normalize
        normalize(record['parameters'])
    else: verify_file(record['result'], '已保存的抠像结果')


def record_files(record):
    return [record[k]['path'] for k in ('source', 'result') if k in record]


def remember_copy(original, copied):
    catalogue = Catalogue()
    if catalogue.path.is_file(): catalogue.alias(original, copied)


def snapshot_layers(layers, catalogue=None):
    """Freeze all candidates, including no-cutout decisions, for one submission."""
    from .components import walk_components
    result = copy.deepcopy(layers); catalogue = catalogue or Catalogue()
    for layer in walk_components(result):
        if layer.get('kind') != 'image' or not (layer.get('use_saved_matte', True) or layer.get('key_method') == 'ai'): continue
        if '_matting_snapshot' in layer: continue
        paths = [layer.get('source_file') or layer.get('path')]
        paths += [item.get('path') for item in layer.get('sequence_items', [])]
        snapshot = {}
        for path in dict.fromkeys(p for p in paths if p):
            try:
                record = catalogue.lookup(path)
                snapshot[identity(path)] = freeze(record) if record else None
            except (ValueError, OSError) as error:
                # An unused pool item/fallback must not prevent using a valid
                # connected source. Raise the frozen error only when selected.
                snapshot[identity(path)] = {'error': str(error)}
        layer['_matting_snapshot'] = snapshot
    return result

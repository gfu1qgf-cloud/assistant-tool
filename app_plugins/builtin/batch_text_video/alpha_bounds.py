"""One fixed union of native-alpha bounds per source, never per-frame zoom.

FFmpeg scans at native resolution. Only a tiny JSON survives; the DNx helper
streams its alpha directly, without rendering or caching another movie.
"""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time

_MEMORY, _LOCK = OrderedDict(), threading.RLock()
VERSION = 'alpha-union-2'
_CONTEXT = threading.local()


@contextmanager
def cancel_scope(cancel):
    previous = getattr(_CONTEXT, 'cancel', None)
    _CONTEXT.cancel = cancel
    try: yield
    finally: _CONTEXT.cancel = previous


def current_cancel():
    return getattr(_CONTEXT, "cancel", None)


def enabled(layer):
    return bool(layer.get('media_crop_alpha', False))


def _reduce(lines, width, height, padding):
    union = None
    frame = {}
    count = 0
    def include():
        nonlocal union, count
        if not frame: return
        count += 1
        if frame.get('w', 0) <= 0 or frame.get('h', 0) <= 0: return
        box = [frame['x1'], frame['y1'], frame['x2']+1, frame['y2']+1]
        if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            raise ValueError('透明边界检测返回无效范围。')
        union = box if union is None else [min(union[0], box[0]), min(union[1], box[1]),
                                          max(union[2], box[2]), max(union[3], box[3])]
    for raw in lines:
        line = raw.decode('utf8', 'replace').strip() if isinstance(raw, bytes) else raw.strip()
        if line.startswith('frame:'): include(); frame = {'seen': 1}
        elif line.startswith('lavfi.bbox.'):
            key, value = line[len('lavfi.bbox.'):].split('=', 1); frame[key] = int(value)
    include()
    if not count: raise ValueError('视频没有可解码的透明画面。')
    # An entirely transparent clip stays transparent, with its original geometry.
    if union is None: union = [0, 0, width, height]
    x, y = max(0, union[0]-padding), max(0, union[1]-padding)
    right, bottom = min(width, union[2]+padding), min(height, union[3]+padding)
    return dict(width=width, height=height, x=x, y=y, w=right-x, h=bottom-y, frames=count)


def bounds(path, *, ffmpeg='', threshold=1, padding=2, cancel=None):
    cancel = cancel if cancel is not None else getattr(_CONTEXT, "cancel", None)
    from .engine import resolve_tools, probe
    from .dnx_alpha import signature, describe, check, file_lock
    from .preview_cache import cache_root
    from model.AppDataStorage import data_directory, data_file, data_io
    from .video_geometry import decoded_size
    ffmpeg, ffprobe = resolve_tools(ffmpeg)
    stamp = signature(path)
    key = hashlib.sha256(json.dumps([VERSION, stamp, threshold, padding, ffmpeg], ensure_ascii=False).encode()).hexdigest()
    check(cancel)
    with _LOCK:
        if key in _MEMORY: _MEMORY.move_to_end(key); return dict(_MEMORY[key])
    root = cache_root('alpha-bounds'); data_directory(root).ensure()
    target = root/(key+'.json')
    with file_lock(root/(key+'.lock'), cancel):
        check(cancel)
        try:
            result = json.loads(data_file(target).read_text(encoding='utf8'))
            if result['key'] != key: raise ValueError('旧透明边界缓存。')
            _validate(result)
        except (OSError, ValueError, KeyError, TypeError):
            info = probe(path, ffprobe); width, height = decoded_size(info['video'])
            native = describe(path, ffmpeg, cancel)
            result = _scan(path, ffmpeg, width, height, threshold, padding, native, cancel)
            if signature(path) != stamp: raise ValueError('检测透明边界期间素材发生变化，请重新预览。')
            result['key'] = key
            # Atomic publication: canceled or partial scans are never cached.
            with data_io.TemporaryDirectory(prefix='bounds-', dir=root) as folder:
                pending = Path(folder)/'result.json'
                data_file(pending).write_text(json.dumps(result), encoding='utf8')
                data_io.replace(pending, target)
        if signature(path) != stamp: raise ValueError('透明素材已改变，请重新预览。')
    with _LOCK:
        _MEMORY[key] = dict(result)
        while len(_MEMORY) > 128: _MEMORY.popitem(last=False)
    return result


def _validate(value):
    w, h, x, y, cw, ch = (value[k] for k in ('width', 'height', 'x', 'y', 'w', 'h'))
    if any(type(v) is not int for v in (w,h,x,y,cw,ch)) or not (0 <= x < x+cw <= w and 0 <= y < y+ch <= h):
        raise ValueError('透明边界缓存无效。')


def _scan(path, ffmpeg, width, height, threshold, padding, native, cancel):
    from .dnx_alpha import packets, decoder, check, FLAGS
    decode = process = None
    with tempfile.TemporaryDirectory(prefix='alpha-bounds-') as folder, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            args = [ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1']
            if native:
                rows = packets(native, cancel)
                index = Path(folder)/'frames.index'
                index.write_bytes(b'DNA1'+struct.pack('<I', len(rows))+b''.join(struct.pack('<QI', *row) for row in rows))
                decode = subprocess.Popen([decoder(), '--stream', str(path), str(index)], stdout=subprocess.PIPE, stderr=errors, creationflags=FLAGS)
                args += ['-f', 'rawvideo', '-pixel_format', 'gray', '-video_size', f'{width}x{height}', '-framerate', native['rate'], '-i', 'pipe:0']
                vf = ''
            else:
                args += ['-i', str(path)]; vf = 'format=rgba,alphaextract,'
            args += ['-map', '0:v:0', '-vf', vf+f'bbox=min_val={threshold-1},metadata=mode=add:key=alpha_scan:value=1,metadata=mode=print:file=-', '-an', '-sn', '-f', 'null', '-']
            process = subprocess.Popen(args, stdin=decode.stdout if decode else subprocess.DEVNULL,
                                       stdout=output, stderr=errors, creationflags=FLAGS)
            if decode: decode.stdout.close()
            started = time.monotonic()
            while process.poll() is None or (decode and decode.poll() is None):
                check(cancel)
                if time.monotonic()-started > 1800: raise TimeoutError('透明边界检测超时，请缩短素材后重试。')
                if output.tell() > 256*1024**2 or errors.tell() > 1024**2: raise ValueError('透明边界检测输出异常。')
                if process.poll() is not None and process.returncode: break
                time.sleep(.05)
            check(cancel)
            if process.poll() != 0 or (decode and decode.poll() != 0):
                errors.seek(max(0, errors.seek(0, 2)-2000))
                raise ValueError('检测透明边界失败：'+errors.read().decode('utf8', 'replace'))
            output.seek(0)
            return _reduce(output, width, height, padding)
        finally:
            for child in (process, decode):
                if child and child.poll() is None: child.kill(); child.wait(timeout=10)


def for_layer(layer, ffmpeg='', cancel=None):
    if not enabled(layer): return None
    from .visual_media import is_animated
    path = layer.get('path', '')
    if not is_animated(path): return None
    return bounds(path, ffmpeg=ffmpeg, threshold=layer.get('media_crop_alpha_threshold', 1),
                  padding=layer.get('media_crop_alpha_padding', 2), cancel=cancel)


def filter_for(box):
    if box is None: return ''
    # Loop/AI streams may already be downscaled: enclose the original union.
    x = f"floor(iw*{box['x']}/{box['width']})"
    y = f"floor(ih*{box['y']}/{box['height']})"
    return (f"crop=w='max(1,ceil(iw*{box['x']+box['w']}/{box['width']})-{x})':"
            f"h='max(1,ceil(ih*{box['y']+box['h']}/{box['height']})-{y})':x='{x}':y='{y}':exact=1")


def crop_image(image, box):
    if box is None or image.isNull(): return image
    x = math.floor(image.width()*box['x']/box['width']); y = math.floor(image.height()*box['y']/box['height'])
    r = math.ceil(image.width()*(box['x']+box['w'])/box['width']); b = math.ceil(image.height()*(box['y']+box['h'])/box['height'])
    return image.copy(x, y, max(1, r-x), max(1, b-y))

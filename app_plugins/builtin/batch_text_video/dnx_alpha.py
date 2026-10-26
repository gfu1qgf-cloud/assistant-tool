"""Restore DNxHR's encoded alpha; FFmpeg still decodes the original colour.

Only MOV DNxHR alpha is intercepted. No inference, keying, or RGB transcode.
Deferred masks keep graph preparation cheap; an encoder acquires its own lease.
"""
from collections import OrderedDict
from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import threading
import time
import uuid
import re

VERSION = 'dnx-alpha-1'
DISK_LIMIT = 512*1024**2
ENTRY_LIMIT = 128*1024**2
_INFO, _LOCK = OrderedDict(), threading.RLock()
_BUILDING = threading.BoundedSemaphore(2)
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def check(cancel):
    if cancel is not None and cancel.is_set():
        from .engine import Cancelled
        raise Cancelled('已取消读取素材透明通道。')


def signature(path):
    path = Path(path).resolve(); stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def command(args, cancel=None, timeout=30):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=FLAGS)
    started = time.monotonic()
    try:
        while True:
            check(cancel)
            if time.monotonic()-started > timeout: raise TimeoutError('读取素材透明通道超时。')
            try: output, errors = process.communicate(timeout=.1); break
            except subprocess.TimeoutExpired: pass
        if process.returncode: raise ValueError('读取素材透明通道失败：'+errors[-2000:].decode('utf8', 'replace'))
        return output
    finally:
        if process.poll() is None: process.kill(); process.communicate()


def decoder():
    path = Path(__file__).parent/'native'/'dnx-alpha.exe'
    if not path.is_file(): raise FileNotFoundError('缺少DNxHR透明通道解码器，请修复软件安装。')
    return str(path)


def describe(path, ffmpeg='', cancel=None):
    # Other formats incur no new subprocess or source reads.
    if Path(path).suffix.casefold() != '.mov': return None
    check(cancel)
    from .engine import resolve_tools
    ffmpeg, ffprobe = resolve_tools(ffmpeg)
    stamp = signature(path); key = (stamp, ffprobe)
    with _LOCK:
        if key in _INFO:
            _INFO.move_to_end(key); return _INFO[key]
    info = json.loads(command([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+#1',
        '-show_streams', '-show_packets', '-show_entries', 'packet=pos,size:stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,field_order:stream_tags=rotate:stream_side_data',
        '-of', 'json', str(path)], cancel))
    stream = next(iter(info.get('streams', [])), {})
    result = None
    if stream.get('codec_name') == 'dnxhd':
        packets = info.get('packets', [])
        if not packets: raise ValueError('DNxHR素材没有可读的视频帧。')
        pos, size = int(packets[0]['pos']), int(packets[0]['size'])
        if pos < 0 or not 640 <= size <= 64*1024**2 or pos+size > stamp[1]: raise ValueError('DNxHR帧位置无效。')
        with open(path, 'rb') as source: source.seek(pos); header = source.read(640)
        if header[7] & 1:
            cid = int.from_bytes(header[40:44], 'big')
            # Restrict to progressive RI profiles. Never silently use opaque
            # FFmpeg output when a recognised but unsupported alpha is present.
            if cid not in range(1270, 1275) or header[5] & 3 in (2, 3) or header[34] & 4:
                raise ValueError('这段DNxHD／交错Alpha暂不支持，请使用逐行DNxHR素材。')
            if header[7] & 4:
                raise ValueError('这段DNxHR使用预乘Alpha，当前兼容器仅支持直通Alpha。')
            width, height = int.from_bytes(header[26:28], 'big'), int.from_bytes(header[24:26], 'big')
            from .video_geometry import decoded_size
            if (width, height) != decoded_size(stream):
                raise ValueError('DNxHR带裁切／旋转的Alpha暂不支持，未忽略透明通道。')
            if not (0 < width <= 8192 and 0 < height <= 8192 and width*height <= 40_000_000):
                raise ValueError('DNxHR透明素材画幅过大。')
            rate = Fraction(stream.get('avg_frame_rate') or '0')
            if not 0 < rate <= 120 or rate != Fraction(stream.get('r_frame_rate') or '0'):
                raise ValueError('DNxHR透明素材需使用固定帧率。')
            result = dict(stamp=stamp, width=width, height=height, rate=str(rate), first=(pos, size), ffmpeg=ffmpeg,
                          ffprobe=ffprobe, cid=cid, header_sha256=hashlib.sha256(header).hexdigest())
    if signature(path) != stamp: raise ValueError('读取透明通道期间素材发生变化。')
    with _LOCK:
        _INFO[key] = result
        while len(_INFO) > 128: _INFO.popitem(last=False)
    return result


def cache_root():
    from .preview_cache import cache_root as preview_root
    return preview_root('dnx-alpha')


def cache_key(info):
    return hashlib.sha256(json.dumps([VERSION, info['stamp'], info['header_sha256']], ensure_ascii=False).encode()).hexdigest()


@contextmanager
def file_lock(path, cancel=None, *, blocking=True):
    """Cross-process build/prune exclusion; OS releases it after a crash."""
    import msvcrt
    with open(path, 'a+b') as handle:
        if handle.tell() == 0: handle.write(b'0'); handle.flush()
        held = False
        try:
            while not held:
                check(cancel); handle.seek(0)
                try: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); held = True
                except OSError:
                    if not blocking: break
                    time.sleep(.1)
            yield held
        finally:
            if held: handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def prune(root, keep=None):
    # Only this private namespace's SHA256 files. Active read handles prevent
    # deletion on Windows, including leases owned by another app process.
    # Reap only abandoned helper scratch; never touch a live builder's files.
    for pending in root.glob('*.part.mkv'):
        if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{32}\.part\.mkv', pending.name): continue
        try:
            if time.time()-pending.stat().st_mtime < 3600: continue
            stem = pending.name[:-len('.part.mkv')]
            with file_lock(root/(stem.split('.')[0]+'.lock'), blocking=False) as held:
                if held:
                    for suffix in ('.part.mkv', '.index', '.log'): (root/(stem+suffix)).unlink(missing_ok=True)
        except OSError: pass
    files = []
    for path in root.glob('*.mkv'):
        if len(path.stem) != 64 or any(c not in '0123456789abcdef' for c in path.stem) or path.is_symlink(): continue
        try: stat = path.stat(); files.append((stat.st_mtime_ns, path, stat.st_size))
        except OSError: pass
    total = sum(item[2] for item in files)
    for _, path, size in sorted(files):
        if total <= DISK_LIMIT: break
        if path == keep: continue
        try:
            with file_lock(path.with_suffix('.lock'), blocking=False) as held:
                if held:
                    path.unlink(); path.with_suffix('.json').unlink(missing_ok=True); total -= size
        except OSError: pass


def _ready(path, info):
    try:
        data = json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
        return (data['key'] == cache_key(info) and data['bytes'] == path.stat().st_size and
                data['frames'] > 0 and data['bytes'] <= ENTRY_LIMIT and not path.is_symlink())
    except (OSError, ValueError, KeyError, TypeError): return False


def packets(info, cancel=None):
    data = json.loads(command([info['ffprobe'], '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+#100001', '-show_packets',
        '-show_entries', 'packet=pos,size,pts_time', '-of', 'json', info['stamp'][0]], cancel, 120))['packets']
    if not 0 < len(data) <= 100_000: raise ValueError('DNxHR素材帧数过多，请分段使用。')
    rate = float(Fraction(info['rate'])); first_time = float(data[0]['pts_time'])
    result = []
    for index, packet in enumerate(data):
        pos, size = int(packet['pos']), int(packet['size'])
        if pos < 0 or not 640 <= size <= 64*1024**2 or pos+size > info['stamp'][1]:
            raise ValueError('DNxHR压缩帧超出文件范围。')
        timestamp = float(packet['pts_time'])
        if not math.isfinite(timestamp) or not math.isfinite(first_time) or abs(timestamp-first_time-index/rate) > .1/rate:
            raise ValueError('DNxHR Alpha暂不支持变帧率或不连续时间戳。')
        result.append((pos, size))
    return result


def _build(path, info, cancel, report):
    from model.MediaStorage import require_media_space
    root = path.parent; require_media_space(root, ENTRY_LIMIT)
    token = path.stem+'.'+uuid.uuid4().hex
    pending, index_path, log_path = (root/(token+suffix) for suffix in ('.part.mkv', '.index', '.log'))
    decode = encode = None
    try:
        rows = packets(info, cancel)
        index_path.write_bytes(b'DNA1'+struct.pack('<I', len(rows))+b''.join(struct.pack('<QI', *row) for row in rows))
        report('准备素材原生透明通道（只缓存遮罩）：'+Path(info['stamp'][0]).name)
        with open(log_path, 'w+b') as log:
            decode = subprocess.Popen([decoder(), '--stream', info['stamp'][0], str(index_path)],
                stdout=subprocess.PIPE, stderr=log, creationflags=FLAGS)
            encode = subprocess.Popen([info['ffmpeg'], '-nostdin', '-y', '-v', 'error', '-f', 'rawvideo',
                '-pixel_format', 'gray', '-video_size', f"{info['width']}x{info['height']}", '-framerate', info['rate'],
                '-i', 'pipe:0', '-an', '-c:v', 'ffv1', '-level', '3', '-threads', '1', str(pending)],
                stdin=decode.stdout, stdout=subprocess.DEVNULL, stderr=log, creationflags=FLAGS)
            decode.stdout.close(); started = time.monotonic()
            while decode.poll() is None or encode.poll() is None:
                check(cancel)
                if time.monotonic()-started > 1800: raise TimeoutError('准备原生Alpha超时。')
                if pending.exists() and pending.stat().st_size > ENTRY_LIMIT: raise ValueError('透明遮罩超过128 MB，请缩短素材。')
                if log.tell() > 1024**2: raise ValueError('透明通道解码异常。')
                if encode.poll() is not None and decode.poll() is None and encode.returncode: break
                time.sleep(.05)
            if decode.poll() != 0 or encode.poll() != 0:
                log.seek(max(0, log.seek(0, 2)-2000)); raise ValueError('DNxHR透明通道读取失败：'+log.read().decode('utf8','replace'))
        # A successful process can still produce a short movie. Count frames
        # before publication, never stretch a partial mask over the full RGB.
        stream = json.loads(command([info['ffprobe'], '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', str(pending)], cancel, 120))['streams'][0]
        if int(stream['nb_read_frames']) != len(rows) or (int(stream['width']), int(stream['height'])) != (info['width'], info['height']):
            raise ValueError('DNxHR透明遮罩帧数或画幅不完整。')
        if signature(info['stamp'][0]) != tuple(info['stamp']): raise ValueError('准备透明遮罩期间原素材发生变化。')
        size = pending.stat().st_size
        if size > ENTRY_LIMIT: raise ValueError('透明遮罩超过128 MB，请缩短素材。')
        meta = dict(key=cache_key(info), bytes=size, frames=len(rows), version=VERSION)
        os.replace(pending, path)
        path.with_suffix('.json').write_text(json.dumps(meta), encoding='utf8')
    finally:
        for process in (decode, encode):
            if process and process.poll() is None: process.kill(); process.wait(timeout=10)
        for temporary in (pending, index_path, log_path): temporary.unlink(missing_ok=True)


@contextmanager
def mask(info, cancel=None, report=None):
    check(cancel)
    root = cache_root(); root.mkdir(parents=True, exist_ok=True)
    prune(root)
    path = root/(cache_key(info)+'.mkv')
    handle = None
    try:
        with file_lock(path.with_suffix('.lock'), cancel):
            if signature(info['stamp'][0]) != tuple(info['stamp']): raise ValueError('原素材已改变，请重新读取。')
            if not _ready(path, info):
                acquired = False
                try:
                    while not acquired: check(cancel); acquired = _BUILDING.acquire(timeout=.1)
                    _build(path, info, cancel, report or (lambda text: None))
                finally:
                    if acquired: _BUILDING.release()
            # No prune gap between publish and lease acquisition.
            handle = open(path, 'rb'); os.utime(path, None)
        prune(root, path)
        yield str(path)
        if signature(info['stamp'][0]) != tuple(info['stamp']): raise ValueError('使用透明遮罩期间原素材发生变化。')
    finally:
        if handle: handle.close()
        prune(root)


class MaskInput(str):
    def __new__(cls, info):
        value = super().__new__(cls, info['stamp'][0]); value.info = dict(info); return value
    def __deepcopy__(self, memo): return self


def add_source(args, filters, path, index, label, *, loop=False, ffmpeg='', cancel=None):
    """Caller owns RGB input index. Return RGBA pad and total consumed inputs."""
    info = describe(path, ffmpeg, cancel)
    if info is None: return f'{index}:v', 1
    if loop: args += ['-stream_loop', '-1']
    args += ['-i', MaskInput(info)]
    clock = f'settb=AVTB,setpts=N/({float(Fraction(info["rate"])):.9f}*TB)'
    filters += [f'[{index}:v]{clock},format=rgba[{label}_rgb]',
                f'[{index+1}:v]{clock},format=gray[{label}_alpha]',
                f'[{label}_rgb][{label}_alpha]alphamerge=shortest=1,format=rgba[{label}_rgba]']
    return label+'_rgba', 2


def raw_args(path, ffmpeg, *, vf='', first=0, cancel=None):
    """Native-size RGBA for workbench / AI; caller owns an Inputs lease."""
    if describe(path, ffmpeg, cancel) is None: return None
    args = [ffmpeg, '-nostdin', '-v', 'error', '-filter_complex_threads', '1', '-threads', '1', '-i', str(path)]
    filters = []
    label, _ = add_source(args, filters, path, 0, 'native', ffmpeg=ffmpeg, cancel=cancel)
    effects = f'trim=start_frame={first},setpts=PTS-STARTPTS' + (','+vf if vf else '')
    filters.append(f'[{label}]{effects}[decoded]')
    return args+['-filter_complex', ';'.join(filters), '-map', '[decoded]', '-an', '-sn',
                 '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1']


def poster(path, bounds=None, *, ffmpeg='', cancel=None, position=0.):
    """Seek/decode one native alpha frame without building a full-clip cache."""
    info = describe(path, ffmpeg, cancel)
    if info is None: return None
    from .engine import resolve_tools
    from qt_compat import QtGui
    ffmpeg, _ = resolve_tools(ffmpeg)
    index = max(0, round(position*float(Fraction(info['rate']))))
    rows = [info['first']] if not index else packets(info, cancel)
    if index >= len(rows): raise ValueError('所选透明视频帧超出时长。')
    pos, size = rows[index if index else 0]
    import tempfile
    # Temporary files contain one compressed frame / small alpha plane only.
    root = cache_root(); root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='poster-', dir=root) as directory:
        packet, pgm = Path(directory)/'frame.dnx', Path(directory)/'alpha.pgm'
        with open(path, 'rb') as source: source.seek(pos); packet.write_bytes(source.read(size))
        command([decoder(), '--frame', str(packet), str(pgm)], cancel)
        scale = f',scale={bounds.width()}:{bounds.height()}:force_original_aspect_ratio=decrease' if bounds else ''
        output = command([ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-i', str(path), '-i', str(pgm),
            '-filter_complex_threads', '1', '-filter_complex',
            f'[0:v]select=eq(n\\,{index}),setpts=PTS-STARTPTS,format=rgba[rgb];[rgb][1:v]alphamerge=shortest=1{scale}[out]',
            '-map', '[out]', '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'], cancel)
        image = QtGui.QImage.fromData(output)
        if image.isNull(): raise ValueError('无法读取DNxHR透明首帧。')
    if signature(path) != tuple(info['stamp']): raise ValueError('读取透明首帧期间素材发生变化。')
    return image

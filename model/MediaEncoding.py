"""Explicit H.264 encoding policy; hardware probes run only in render workers."""
import os
from pathlib import Path
import subprocess
import threading
import time

ENCODERS = (('cpu', 'CPU · H.264（兼容）'), ('nvenc', 'NVIDIA 显卡 · H.264 NVENC'),
            ('qsv', 'Intel 核显 · H.264 QSV'), ('auto', '自动 · NVIDIA → Intel → CPU'))
EXPORT_QUALITIES = (('high', '高 · 画质优先'), ('medium', '中 · 画质与体积均衡'),
                    ('low', '低 · 体积优先'))
_QUALITY_VALUES = {'cpu': {'high': 20, 'medium': 23, 'low': 27},
                   'nvenc': {'high': 19, 'medium': 23, 'low': 27},
                   'qsv': {'high': 19, 'medium': 23, 'low': 27}}
_PROBES = {}
_LOCK = threading.Lock()


def normalize_export_settings(settings):
    settings.setdefault('video_encoder', 'cpu')
    settings.setdefault('encoder_fallback', True)
    settings.setdefault('export_workers', 1)
    settings.setdefault('export_quality', 'high')
    if settings['video_encoder'] not in {key for key, _ in ENCODERS}:
        raise ValueError('视频编码方式无效。')
    if type(settings['encoder_fallback']) is not bool:
        raise ValueError('编码失败回退选项必须为开关。')
    if type(settings['export_workers']) is not int or not 1 <= settings['export_workers'] <= 8:
        raise ValueError('同时导出数量须为1–8。')
    quality_label(settings['export_quality'])


def quality_label(quality):
    if not isinstance(quality, str) or quality not in dict(EXPORT_QUALITIES):
        raise ValueError('导出质量须为高、中或低。')
    return dict(EXPORT_QUALITIES)[quality]


def video_encoding_args(encoder, *, preview=False, cache=False, workers=1, quality='high'):
    quality_label(quality)
    if encoder not in _QUALITY_VALUES:
        raise ValueError('未解析的视频编码方式。')
    # Keep the original final-quality defaults. Preview/cache quality is a
    # separate policy; an export preset never reduces an intermediate's quality.
    value = '16' if cache else '25' if preview else str(_QUALITY_VALUES[encoder][quality])
    if encoder == 'cpu':
        args = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', value]
        if workers > 1:
            args += ['-threads', str(max(1, min(8, (os.cpu_count() or 4)//workers)))]
        return args
    if encoder == 'qsv':
        return ['-c:v', 'h264_qsv', '-preset', 'medium', '-global_quality',
                value]
    # CQ and CRF are different controls; do not claim numerically identical quality.
    return ['-c:v', 'h264_nvenc', '-preset', 'p5', '-tune', 'hq', '-rc', 'vbr',
            '-cq', value, '-b:v', '0',
            '-spatial-aq', '1', '-aq-strength', '8']


def encoder_label(encoder):
    return {'cpu':'CPU x264', 'nvenc':'NVIDIA NVENC', 'qsv':'Intel QSV'}[encoder]


def video_pixel_format(encoder):
    return 'nv12' if encoder == 'qsv' else 'yuv420p'


def filter_thread_args(workers):
    if workers == 1:
        return []  # Preserve the existing single-export threading policy.
    threads = max(1, min(4, (os.cpu_count() or 4)//workers))
    return ['-filter_threads', str(threads), '-filter_complex_threads', str(threads)]


def hardware_error(error):
    text = str(error).lower()
    return any(part in text for part in ('nvenc', 'nvcuda', 'nvencode', 'no capable devices',
        'openencodesessionex', 'no encode device', 'cannot load cuda', 'cuda_error',
        'qsv', 'mfx', 'libvpl', 'onevpl'))


def _probe_hardware(ffmpeg, encoder, cancel=None):
    path = Path(ffmpeg).resolve()
    stat = path.stat()
    key = str(path), stat.st_size, stat.st_mtime_ns, encoder
    with _LOCK:
        cached = _PROBES.get(key)
        if cached and time.monotonic()-cached[0] < (300 if cached[1] else 30):
            return cached[1:]
    args = [str(path), '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'color=c=blue:s=320x240:r=30:d=0.1', '-frames:v', '3', '-an',
            *video_encoding_args(encoder), '-pix_fmt', video_pixel_format(encoder), '-f', 'null', '-']
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    started = time.monotonic()
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError('已取消硬件编码检测。')
            try:
                stdout, stderr = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic()-started > 15:
                    raise TimeoutError('显卡编码检测超过15秒。')
    except BaseException:
        process.kill()
        process.communicate(timeout=10)
        raise
    detail = (stderr+stdout).decode('utf-8', 'replace')[-3000:]
    value = process.returncode == 0, detail
    with _LOCK:
        _PROBES[key] = (time.monotonic(), *value)
    return value


def probe_nvenc(ffmpeg, cancel=None):
    return _probe_hardware(ffmpeg, 'nvenc', cancel)


def probe_qsv(ffmpeg, cancel=None):
    return _probe_hardware(ffmpeg, 'qsv', cancel)


def resolve_video_encoder(ffmpeg, settings, cancel=None, report=None):
    normalize_export_settings(settings)
    requested = settings['video_encoder']
    if requested == 'cpu':
        return 'cpu'
    failures = []
    for encoder in ('nvenc', 'qsv') if requested == 'auto' else (requested,):
        try:
            available, detail = (probe_nvenc if encoder == 'nvenc' else probe_qsv)(ffmpeg, cancel)
        except (OSError, TimeoutError) as error:
            available, detail = False, str(error)
        if available:
            if failures and report:
                report('\n'.join(failures)+'\n本次改用'+encoder_label(encoder)+'，不回退旧版FFmpeg。')
            return encoder
        reason = encoder_label(encoder)+'硬件编码不可用；可能是驱动版本、编码会话或FFmpeg不兼容。'
        if encoder == 'nvenc' and 'required nvenc api version' in detail.lower():
            reason = '当前FFmpeg要求的NVENC API高于显卡驱动支持版本；若要使用NVIDIA需更新驱动，程序不会自动更新驱动。'
        failures.append(reason+'\n'+detail)
    if requested != 'auto' and not settings['encoder_fallback']:
        raise RuntimeError('\n'.join(failures))
    if report:
        report('\n'.join(failures)+'\n本次明确回退CPU。')
    return 'cpu'

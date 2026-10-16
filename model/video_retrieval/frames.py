"""Bounded FFmpeg decoding. No originals or complete videos are copied."""
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from PIL import Image, ImageStat
from app_paths import APP_ROOT

VIDEO_SUFFIXES = frozenset({'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v',
                           '.mts', '.m2ts', '.mpeg', '.mpg', '.wmv', '.flv'})


class Canceled(Exception):
    pass


def resolve_tools(configured=''):
    value = str(configured or '').strip()
    if value and Path(value).suffix.lower() == '.sha':
        raise ValueError('请使用标准 FFmpeg，不支持 .sha 程序。')
    roots = (APP_ROOT, Path(getattr(sys, '_MEIPASS', APP_ROOT / '_internal')))
    ffmpeg = (value if value and Path(value).is_file() else shutil.which(value)) if value else next(
        (str(root / 'runtime/ffmpeg/ffmpeg.exe') for root in roots
         if (root / 'runtime/ffmpeg/ffmpeg.exe').is_file()), shutil.which('ffmpeg'))
    if not ffmpeg:
        raise FileNotFoundError('找不到 FFmpeg，请在智能搜索 → 视频设置中指定 ffmpeg.exe。')
    sibling = Path(ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    ffprobe = str(sibling) if sibling.is_file() else shutil.which('ffprobe')
    if not ffprobe:
        raise FileNotFoundError('找不到 FFprobe，请将它放在 FFmpeg 同目录。')
    return str(ffmpeg), str(ffprobe)


def run_process(args, canceled=lambda: False, timeout=120):
    if canceled():
        raise Canceled('已取消视频索引。')
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    started = time.monotonic()
    try:
        while True:
            if canceled():
                raise Canceled('已取消视频索引。')
            if time.monotonic() - started > timeout:
                raise TimeoutError('视频读取超时；原视频及旧索引未修改。')
            try:
                out, err = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError('FFmpeg 读取失败：' + err.decode('utf-8', 'replace')[-1200:])
        if canceled():
            raise Canceled('已取消视频索引。')
        return out, err
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def video_duration(path, ffprobe, canceled=lambda: False):
    out, _ = run_process([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries',
                          'format=duration:stream=codec_type,duration', '-of', 'json',
                          str(path)], canceled, 30)
    data = json.loads(out)
    streams = [row for row in data.get('streams', []) if row.get('codec_type') == 'video']
    if not streams:
        raise ValueError('文件没有可读取的视频轨道。')
    raw = data.get('format', {}).get('duration')
    if raw in (None, 'N/A'):
        raw = streams[0].get('duration')
    duration = float(raw)
    if not math.isfinite(duration) or not 0 < duration <= 48 * 3600:
        raise ValueError('视频时长无效或超过 48 小时。')
    return duration


def sample_times(duration, interval=3, max_frames=240, cuts=()):
    """Reserve whole-video coverage, then fill with stable post-cut frames."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('视频时长必须大于零。')
    limit = max(4, int(max_frames))
    last = max(0, duration - min(.1, duration / 10))
    desired = max(2, math.ceil(last / max(.5, float(interval))) + 1)
    # Without cuts use the full budget; with cuts reserve half for scene detail.
    count = min(desired, max(2, limit // 2) if cuts else limit)
    regular = [round(last * i / (count - 1), 3) for i in range(count)]
    extras = sorted({round(min(last, max(0, float(t) + .25)), 3) for t in cuts
                     if math.isfinite(float(t)) and 0 <= float(t) < duration})
    extras = [t for t in extras if all(abs(t - value) > .15 for value in regular)]
    room = limit - len(regular)
    if len(extras) > room:
        extras = [extras[round(i * (len(extras) - 1) / max(1, room - 1))]
                  for i in range(room)]
    return sorted(set(regular + extras))


def scene_times(path, ffmpeg, threshold=.18, canceled=lambda: False, max_cuts=240):
    # Decode low-resolution 2fps for shot changes (not a full-resolution export).
    # Output is only bounded showinfo metadata, never a second video.
    filters = f"scale=160:-2,fps=2,select='gt(scene,{float(threshold):.3f})',showinfo"
    _, err = run_process([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'info',
                          '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0', '-an', '-vf', filters,
                          '-frames:v', str(max_cuts), '-f', 'null', '-'], canceled, 600)
    return [float(t) for t in re.findall(rb'pts_time:([0-9]+(?:\.[0-9]+)?)', err)]


def extract_frame(path, seconds, ffmpeg, canceled=lambda: False):
    out, _ = run_process([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                          '-ss', f'{seconds:.3f}', '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0',
                          '-frames:v', '1', '-vf', 'scale=640:640:force_original_aspect_ratio=decrease',
                          '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'], canceled, 90)
    with Image.open(io.BytesIO(out)) as original:
        image = original.convert('RGB')
    # Ignore actual black leaders, but not ordinary dim/night scenes.
    if sum(ImageStat.Stat(image.resize((32, 32))).mean) / 3 < 2:
        image.close()
        return None
    return image

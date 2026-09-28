"""Bounded audio reads: never decode an entire long track just for indexing."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"})
SAMPLE_RATE = 48000
SEGMENT_SECONDS = 10.0


def resolve_tools(configured=""):
    configured = str(configured or "").strip()
    ffmpeg = configured if configured and Path(configured).is_file() else shutil.which(configured or "ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到 FFmpeg；请在插件设置中指定 ffmpeg.exe")
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("找不到 FFprobe；请把 ffprobe 放在 FFmpeg 同目录")
    return str(ffmpeg), str(ffprobe)


def media_duration(path, ffprobe):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not 0 < duration < 48 * 3600:
        raise ValueError("媒体时长无效")
    return duration


def segment_starts(duration):
    """Cover beginning, ending and the interior, without unbounded inference."""
    last = max(0.0, duration - SEGMENT_SECONDS)
    if last <= 0:
        return [0.0]
    count = min(8, max(3, int(duration // 45) + 1))
    return [round(last * index / (count - 1), 3) for index in range(count)]


def decode_segment(path, start, ffmpeg):
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(start),
         "-i", str(path), "-t", str(SEGMENT_SECONDS), "-vn", "-ac", "1", "-ar",
         str(SAMPLE_RATE), "-f", "f32le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=True,
    )
    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if samples.size < SAMPLE_RATE:
        raise ValueError("音频片段不足 1 秒")
    return samples


def encode_reference(path, start, encoder, ffmpeg, ffprobe):
    """Encode one chosen ten-second excerpt; filenames never affect ranking."""
    duration = media_duration(path, ffprobe)
    if start < 0 or start >= duration:
        raise ValueError(f"参考起点超出文件长度（{duration:.1f} 秒）")
    try:
        samples = decode_segment(path, start, ffmpeg)
    except subprocess.CalledProcessError as exc:
        raise ValueError("参考文件没有可解码的音轨；请选有声音的音频或视频") from exc
    if float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) < 1e-4:
        raise ValueError("选中的参考片段几乎无声，请换一个参考起点")
    return encoder.audio(samples)

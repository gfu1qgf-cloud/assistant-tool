"""Native-size horizontal composition using the bundled FFmpeg, never editing sources."""
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from app_paths import APP_ROOT


class Canceled(Exception):
    pass


def resolve_tools():
    roots = (APP_ROOT, Path(getattr(sys, "_MEIPASS", APP_ROOT / "_internal")))
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffmpeg = next((str(root / "runtime" / "ffmpeg" / executable) for root in roots
                   if (root / "runtime" / "ffmpeg" / executable).is_file()), None)
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到标准 FFmpeg，请检查程序 runtime/ffmpeg 目录。")
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("找不到 FFprobe，请将它放在 FFmpeg 同目录。")
    return str(ffmpeg), str(ffprobe)


def run_process(args, cancel=None, progress=None, duration=0, timeout=40):
    """Bounded diagnostics, responsive cancellation, and no shell/GUI calls."""
    if cancel is not None and cancel.is_set():
        raise Canceled("操作已取消，源视频未改动。")
    with tempfile.TemporaryDirectory(prefix="video-stitch-log-") as folder:
        output, errors = Path(folder) / "stdout", Path(folder) / "stderr"
        with output.open("wb") as out, errors.open("wb") as err, output.open("rb") as reader:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started, pending = time.monotonic(), ""
            try:
                while process.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise Canceled("操作已取消，源视频未改动。")
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("视频处理超时，请查看程序日志。")
                    if max(output.stat().st_size, errors.stat().st_size) > 4 * 1024 * 1024:
                        raise RuntimeError("媒体处理日志异常过大，已停止。")
                    if progress and duration:
                        pending += reader.read(65536).decode("utf-8", "replace")
                        lines = pending.split("\n")
                        pending = lines.pop()
                        for line in lines:
                            match = re.fullmatch(r"out_time_us=(\d+)", line.strip())
                            if match:
                                progress(min(99, int(int(match.group(1)) / 1e6 / duration * 100)))
                    time.sleep(0.05)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                raise
        if cancel is not None and cancel.is_set():
            raise Canceled("操作已取消，源视频未改动。")
        if max(output.stat().st_size, errors.stat().st_size) > 4 * 1024 * 1024:
            raise RuntimeError("媒体处理输出异常过大。")
        if process.returncode:
            raise RuntimeError("FFmpeg/FFprobe 处理失败：" + errors.read_text(encoding="utf-8", errors="replace")[-2400:])
        return output.read_bytes()


def positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Media:
    path: str
    width: int
    height: int
    duration: float
    fps: Fraction
    video_index: int
    audio_index: int = -1
    audio_duration: float = 0


def media_from_data(path, data):
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("没有可播放的视频轨道：" + Path(path).name)
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if width < 1 or height < 1:
        raise ValueError("无法识别视频尺寸：" + Path(path).name)
    try:
        sar = Fraction(str(video.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
        if sar <= 0:
            sar = Fraction(1)
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    width = max(1, round(width * sar))
    rotation = video.get("tags", {}).get("rotate", 0)
    rotation = next((s["rotation"] for s in video.get("side_data_list", []) if "rotation" in s), rotation)
    try:
        angle = float(rotation) % 360
    except (ValueError, TypeError):
        angle = 0
    if min(abs(angle - 90), abs(angle - 270)) < 0.01:
        width, height = height, width
    elif min(abs(angle), abs(angle - 180), abs(angle - 360)) > 0.01:
        raise ValueError("视频含非直角旋转，请先转正：" + Path(path).name)
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = Fraction(str(video.get(key, "0/1")))
            if 0 < candidate <= 1000:
                fps = candidate
                break
        except (ValueError, ZeroDivisionError):
            pass
    fps = fps or Fraction(25)
    duration = positive(video.get("duration"))
    if duration is None:
        frames = positive(video.get("nb_frames"))
        if frames is not None:
            duration = frames / float(fps)
    if duration is None:
        raw = video.get("tags", {}).get("DURATION", "")
        match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", str(raw))
        if match:
            duration = positive(int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]))
    duration = duration or positive(data.get("format", {}).get("duration"))
    if duration is None:
        raise ValueError("无法识别视频时长：" + Path(path).name)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return Media(str(Path(path).resolve()), width, height, duration, fps,
                 int(video["index"]), int(audio["index"]) if audio else -1,
                 (positive(audio.get("duration")) or positive(data.get("format", {}).get("duration")) or duration) if audio else 0)


def probe(path, ffprobe, cancel=None):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("视频不存在：" + str(path))
    data = run_process([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe,crypto",
                        "-show_streams", "-show_format", "-of", "json", str(path)], cancel)
    return media_from_data(path, json.loads(data))


def make_plan(left, right, basis=0, audio="basis", compatible=True):
    if basis not in (0, 1) or audio not in {"basis", "left", "right", "mix", "mute"}:
        raise ValueError("拼接参数无效。")
    baseline = (left, right)[basis]
    frames = max(1, math.ceil(baseline.duration * float(baseline.fps) - 1e-6))
    width, height = left.width + right.width, max(left.height, right.height)
    if compatible:
        width += width % 2
        height += height % 2
    indices = [basis] if audio == "basis" else [0] if audio == "left" else [1] if audio == "right" else [0, 1] if audio == "mix" else []
    audible = [i for i in indices if (left, right)[i].audio_index >= 0]
    return {"width": width, "height": height, "content_width": left.width + right.width,
            "content_height": max(left.height, right.height), "fps": str(baseline.fps),
            "duration": frames / float(baseline.fps), "source_duration": baseline.duration,
            "frames": frames, "basis": basis, "audio_indices": audible,
            "missing_audio": bool(indices and not audible),
            "loop": (left, right)[1 - basis].duration < baseline.duration - 1e-6,
            "pixel_format": "yuv420p" if width % 2 == height % 2 == 0 else "yuv444p"}


def build_command(ffmpeg, media, plan, destination, audio_cycle=None):
    duration = f"{plan['duration']:.9f}"
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-filter_complex_threads", "1"]
    for index, source in enumerate(media):
        if index != plan["basis"] and plan["loop"]:
            args += ["-stream_loop", "-1"]
        args += ["-protocol_whitelist", "file,pipe,crypto", "-threads", "2", "-i", source.path]
    if audio_cycle:
        args += ["-stream_loop", "-1", "-protocol_whitelist", "file,pipe,crypto", "-i", str(audio_cycle)]
    filters = []
    height = plan["content_height"]
    for index, source in enumerate(media):
        filters.append(
            f"[{index}:{source.video_index}]setpts=PTS-STARTPTS,scale={source.width}:{source.height}:flags=lanczos,"
            f"setsar=1,format=yuv444p,fps={plan['fps']},"
            f"tpad=stop_mode=clone:stop_duration={2 / float(Fraction(plan['fps'])):.9f},"
            f"trim=duration={duration},pad={source.width}:{height}:0:{(height - source.height) // 2}:black[v{index}]"
        )
    filters.append(f"[v0][v1]hstack=inputs=2:shortest=1,pad={plan['width']}:{plan['height']}:0:0:black,"
                   f"format={plan['pixel_format']}[video]")
    for index in plan["audio_indices"]:
        source_label = "2:a:0" if audio_cycle and index != plan["basis"] else f"{index}:{media[index].audio_index}"
        filters.append(f"[{source_label}]asetpts=PTS-STARTPTS,"
                       f"aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,"
                       f"apad,atrim=duration={duration}[a{index}]")
    audio = plan["audio_indices"]
    if len(audio) == 2:
        filters.append(f"[a0][a1]amix=inputs=2:duration=longest:normalize=1,atrim=duration={duration}[audio]")
    args += ["-filter_complex", ";".join(filters), "-map", "[video]"]
    if audio:
        args += ["-map", "[audio]" if len(audio) == 2 else f"[a{audio[0]}]", "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]
    return args + ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2",
                   "-pix_fmt", plan["pixel_format"], "-t", duration,
                   "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart",
                   "-progress", "pipe:1", "-nostats", str(destination)]


def thumbnail(media, ffmpeg, cancel=None):
    return run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-protocol_whitelist", "file,pipe,crypto", "-i", media.path,
                        "-map", f"0:{media.video_index}", "-vf",
                        f"scale={media.width}:{media.height},setsar=1,scale=480:480:force_original_aspect_ratio=decrease",
                        "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"], cancel)


def signature(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def prepare_audio_cycle(media, plan, ffmpeg, directory, cancel=None):
    """A long audio tail must not delay video looping when that audio is selected.

    Only this uncommon case needs a temporary lossless audio cycle; the video
    still renders once, with no whole-video/frame cache in memory.
    """
    index = 1 - plan["basis"]
    source = media[index]
    if not (plan["loop"] and index in plan["audio_indices"]
            and source.audio_duration > source.duration + 2 / float(source.fps)):
        return None
    path = Path(directory) / "audio-cycle.flac"
    duration = f"{source.duration:.9f}"
    run_process([ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file,pipe,crypto",
                 "-i", source.path, "-map", f"0:{source.audio_index}", "-vn", "-af",
                 f"asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,apad,atrim=duration={duration}",
                 "-t", duration, "-ac", "2", "-c:a", "flac", "-compression_level", "0", str(path)],
                cancel, timeout=max(60, source.duration * 5))
    return path


def export_pair(left, right, output, *, basis=0, audio="basis", compatible=True,
                overwrite=False, cancel=None, progress=None, report=None):
    output = Path(output).expanduser().resolve()
    inputs = [Path(left).expanduser().resolve(), Path(right).expanduser().resolve()]
    if output.suffix.casefold() != ".mp4":
        raise ValueError("成品请选择 .mp4 文件。")
    if output.exists() and not output.is_file():
        raise ValueError("成品路径不是普通文件，请更换输出名称。")
    if any(output == path or (output.exists() and path.exists() and os.path.samefile(output, path)) for path in inputs):
        raise ValueError("成品不能覆盖任何一个源视频，请更换输出名称。")
    initial = signature(output) if output.exists() else None
    if initial is not None and not overwrite:
        raise FileExistsError("同名成品已存在，请确认覆盖或修改名称。")
    source_signatures = [signature(path) for path in inputs]
    ffmpeg, ffprobe = resolve_tools()
    media = [probe(path, ffprobe, cancel) for path in inputs]
    plan = make_plan(*media, basis, audio, compatible)
    if report:
        report(media, plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / (".video-stitch-" + uuid4().hex + ".mp4")
    try:
        with tempfile.TemporaryDirectory(prefix=".video-stitch-audio-", dir=output.parent) as cycle_folder:
            audio_cycle = prepare_audio_cycle(media, plan, ffmpeg, cycle_folder, cancel)
            run_process(build_command(ffmpeg, media, plan, temporary, audio_cycle), cancel, progress, plan["duration"],
                        timeout=max(180, plan["duration"] * 30 + 60))
        result = probe(temporary, ffprobe, cancel)
        tolerance = max(0.15, 2 / float(Fraction(plan["fps"])))
        if (result.width, result.height) != (plan["width"], plan["height"]) or abs(result.duration - plan["duration"]) > tolerance:
            raise RuntimeError("拼接结果尺寸或时长校验失败，未替换任何文件。")
        if bool(plan["audio_indices"]) != (result.audio_index >= 0):
            raise RuntimeError("拼接结果声音校验失败，未替换任何文件。")
        if cancel is not None and cancel.is_set():
            raise Canceled("操作已取消，源视频未改动。")
        if any(signature(path) != before for path, before in zip(inputs, source_signatures)):
            raise RuntimeError("源视频在导出期间被修改，请重新导出。")
        if initial is not None:
            if not output.exists() or signature(output) != initial:
                raise RuntimeError("已有成品在导出期间发生变化，为避免覆盖已停止。")
            os.replace(temporary, output)
        elif os.name == "nt":
            os.rename(temporary, output)  # Windows refuses a concurrently-created destination.
        else:
            os.link(temporary, output)
            temporary.unlink()
        if progress:
            progress(100)
        return {"output": str(output), "plan": plan}
    finally:
        if temporary.exists():
            temporary.unlink()

"""Local FFmpeg renderer. Rendering is sequential, cancellable and resumable."""
import hashlib
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .layout import render_overlay
from .layers import ImageCache
from .store import file_identity
from app_paths import APP_ROOT
from .adaptation import adaptation_plan, base_filter, prepare_cycle, source_fps
from .mixing import audio_graph
from .effects import blur_filter

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"})
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"})
_LAYER_CACHE = threading.local()


class Cancelled(Exception):
    pass


def resolve_tools(configured=""):
    ffmpeg = str(configured or "").strip()
    if ffmpeg:
        if Path(ffmpeg).suffix.casefold() == ".sha":
            raise ValueError("本插件使用标准FFmpeg，请选择ffmpeg.exe，或留空使用随程序提供的版本。")
        ffmpeg = ffmpeg if Path(ffmpeg).is_file() else shutil.which(ffmpeg)
    else:
        roots = [APP_ROOT, Path(getattr(sys, "_MEIPASS", APP_ROOT / "_internal"))]
        ffmpeg = next((str(root / "runtime" / "ffmpeg" / "ffmpeg.exe") for root in roots
                       if (root / "runtime" / "ffmpeg" / "ffmpeg.exe").is_file()), None)
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到FFmpeg，请在本插件参数中指定。")
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("找不到FFprobe，请放到FFmpeg同目录或PATH中。")
    return str(ffmpeg), str(ffprobe)


def _run(args, cancel=None, progress=None, duration=0, timeout=3600):
    if cancel and cancel.is_set():
        raise Cancelled("已取消。")
    with tempfile.TemporaryDirectory(prefix="text-video-log-") as directory:
        path = Path(directory) / "ffmpeg.log"
        # Independent handles: reading progress must not move FFmpeg's write offset.
        with path.open("wb") as writer, path.open("rb") as log:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=writer, stderr=writer,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if cancel and cancel.is_set():
                        raise Cancelled("已取消；本次未消耗音乐轮换进度。")
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("媒体处理超时。")
                    if progress and duration:
                        text = log.read().decode("utf-8", "replace")
                        for value in re.findall(r"out_time_us=(\d+)", text):
                            progress(min(99, int(float(value) / 1e6 / duration * 100)))
                    time.sleep(0.1)
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise
            if path.stat().st_size > 4 * 1024 * 1024:
                raise RuntimeError("媒体日志异常过大，停止处理，未忽略检测结果。")
            log.seek(0)
            text = log.read().decode("utf-8", "replace")
    if process.returncode:
        raise RuntimeError("FFmpeg处理失败：" + text[-2000:])
    if cancel and cancel.is_set():
        raise Cancelled("已取消。")
    return text


@lru_cache(maxsize=8)
def filter_file_option(ffmpeg):
    """FFmpeg 9 removed the legacy script option; slash syntax reads a file."""
    result = subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    version = re.search(r"ffmpeg version (\d+)", text)
    return "-/filter_complex" if version and int(version.group(1)) >= 7 else "-filter_complex_script"


def probe(path, ffprobe, audio_only=False):
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("无法读取媒体：" + Path(path).name)
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    duration_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None) if audio_only else stream
    raw_duration = (duration_stream or {}).get("duration") or data.get("format", {}).get("duration")
    duration = float(raw_duration or 0)
    if not math.isfinite(duration) or not 0 < duration <= 24 * 3600:
        raise ValueError("媒体时长无效：" + Path(path).name)
    return {"duration": duration, "video": stream,
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def output_size(settings, media=None):
    if settings["aspect"] == "landscape":
        return 1920, 1080
    if settings["aspect"] == "source" and media and media.get("video"):
        stream = media["video"]
        width, height = int(stream["width"]), int(stream["height"])
        rotation = stream.get("tags", {}).get("rotate", 0)
        rotation = next((s.get("rotation", rotation) for s in stream.get("side_data_list", []) if "rotation" in s), rotation)
        if abs(float(rotation)) % 180 == 90:
            width, height = height, width
        if width * height > 16_777_216:
            raise ValueError("原视频分辨率过大，请选1080p画布。")
        return max(2, width // 2 * 2), max(2, height // 2 * 2)
    return 1080, 1920


def audible_ranges(text, duration):
    quiet, start = [], None
    for event, raw in re.findall(r"silence_(start|end):\s*(-?[\d.]+)", text):
        value = max(0, min(duration, float(raw)))
        if event == "start":
            start = value
        elif start is not None:
            quiet.append((start, max(start, value)))
            start = None
    if start is not None:
        quiet.append((start, duration))
    # Keep a short transition cushion rather than cutting into music attacks.
    result, cursor = [], 0.0
    for start, end in quiet:
        keep_until = min(duration, start + 0.04) if start > 0 else 0
        if keep_until - cursor > 0.04:
            result.append((cursor, keep_until))
        cursor = max(cursor, end - 0.04 if end < duration else duration)
    if duration - cursor > 0.04:
        result.append((cursor, duration))
    return result


def clean_music(path, settings, cache, ffmpeg, ffprobe, cancel=None):
    path = Path(path)
    info = path.stat()
    signature = json.dumps([file_identity(path), info.st_size, info.st_mtime_ns,
                            settings["skip_silence"], settings["silence_db"], settings["silence_seconds"]])
    key = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    cleaned, metadata = cache / (key + ".flac"), cache / (key + ".json")
    if cleaned.is_file() and metadata.is_file():
        try:
            cached_duration = float(json.loads(metadata.read_text("utf-8"))["duration"])
            if math.isfinite(cached_duration) and 0.5 <= cached_duration <= 24 * 3600 and cleaned.stat().st_size > 0:
                os.utime(cleaned, None)
                return cleaned, cached_duration, key
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Rebuild a damaged generated cache; never discard the source music.
    duration = probe(path, ffprobe)["duration"]
    if settings["skip_silence"]:
        text = _run([ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                     f"silencedetect=noise={float(settings['silence_db'])}dB:d={float(settings['silence_seconds'])}",
                     "-f", "null", "-"], cancel, timeout=900)
        ranges = audible_ranges(text, duration)
    else:
        ranges = [(0, duration)]
    audible = sum(end - start for start, end in ranges)
    if audible < 0.5:
        raise ValueError("音乐没有足够的非静音内容：" + path.name)
    with tempfile.TemporaryDirectory(dir=cache, prefix="prepare-") as folder:
        folder = Path(folder)
        graph = []
        if len(ranges) > 1:
            graph.append("[0:a]asplit=" + str(len(ranges)) + "".join(f"[a{i}]" for i in range(len(ranges))))
        for i, (start, end) in enumerate(ranges):
            source = f"[a{i}]" if len(ranges) > 1 else "[0:a]"
            graph.append(f"{source}atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[p{i}]")
        graph.append("".join(f"[p{i}]" for i in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1[out]")
        script = folder / "audio.filter"
        script.write_text(";\n".join(graph), "utf-8")
        temporary = folder / "clean.flac"
        _run([ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
              filter_file_option(ffmpeg), str(script), "-map", "[out]", "-ar", "48000", "-ac", "2",
              "-c:a", "flac", str(temporary)], cancel, timeout=900)
        actual = probe(temporary, ffprobe)["duration"]
        os.replace(temporary, cleaned)
        metadata.write_text(json.dumps({"duration": actual}), "utf-8")
    # Limit only our generated music cache, never touch source music files.
    cached = sorted(cache.glob("*.flac"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in cached)
    for old in cached:
        if total <= 1024 ** 3:
            break
        if old != cleaned:
            total -= old.stat().st_size
            old.unlink()
            old.with_suffix(".json").unlink(missing_ok=True)
    return cleaned, actual, key


def choose_music(state, duration, cache, ffmpeg, ffprobe, cancel, report):
    items = state["music"]
    if not items:
        return None
    start = next((i for i, item in enumerate(items) if item["id"] == state["next_music_id"]), 0)
    for offset in range(len(items)):
        item = items[(start + offset) % len(items)]
        try:
            report("检查音乐：" + Path(item["path"]).name)
            path, total, key = clean_music(item["path"], state["settings"], cache, ffmpeg, ffprobe, cancel)
            cursor = float(item.get("cursor") or 0) % total if item.get("audio_key") == key else 0
            return {"id": item["id"], "path": str(path), "start": cursor,
                    "cursor": (cursor + duration) % total, "audio_key": key,
                    "name": Path(item["path"]).name, "duration": total}
        except Cancelled:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            report("跳过不可用音乐：" + str(error))
    raise ValueError("音乐列表没有可用的非静音音频，请检查音乐或静音阈值。")


def safe_output(folder, name, inputs):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    if not name:
        raise ValueError("输出文件名为空。")
    if name.lower().endswith(".mp4"):
        name = name[:-4]
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    result = Path(folder).resolve() / (name + ".mp4")
    if file_identity(result) in {file_identity(path) for path in inputs}:
        raise ValueError("输出会覆盖源文件，已阻止。请另选目录或文件名。")
    return result


def render_static_text(job, state, directory, cancel=None, progress=None, report=None, preview=False):
    if cancel and cancel.is_set():
        raise Cancelled("已取消。")
    settings = state["settings"]
    report = report or (lambda _text: None)
    ffmpeg, ffprobe = resolve_tools(settings["ffmpeg_path"])
    report("编码器：" + ffmpeg)
    if not job.get("path") or not Path(job["path"]).is_file():
        raise ValueError("缺少背景视频，请使用‘批量分配背景’。")
    media = probe(job["path"], ffprobe)
    if not media["video"]:
        raise ValueError("文件没有视频轨道。")
    width, height = output_size(settings, media)
    if preview:
        scale = min(1,540/width,960/height)
        width, height = max(2,int(width*scale)//2*2), max(2,int(height*scale)//2*2)
    if not hasattr(_LAYER_CACHE, "images"):
        _LAYER_CACHE.images = ImageCache()
    overlay, layout = render_overlay(job["title"], job["body"], width, height, settings, _LAYER_CACHE.images)
    duration = media["duration"]
    voice_path, adaptation = "", None
    if settings.get("use_voice"):
        voice_path = str(job.get("voice_path") or "")
        if not voice_path:
            from .matching import folder_entry
            match = folder_entry(job.get("task_dir") or Path(job["path"]).parent)
            if match["match_error"]:
                raise ValueError(match["match_error"])
            voice_path = match["voice_path"]
        if not Path(voice_path).is_file():
            raise FileNotFoundError("任务人声不存在：" + voice_path)
        voice_info = probe(voice_path, ffprobe, audio_only=True)
        if not voice_info["audio"]:
            raise ValueError("任务人声文件没有音轨。")
        duration = voice_info["duration"]
        fps = source_fps(media)
        adaptation = adaptation_plan(media["duration"], duration, settings, fps)
        report(f"人声 {duration:.2f}秒；背景 {media['duration']:.2f}秒；速度 {adaptation['speed']:.2f}×；"
               f"{'循环叠化' if adaptation['fade'] else '循环直切' if adaptation['repeating'] else '单次播放'}")
        if settings["keep_audio"]:
            report("已添加任务人声，自动忽略背景视频原声，避免叠音。")
    if job.get("requires_title") and not job.get("task_dir"):
        raise ValueError("表格任务缺少绑定的任务目录，请重新导入；未写入其他目录。")
    folder = job.get("task_dir") or settings["output_dir"] or str(Path(job["path"]).parent)
    if not folder:
        raise ValueError("请选择输出文件夹。")
    inputs = [item.get(key) for item in state["jobs"] for key in ("path", "voice_path") if item.get(key)]
    output = safe_output(folder, job["name"], inputs)
    report("成品目录："+str(output.parent))
    if output.exists() and not settings["overwrite"]:
        raise FileExistsError("成品已存在，未覆盖；请改名或启用覆盖：" + output.name)
    output.parent.mkdir(parents=True, exist_ok=True)
    music = choose_music(state, duration, Path(directory) / "music-cache", ffmpeg, ffprobe, cancel, report)
    with tempfile.TemporaryDirectory(prefix=".text-video-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        png, target = temporary / "text.png", temporary / "video.mp4"
        if not overlay.save(str(png), "PNG"):
            raise RuntimeError("文字图层保存失败。")
        args = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"]
        if adaptation and adaptation["repeating"] and not adaptation["fade"]:
            args += ["-stream_loop", str(adaptation["loops"])]
        args += ["-i", job["path"]]
        next_index = 1
        if adaptation and adaptation["fade"]:
            cycle, cycle_duration = prepare_cycle(job["path"], directory, ffmpeg, ffprobe, width, height,
                settings, adaptation, fps, cancel, report)
            prefix = adaptation["clip_duration"] - adaptation["fade"]
            cycle_loops = max(0, math.ceil((duration-prefix)/cycle_duration)-1)
            args += ["-stream_loop", str(cycle_loops), "-i", str(cycle)]
            next_index += 1
            filters = [f"[0:v]{base_filter(width,height,settings,adaptation,fps)},trim=duration={prefix:.9f}[prefix]",
                       f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,trim=duration={duration-prefix:.9f}[cycles]",
                       f"[prefix][cycles]concat=n=2:v=1:a=0,trim=duration={duration:.9f}[background]"]
        elif adaptation:
            simple_plan = {**adaptation, "clip_duration": duration}
            filters = [f"[0:v]{base_filter(width,height,settings,simple_plan,fps)}[background]"]
        else:
            if settings["fit"] == "cover":
                fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
            else:
                fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            filters = [f"[0:v]setpts=PTS-STARTPTS,{fit},setsar=1[background]"]
        overlay_index = next_index
        args += ["-loop", "1", "-i", str(png)]
        next_index += 1
        blur = blur_filter(settings, width)
        if blur:
            filters.append(f"[background]{blur}[blurred_background]")
        background = "blurred_background" if blur else "background"
        filters.append(f"[{background}][{overlay_index}:v]overlay=0:0:shortest=1,format=yuv420p[v]")
        voice_index = None
        if voice_path:
            voice_index = next_index
            args += ["-i", voice_path]
            next_index += 1
        music_index = None
        if music:
            music_index = next_index
            loops = max(0, math.ceil((music["start"] + duration) / music["duration"]) - 1)
            args += ["-stream_loop", str(loops), "-i", music["path"]]
        audio_filters, has_audio = audio_graph(settings, duration, voice_index, music_index, music, media["audio"])
        filters += audio_filters
        script = temporary / "render.filter"
        script.write_text(";\n".join(filters), "utf-8")
        args += [filter_file_option(ffmpeg), str(script), "-map", "[v]"]
        if has_audio:
            args += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-an"]
        args += ["-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25" if preview else "20",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
        report(f"生成中；标题字号{layout['title_size']:.0f} / 正文字号{layout['body_size']:.0f}" +
               (f"；音乐：{music['name']}，从{music['start']:.1f}秒继续" if music else "；无背景音乐"))
        _run(args, cancel, progress, duration, timeout=max(600, duration * 30))
        check = probe(target, ffprobe)
        if abs(check["duration"] - duration) > max(0.12, duration * 0.001):
            raise RuntimeError(f"成品时长{check['duration']:.3f}秒与目标{duration:.3f}秒不一致，未发布该成品。")
        if has_audio and not check["audio"]:
            raise RuntimeError("成品音轨缺失，未发布该成品。")
        if output.exists() and not settings["overwrite"]:
            raise FileExistsError("生成期间已有同名文件出现，未覆盖。")
        if cancel and cancel.is_set():
            raise Cancelled("已取消。")
        os.replace(target, output)
    return output, music


RENDERERS = {"static_text": render_static_text}

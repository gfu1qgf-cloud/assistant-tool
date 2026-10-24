"""One composition pipeline; preparation and final encoding can be scheduled separately."""
from model.AppDataStorage import data_directory, data_file, data_io
from contextlib import contextmanager, ExitStack
import copy
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
from .adaptation import adaptation_plan, base_filter, prepare_cycle, source_fps, target_duration
from .mixing import audio_graph
from .effects import blur_filter
from .audio_sources import resolve_voice

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


def _run(args, cancel=None, progress=None, duration=0, timeout=3600, *, cwd=None):
    from .matting_live import Inputs
    with Inputs(args) as inputs:
        return _run_inputs(inputs, cancel, progress, duration, timeout, cwd=cwd)


def _run_inputs(inputs, cancel=None, progress=None, duration=0, timeout=3600, *, cwd=None):
    args = inputs.args
    from model.MediaStorage import require_media_output
    from model.AppDataStorage import application_storage
    output = Path(args[-1]) if args and str(args[-1]).lower().endswith(('.mp4','.mkv','.mov','.webm','.flac','.wav','.mp3','.m4a','.ogg')) else None
    if output is not None and cwd is not None and not output.is_absolute():
        output = Path(cwd) / output
    if output is not None: require_media_output(output)
    if cancel and cancel.is_set():
        raise Cancelled("已取消。")
    with data_io.TemporaryDirectory(prefix="text-video-log-") as directory:
        path = Path(directory) / "ffmpeg.log"
        # Independent handles: reading progress must not move FFmpeg's write offset.
        with data_file(path).open("wb") as writer, data_file(path).open("rb") as log:
            if cwd is not None:
                args = list(args)
                for index, value in enumerate(args[:-1]):
                    if (value == '-i' and args[max(0, index-2):index] != ['-f', 'lavfi']
                            and not str(args[index+1]).startswith('\\\\.\\pipe\\')):
                        args[index+1] = str(Path(args[index+1]).resolve())
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=writer, stderr=writer, cwd=cwd,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = time.monotonic()
            try:
                while process.poll() is None:
                    inputs.check()
                    if path.stat().st_size > 4 * 1024 * 1024:
                        raise RuntimeError('媒体日志超过4 MB，已停止处理。')
                    if application_storage().protected: application_storage().check(path, write=True)
                    if output is not None: require_media_output(output)
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
            raise ValueError("原素材分辨率过大，请选1080p画布。")
        return max(2, width // 2 * 2), max(2, height // 2 * 2)
    return 1080, 1920


def output_folder(job, settings):
    """No source is valid; no destination must never silently mean the working directory."""
    folder = job.get('task_dir') or settings.get('output_dir')
    if not folder and job.get('path'):
        folder = str(Path(job['path']).parent)
    if not folder:
        raise ValueError('请选择输出文件夹。')
    return folder


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
    from .music_preparation import music_key
    key = music_key(path, settings)
    cache = Path(cache)
    data_directory(cache).ensure(parents=True, exist_ok=True)
    cleaned, metadata = cache / (key + ".flac"), cache / (key + ".json")
    from model.MediaCacheLeases import media_cache_build
    try:
        with media_cache_build(cleaned, cancel):
            result = _clean_music_cached(path, settings, cache, ffmpeg, ffprobe, cancel, cleaned, metadata, key)
            if music_key(path, settings) != key:
                raise ValueError('音乐准备期间原文件发生变化，请重新提交：' + path.name)
            return result
    except InterruptedError as error:
        raise Cancelled(str(error)) from error


def _clean_music_cached(path, settings, cache, ffmpeg, ffprobe, cancel, cleaned, metadata, key):
    if cancel and cancel.is_set():
        raise Cancelled('已取消音乐准备。')
    if metadata.is_file():
        try:
            cached = json.loads(data_file(metadata).read_text("utf-8"))
            cached_duration = float(cached["duration"])
            if math.isfinite(cached_duration) and 0.5 <= cached_duration <= 24 * 3600:
                if cached.get('direct') is True:
                    return path, cached_duration, key
                if settings['skip_silence'] and cleaned.is_file() and cleaned.stat().st_size > 0:
                    os.utime(cleaned, None)
                    return cleaned, cached_duration, key
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Rebuild a damaged generated cache; never discard the source music.
    from .music_preparation import processing_slot
    with processing_slot(cancel):
        return _build_music_cache(path, settings, cache, ffmpeg, ffprobe, cancel, cleaned, metadata, key)


def _build_music_cache(path, settings, cache, ffmpeg, ffprobe, cancel, cleaned, metadata, key):
    info = probe(path, ffprobe, audio_only=True)
    if not info['audio']:
        raise ValueError('音乐文件没有音轨：' + path.name)
    duration = info['duration']
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
    if ranges == [(0, duration)]:
        # FFmpeg can mix MP3/AAC/WAV/FLAC directly. Do not decode and rewrite
        # an entire track unless silence actually needs to be cut out.
        data_file(metadata).write_text(json.dumps({'duration': duration, 'direct': True}), 'utf-8')
        return path, duration, key
    with data_io.TemporaryDirectory(dir=cache, prefix="prepare-") as folder:
        folder = Path(folder)
        graph = []
        if len(ranges) > 1:
            graph.append("[0:a]asplit=" + str(len(ranges)) + "".join(f"[a{i}]" for i in range(len(ranges))))
        for i, (start, end) in enumerate(ranges):
            source = f"[a{i}]" if len(ranges) > 1 else "[0:a]"
            graph.append(f"{source}atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[p{i}]")
        graph.append("".join(f"[p{i}]" for i in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1[out]")
        script = folder / "audio.filter"
        data_file(script).write_text(";\n".join(graph), "utf-8")
        temporary = folder / "clean.flac"
        _run([ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
              filter_file_option(ffmpeg), str(script), "-map", "[out]", "-ar", "48000", "-ac", "2",
              "-c:a", "flac", str(temporary)], cancel, timeout=900)
        actual = probe(temporary, ffprobe)["duration"]
        data_io.replace(temporary, cleaned)
        data_file(metadata).write_text(json.dumps({"duration": actual, 'direct': False}), "utf-8")
    # The export's lease trims generated cache after all readers release it.
    return cleaned, actual, key


def choose_music(state, duration, cache, ffmpeg, ffprobe, cancel, report):
    if not state["settings"].get("music_enabled", True):
        return None
    from .custom_pools import music_source
    items, next_id, pool = music_source(state)
    if not items:
        return None
    start = next((i for i, item in enumerate(items) if item["id"] == next_id), 0)
    for offset in range(len(items)):
        item = items[(start + offset) % len(items)]
        try:
            report("检查音乐：" + Path(item["path"]).name)
            from .music_preparation import current_music_session
            session = current_music_session()
            if session is None:
                path, total, key = clean_music(item["path"], state["settings"], cache, ffmpeg, ffprobe, cancel)
            else:
                path, total, key = session.get(item['path'], state['settings'], cache,
                                               ffmpeg, ffprobe, cancel, report)
            cursor = float(item.get("cursor") or 0) % total if item.get("audio_key") == key else 0
            return {"id": item["id"], "path": str(path), "start": cursor, "pool_id": pool["id"] if pool else "",
                    "cursor": (cursor + duration) % total, "audio_key": key,
                    "name": Path(item["path"]).name, "duration": total,
                    'generated': file_identity(path) != file_identity(item['path'])}
        except Cancelled:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            report("跳过不可用音乐：" + str(error))
    raise ValueError("音乐列表没有可用的非静音音频，请检查音乐或静音阈值。")


def source_paths(state):
    """Protect registered source media even when it has not been paired yet."""
    inputs = list(state.get('backgrounds', []))
    for key in ('jobs', 'copy_pool', 'music', 'image_pool'):
        for item in state.get(key, []):
            inputs.extend(item.get(field) for field in (
                'path', 'voice_path', 'task_voice_path', 'task_subtitle_path', 'subtitle_path', 'srt_path'))
    inputs.extend(item['path'] for pool in state.get('resource_pools', []) for item in pool['items'])
    from .components import walk_components
    from .chroma_key import active_mask
    for layer in walk_components(state.get('settings', {}).get('layers', [])):
        binding = layer.get('_matting_binding')
        if binding:
            from .matting_assets import record_files
            inputs.extend(record_files(binding))
        inputs.extend(layer.get(key) for key in ('path', 'source_file', 'caption_file'))
        if layer.get('kind') == 'image' and active_mask(layer):
            inputs.append(active_mask(layer))
        inputs.extend(item.get('path') for item in layer.get('sequence_items', []))
    return [path for path in inputs if path]


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


@contextmanager
def prepare_static_text(job, state, directory, cancel=None, progress=None, report=None, preview=False, *, node_effects=None, preview_limit=None):
    started = time.monotonic()
    if preview_limit is not None and (not preview or isinstance(preview_limit, bool) or
            not isinstance(preview_limit, (int, float)) or not math.isfinite(preview_limit) or
            not 0 < preview_limit <= 86400):
        raise ValueError('预览时长限制只用于播放预览，须为0–86400秒之间的正数；不会截短正式导出。')
    if cancel and cancel.is_set():
        raise Cancelled("已取消。")
    from .background_visual import background_job
    job = background_job(job, state)
    settings = dict(state["settings"])
    from .voice_effects import active as voice_fx_active, normalized as normalize_voice_fx, PRESETS
    settings.update(normalize_voice_fx(settings))
    from model.MediaEncoding import normalize_export_settings, resolve_video_encoder, video_encoding_args, filter_thread_args, encoder_label, video_pixel_format, quality_label
    normalize_export_settings(settings)
    workers = 1 if preview else settings['export_workers']
    if node_effects is not None and not preview:
        from .export_pipeline import pipeline_workers
        workers = pipeline_workers(settings, node_effects)
    from .node_effects import active_effects
    effects = active_effects(settings)
    if effects and node_effects is None:
        raise ValueError('当前配置有节点效果，但未准备工作流快照；请从文案视频窗口提交。')
    if node_effects is not None: node_effects.start_job(job, preview=preview)
    from .text_colors import job_color_scope
    settings["_color_scope"] = job_color_scope(job)
    from .components import resolve_sources
    settings["layers"], sequence_plan = resolve_sources(settings["layers"], job.get("title", ""), job.get("body", ""), job=job)
    report = report or (lambda _text: None)
    from .render_progress import stage, observe_report
    report = observe_report(report, progress)
    stage(progress, '准备素材')
    ffmpeg, ffprobe = resolve_tools(settings["ffmpeg_path"])
    report("编码器：" + ffmpeg)
    try:
        encoder = resolve_video_encoder(ffmpeg, settings, cancel, report)
    except InterruptedError as error:
        raise Cancelled(str(error)) from error
    report('视频编码：'+encoder_label(encoder)+'；'
        + ('播放预览采用快速编码；成品质量：' if preview else '导出质量：')
        + quality_label(settings['export_quality']))
    if job.get("path") and not Path(job["path"]).is_file():
        raise ValueError("背景素材不存在：" + Path(job["path"]).name)
    from .image_background import probe_background, duration as image_duration, materialize, motion_filter, FPS
    media = probe_background(job["path"], ffprobe, settings)
    still = media.get("still_image", False)
    empty = media.get("empty_background", False)
    settings['_still_background'] = still
    if not media["video"]:
        raise ValueError("文件没有视频轨道。")
    from model.MediaColor import sdr_input_filter, sdr_output_filter, sdr_output_args
    settings['_source_color_filter'] = sdr_input_filter(media['video'])
    if settings['_source_color_filter']:
        report('检测到HDR／宽色域背景：先转换为SDR Rec.709，再合成图片和字幕，避免成片偏色。')
    width, height = output_size(settings, media)
    from .background_framing import prepare_background_settings, framing_filter, preview_size
    encoded_width, encoded_height = preview_size(width, height) if preview else (width, height)
    duration = image_duration(settings) if still or empty else target_duration(media["duration"], settings)
    voice_path, adaptation = resolve_voice(job, state, discover=True), None
    from .captions import active_captions, task_word_timings, static_planes, write_caption_ass, filter_path
    captions = active_captions(settings["layers"])
    if captions and not voice_path:
        raise ValueError("动态字幕需要人声。请启用人声并选择任务音频或人声池；不会使用背景音乐充当朗读。")
    if voice_path:
        if not Path(voice_path).is_file():
            raise FileNotFoundError("任务人声不存在：" + voice_path)
        voice_info = probe(voice_path, ffprobe, audio_only=True)
        if not voice_info["audio"]:
            raise ValueError("任务人声文件没有音轨。")
        duration = target_duration(media["duration"], settings, voice_info["duration"])
        if settings["keep_audio"]:
            report("已添加任务人声，自动忽略背景视频原声，避免叠音。")
    if empty:
        report(f"无底层背景：{duration:.2f}秒；由控件合成画面。")
    elif still:
        report(f"图片背景：{duration:.2f}秒；无视频中间缓存。")
    elif voice_path or settings["unvoiced_duration_mode"] == "fixed":
        fps = source_fps(media)
        adaptation = adaptation_plan(media["duration"], duration, settings, fps)
        report(f"{'人声' if voice_path else '固定成品'} {duration:.2f}秒；背景 {media['duration']:.2f}秒；速度 {adaptation['speed']:.2f}×；"
               f"{'循环叠化' if adaptation['fade'] else '循环直切' if adaptation['repeating'] else '单次播放'}")
    if job.get("requires_title") and not job.get("task_dir"):
        raise ValueError("表格任务缺少绑定的任务目录，请重新导入；未写入其他目录。")
    folder = output_folder(job, settings)
    inputs = source_paths(state) + source_paths({'settings': settings})
    if voice_path:
        inputs.append(voice_path)
    from .task_naming import output_stem
    name = job["name"] if preview else output_stem(job, settings.get("task_naming_rules", {}))
    output = safe_output(folder, name, inputs)
    report("成品目录："+str(output.parent))
    if output.exists() and not settings["overwrite"]:
        raise FileExistsError("成品已存在，未覆盖；请改名或启用覆盖：" + output.name)
    data_directory(output.parent).ensure(parents=True, exist_ok=True)
    from model.MediaStorage import media_cache_root
    media_directory = media_cache_root(directory, anchors=(output.parent, Path(job['path']).parent if job.get('path') else None), namespace='batch-text-video')
    from .music_preparation import current_music_session
    music_session = current_music_session()
    if music_session is not None:
        music_session.prime(state, media_directory / 'music-cache', ffmpeg, ffprobe)
    settings = prepare_background_settings(job["path"], media, settings, ffmpeg, directory, cancel)
    if settings.get("_background_crop"):
        report("铺满画面：自动去除素材自带的稳定黑边（不修改原文件）。")
    # Keep all Qt/font work on its stable preparation thread, while independent
    # audio read-ahead overlaps background analysis, text layout and alignment.
    if not hasattr(_LAYER_CACHE, "images"):
        _LAYER_CACHE.images = ImageCache()
    stage(progress, '生成文字／图层')
    overlay, layout = render_overlay(job["title"], job["body"], width, height, settings, _LAYER_CACHE.images)
    with data_io.TemporaryDirectory(prefix=".text-video-", dir=output.parent) as temporary, ExitStack() as leases:
        temporary = Path(temporary)
        from model.MediaCacheLeases import media_cache_lease, trim_generated_cache
        target = temporary / "video.mp4"
        from .visual_media import needs_plane, add_plane
        from .scrolling_text import is_scrolling, prepare_scroll_plane, add_scroll_plane
        planes = static_planes(job["title"], job["body"], width, height, settings, _LAYER_CACHE.images) if captions or any(
            (needs_plane(layer) or is_scrolling(layer) or layer['kind'] == 'node_effect') and layer["enabled"] and layer["opacity"] for layer in settings["layers"]) else [("image", overlay)]
        prepared = []
        for index, (kind, value) in enumerate(planes):
            if cancel and cancel.is_set():
                raise Cancelled("已取消。")
            if kind in {"visual", 'node_effect'}:
                prepared.append((kind, value))
                continue
            if kind == "scroll":
                prepared.append((kind, prepare_scroll_plane(value, width, height, duration, temporary)))
                continue
            path = temporary / f"layer-{index}.{'ass' if kind == 'caption' else 'png'}"
            if kind == "caption":
                stage(progress, '准备字幕')
                words = task_word_timings(job,voice_path,value,job["title"],job["body"],duration,directory,cancel,report)
                stage(progress, '生成字幕')
                write_caption_ass(path, words, value, width, height, duration)
            elif not value.save(str(path), "PNG"):
                raise RuntimeError("文字图层保存失败。")
            prepared.append((kind, path))
        stage(progress, '准备音乐')
        music = choose_music(state, duration, media_directory / 'music-cache', ffmpeg, ffprobe, cancel, report)
        if music and music.get('generated', False):
            leases.callback(trim_generated_cache, Path(music['path']).parent, '.flac', 1024**3)
            leases.enter_context(media_cache_lease(music['path']))
            if music_session is not None:
                music_session.release(music['path'])
        args = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", *filter_thread_args(workers)]
        if adaptation and adaptation["repeating"] and (not adaptation["fade"] or
                media["audio"] and settings["keep_audio"] and not voice_path):
            args += ["-stream_loop", str(adaptation["loops"])]
        if empty:
            args += ['-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={FPS}']
        elif still:
            image_path = materialize(job["path"], temporary/"background.png")
            args += ["-framerate", str(FPS), "-i", image_path]
        else:
            args += ["-i", job["path"]]
        next_index = 1
        if empty:
            filters = ['[0:v]setpts=PTS-STARTPTS,setsar=1[background]']
        elif still:
            fit = framing_filter(width, height, settings)
            motion = motion_filter(width, height, settings, duration)
            # Decode and fit once. Only one fitted frame lives in FFmpeg's RAM loop.
            filters = [f"[0:v]{fit},loop=loop=-1:size=1:start=0,setpts=N/({FPS}*TB),{motion}[background]"]
        elif adaptation and adaptation["fade"]:
            cycle, cycle_duration = prepare_cycle(job["path"], media_directory, ffmpeg, ffprobe, width, height,
                settings, adaptation, fps, cancel, report, leases=leases)
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
            fit = framing_filter(width, height, settings)
            color = settings['_source_color_filter']
            filters = [f"[0:v]setpts=PTS-STARTPTS,{color + ',' if color else ''}{fit},setsar=1[background]"]
        from .background_visual import control as background_control
        if not (still or empty) and background_control(settings)["visual_animation"] != "none":
            filters.append(f"[background]{motion_filter(width, height, settings, duration, source_fps(media))}[moving_background]")
            background_base = "moving_background"
        else:
            background_base = "background"
        blur = blur_filter(settings, width)
        if blur:
            filters.append(f"[{background_base}]{blur}[blurred_background]")
        background = "blurred_background" if blur else background_base
        # Graphics are SDR RGB. Keep their full chroma until the final explicit
        # Rec.709 conversion; intermediate default 4:2:0 overlays may choose a
        # different YUV matrix and then get relabelled later.
        filters.append(f"[{background}]format=rgba[composition_background]")
        background = 'composition_background'
        for index, (kind, path) in enumerate(prepared):
            label = f"layer{index}"
            if kind == "caption":
                filters.append(f"[{background}]ass=filename={filter_path(path)}[{label}]")
            elif kind == 'node_effect':
                info = {'duration': duration, 'audio': False, 'video': {**media['video'],
                    'width': width, 'height': height, 'display_width': width, 'display_height': height,
                    'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709', 'color_range': 'tv'}}
                next_index = node_effects.append(job, path, args, filters, background, next_index, label,
                    temporary, info, voice_path, ffmpeg)
            elif kind == "visual":
                next_index += add_plane(args, filters, path, next_index, background, label, width, height, duration, source_fps(media))
            elif kind == "scroll":
                next_index += add_scroll_plane(args, filters, path, next_index, background, label, duration, source_fps(media))
            else:
                args += ["-loop", "1", "-i", str(path)]
                filters.append(f"[{background}][{next_index}:v]overlay=0:0:shortest=1:format=auto[{label}]")
                next_index += 1
            background = label
        downscale = f"scale={encoded_width}:{encoded_height}:flags=lanczos," if (encoded_width, encoded_height) != (width, height) else ""
        filters.append(f"[{background}]{downscale}{sdr_output_filter()}[v]")
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
            next_index += 1
        voice_ir_index = None
        if voice_path and voice_fx_active(settings):
            from .voice_reverb import write_ir
            report('人声空间特效：'+PRESETS[settings['voice_fx_preset']]['name'])
            response = write_ir(temporary/'voice-space.wav', settings, cancel)
            voice_ir_index = next_index
            args += ['-i', str(response)]
            next_index += 1
        audio_filters, has_audio = audio_graph(settings, duration, voice_index, music_index, music, media["audio"],
            original_speed=adaptation["speed"] if adaptation and not voice_path else None, voice_ir_index=voice_ir_index)
        filters += audio_filters
        script = temporary / "render.filter"
        data_file(script).write_text(";\n".join(filters), "utf-8")
        args += [filter_file_option(ffmpeg), str(script), "-map", "[v]"]
        if has_audio:
            args += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-an"]
        # Effects, scrolling, word alignment and resource choice keep the full
        # job clock. Limit only the encoded output, not the meaning of the job.
        encoded_duration = min(duration, preview_limit) if preview_limit is not None else duration
        if preview_limit is not None:
            report(f'快速预览：只编码开头{encoded_duration:.2f}秒；正式成品仍为{duration:.2f}秒。')
        args += ["-t", f"{encoded_duration:.6f}", *video_encoding_args(encoder, preview=preview, workers=workers, quality=settings['export_quality']),
                 "-pix_fmt", video_pixel_format(encoder), *sdr_output_args(), "-movflags", "+faststart", str(target)]
        report(f"生成中；标题字号{layout['title_size']:.0f} / 正文字号{layout['body_size']:.0f}" +
               (f"；音乐：{music['name']}，从{music['start']:.1f}秒继续" if music else "；无背景音乐"))
        if effects:
            node_effects.protect_output(output)
        if node_effects is not None:
            sequence_plan.extend(copy.deepcopy(node_effects.sequence_plan))
        from .prepared_export import PreparedExport
        stage(progress, '等待编码名额')
        yield PreparedExport(args, target, output, ffprobe, encoded_duration, has_audio, music,
            sequence_plan, cancel, progress, report, settings['overwrite'], encoder,
            settings['encoder_fallback'], preview, workers, effects=node_effects if effects else None,
            cwd=temporary if effects else None, timings={'prepare_seconds': time.monotonic()-started},
            quality=settings['export_quality'], render_scope='' if preview else file_identity(Path(directory)/'state.json'),
            source_paths=tuple(inputs))


def render_static_text(job, state, directory, cancel=None, progress=None, report=None, preview=False, *, node_effects=None, preview_limit=None):
    from .music_preparation import MusicPreparationSession, current_music_session, music_session
    with ExitStack() as stack:
        if current_music_session() is None:
            # This entry point renders one video; do not pre-process music for
            # unrelated future jobs. Batch read-ahead is owned by its pipeline.
            session = stack.enter_context(MusicPreparationSession(cancel, 1))
            stack.enter_context(music_session(session))
        prepared = stack.enter_context(prepare_static_text(job, state, directory, cancel, progress, report, preview,
            node_effects=node_effects, preview_limit=preview_limit))
        prepared.encode()
        return prepared.publish(), prepared.music


render_static_text.prepare_export = prepare_static_text


RENDERERS = {"static_text": render_static_text}

"""One final encoding from a deferred filter plan, with cooperative cancellation."""

from model.AppDataStorage import data_io, data_file, data_directory
import json
from contextlib import ExitStack, contextmanager
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

from app_paths import APP_ROOT
from .contracts import RenderPlan, VideoTimeline, VideoComposition, SubtitleTrack, TaskSnapshot
from .operations import bind_operations
from .scope import initialize_execution
from .video_stream import VideoStream, normalize_plan
from .encoding import output_filters, mux_args, video_encoder_args, audio_encoder_args, summary as encoding_summary


def resolve_tools(configured=""):
    import sys
    roots = (APP_ROOT, Path(getattr(sys, "_MEIPASS", APP_ROOT / "_internal")))
    ffmpeg = (str(Path(configured).resolve()) if configured and Path(configured).is_file()
              else next((str(root / "runtime" / "ffmpeg" / "ffmpeg.exe") for root in roots
                         if (root / "runtime" / "ffmpeg" / "ffmpeg.exe").is_file()), None))
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("找不到随程序提供的 FFmpeg。")
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("找不到 FFprobe，请放在 FFmpeg 同目录。")
    return ffmpeg, ffprobe


def probe(path, ffprobe):
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError("无法读取视频：" + Path(path).name)
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    duration = float((video or {}).get("duration") or data.get("format", {}).get("duration") or 0)
    if video is None or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError("视频轨道或时长无效。")
    video = dict(video)
    rotations = [item.get("rotation") for item in video.get("side_data_list", []) if "rotation" in item]
    rotation = rotations[0] if rotations else video.get("tags", {}).get("rotate", 0)
    try:
        quarter_turn = round(float(rotation) / 90) % 2
    except (TypeError, ValueError, OverflowError):
        quarter_turn = 0
    video["display_width"] = video["height"] if quarter_turn else video["width"]
    video["display_height"] = video["width"] if quarter_turn else video["height"]
    return {"duration": duration, "video": video,
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def verify_resource(resource, context):
    if isinstance(resource, VideoStream):
        resource.verify()
        return verify_resource(resource.source, context)
    if isinstance(resource, VideoComposition):
        resource.verify()
        for value in resource.inputs:
            verify_resource(value, context)
        return
    resource.verify()
    if isinstance(resource, (VideoTimeline, SubtitleTrack)):
        return
    if resource.storage != "file":
        session = getattr(context, "media", None)
        if session is None:
            raise ValueError("内存/管道资源缺少执行会话，不能作为普通文件路径读取。")
        session.verify(resource)


def probe_resource(resource, context, configured=""):
    if isinstance(resource, VideoStream):
        return probe_resource(resource.source, context, configured)
    verify_resource(resource, context)
    if isinstance(resource, VideoComposition):
        return resource.info
    if isinstance(resource, VideoTimeline):
        return {"duration": resource.duration,
                "video": {"width": resource.width, "height": resource.height, "avg_frame_rate": resource.fps},
                "audio": resource.has_audio}
    if resource.storage != "file":
        return context.media.info(resource)
    _, ffprobe = resolve_tools(configured)
    return probe(resource.path, ffprobe)


def compile_filters(plan, directory, context=None, *, video_info=None, asset_prefix=''):
    """Assets get safe relative names. No arbitrary code or shell is executed."""
    if asset_prefix and not re.fullmatch(r'[a-z][a-z0-9_]{0,50}', asset_prefix):
        raise ValueError('过滤器资源前缀无效。')
    plan = normalize_plan(plan)
    filters, count = [], 0
    operations = (bind_operations(plan.video, plan.operations, context, video_info=video_info)
                  if video_info is not None or any(op.video is None for op in plan.operations) else plan.operations)
    from .contracts import FilterOperation, VideoFilterFragment
    from .filter_graph import VideoFilterChain, check_fragment
    expanded = []
    registry = getattr(context, "workflow_registry", None)
    for operation in operations:
        definition = registry.definitions.get(operation.name) if registry else None
        if definition is not None and definition.compile_operation is not None:
            if video_info is None:
                video_info = probe_resource(plan.video, context, getattr(context, "ffmpeg", ""))
            native = definition.compile_operation(operation, directory, context, video_info)
            if (not isinstance(native, tuple) or not 1 <= len(native) <= 32
                    or any(not isinstance(item, (FilterOperation, VideoFilterFragment)) or item.video != plan.video
                           or isinstance(item, FilterOperation) and "." in item.name for item in native)):
                raise ValueError("插件操作编译器必须返回当前视频的原生过滤器，不能递归调用插件操作。")
            expanded.extend(native)
        else:
            if "." in operation.name:
                raise ValueError("未注册此插件的操作编译器：" + operation.name)
            expanded.append(operation)
    operations = expanded
    for operation in operations:
        if isinstance(operation, VideoFilterFragment):
            check_fragment(operation, plan.video)
            filters.append(operation)
            continue
        if operation.video != plan.video:
            raise ValueError("操作来自另一个视频，请连接相同的视频输出。")
        verify_resource(operation.video, context)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", operation.name):
            raise ValueError("过滤器名称无效。")
        options = dict(operation.options)
        for key, ref in operation.assets:
            ref.verify()
            if isinstance(ref, SubtitleTrack):
                name = f"{asset_prefix}asset_{count}.srt"
                data_file(Path(directory) / name).write_text_direct(ref.to_srt(), encoding="utf-8")
                count += 1
                options[key] = name
                continue
            if ref.data_type == "video" or ref.size > 2 * 1024 * 1024:
                raise ValueError("操作附件必须是小型非视频资源，不能生成中间视频。")
            suffix = Path(ref.path).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                raise ValueError("操作附件扩展名无效。")
            name = f"{asset_prefix}asset_{count}{suffix}"
            data_io.copyfile(ref.path, Path(directory) / name)
            ref.verify()
            count += 1
            options[key] = name
        values = []
        for key, value in options.items():
            if (not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not isinstance(value, str)
                    or len(value) > 2000 or any(ch in value for ch in "'\\:;[]\r\n\x00")):
                raise ValueError("过滤器参数无效；不接受原始 FFmpeg 表达式。")
            values.append(key + "='" + value + "'")
        filters.append(operation.name + ("=" + ":".join(values) if values else ""))
    return (VideoFilterChain(tuple(filters)) if any(isinstance(item, VideoFilterFragment) for item in filters)
            else ",".join(filters) if filters else "null")


def timeline_source(video, ffprobe):
    """Reusable timeline inputs/filters, without an encoder or destination."""
    paths = list(dict.fromkeys(section.source.path for section in video.sections))
    info = {path: probe(path, ffprobe) for path in paths}
    command = []
    filters, seeks = [], {}
    for source_index, path in enumerate(paths):
        indices = [index for index, section in enumerate(video.sections) if section.source.path == path]
        seek = min(video.sections[index].start for index in indices)
        seeks[path] = seek
        command += ["-threads", "2", "-ss", f"{seek:.9f}", "-i", path]
        tags = "".join(f"[vsrc{index}]" for index in indices)
        split = f"split={len(indices)}" if len(indices) > 1 else "null"
        filters.append(f"[{source_index}:v:0]{split}{tags}")
        if video.has_audio and info[path]["audio"]:
            tags = "".join(f"[asrc{index}]" for index in indices)
            split = f"asplit={len(indices)}" if len(indices) > 1 else "anull"
            filters.append(f"[{source_index}:a:0]{split}{tags}")
    for index, section in enumerate(video.sections):
        start, end = section.start - seeks[section.source.path], section.end - seeks[section.source.path]
        duration = section.end - section.start
        filters.append(f"[vsrc{index}]trim=start={start:.9f}:end={end:.9f},setpts=PTS-STARTPTS,"
                       f"scale={video.width}:{video.height}:force_original_aspect_ratio=decrease,"
                       f"pad={video.width}:{video.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={video.fps},"
                       f"tpad=stop_mode=clone:stop_duration=0.1,trim=duration={duration:.9f},"
                       f"setpts=PTS-STARTPTS[v{index}]")
        if video.has_audio:
            if info[section.source.path]["audio"]:
                audio = f"[asrc{index}]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,"
                audio += "aformat=sample_rates=48000:channel_layouts=stereo,apad,"
            else:
                audio = "anullsrc=sample_rate=48000:channel_layout=stereo,"
            filters.append(audio + f"atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a{index}]")
    tags = "".join(f"[v{index}]" + (f"[a{index}]" if video.has_audio else "") for index in range(len(video.sections)))
    filters.append(tags + f"concat=n={len(video.sections)}:v=1:a={1 if video.has_audio else 0}[joined]"
                   + ("[audio]" if video.has_audio else ""))
    return command, filters, len(paths)


def timeline_command(video, ffmpeg, ffprobe, chain, target, encoding=None):
    """Decode each source once, trim/concat/filter, then encode the final once."""
    inputs, filters, count = timeline_source(video, ffprobe)
    command = [ffmpeg, "-n", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"] + inputs
    from .filter_graph import append_chain
    append_chain(chain, command, filters, "joined", count)
    script = Path(target).parent / "filters.txt"
    data_file(script).write_text_direct(";\n".join(filters), encoding="utf-8")
    command += ["-filter_complex_threads", "2", "-/filter_complex", str(script), "-map", "[final]",
                '-threads', '2'] + video_encoder_args(encoding)
    if video.has_audio:
        command += ['-map', '[audio]'] + audio_encoder_args(encoding, timeline=True)
    return command + mux_args(encoding) + [str(target)]


def _publish(files, context):
    published = []
    try:
        for source, destination in files:
            context.check_canceled()
            if os.name == "nt":
                data_io.rename(source, destination)
            else:
                os.link(source, destination)
            published.append(destination)
        context.check_canceled()
    except BaseException:
        for path in reversed(published):
            data_file(path).unlink(missing_ok=True)
        raise


@contextmanager
def render_directory(parent, context):
    """Windows can retain inherited/antivirus log handles briefly after exit."""
    import sys
    directory = Path(data_io.mkdtemp(prefix=".node-render-", dir=parent))
    try:
        yield str(directory)
    finally:
        primary_error = sys.exc_info()[0] is not None
        for attempt in range(20):
            try:
                data_io.rmtree(directory)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 19:
                    message = "临时目录仍被系统占用：" + str(directory)
                    context.log(message)
                    if not primary_error:
                        if not hasattr(context, "notes"):
                            context.notes = []
                        context.notes.append({"kind": "temporary_cleanup", "detail": message})
                else:
                    time.sleep(.1)


def render_plan(plan, context, configured=""):
    if not isinstance(plan, RenderPlan):
        raise TypeError("结算器只接受 RenderPlan。")
    plan = normalize_plan(plan)
    context.check_canceled()
    verify_resource(plan.video, context)
    destination = Path(plan.output)
    from model.MediaStorage import require_media_output
    require_media_output(destination)
    if destination.exists() or (plan.video.path and destination.resolve() == Path(plan.video.path).resolve()):
        raise ValueError("成品路径已存在，未覆盖源文件或旧成品。")
    data_directory(destination.parent).ensure(parents=True, exist_ok=True)
    ffmpeg, ffprobe = resolve_tools(configured)
    info = probe_resource(plan.video, context, configured)
    context.check_canceled()
    started = time.monotonic()
    # Keep temporary media on the output disk, not the user's possibly full C:.
    with render_directory(destination.parent, context) as directory:
        chain = compile_filters(plan, directory, context, video_info=info)
        chain = output_filters(chain, plan.encoding)
        target = Path(directory) / "result.mp4"
        if isinstance(plan.video, VideoComposition):
            from .source_composition import composition_command
            command = composition_command(plan.video, ffmpeg, ffprobe, chain, target, context, plan.encoding)
        elif isinstance(plan.video, VideoTimeline):
            command = timeline_command(plan.video, ffmpeg, ffprobe, chain, target, plan.encoding)
        else:
            from .filter_graph import file_filter_options
            command = [ffmpeg, "-n", "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-progress", "pipe:1", "-i", plan.video.path if plan.video.storage == "file" else "pipe:0"]
            options = file_filter_options(chain, command, directory)
            command += options + ['-map', '0:a?'] + video_encoder_args(plan.encoding) + audio_encoder_args(plan.encoding)
            command += mux_args(plan.encoding) + [str(target)]
        if plan.encoding is not None:
            context.log('实际编码设置：' + encoding_summary(plan.encoding))
            if plan.encoding.audio == 'copy' and isinstance(plan.video, (VideoTimeline, VideoComposition)) and info['audio']:
                context.log('拼接／混合音轨经过滤镜，不能直接复制编码；本次使用AAC重新编码音频。')
        context.log("节点渲染：" + " → ".join(op.title for op in plan.operations) + "；一次视频编码。")
        log_path = Path(directory) / "render.log"
        feeder, feed_errors = None, []
        with ExitStack() as stack:
            writer = stack.enter_context(data_io.open(log_path, "wb"))
            reader = stack.enter_context(data_io.open(log_path, "rb"))
            media_reader = (stack.enter_context(context.media.open_reader(plan.video))
                            if plan.video.storage not in {"file", "timeline", "composition"} else None)
            process = subprocess.Popen(command, cwd=directory,
                stdin=subprocess.PIPE if media_reader is not None else subprocess.DEVNULL,
                stdout=writer, stderr=writer, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if media_reader is not None:
                def feed():
                    try:
                        while True:
                            context.check_canceled()
                            data = media_reader.read(64 * 1024)
                            if not data: break
                            process.stdin.write(data)
                    except BaseException as error:
                        feed_errors.append(error)
                    finally:
                        try: process.stdin.close()
                        except OSError: pass
                feeder = threading.Thread(target=feed, daemon=True)
                feeder.start()
            try:
                while process.poll() is None:
                    context.check_canceled()
                    require_media_output(destination)
                    if time.monotonic() - started > 86400:
                        raise TimeoutError("节点渲染超过 24 小时，已停止。")
                    for value in re.findall(r"out_time_us=(\d+)", reader.read(8 * 1024**2 + 1).decode("utf-8", "replace")):
                        context.report("统一渲染中", min(99, int(int(value) / 1e6 / info["duration"] * 100)))
                    if log_path.stat().st_size > 8 * 1024 * 1024:
                        raise RuntimeError("媒体处理日志异常过大，已停止。")
                    time.sleep(.1)
                context.check_canceled()
                if log_path.stat().st_size > 8 * 1024**2:
                    raise RuntimeError("媒体处理日志异常过大，已停止。")
                reader.seek(0)
                output = reader.read(8 * 1024**2).decode("utf-8", "replace")
                if process.returncode:
                    raise RuntimeError("FFmpeg 渲染失败：" + output[-2000:])
                if feeder is not None:
                    feeder.join(timeout=5)
                    if feeder.is_alive():
                        raise RuntimeError("媒体传输线程未正常结束。")
                    if feed_errors:
                        raise feed_errors[0]
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if media_reader is not None:
                    media_reader.close()
                if feeder is not None:
                    feeder.join(timeout=5)
        context.check_canceled()
        verify_resource(plan.video, context)
        for operation in plan.operations:
            for _, ref in operation.assets:
                ref.verify()
        final = probe(target, ffprobe)
        if abs(final["duration"] - info["duration"]) > .25:
            raise ValueError("成品时长与源视频不符，未落地成品。")
        if info["audio"] and not final["audio"]:
            raise ValueError("成品缺失原有音频，未落地成品。")
        output_bytes = target.stat().st_size
        input_bytes = plan.video.size if getattr(plan.video, 'storage', '') in {'file','memory','stream'} else None
        size_change = (100*(1-output_bytes/input_bytes) if input_bytes else None)
        if plan.encoding is not None:
            if size_change is None:
                context.log(f'编码完成：输出 {output_bytes/1024**2:.2f} MB；多视频合成不以原文件总大小冒充压缩率。')
            else:
                change = f'减少 {size_change:.1f}%' if size_change >= 0 else f'增大 {-size_change:.1f}%'
                context.log(f'压缩结果：{input_bytes/1024**2:.2f} MB → {output_bytes/1024**2:.2f} MB，体积{change}。')
                if size_change < 0:
                    context.log('本次输出比源文件大。恒定质量不保证变小；如需控制体积，请在压缩节点选择目标码率。')
        context.check_canceled()
        publications = [(target, destination)]
        srt, report = "", ""
        task = getattr(context, "task", None)
        if task is not None:
            tracks = [ref for operation in plan.operations for _, ref in operation.assets if isinstance(ref, SubtitleTrack)]
            if not tracks and isinstance(getattr(context, "generated_subtitles", None), SubtitleTrack):
                tracks = [context.generated_subtitles]
            if tracks:
                subtitle_path = Path(directory) / "final.srt"
                data_file(subtitle_path).write_text_direct(tracks[-1].to_srt(), encoding="utf-8")
                srt = str(destination.with_suffix(".srt"))
                publications.append((subtitle_path, Path(srt)))
            report = str(destination.with_suffix(".workflow.json"))
            report_path = Path(directory) / "summary.json"
            data_file(report_path).write_text_direct(json.dumps({"format": "task-workflow-result-v1", "task": {
                "key": task.key, "label": task.label, "table": task.table, "source_row": task.source_row},
                "summary": getattr(context, "processing_summary", {}), "notes": getattr(context, "notes", []),
                "video": str(destination), "subtitles": srt, "duration": final["duration"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            publications.append((report_path, Path(report)))
        _publish(publications, context)
        seconds = time.monotonic() - started
        context.report("成品已完成", 100)
    return {"paths": [str(destination)], "video": str(destination), "subtitles": srt, "report": report,
            "notes": list(getattr(context, "notes", [])), "seconds": round(seconds, 3),
            "duration": final["duration"], "video_encodings": 1, "intermediate_videos": 0,
            "encoding": encoding_summary(plan.encoding) if plan.encoding is not None else '',
            "input_bytes": input_bytes, "output_bytes": output_bytes, "size_reduction_percent": size_change,
            "input_storage": plan.video.storage,
            "compressed_memory_peak": getattr(getattr(context, "media", None), "peak_bytes", 0)}


def run_workflow(payload, context, *, registry):
    from .graph import build_plan
    from .media import MediaSession
    context.ffmpeg = payload.get("ffmpeg", "")
    initialize_execution(payload, context)
    context.prepared = payload.get("prepared", {})
    context.notes = []
    context.failed_node = {}
    context.skipped_reason = ""
    context.generated_subtitles = None
    context.processing_summary = {}
    previous = getattr(context, "media", None)
    try:
        with MediaSession(context, context.ffmpeg) as session:
            context.media = session
            plan = build_plan(payload["graph"], registry, context)
            output_node = next(node for node in payload["graph"]["nodes"] if node["id"] == payload["graph"]["output_node"])
            context.failed_node = {"id": output_node["id"], "title": registry.definitions[output_node["kind"]].title}
            result = render_plan(plan, context, context.ffmpeg)
            context.failed_node = {}
            return result
    finally:
        if previous is None:
            del context.media
        else:
            context.media = previous


def run_task_workflow(payload, context, *, registry):
    from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskResultError
    try:
        result = run_workflow(payload, context, registry=registry)
        return {**result, "status": "completed_with_notes" if result.get("notes") else "completed",
                "task_label": payload["task"]["label"], "task_key": payload["task"]["key"]}
    except BackgroundTaskCanceled:
        raise
    except Exception as error:
        context.check_canceled()
        result = {"task_label": payload["task"]["label"], "task_key": payload["task"]["key"],
                  "paths": [], "notes": list(getattr(context, "notes", [])),
                  "failed_node": getattr(context, "failed_node", {}), "error": str(error)}
        if getattr(context, "skipped_reason", ""):
            return {**result, "status": "skipped", "error": context.skipped_reason}
        raise BackgroundTaskResultError(str(error), {**result, "status": "failed"}) from error

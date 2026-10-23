"""On-demand, bounded inspection. No final export or full intermediate video."""

from model.AppDataStorage import data_io, data_file, data_directory
import base64
from contextlib import ExitStack
import json
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from .contracts import (NodeDefinition, NodeParameter, NodePort, ResourceRef, VideoTimeline,
                        VideoSection, SubtitleTrack, DirectoryRef, FileList, RenderPlan, TaskSnapshot, ResourcePool)
from .video_stream import VideoStream, normalize_plan


def register_preview(registry):
    registry.register("core", NodeDefinition("core.preview", "预览",
        inputs=(NodePort("value", "内容", "any", type_group="value"),),
        outputs=(NodePort("value", "内容", "any", type_group="value"),),
        parameters=(NodeParameter("time", "视频预览位置（秒）", "number", 1, 0, 86400),),
        build=lambda i, _p, _c: {"value": i["value"]},
        description="独立预览节点，接线自动确定类型。双击查看文本、数值、文件列表、字幕或视频画面；原样传递数据，只执行上游，不导出完整视频。"))
    for kind, title in (("text", "文本"), ("number", "数值"), ("boolean", "真／假"),
                        ("directory", "目录"), ("files", "文件列表"), ("subtitles", "字幕"),
                        ("video", "视频"), ("render_plan", "视频计划 · 含处理效果")):
        registry.register("core", NodeDefinition("core.preview_" + kind, "预览 · " + title,
            inputs=(NodePort("value", title, kind, capabilities=("seek",) if kind == "video" else ()),),
            outputs=(NodePort("value", title, kind),),
            parameters=(NodeParameter("time", "预览位置（秒）", "number", 1, 0, 86400),) if kind in {"video", "render_plan"} else (),
            build=lambda i, _p, _c: {"value": i["value"]},
            description="连接需要查看的数据，双击此预览节点打开内容（或右键打开预览）。仅执行上游，不执行下游；视频显示所选时间的画面，不导出完整成品。"))


def is_preview(kind):
    return kind == "core.preview" or kind.startswith("core.preview_")


def run_data_workflow(payload, context, *, registry):
    """Normal (not preview) execution of a pure-data Detail workflow."""
    from .graph import evaluate_graph
    from .media import MediaSession
    from .scope import initialize_execution
    from .python_values import PythonValue
    context.ffmpeg = payload.get("ffmpeg", "")
    initialize_execution(payload, context)
    context.prepared = payload.get("prepared", {})
    context.notes, context.processing_summary = [], {}
    context.failed_node, context.skipped_reason, context.generated_subtitles = {}, "", None
    node = next(n for n in payload["graph"]["nodes"] if n["id"] == payload["graph"]["output_node"])
    if node["kind"] != "core.py_output":
        raise ValueError("数据执行需要连接到数据输出节点。")
    previous = getattr(context, "media", None)
    try:
        with MediaSession(context, context.ffmpeg) as session:
            context.media = session
            values = evaluate_graph(payload["graph"], registry, context)
            value = values["value"]
            if not isinstance(value, PythonValue):
                raise TypeError("数据输出必须是有效的 Python 数据。")
            text = describe(value)
            context.report("数据处理完成", 100)
            return {"result_kind": "data", "title": registry.definitions[node["kind"]].title,
                    "text": text, "data_json": value.data_json, "png": "", "task_label": "", "time": None}
    finally:
        if previous is None:
            del context.media
        else:
            context.media = previous


def describe(value):
    from .contracts import ColorValue
    if isinstance(value, ColorValue):
        return f"sRGB 颜色 {value.hex_rgb} · Alpha {value.alpha:.3f}\nRGBA (0–1): {value.red:.4f}, {value.green:.4f}, {value.blue:.4f}, {value.alpha:.4f}"
    from .python_values import PythonValue, validate_data
    from .loops import LoopBlock, LoopResults
    if isinstance(value, LoopBlock):
        return "Begin/End 循环块 · " + value.begin_id[:8]
    if isinstance(value, LoopResults):
        rows = [f"循环结果：{len(value.items)} 项 · {value.data_type}（按迭代顺序）"]
        for index, item in enumerate(value.items[:100], 1):
            rows.append(f"{index}. " + describe(item)[:2000])
        if len(value.items) > 100:
            rows.append("仅预览前 100 项；实际结果没有截断。")
        return "\n".join(rows)
    if isinstance(value, PythonValue) or type(value) in (list, dict):
        data = value.value if isinstance(value, PythonValue) else validate_data(value)
        text = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
        return text[:250000] + ("\n（预览显示截断；实际数据完整）" if len(text) > 250000 else "")
    if value is None:
        return "（空值；连接到必填输入时会报错，不会伪造默认值）"
    if isinstance(value, RenderPlan):
        return describe(value.video) + "\n处理操作：" + (" → ".join(op.title for op in value.operations) or "无") + "\n导出路径：" + (value.output or "尚未连接导出")
    if isinstance(value, VideoStream):
        return (describe(value.source) + '\n待执行操作：' + (' → '.join(op.title for op in value.operations) or '无')
                + '\n编码：' + str(value.encoding or '默认') + '\n尚未编码或保存')
    if isinstance(value, VideoTimeline):
        rows = [f"剪辑计划：{len(value.sections)} 段，{value.duration:.3f} 秒，{value.width}×{value.height}，{value.fps} fps"]
        rows.extend(f"{index}. {item.source.path} [{item.start:.3f} – {item.end:.3f}]" for index, item in enumerate(value.sections[:1000], 1))
        return "\n".join(rows)
    if isinstance(value, FileList):
        rows = [f"目录：{value.directory.path}\n文件数量：{len(value.files)}（按实际顺序）"]
        rows.extend(f"{index}. {item.path} ({item.size} 字节)" for index, item in enumerate(value.files[:1000], 1))
        if len(value.files) > 1000:
            rows.append("仅展示前 1000 项，实际列表没有截断。")
        return "\n".join(rows)
    if isinstance(value, DirectoryRef):
        return value.path
    if isinstance(value, ResourcePool):
        data = value.data
        rows = [f"资源池：{value.name} · {value.kind}（只读快照）"]
        items = data.get("items", [])
        rows.append(f"共 {len(items)} 项；不消耗文案视频配置的轮换进度。")
        rows.extend(f"{index}. " + str(item.get("name") or item.get("path") or item.get("title") or "文案")
                    for index, item in enumerate(items, 1))
        return "\n".join(rows)
    if isinstance(value, SubtitleTrack):
        return f"字幕：{len(value.cues)} 块，{value.duration:.3f} 秒\n" + value.to_srt()[:250000]
    if isinstance(value, ResourceRef):
        return f"{value.data_type} · {value.storage}\n{value.path or '本次会话内存资源'}\n{value.size} 字节"
    if isinstance(value, bool):
        return "真 (True)" if value else "假 (False)"
    if isinstance(value, tuple):
        return "\n".join(f"{i}. {item.title} · {item.name}" for i, item in enumerate(value[:1000], 1)) or "（空操作集合）"
    return str(value)[:250000]


def frame_preview(plan, at, context, directory, max_edge=1080):
    from .contracts import VideoComposition
    from .render import compile_filters, probe_resource, resolve_tools, timeline_command, verify_resource
    plan = normalize_plan(plan)
    video = plan.video
    info = probe_resource(video, context, context.ffmpeg)
    if type(max_edge) is not int or max_edge not in {720, 1080, 2160}:
        raise ValueError('预览分辨率选项无效。')
    context.preview_source_size = [info['video'].get('display_width', info['video']['width']),
                                   info['video'].get('display_height', info['video']['height'])]
    scale = f"scale=w='min(iw,{max_edge})':h='min(ih,{max_edge})':force_original_aspect_ratio=decrease,setsar=1"
    at = max(0.0, min(float(at), max(0.0, info["duration"] - .1)))
    ffmpeg, ffprobe = resolve_tools(context.ffmpeg)
    target = Path(directory) / "preview.png"
    # Keep filter timestamps on the full edited timeline, while seeking to a single source frame.
    from .filter_graph import wrap_filters, file_filter_options
    compiled = compile_filters(plan, directory, context, video_info=info)
    from .encoding import output_filters
    compiled = output_filters(compiled, plan.encoding)
    chain = wrap_filters(compiled, f"setpts=PTS-STARTPTS+{at:.9f}/TB",
                         "setpts=PTS-STARTPTS," + scale)
    from .filter_graph import VideoFilterChain
    # Dynamic elements must advance to the same timeline time as the base.
    # For complex layers decode from the start and select a single final frame.
    if isinstance(chain, VideoFilterChain):
        chain = wrap_filters(compiled, "setpts=PTS-STARTPTS",
            f"select=gte(t\\,{at:.9f}),setpts=PTS-STARTPTS," + scale)
    if isinstance(video, VideoComposition):
        from .source_composition import composition_command
        chain = wrap_filters(compiled, 'setpts=PTS-STARTPTS',
            f'select=gte(t\\,{at:.9f}),setpts=PTS-STARTPTS,' + scale)
        command = composition_command(video, ffmpeg, ffprobe, chain, target, context, include_audio=False)
        command = command[:command.index('-c:v')] + ['-frames:v', '1', '-an', '-c:v', 'png', '-threads', '1', '-pix_fmt', 'rgb24', str(target)]
    elif isinstance(video, VideoTimeline):
        remaining = 0 if isinstance(chain, VideoFilterChain) else at
        section = video.sections[-1]
        for item in video.sections:
            section = item
            if remaining < item.end - item.start:
                break
            remaining -= item.end - item.start
        start = min(section.end - .001, section.start + remaining)
        frame_video = (replace(video, has_audio=False) if isinstance(chain, VideoFilterChain) else
                       replace(video, sections=(VideoSection(section.source, start, min(section.end, start + .5)),), has_audio=False))
        command = timeline_command(frame_video, ffmpeg, ffprobe, chain, target)
        # Retain the safe compiled filter graph, discard final-video encoding options.
        command = command[:command.index("-c:v")] + ["-frames:v", "1", "-an", "-c:v", "png", "-threads", "1", "-pix_fmt", "rgb24", str(target)]
    else:
        if "seek" not in video.capabilities:
            raise ValueError("逐帧预览需要可定位资源；顺序流请先接入内存缓存。")
        command = [ffmpeg, "-n", "-nostdin", "-v", "error", "-threads", "2"]
        if not isinstance(chain, VideoFilterChain): command += ["-ss", f"{at:.9f}"]
        command += ["-i", video.path if video.storage == "file" else "pipe:0"]
        options = file_filter_options(chain, command, directory)
        command += options + ["-frames:v", "1", "-an", "-c:v", "png", "-threads", "1", "-pix_fmt", "rgb24", str(target)]
    reader = context.media.open_reader(video) if isinstance(video, ResourceRef) and video.storage != "file" else None
    feeder, errors = None, []
    process = None
    started = time.monotonic()
    try:
        with ExitStack() as stack, data_io.open(Path(directory) / "preview.log", "wb") as log:
            factory = getattr(context, 'deferred_input_factory', None)
            deferred = stack.enter_context(factory(command)) if callable(factory) else None
            if deferred is not None: command = deferred.args
            process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE if reader else subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if reader is not None:
                def feed():
                    try:
                        while process.poll() is None:
                            context.check_canceled()
                            block = reader.read(65536)
                            if not block:
                                break
                            process.stdin.write(block)
                    except BrokenPipeError:
                        pass  # FFmpeg stops after the requested frame.
                    except OSError as error:
                        if process.poll() != 0:
                            errors.append(error)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                feeder = threading.Thread(target=feed, daemon=True)
                feeder.start()
            while process.poll() is None:
                context.check_canceled()
                if deferred is not None: deferred.check()
                if time.monotonic() - started > 60:
                    raise TimeoutError("预览画面处理超过 60 秒，已停止。")
                if (Path(directory) / "preview.log").stat().st_size > 1024 * 1024:
                    raise RuntimeError("预览日志异常过大。")
                time.sleep(.03)
        if process.returncode or not target.is_file():
            raise ValueError("预览画面失败：" + data_file(Path(directory) / "preview.log").read_text(encoding="utf-8", errors="replace")[-1500:])
        verify_resource(video, context)
        for operation in plan.operations:
            for _, ref in operation.assets:
                ref.verify()
        context.check_canceled()
        if target.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("预览画面过大。")
        return base64.b64encode(data_file(target).read_bytes()).decode("ascii"), at
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if feeder is not None:
            feeder.join(timeout=5)
        if reader is not None:
            reader.close()
        if feeder is not None:
            if feeder.is_alive():
                raise RuntimeError("预览媒体传输线程未正常结束。")
        if errors:
            raise errors[0]


def run_preview(payload, context, *, registry):
    from .workflow_types import selected_type
    value = selected_type(payload['graph'], registry)
    if value is None:
        return _run_preview(payload, context, registry=registry)
    if value.preview_factory is None:
        raise ValueError('此插件工作流尚未提供预览模拟；可以保存和执行，不能把真实任务当作预览输入。')
    context.ffmpeg = payload.get('ffmpeg', '')
    previous = getattr(context, 'workflow_input', None)
    try:
        with data_io.TemporaryDirectory(prefix='node-simulation-', ignore_cleanup_errors=True) as directory:
            context.workflow_input = value.preview_factory(context, directory, payload['graph'])
            result = _run_preview(payload, context, registry=registry, preview_adapter=value.preview_plan_factory)
            result['simulation'] = True
            result['title'] = '纯色模拟 · ' + result['title']
            result['text'] = '仅模拟排版与效果，不使用真实任务视频，也不会上传。\n\n' + result['text']
            return result
    finally:
        context.workflow_input = previous


def _run_preview(payload, context, *, registry, preview_adapter=None):
    from .graph import evaluate_graph
    from .media import MediaSession
    from .scope import initialize_execution
    context.ffmpeg = payload.get("ffmpeg", "")
    context.workflow_registry = registry
    initialize_execution(payload, context)
    context.prepared = payload.get("prepared", {})
    context.notes, context.processing_summary = [], {}
    context.failed_node, context.skipped_reason, context.generated_subtitles = {}, "", None
    node_id = payload["preview_node"]
    node = next(n for n in payload["graph"]["nodes"] if n["id"] == node_id)
    definition = registry.definitions[node["kind"]]
    previous = getattr(context, "media", None)
    try:
        with MediaSession(context, context.ffmpeg) as session:
            context.media = session
            values = evaluate_graph(payload["graph"], registry, context, root_id=node_id)
            text = "\n\n".join(port.title + "\n" + describe(values[port.key]) for port in definition.result_ports(node['params']))[:500000]
            result = {"node_id": payload.get('preview_alias', node_id), "title": definition.title, "text": text, "png": "",
                      "task_label": context.task.label if context.task else "", "time": None}
            visual = next((value for value in values.values() if isinstance(value, (RenderPlan, VideoTimeline, VideoStream))
                           or isinstance(value, ResourceRef) and value.data_type == "video"), None)
            if visual is None and preview_adapter is not None:
                visual = preview_adapter(context, values)
            if visual is not None:
                plan = visual if isinstance(visual, RenderPlan) else RenderPlan(visual, (), "")
                with data_io.TemporaryDirectory(prefix="node-preview-", ignore_cleanup_errors=True) as directory:
                    result["png"], result["time"] = frame_preview(plan, node["params"].get("time", 1), context, directory,
                                                                payload.get('preview_max_edge', 1080))
                    result['source_size'] = context.preview_source_size
                    result['preview_max_edge'] = payload.get('preview_max_edge', 1080)
            context.report("预览结果已准备", 100)
            return result
    finally:
        if previous is None:
            del context.media
        else:
            context.media = previous

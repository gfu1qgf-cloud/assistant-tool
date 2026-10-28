"""Smart-edit's typed post-export workflow; no widgets or live host in workers."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import re
import traceback

from model.AppDataStorage import data_directory, data_file, data_io
from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef, RenderPlan, SubtitleCue, SubtitleTrack
from model.node_workflow.video_stream import stream, unchanged_source
from model.node_workflow.workflow_types import WorkflowTypeDefinition

TYPE_ID = "smart_video_editor.postprocess"
ENTRY = "smart_video_editor.export_input"
OUTPUT = "smart_video_editor.export_output"
ENABLED = "postprocess_enabled"
WORKFLOW = "postprocess_workflow"


def build_input(_inputs, _params, context):
    value = getattr(context, "workflow_input", None)
    if not isinstance(value, dict) or value.get("provider") != TYPE_ID:
        raise ValueError("请由智能剪辑导出后处理注入成片，或使用面板中的模拟预览。")
    subtitles = value.get("subtitles")
    if subtitles is not None:
        subtitles.verify()
    return {"video": stream(value["video"]), "subtitles": subtitles,
            "has_subtitles": subtitles is not None,
            **{key: str(value.get(key) or "") for key in
               ("video_path", "subtitle_path", "task_id", "task_name", "script", "language", "task_dir")},
            "duration": float(value["duration"]), "segment_index": int(value.get("segment_index", 1)),
            "segment_count": int(value.get("segment_count", 1))}


def build_output(inputs, _params, _context):
    return {"video": stream(inputs["video"])}


def example(registry):
    from model.node_workflow.graph import empty_graph, new_node
    graph = empty_graph()
    graph.update(workflow_type=TYPE_ID, workflow_type_version=1)
    kinds = [ENTRY, OUTPUT]
    if "batch_text_video.caption" in registry.definitions:
        kinds = [ENTRY, "batch_text_video.caption", "core.settle", OUTPUT]
    nodes = [new_node(registry.definitions[kind], (index * 310, 40)) for index, kind in enumerate(kinds)]
    first, last = nodes[0]["id"], nodes[-1]["id"]
    edges = [[first, "video", last, "video"]]
    if len(nodes) == 4:
        caption, settle = nodes[1]["id"], nodes[2]["id"]
        edges = [[first, "subtitles", caption, "subtitles"], [first, "video", settle, "video"],
                 [caption, "operations", settle, "operations"], [settle, "video", last, "video"]]
    graph.update(nodes=nodes, connections=edges, output_node=last)
    return graph


def preview_input(context, directory, graph):
    # Reuse the existing bounded synthetic-clip generator; never read a real task.
    from app_plugins.builtin.task_delivery_workflow import preview_input as synthetic_input
    base = synthetic_input(context, directory, graph)
    from model.node_workflow.render import probe, resolve_tools
    duration = probe(base["video"].path, resolve_tools(context.ffmpeg)[1])["duration"]
    track = SubtitleTrack((SubtitleCue(0.0, duration, "这是智能剪辑导出后处理的模拟字幕。"),), "", duration, "simulation")
    return {"provider": TYPE_ID, "video": base["video"], "video_path": base["video"].path,
            "subtitles": track, "duration": duration, "task_id": "1000", "task_name": "智能剪辑模拟任务",
            "script": "这是智能剪辑导出后处理的模拟字幕。", "language": "zh", "task_dir": str(directory)}


def preview_plan(context, values):
    operations = values.get("operations")
    if operations is not None:
        return RenderPlan(context.workflow_input["video"], tuple(operations), "")
    return None


def register(context):
    context.register_node(NodeDefinition(ENTRY, "智能剪辑 · 成片输入",
        outputs=(NodePort("video", "视频流", "video"), NodePort("subtitles", "成片字幕", "subtitles"),
                 NodePort("has_subtitles", "有可用字幕", "boolean"), NodePort("video_path", "原成片路径", "text"),
                 NodePort("subtitle_path", "原SRT路径", "text"), NodePort("task_id", "任务编号", "text"),
                 NodePort("task_name", "任务名称", "text"), NodePort("script", "任务文案", "text"),
                 NodePort("language", "语言", "text"), NodePort("task_dir", "任务目录", "text"),
                 NodePort("duration", "成片时长（秒）", "number"), NodePort("segment_index", "分段序号", "number"),
                 NodePort("segment_count", "本任务分段数", "number")), build=build_input,
        description="由智能剪辑注入本次导出的真实视频流、成片时间轴字幕及独立任务字段。分段导出逐段注入，字幕映射到各段自身时间轴；无可用字幕时输出为空，可用布尔端判断。不读取当前窗口或配置。"))
    context.register_node(NodeDefinition(OUTPUT, "智能剪辑 · 返回后处理结果",
        inputs=(NodePort("video", "处理后视频流", "video"),),
        terminal_result=NodePort("video", "处理后视频流", "video"), build=build_output,
        description="返回连接的视频流，由智能剪辑复用节点渲染器另存后处理成品；不覆盖原成片、原SRT或旧后处理结果。可以使用字幕、图层、压缩等现有节点。"))
    context.register_workflow_type(WorkflowTypeDefinition(TYPE_ID, "智能剪辑 · 导出后处理", ENTRY, OUTPUT,
        example_factory=example, preview_factory=preview_input, preview_plan_factory=preview_plan,
        description="智能剪辑导出后逐个处理成片或分段；输入含视频、字幕及任务信息，默认关闭，普通导出保留。"))


def prepare(settings, services):
    if not settings.get(ENABLED):
        return None, None, ()
    key = str(settings.get(WORKFLOW) or "").strip()
    if not key:
        raise ValueError("已开启导出后处理，请先选择一个“智能剪辑 · 导出后处理”工作流。")
    registry = services.available_nodes()
    graph = services.workflow_library.load(key, registry)
    if graph.get("workflow_type") != TYPE_ID:
        raise ValueError("请选择“智能剪辑 · 导出后处理”类型的工作流。")
    from model.node_workflow.tasks import prepare_job
    payload, resources = prepare_job(graph, registry, None, services)
    return payload, registry, resources


def _track(cues, duration, task_key, timing):
    values = tuple(SubtitleCue(max(0.0, float(cue["start"])), min(duration, float(cue["end"])), str(cue["text"]))
                   for cue in cues if min(duration, float(cue["end"])) > max(0.0, float(cue["start"])) + .001)
    return SubtitleTrack(values, task_key, duration, timing).verify() if values else None


def track_from_srt(path, duration, task_key):
    from model.node_workflow.operations import validate_subtitle
    ref = ResourceRef.capture(path, "subtitles")
    validate_subtitle(ref, duration)
    raw = data_file(Path(ref.path)).read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    pattern = re.compile(r"^\s*(?:\d+\s*\n)?(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{3})[^\n]*\n(.*)$", re.S)
    def seconds(value):
        hour, minute, last = value.replace(",", ".").split(":")
        return int(hour) * 3600 + int(minute) * 60 + float(last)
    cues = []
    for block in re.split(r"\n[ \t]*\n", text):
        start, end, content = pattern.fullmatch(block).groups()  # Already validated as SRT.
        cues.append({"start": seconds(start), "end": seconds(end), "text": content})
    ref.verify()
    return _track(cues, duration, task_key, "final_media_stable_whisper"), ref


def _target(directory, source):
    base = directory / (source.stem + "_节点后处理.mp4")
    target, index = base, 2
    while any(target.with_suffix(ext).exists() for ext in (".mp4", ".srt", ".workflow.json")):
        target = base.with_name(f"{base.stem}_{index:03}.mp4")
        index += 1
    return target


def process_file(item, task, payload, registry, context, settings, *, clip=None, index=1, count=1):
    from model.node_workflow.graph import evaluate_graph
    from model.node_workflow.scope import initialize_execution
    from model.node_workflow.media import MediaSession
    from model.node_workflow.render import probe, render_plan, resolve_tools, _publish
    from .engine import build_srt_cues
    original = Path(item["video"]).resolve()
    task_key = str(Path(task["task_dir"]).resolve()).casefold()
    ref = replace(ResourceRef.capture(original, "video"), task_key=task_key)
    ffmpeg, ffprobe = resolve_tools(settings.get("ffmpeg_path", ""))
    duration = probe(ref.path, ffprobe)["duration"]
    subtitle_ref, subtitles = None, None
    if item.get("srt"):
        subtitles, subtitle_ref = track_from_srt(item["srt"], duration, task_key)
    elif clip is not None:
        subtitles = _track(build_srt_cues(dict(task, clips=[clip]), settings), duration, task_key,
                           "source_words_mapped_to_segment")
    names = ("media", "task", "workflow_input", "workflow_mode", "workflow_globals", "workflow_execution_id",
             "ffmpeg", "prepared", "notes", "generated_subtitles",
             "workflow_registry", "processing_summary", "failed_node", "skipped_reason")
    previous = {name: getattr(context, name, None) for name in names}
    try:
        initialize_execution(payload, context)
        context.ffmpeg, context.workflow_registry = ffmpeg, registry
        context.prepared = copy.deepcopy(payload.get("prepared", {}))
        context.notes, context.failed_node, context.skipped_reason = [], {}, ""
        context.processing_summary, context.generated_subtitles = {}, subtitles
        context.workflow_input = {"provider": TYPE_ID, "video": ref, "subtitles": subtitles, "duration": duration,
            "video_path": ref.path, "subtitle_path": str(item.get("srt") or ""), "task_dir": task["task_dir"],
            "task_id": task.get("task_id", ""), "task_name": task.get("task_name", ""),
            "script": task.get("script", ""), "language": task.get("language", ""),
            "segment_index": index, "segment_count": count}
        with MediaSession(context, ffmpeg) as media:
            context.media = media
            values = evaluate_graph(payload["graph"], registry, context)
            value = stream(values["video"])
            if unchanged_source(value, ref, media):
                ref.verify()
                if subtitle_ref is not None:
                    subtitle_ref.verify()
                return dict(item, postprocess="unchanged", postprocess_workflow=settings[WORKFLOW])
            directory = Path(task["output_dir"]).resolve() / "节点后处理结果"
            data_directory(directory).ensure(parents=True, exist_ok=True)
            target = _target(directory, original)
            with data_io.TemporaryDirectory(prefix=".smart-node-", dir=str(directory)) as staging:
                stage = Path(staging)
                rendered = render_plan(RenderPlan(value, (), str(stage / "result.mp4")), context, ffmpeg)
                ref.verify()
                if subtitle_ref is not None:
                    subtitle_ref.verify()
                publications = [(Path(rendered["video"]), target)]
                tracks = [asset for op in value.operations for _, asset in op.assets if isinstance(asset, SubtitleTrack)]
                track = tracks[-1] if tracks else subtitles
                output_srt = ""
                if track is not None and abs(track.duration - rendered["duration"]) <= .1:
                    data_file(stage / "result.srt").write_text(track.to_srt(), encoding="utf-8")
                    output_srt = str(target.with_suffix(".srt"))
                    publications.append((stage / "result.srt", Path(output_srt)))
                report = {"format": "smart-video-postprocess-v1", "task_id": str(task.get("task_id") or ""),
                    "workflow_id": settings[WORKFLOW], "workflow_name": payload["graph"].get("workflow_name", ""),
                    "original_video": ref.path, "original_srt": str(item.get("srt") or ""),
                    "video": str(target), "srt": output_srt, "duration": rendered["duration"],
                    "segment_index": index, "segment_count": count, "notes": context.notes}
                data_file(stage / "result.workflow.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                publications.append((stage / "result.workflow.json", target.with_suffix(".workflow.json")))
                _publish(publications, context)
            return dict(item, video=str(target), srt=output_srt, original_video=ref.path,
                        original_srt=str(item.get("srt") or ""), postprocess="completed",
                        postprocess_workflow=settings[WORKFLOW], postprocess_report=str(target.with_suffix(".workflow.json")))
    finally:
        for name, value in previous.items():
            setattr(context, name, value)


def process_results(result, bundle, payload, registry, context, settings):
    """Failures retain ordinary exports, are reported, and do not stop other tasks."""
    completed = []
    for item in result.get("completed", []):
        if item.get("status") != "completed":
            completed.append(item)
            continue
        context.check_canceled()
        try:
            candidates = [task for task in bundle.get("tasks", [])
                          if str(task.get("task_id") or "") == str(item.get("task_id") or "")
                          and Path(task["output_dir"]).resolve() == Path(item["video"]).resolve().parent]
            if len(candidates) != 1:
                raise ValueError("无法唯一关联导出结果与任务，原成片已保留，未混用其他任务的字幕。")
            task = candidates[0]
            context.report("导出后处理：" + str(task.get("label") or task.get("task_id") or "任务"), None)
            if item.get("export_mode") == "segments":
                segments = []
                for index, part in enumerate(item["segments"], 1):
                    context.check_canceled()
                    clips = [clip for clip in task["clips"] if Path(clip["source"]).resolve() == Path(part["source"]).resolve()]
                    if len(clips) != 1:
                        raise ValueError("分段清单无法唯一关联源片段，原分段已保留。")
                    segments.append(process_file(dict(part, status="completed", srt=""), task, payload, registry,
                        context, settings, clip=clips[0], index=index, count=len(item["segments"])))
                changed = any(part.get("postprocess") == "completed" for part in segments)
                completed.append(dict(item, segments=segments, postprocess="completed" if changed else "unchanged",
                    original_video=item["video"], video=str(Path(task["output_dir"]) / "节点后处理结果") if changed else item["video"]))
            else:
                completed.append(process_file(item, task, payload, registry, context, settings))
        except Exception as error:
            context.check_canceled()
            if isinstance(error, InterruptedError):
                raise
            result.setdefault("failed", []).append({"task_id": item.get("task_id", ""), "stage": "postprocess",
                "error": "导出后处理失败，原成片已保留：" + str(error), "original_video": item.get("video", ""),
                "original_srt": item.get("srt", ""), "traceback": traceback.format_exc()})
    result["completed"] = completed
    return result

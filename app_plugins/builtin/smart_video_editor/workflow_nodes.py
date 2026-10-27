"""Unattended adapter: reuse analysis, return cuts and mapped subtitles, no UI."""
import copy
from fractions import Fraction
from pathlib import Path

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from model.node_workflow.contracts import VideoTimeline, VideoSection, SubtitleTrack, SubtitleCue, FileList
from model.node_workflow.render import probe, resolve_tools
from . import engine


class WorkflowTaskSkipped(RuntimeError):
    """A configured automatic outcome, not a request for user review."""
    workflow_skipped = True


def prepare_edit(task, params, services):
    # Called once on the GUI thread. Only scoped settings enter worker payloads.
    settings = services._effective_settings()
    settings = engine.normalize_smart_video_editor_settings(settings)
    settings["ffmpeg_path"] = str(settings.get("ffmpeg_path") or "")
    for key in ("lead_padding_ms", "tail_padding_ms", "pause_threshold_ms"):
        if params.get(key, -1) >= 0:
            settings[key] = params[key]
    if params.get("internal_pauses") != "inherit":
        settings["compress_internal_pauses"] = params["internal_pauses"] == "on"
    if params.get("model") != "inherit":
        settings["whisper_model_size"] = params["model"]
    if params.get("pass_threshold", 0):
        settings["pass_similarity_percent"] = params["pass_threshold"]
    settings = engine.normalize_smart_video_editor_settings(settings)
    # Configuration only. Files and text are supplied by wires, never searched here.
    return {"settings": settings, "relative_writes": [settings["output_folder_name"],
            str(Path(settings["output_folder_name"]) / ".smart_edit_cache")]}


def _warning_text(value):
    text = str(value or "")
    for old in ("必须人工核对后再生成", "必须人工核对顺序后再生成", "必须人工核对", "请人工核对", "请试听核对"):
        text = text.replace(old, "已按节点策略处理")
    return text


def apply_policy(task, policy, notes):
    task = copy.deepcopy(task)  # Never rewrite interactive analysis/cache decisions.
    problems = []
    for name in ("missing_blocks", "unverified_blocks"):
        for block in task.get(name, []):
            problems.append({"kind": name, "detail": str(block.get("text") or block.get("detail") or "文案覆盖不完整")})
    extra = engine.smart_video_extra_clip_findings({"tasks": [task]})
    for item in extra:
        problems.append({"kind": "extra_clip", "file": item["file_name"], "detail": _warning_text(item["issue_reason"])})
    for clip in task.get("clips", []):
        for issue in clip.get("issues", []):
            if issue.get("severity") != "info":
                problems.append({"kind": issue.get("kind", "analysis"), "file": clip.get("file_name", ""),
                                 "detail": _warning_text(issue.get("detail") or issue.get("title"))})
    notes.extend(problems)
    if policy == "skip" and problems:
        raise WorkflowTaskSkipped(f"按策略跳过：分析发现 {len(problems)} 项内容疑点。")
    if policy == "conservative":
        for clip in task.get("clips", []):
            if clip.get("included", True) and clip.get("status") == "pink":
                clip["kept_ranges"] = engine.kept_ranges(
                    0.0, float(clip.get("original_duration") or 0), clip.get("repeated_speech_removals", []))
                notes.append({"kind": "preserved_uncertain_clip", "file": clip.get("file_name", ""),
                              "detail": "保留严重不确定片段的首尾和气口；有直接证据的重复朗读仍按计划裁切。"})
    elif policy == "automatic":
        for item in extra:
            clip = task["clips"][item["clip_index"]]
            clip["included"] = False
            notes.append({"kind": "automatic_extra_cut", "file": item["file_name"],
                          "detail": "按自动处理策略排除疑似多余片段。"})
    elif policy != "skip":
        raise ValueError("未知的无人值守处理策略。")
    for clip in task.get("clips", []):
        if not clip.get("included", True):
            notes.append({"kind": "excluded_clip", "file": clip.get("file_name", ""),
                          "detail": _warning_text(clip.get("issue_reason") or "未参与成品")})
        elif clip.get("repeated_speech_removals"):
            notes.append({"kind": "repeated_speech_cut", "file": clip.get("file_name", ""),
                          "detail": f"处理了 {len(clip['repeated_speech_removals'])} 处重复朗读。"})
    return task


def _recognized_cues(clip, offset, settings):
    # Unmatched source speech must not be replaced by invented authoritative text.
    words = clip.get("words", [])
    ranges = clip["kept_ranges"]
    eligible = [word for word in words if any(float(word.get("end", 0)) > start and
                float(word.get("start", 0)) < end for start, end in ranges)]
    groups, current = [], []
    for word in eligible:
        text = " ".join(str(item.get("text") or "") for item in current + [word])
        if current and ((settings["srt_max_words_per_block"] > 0 and len(current) >= settings["srt_max_words_per_block"])
                        or (settings["srt_max_chars_per_block"] > 0 and len(text) > settings["srt_max_chars_per_block"])):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    cues = []
    for group in groups:
        start = offset + engine.source_time_in_kept_ranges(float(group[0].get("start", 0)), ranges)
        end = offset + engine.source_time_in_kept_ranges(float(group[-1].get("end", 0)), ranges)
        text = " ".join(str(word.get("text") or "").strip() for word in group).strip()
        if end > start + .05 and text:
            cues.append({"start": start, "end": end, "text": text})
    return cues


def build_edit(inputs, params, context):
    file_list = inputs["files"]
    if not isinstance(file_list, FileList):
        raise TypeError("智能剪辑的视频输入必须是文件列表。")
    file_list.verify(context.check_canceled)
    prepared = context.node_prepared
    settings = prepared["settings"]
    sources = file_list.files
    if not sources:
        raise FileNotFoundError("输入文件列表为空；请调整 listdir 的目录或过滤规则。")
    if any(source.data_type != "video" for source in sources):
        raise ValueError("输入列表包含非视频文件，请在 listdir 的包含规则中只选择视频。")
    directory = Path(file_list.directory.path).resolve()
    output = (directory / settings["output_folder_name"]).resolve()
    if output == directory or not output.is_relative_to(directory):
        raise ValueError("智能剪辑缓存目录必须位于输入列表所属目录内。")
    script = str(inputs["script"]).strip()
    if not script:
        raise ValueError("文案文本输入为空，无法进行文案对齐剪辑。")
    identifier = str(inputs.get("task_id") or directory.name)
    name = str(inputs.get("task_name") or "")
    job = {"task_id": identifier, "task_name": name, "label": (identifier + " | " + name).strip(" |"),
           "task_dir": str(directory), "script": script,
           "language": str(inputs.get("language") or params.get("language") or ""),
           "sources": [source.path for source in sources]}
    model = None
    if engine.smart_video_jobs_require_model([job], settings):
        from globalValue import globalValue
        context.report("加载字幕模型 " + settings["whisper_model_size"], None)
        model = globalValue.get_whisper_model(settings["whisper_model_size"])
    def progress(message):
        context.check_canceled()
        context.report(_warning_text(message), None)
        context.log(message)
    try:
        bundle = engine.analyze_smart_video_jobs([job], model, settings, progress, context.is_canceled)
    except InterruptedError:
        context.check_canceled()
        raise
    analyzed = apply_policy(bundle["tasks"][0], params.get("policy", "conservative"), context.notes)
    ffmpeg, ffprobe = resolve_tools(context.ffmpeg)
    infos = {source.path: probe(source.path, ffprobe) for source in sources}
    ordered = sorted((clip for clip in analyzed["clips"] if clip.get("included", True)),
                     key=lambda clip: (int(clip.get("export_order", 0)), int(clip.get("source_index", 0))))
    if not ordered:
        raise ValueError("按节点策略处理后没有可导出的片段。")
    first = infos[ordered[0]["source"]]["video"]
    try:
        fps = Fraction(first.get("avg_frame_rate") or first.get("r_frame_rate") or "25/1")
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    if not 1 <= float(fps) <= 240:
        fps = Fraction(25)
        context.notes.append({"kind": "frame_rate_fallback", "detail": "无法确认帧率，使用 25fps。"})
    by_path = {source.path: source for source in sources}
    sections, offset, cues = [], 0.0, []
    for clip in ordered:
        normalized = []
        duration = infos[clip["source"]]["duration"]
        for start, end in clip.get("kept_ranges", []):
            start, end = max(0.0, float(start)), min(duration, float(end))
            if end - start < .08:
                continue
            quantized_end = start + max(1, round((end - start) * float(fps))) / float(fps)
            normalized.append([start, quantized_end])
            sections.append(VideoSection(by_path[clip["source"]], start, quantized_end))
        clip["kept_ranges"] = normalized
        clip_duration = sum(end - start for start, end in normalized)
        if not normalized:
            continue
        if int(clip.get("script_word_start", -1)) < 0:
            cues.extend(_recognized_cues(clip, offset, settings))
            context.notes.append({"kind": "recognized_subtitle_fallback", "file": clip.get("file_name", ""),
                                  "detail": "此片段无法定位原文，字幕采用实际识别文字，不补造缺失文案。"})
        offset += clip_duration
    timeline = VideoTimeline(tuple(sections), (int(first.get("display_width", first["width"])) + 1) // 2 * 2,
                             (int(first.get("display_height", first["height"])) + 1) // 2 * 2, str(fps), file_list.directory.task_key,
                             any(info["audio"] for info in infos.values()))
    timeline.verify()
    cues += engine.build_srt_cues(analyzed, settings)
    final = []
    for item in sorted(cues, key=lambda cue: (cue["start"], cue["end"])):
        start, end = max(0.0, float(item["start"])), min(timeline.duration, float(item["end"]))
        if end > start + .05:
            final.append(SubtitleCue(start, end, str(item["text"])))
    subtitles = SubtitleTrack(tuple(final), file_list.directory.task_key, timeline.duration)
    subtitles.verify()
    context.generated_subtitles = subtitles
    context.processing_summary = {"policy": params.get("policy", "conservative"),
        "model": settings["whisper_model_size"], "source_count": len(sources),
        "input_files": [source.path for source in sources], "input_directory": str(directory),
        "section_count": len(sections), "duration": timeline.duration,
        "subtitle_timing": subtitles.timing,
        "analysis_from_cache": bool(analyzed.get("analysis_from_cache")),
        "cuts": [{"source": section.source.path, "start": section.start, "end": section.end} for section in sections]}
    return {"video": timeline, "subtitles": subtitles}


def edit_node(services):
    return NodeDefinition("smart_video_editor.edit", "智能剪辑 · 自动处理",
        inputs=(NodePort("files", "视频文件列表", "files"), NodePort("script", "文案文本", "text"),
                NodePort("language", "识别语言", "text", required=False), NodePort("task_id", "任务编号（标识）", "text", required=False),
                NodePort("task_name", "任务名称（日志）", "text", required=False)),
        outputs=(NodePort("video", "剪辑后视频", "video"), NodePort("subtitles", "剪辑后字幕", "subtitles")),
        parameters=(NodeParameter("language", "默认识别语言（可留空自动识别）", default="", required=False),
                    NodeParameter("policy", "内容疑点处理", "choice", "conservative",
                        choices=(("conservative", "保守：保留严重不确定内容"), ("automatic", "自动：排除疑似多余片段"),
                                 ("skip", "跳过有内容疑点的任务"))),
                    NodeParameter("model", "字幕模型", "choice", "inherit",
                        choices=(("inherit", "沿用智能剪辑设置"), ("large-v3", "large-v3"), ("medium", "medium"),
                                 ("small", "small"), ("base", "base"))),
                    NodeParameter("pass_threshold", "匹配阈值（0 沿用设置）", "integer", 0, 0, 100),
                    NodeParameter("lead_padding_ms", "向前保留 ms（-1 沿用设置）", "integer", -1, -1, 3000),
                    NodeParameter("tail_padding_ms", "向后保留 ms（-1 沿用设置）", "integer", -1, -1, 3000),
                    NodeParameter("internal_pauses", "句内气口压缩", "choice", "inherit",
                        choices=(("inherit", "沿用智能剪辑设置"), ("on", "开启"), ("off", "关闭"))),
                    NodeParameter("pause_threshold_ms", "句内气口阈值 ms（-1 沿用设置）", "integer", -1, -1, 10000)),
        build=build_edit, prepare=lambda task, params, _host: prepare_edit(task, params, services), execution="compute", version=2,
        mutex="whisper",
        description="必填：视频列表、文案。可选语言会覆盖默认识别语言；任务编号／名称仅用于任务标识与日志，不改变剪辑策略。未连接时编号用目录名，名称留空。支持 Detail 和 Line，只处理连线数据，不隐式读取当前任务；复用缓存，输出剪辑计划与对齐字幕，最后一次编码。")

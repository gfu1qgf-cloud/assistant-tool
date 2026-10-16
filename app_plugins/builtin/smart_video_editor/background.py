"""Host job adapters. Input checks and rendering never touch Qt widgets."""

import re
from pathlib import Path

from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskResultError, directory_resource
from model.TaskDeliveryBackground import version_of
from .engine import (smart_video_task_review_decision, analyze_smart_video_jobs, analyze_breath_cut_files,
                     export_smart_video_bundle, export_breath_cut_bundle, BREATH_CUT_OUTPUT_FOLDER_NAME)


def analysis_resources(phase, settings, jobs, files):
    # Independent task directories may analyze in parallel. Native Whisper
    # work is protected by WhisperInference, not a whole-batch queue lock.
    # Reports/caches for the SAME task must still exclude simultaneous export.
    directories = (Path(job["task_dir"]) / settings.get("output_folder_name", "智能剪辑结果")
                   for job in jobs or [] if job.get("task_dir")) if phase == "analyze" else (
        Path(source).parent / BREATH_CUT_OUTPUT_FOLDER_NAME for source in files or [])
    return tuple(sorted({directory_resource(path) for path in directories}))


def render_inputs(bundle):
    return {str(Path(clip["source"]).resolve()): version_of(clip["source"])
            for task in (bundle or {}).get("tasks", [])
            for clip in task.get("clips", [])
            if clip.get("included", True) and clip.get("source")
            and smart_video_task_review_decision(task) != "skipped"}


def refresh_inputs(jobs):
    return {str(Path(source).resolve()): version_of(source)
            for job in jobs or [] if job.get("force_task_reanalysis")
            for source in job.get("sources", [])}


def render_resources(bundle):
    return tuple(sorted({directory_resource(task["output_dir"])
                  for task in (bundle or {}).get("tasks", []) if task.get("output_dir")
                  and smart_video_task_review_decision(task) != "skipped"}))


def run_smart_video_job(payload, context):
    phase, settings = payload["phase"], payload["settings"]
    context.report("正在准备视频处理", 0)
    for source, version in payload.get("source_versions", {}).items():
        if version_of(source) != version:
            raise RuntimeError(f"排队期间源视频已变化：{Path(source).name}；请刷新片段后重试。")
    model = model_loader = None
    def load_model():
        from globalValue import globalValue
        context.check_canceled()
        name = settings.get("whisper_model_size", "base")
        context.report(f"加载字幕模型 {name}", None)
        return globalValue.get_whisper_model(name)
    if payload.get("load_model"):
        if phase == "export":
            # No model is needed to crop/concatenate video. The shared subtitle
            # helper invokes this loader inside its native-inference guard.
            model_loader = load_model
        elif phase == "analyze":
            model = load_model()
    percent = 0

    def progress(message):
        nonlocal percent
        context.check_canceled()
        # Engine reports task/clip counts, not fake frame-level percentages.
        match = re.search(r"(?:导出任务|\[气口剪辑\] 导出)\s*(\d+)/(\d+)", message)
        if match and int(match[2]):
            percent = max(percent, min(95, int(95 * (int(match[1]) - 1) / int(match[2]))))
        context.report(message, percent)
        context.log(message)

    try:
        if phase == "analyze":
            return analyze_smart_video_jobs(payload["jobs"], model, settings,
                progress=progress, cancelled=context.is_canceled)
        if phase == "breath_analyze":
            return analyze_breath_cut_files(payload["files"], settings,
                progress=progress, cancelled=context.is_canceled)
        if phase == "export":
            result = export_smart_video_bundle(payload["bundle"], settings,
                progress=progress, cancelled=context.is_canceled, alignment_model=model,
                alignment_model_loader=model_loader)
        elif phase == "breath_export":
            result = export_breath_cut_bundle(payload["bundle"], settings,
                progress=progress, cancelled=context.is_canceled)
        else:
            raise ValueError(f"未知的视频处理阶段：{phase}")
    except InterruptedError as error:
        raise BackgroundTaskCanceled(str(error)) from error
    result["paths"] = [str(item["video"]) for item in result["completed"] if item.get("video")]
    if result["failed"]:
        raise BackgroundTaskResultError(
            f"成功 {len(result['completed'])}，暂缓 {len(result['skipped'])}，失败 {len(result['failed'])}；点击结果核对。", result)
    context.report(f"导出完成：成功 {len(result['completed'])}，暂缓 {len(result['skipped'])}", 100)
    return result

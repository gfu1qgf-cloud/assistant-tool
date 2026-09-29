"""Isolated Resolve API worker for the PyQt DaVinci remote.

The GUI launches this module through main.py (also in PyInstaller builds).
Only JSON-compatible data crosses the process boundary; Resolve objects stay
inside this process so a scripting bridge failure cannot crash Qt itself.
"""

import json
import os
import sys
import traceback


RESULT_PREFIX = "__DAVINCI_RESULT__"


def _resolve_module():
    if sys.platform.startswith("win"):
        module_dir = os.path.join(
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            "Blackmagic Design", "DaVinci Resolve", "Support", "Developer",
            "Scripting", "Modules",
        )
    elif sys.platform == "darwin":
        module_dir = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
    else:
        module_dir = "/opt/resolve/Developer/Scripting/Modules"
    if module_dir not in sys.path:
        sys.path.append(module_dir)
    import DaVinciResolveScript  # type: ignore[import-not-found]
    return DaVinciResolveScript


def _current_timeline():
    api = _resolve_module()
    resolve = api.scriptapp("Resolve")
    if not resolve:
        raise RuntimeError(
            "无法连接 DaVinci Resolve。请先启动达芬奇并打开项目；"
            "如果使用免费版，外部脚本连接可能不可用。"
        )
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject() if manager else None
    if not project:
        raise RuntimeError("达芬奇尚未打开项目。")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise RuntimeError("达芬奇尚未打开时间线。")
    return resolve, project, timeline


def _probe(_payload):
    from davinci_legacy import subtitle_review

    _resolve, project, timeline = _current_timeline()
    subtitles = subtitle_review.get_all_subtitle_items(timeline)
    return {
        "project": str(project.GetName() or ""),
        "timeline": str(timeline.GetName() or ""),
        "task_name": __import__("davinci_legacy.batch_export", fromlist=["get_timeline_task_name"])
            .get_timeline_task_name(timeline),
        "subtitle_count": len(subtitles),
        "subtitles": [
            {"index": index, "start": int(item["start"]),
             "original": str(item.get("original_text") or "")}
            for index, item in enumerate(subtitles, 1)
        ],
    }


def _export(payload):
    from davinci_legacy import batch_export

    _resolve, project, timeline = _current_timeline()
    settings = dict(batch_export.DEFAULTS)
    settings.update(payload.get("settings") or {})
    # The old routine displays a Fusion error dialog only if ui is supplied.
    # Run the same preflight here so failure is returned to the Qt interface.
    if settings.get("preflight_alignment", True):
        audio = batch_export.sort_timeline_items(
            timeline.GetItemListInTrack("audio", settings["audio_track"]) or []
        )
        if audio:
            check = batch_export.check_video_audio_alignment(timeline, audio, settings)
            if check["issues"]:
                raise RuntimeError(batch_export.format_preflight_report(settings, check))
            print("导出前音视频对齐检查通过。")
            settings["preflight_alignment"] = False  # Do not scan twice.
    jobs = batch_export.execute_export(project, timeline, settings, ui=None)
    return {"message": "已创建 {} 个渲染任务。".format(len(jobs)), "job_count": len(jobs)}


def _subtitle(payload):
    from davinci_legacy import subtitle_review

    resolve, _project, timeline = _current_timeline()
    subtitles = subtitle_review.get_all_subtitle_items(timeline)
    if not subtitles:
        raise RuntimeError("当前时间线没有字幕。")
    settings = payload.get("settings") or {}
    correct_text = str(settings.get("correct_text") or "").strip()
    if not correct_text:
        raise ValueError("请先输入正确文稿。")
    result = {"message": "", "reviews": [], "ai_review_prompt": ""}

    def capture_review(_resolve, _project, _timeline, _fusion, message, items, prompt):
        result["message"] = str(message)
        result["reviews"] = [
            {key: item.get(key) for key in (
                "index", "start", "original", "review_text", "color_name",
                "severity", "accuracy", "error_ratio", "error_count",
            )}
            for item in items
        ]
        result["ai_review_prompt"] = str(prompt)

    subtitle_review.show_review_dialog = capture_review
    subtitle_review.show_result_dialog = lambda _fusion, _title, message: result.update(
        message=str(message)
    )
    ok = subtitle_review.process_subtitles(
        resolve, resolve.Fusion(), timeline, subtitles, correct_text,
        int(settings.get("pink_threshold", 35)),
        bool(settings.get("clear_previous_marks", True)),
        bool(settings.get("force_first_block", True)),
    )
    if not ok:
        raise RuntimeError(result["message"] or "无法可靠核对当前字幕范围。")
    return result


def _track(payload):
    from davinci_legacy import track_fill

    _resolve, project, timeline = _current_timeline()
    settings = payload.get("settings") or {}
    if settings.get("operation") == "watermark_batch":
        count = track_fill.run_watermark_batch(timeline, project, settings)
        return {"message": "水印铺设完成：{} 个片段。".format(count)}
    if not track_fill.match_media_to_clips(timeline, project, settings):
        raise RuntimeError("轨道铺设未完成，请查看上方执行日志中的 ERROR 说明。")
    return {"message": "轨道铺设完成；请在时间线上核对结果。"}


def _jump(payload):
    from davinci_legacy import subtitle_review

    resolve, project, timeline = _current_timeline()
    frame = int(payload.get("frame"))
    timecode = subtitle_review.timeline_frame_to_timecode(project, timeline, frame)
    resolve.OpenPage("edit")
    if not timeline.SetCurrentTimecode(timecode):
        raise RuntimeError("达芬奇未接受时间码 {}。".format(timecode))
    return {"message": "已定位到 {}（帧 {}）。".format(timecode, frame)}


def _fusion_preview(payload):
    from davinci_legacy import fusion_batch

    api = _resolve_module()
    resolve, _project, timeline = _current_timeline()
    return fusion_batch.preview(api, resolve, timeline, str(payload.get("text") or ""),
                                int(payload.get("track") or 0))


def _fusion_apply(payload):
    from davinci_legacy import fusion_batch

    api = _resolve_module()
    resolve, _project, timeline = _current_timeline()
    return fusion_batch.apply(api, resolve, timeline, payload)


_ACTIONS = {
    "probe": _probe,
    "export": _export,
    "subtitle": _subtitle,
    "track": _track,
    "jump": _jump,
    "fusion_preview": _fusion_preview,
    "fusion_apply": _fusion_apply,
}


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    action = args[0] if args else ""
    try:
        if action not in _ACTIONS:
            raise ValueError("未知达芬奇命令：{}".format(action))
        payload = json.load(sys.stdin)
        result = _ACTIONS[action](payload)
        print(RESULT_PREFIX + json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return 0
    except BaseException as exc:
        traceback.print_exc()
        print(RESULT_PREFIX + json.dumps({
            "ok": False, "error": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

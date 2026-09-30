#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import traceback

from davinci_legacy.timeline_naming import get_timeline_task_name


if sys.platform.startswith("win"):
    SCRIPT_MODULE_PATH = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"
elif sys.platform == "darwin":
    SCRIPT_MODULE_PATH = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
else:
    SCRIPT_MODULE_PATH = "/opt/resolve/Developer/Scripting/Modules"

if SCRIPT_MODULE_PATH not in sys.path:
    sys.path.append(SCRIPT_MODULE_PATH)

import DaVinciResolveScript as bmd


DEFAULTS = {
    "task_name": "",
    "category": "reels",
    "begin_index": 1,
    "audio_track": 2,
    "video_track": 1,
    "preflight_alignment": True,
    "render_preset": "MyExportSetting",
    "output_root": os.path.expanduser("~/Desktop/任务"),
    "name_source": "timeline",
    "include_index_in_name": False,
    "auto_start_render": False,
}


INVALID_NAME_CHARS = r'<>:"/\\|?*\x00-\x1f'
PREFLIGHT_ERROR_COLOR = "Orange"
PREFLIGHT_ERROR_COLOR_LABEL = "橙色"


def sanitize_name(value, fallback="Untitled"):
    text = str(value or "").strip()
    text = re.sub("[{}]".format(INVALID_NAME_CHARS), "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:120] or fallback


def parse_int(value, default_value, minimum=None):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default_value
    if minimum is not None and parsed < minimum:
        return default_value
    return parsed


def get_resolve_objects():
    resolve = bmd.scriptapp("Resolve")
    if not resolve:
        raise RuntimeError("无法连接 DaVinci Resolve。")

    project_manager = resolve.GetProjectManager()
    if not project_manager:
        raise RuntimeError("无法获取 Project Manager。")

    project = project_manager.GetCurrentProject()
    if not project:
        raise RuntimeError("当前没有打开项目。")

    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise RuntimeError("当前没有打开时间线。")

    fusion = resolve.Fusion()
    if not fusion:
        raise RuntimeError("无法获取 Fusion 对象。")

    ui = fusion.UIManager
    dispatcher = bmd.UIDispatcher(ui)
    return resolve, project, timeline, ui, dispatcher


def bool_value(item, default=False):
    try:
        return bool(item.Checked)
    except Exception:
        return bool(default)


def text_value(item, default=""):
    try:
        return str(item.Text).strip()
    except Exception:
        return str(default)


def build_dialog(ui, dispatcher, timeline):
    result = {"settings": None}
    default_task_name = get_timeline_task_name(timeline)

    win = dispatcher.AddWindow(
        {
            "ID": "BatchExportWin",
            "WindowTitle": "DaVinci 批量导出工具",
            "Geometry": [420, 180, 900, 640],
            "MinimumSize": [900, 640],
        },
        [
            ui.VGroup(
                {"Spacing": 6, "Margin": 8},
                [
                    ui.Label({"Text": "按音频轨片段范围批量创建渲染任务", "FontSize": 12}),
                    ui.HGroup(
                        {"Spacing": 8},
                        [
                            ui.Label({"Text": "任务名", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "task_name", "Text": default_task_name, "PlaceholderText": "使用当前时间线名称"}),
                        ],
                    ),
                    ui.HGroup(
                        {"Spacing": 8},
                        [
                            ui.Label({"Text": "输出根目录", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "output_root", "Text": DEFAULTS["output_root"]}),
                        ],
                    ),
                    ui.HGroup(
                        {"Spacing": 8},
                        [
                            ui.Label({"Text": "渲染预设", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "render_preset", "Text": DEFAULTS["render_preset"]}),
                        ],
                    ),
                    ui.HGroup(
                        {"Spacing": 8},
                        [
                            ui.Label({"Text": "音频轨", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "audio_track", "Text": str(DEFAULTS["audio_track"]), "MinimumSize": [90, 24]}),
                            ui.Label({"Text": "视频轨", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "video_track", "Text": str(DEFAULTS["video_track"]), "MinimumSize": [90, 24]}),
                            ui.Label({"Text": "起始编号", "MinimumSize": [90, 24]}),
                            ui.LineEdit({"ID": "begin_index", "Text": str(DEFAULTS["begin_index"]), "MinimumSize": [90, 24]}),
                        ],
                    ),
                    ui.Label({"Text": "导出分类"}),
                    ui.HGroup(
                        {"Spacing": 22},
                        [
                            ui.CheckBox({"ID": "category_koubo", "Text": "口播", "Checked": DEFAULTS["category"] == "口播"}),
                            ui.CheckBox({"ID": "category_reels", "Text": "reels", "Checked": DEFAULTS["category"] == "reels"}),
                        ],
                    ),
                    ui.Label({"Text": "输出文件名"}),
                    ui.HGroup(
                        {"Spacing": 22},
                        [
                            ui.CheckBox({"ID": "name_timeline", "Text": "使用时间线名", "Checked": True}),
                            ui.CheckBox({"ID": "name_clip", "Text": "使用音频片段名", "Checked": False}),
                        ],
                    ),
                    ui.CheckBox({"ID": "include_index", "Text": "文件名前加编号", "Checked": False}),
                    ui.CheckBox({"ID": "preflight_alignment", "Text": "导出前检查视频与背景音乐是否逐帧对齐（问题片段标橙色）", "Checked": True}),
                    ui.CheckBox({"ID": "auto_start", "Text": "创建任务后自动开始渲染", "Checked": False}),
                    ui.Label({"Text": "输出结构: 根目录 / 任务名 / 分类 / 编号"}),
                    ui.Label({"Text": "提示: 禁用的音频片段会占用编号，但不会创建渲染任务。"}),
                    ui.HGroup(
                        {"Spacing": 12},
                        [
                            ui.Button({"ID": "ok_btn", "Text": "创建任务", "MinimumSize": [112, 34]}),
                            ui.Button({"ID": "cancel_btn", "Text": "取消", "MinimumSize": [96, 34]}),
                        ],
                    ),
                ],
            )
        ],
    )

    task_name = win.Find("task_name")
    output_root = win.Find("output_root")
    render_preset = win.Find("render_preset")
    audio_track = win.Find("audio_track")
    video_track = win.Find("video_track")
    begin_index = win.Find("begin_index")
    category_koubo = win.Find("category_koubo")
    category_reels = win.Find("category_reels")
    name_timeline = win.Find("name_timeline")
    name_clip = win.Find("name_clip")
    include_index = win.Find("include_index")
    preflight_alignment = win.Find("preflight_alignment")
    auto_start = win.Find("auto_start")

    def close_window(_event=None):
        result["settings"] = None
        dispatcher.ExitLoop()

    def set_category_koubo(_event=None):
        category_koubo.Checked = True
        category_reels.Checked = False

    def set_category_reels(_event=None):
        category_koubo.Checked = False
        category_reels.Checked = True

    def set_name_timeline(_event=None):
        name_timeline.Checked = True
        name_clip.Checked = False

    def set_name_clip(_event=None):
        name_timeline.Checked = False
        name_clip.Checked = True

    def ok(_event=None):
        preset = text_value(render_preset, DEFAULTS["render_preset"]) or DEFAULTS["render_preset"]
        root = os.path.expanduser(text_value(output_root, DEFAULTS["output_root"]) or DEFAULTS["output_root"])
        category = "口播" if bool_value(category_koubo, DEFAULTS["category"] == "口播") else "reels"
        name_source = "timeline" if bool_value(name_timeline, True) else "clip"

        result["settings"] = {
            "task_name": text_value(task_name, default_task_name),
            "category": category,
            "begin_index": parse_int(text_value(begin_index, DEFAULTS["begin_index"]), DEFAULTS["begin_index"], minimum=1),
            "audio_track": parse_int(text_value(audio_track, DEFAULTS["audio_track"]), DEFAULTS["audio_track"], minimum=1),
            "video_track": parse_int(text_value(video_track, DEFAULTS["video_track"]), DEFAULTS["video_track"], minimum=1),
            "preflight_alignment": bool_value(preflight_alignment, True),
            "render_preset": preset,
            "output_root": root,
            "name_source": name_source,
            "include_index_in_name": bool_value(include_index, False),
            "auto_start_render": bool_value(auto_start, False),
        }
        dispatcher.ExitLoop()

    win.On.ok_btn.Clicked = ok
    win.On.cancel_btn.Clicked = close_window
    win.On.category_koubo.Clicked = set_category_koubo
    win.On.category_reels.Clicked = set_category_reels
    win.On.name_timeline.Clicked = set_name_timeline
    win.On.name_clip.Clicked = set_name_clip
    win.On.BatchExportWin.Close = close_window
    win.On.Close = close_window

    win.Show()
    dispatcher.RunLoop()
    win.Hide()
    return result["settings"]


def build_dialog_compact(ui, dispatcher, timeline):
    result = {"settings": None}
    default_task_name = get_timeline_task_name(timeline)

    window = dispatcher.AddWindow(
        {
            "ID": "BatchExportWin",
            "WindowTitle": "DaVinci Batch Export",
            "Geometry": [320, 220, 720, 430],
            "Spacing": 8,
            "Margin": 12,
        },
        ui.VGroup(
            [
                ui.Label({"Text": "按音频轨片段范围创建渲染任务"}),
                ui.HGroup(
                    [
                        ui.Label({"Text": "任务", "MinimumSize": [70, 24]}),
                        ui.LineEdit({"ID": "TaskName", "Text": default_task_name, "PlaceholderText": "使用当前时间线名称", "MinimumSize": [300, 24]}),
                        ui.Label({"Text": "编号", "MinimumSize": [50, 24]}),
                        ui.LineEdit({"ID": "BeginIndex", "Text": str(DEFAULTS["begin_index"]), "MinimumSize": [60, 24]}),
                    ]
                ),
                ui.HGroup(
                    [
                        ui.Label({"Text": "根目录", "MinimumSize": [70, 24]}),
                        ui.LineEdit({"ID": "OutputRoot", "Text": DEFAULTS["output_root"], "MinimumSize": [520, 24]}),
                    ]
                ),
                ui.HGroup(
                    [
                        ui.Label({"Text": "预设", "MinimumSize": [70, 24]}),
                        ui.LineEdit({"ID": "RenderPreset", "Text": DEFAULTS["render_preset"], "MinimumSize": [240, 24]}),
                        ui.Label({"Text": "音轨", "MinimumSize": [42, 24]}),
                        ui.LineEdit({"ID": "AudioTrack", "Text": str(DEFAULTS["audio_track"]), "MinimumSize": [48, 24]}),
                        ui.Label({"Text": "视频轨", "MinimumSize": [54, 24]}),
                        ui.LineEdit({"ID": "VideoTrack", "Text": str(DEFAULTS["video_track"]), "MinimumSize": [48, 24]}),
                    ]
                ),
                ui.HGroup(
                    [
                        ui.Label({"Text": "分类", "MinimumSize": [70, 24]}),
                        ui.CheckBox({"ID": "CategoryKoubo", "Text": "口播", "Checked": DEFAULTS["category"] == "口播"}),
                        ui.CheckBox({"ID": "CategoryReels", "Text": "reels", "Checked": DEFAULTS["category"] == "reels"}),
                    ]
                ),
                ui.HGroup(
                    [
                        ui.Label({"Text": "命名", "MinimumSize": [70, 24]}),
                        ui.CheckBox({"ID": "NameTimeline", "Text": "时间线名", "Checked": True}),
                        ui.CheckBox({"ID": "NameClip", "Text": "音频片段名", "Checked": False}),
                        ui.CheckBox({"ID": "IncludeIndex", "Text": "加编号", "Checked": False}),
                    ]
                ),
                ui.HGroup(
                    [
                        ui.CheckBox({"ID": "PreflightAlignment", "Text": "导出前检查对齐（错位标橙色）", "Checked": True}),
                        ui.CheckBox({"ID": "AutoStart", "Text": "创建后自动开始渲染", "Checked": False}),
                        ui.Label({"Text": ""}),
                    ]
                ),
                ui.Label({"Text": "输出结构: 根目录 / 任务 / 分类 / 编号"}),
                ui.Label({"Text": "提示: 禁用的音频片段会占用编号，但不会创建渲染任务。"}),
                ui.HGroup(
                    [
                        ui.Label({"Text": ""}),
                        ui.Button({"ID": "CancelButton", "Text": "取消", "MinimumSize": [90, 28]}),
                        ui.Button({"ID": "RunButton", "Text": "创建任务", "MinimumSize": [100, 28]}),
                    ]
                ),
            ]
        ),
    )

    items = window.GetItems()
    category_koubo = items["CategoryKoubo"]
    category_reels = items["CategoryReels"]
    name_timeline = items["NameTimeline"]
    name_clip = items["NameClip"]

    def close_window(_event=None):
        result["settings"] = None
        dispatcher.ExitLoop()

    def set_category_koubo(_event=None):
        category_koubo.Checked = True
        category_reels.Checked = False

    def set_category_reels(_event=None):
        category_koubo.Checked = False
        category_reels.Checked = True

    def set_name_timeline(_event=None):
        name_timeline.Checked = True
        name_clip.Checked = False

    def set_name_clip(_event=None):
        name_timeline.Checked = False
        name_clip.Checked = True

    def ok(_event=None):
        preset = text_value(items["RenderPreset"], DEFAULTS["render_preset"]) or DEFAULTS["render_preset"]
        root = os.path.expanduser(text_value(items["OutputRoot"], DEFAULTS["output_root"]) or DEFAULTS["output_root"])
        category = "口播" if bool_value(category_koubo, DEFAULTS["category"] == "口播") else "reels"
        name_source = "timeline" if bool_value(name_timeline, True) else "clip"

        result["settings"] = {
            "task_name": text_value(items["TaskName"], default_task_name),
            "category": category,
            "begin_index": parse_int(text_value(items["BeginIndex"], DEFAULTS["begin_index"]), DEFAULTS["begin_index"], minimum=1),
            "audio_track": parse_int(text_value(items["AudioTrack"], DEFAULTS["audio_track"]), DEFAULTS["audio_track"], minimum=1),
            "video_track": parse_int(text_value(items["VideoTrack"], DEFAULTS["video_track"]), DEFAULTS["video_track"], minimum=1),
            "preflight_alignment": bool_value(items["PreflightAlignment"], True),
            "render_preset": preset,
            "output_root": root,
            "name_source": name_source,
            "include_index_in_name": bool_value(items["IncludeIndex"], False),
            "auto_start_render": bool_value(items["AutoStart"], False),
        }
        dispatcher.ExitLoop()

    window.On.BatchExportWin.Close = close_window
    window.On.CancelButton.Clicked = close_window
    window.On.RunButton.Clicked = ok
    window.On.CategoryKoubo.Clicked = set_category_koubo
    window.On.CategoryReels.Clicked = set_category_reels
    window.On.NameTimeline.Clicked = set_name_timeline
    window.On.NameClip.Clicked = set_name_clip

    window.Show()
    dispatcher.RunLoop()
    window.Hide()
    return result["settings"]


def make_output_dir(settings, timeline_name, index_number):
    task_name = sanitize_name(settings["task_name"], sanitize_name(timeline_name, "Task"))
    category = sanitize_name(settings["category"], "export")
    return os.path.join(settings["output_root"], task_name, category, "{:02d}".format(index_number))


def make_custom_name(settings, timeline_name, clip_name, index_number):
    if settings["name_source"] == "clip":
        base = sanitize_name(clip_name, "clip")
    else:
        base = sanitize_name(timeline_name, "timeline")

    if settings["include_index_in_name"]:
        return "{:02d}_{}".format(index_number, base)
    return base


def is_clip_enabled(clip):
    """Read TimelineItem enabled state, treating an unavailable API as enabled."""
    try:
        enabled = clip.GetClipEnabled()
    except Exception as exc:
        print("警告: 无法读取片段禁用状态，将按启用处理: {}".format(exc))
        return True

    if enabled is None:
        print("警告: 片段禁用状态为空，将按启用处理。")
        return True
    return bool(enabled)


def get_clip_span(clip):
    start = int(round(float(clip.GetStart())))
    end = int(round(float(clip.GetEnd())))
    return start, end


def get_clip_name(clip):
    try:
        return str(clip.GetName() or "未命名片段")
    except Exception:
        return "未命名片段"


def sort_timeline_items(items):
    def sort_key(clip):
        try:
            start, end = get_clip_span(clip)
            return start, end, get_clip_name(clip)
        except Exception:
            return sys.maxsize, sys.maxsize, get_clip_name(clip)

    return sorted(list(items or []), key=sort_key)


def ranges_overlap(first_start, first_end, second_start, second_end):
    return first_start < second_end and second_start < first_end


def build_contiguous_video_blocks(video_entries):
    """Merge touching/overlapping V-track edits into complete picture regions."""
    blocks = []
    for entry in sorted(video_entries, key=lambda item: (item["start"], item["end"])):
        if not blocks or entry["start"] > blocks[-1]["end"]:
            blocks.append(
                {
                    "start": entry["start"],
                    "end": entry["end"],
                    "clips": [entry],
                }
            )
            continue

        block = blocks[-1]
        block["end"] = max(block["end"], entry["end"])
        block["clips"].append(entry)
    return blocks


def mark_preflight_clips(clips):
    marked = 0
    failed = 0
    seen = set()
    for clip in clips:
        clip_identity = id(clip)
        if clip_identity in seen:
            continue
        seen.add(clip_identity)
        try:
            if clip.SetClipColor(PREFLIGHT_ERROR_COLOR):
                marked += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return marked, failed


def check_video_audio_alignment(timeline, audio_items, settings):
    """Return every alignment problem before any render job is created."""
    issues = []
    clips_to_mark = []
    active_audio = []
    disabled_audio = []

    for position, clip in enumerate(audio_items, start=1):
        index_number = settings["begin_index"] + position - 1
        entry = {
            "clip": clip,
            "name": get_clip_name(clip),
            "index": index_number,
            "enabled": is_clip_enabled(clip),
        }
        try:
            entry["start"], entry["end"] = get_clip_span(clip)
        except Exception as exc:
            if entry["enabled"]:
                issues.append(
                    "编号 {:02d}：无法读取音频片段 '{}' 的帧范围（{}）。".format(
                        index_number, entry["name"], exc
                    )
                )
                clips_to_mark.append(clip)
            continue

        if entry["end"] <= entry["start"]:
            if entry["enabled"]:
                issues.append(
                    "编号 {:02d}：音频片段 '{}' 的范围无效（{} - {}）。".format(
                        index_number, entry["name"], entry["start"], entry["end"]
                    )
                )
                clips_to_mark.append(clip)
            continue

        if entry["enabled"]:
            active_audio.append(entry)
        else:
            disabled_audio.append(entry)

    raw_video_items = sort_timeline_items(
        timeline.GetItemListInTrack("video", settings["video_track"]) or []
    )
    video_entries = []
    for clip in raw_video_items:
        entry = {
            "clip": clip,
            "name": get_clip_name(clip),
            "enabled": is_clip_enabled(clip),
        }
        try:
            entry["start"], entry["end"] = get_clip_span(clip)
        except Exception as exc:
            issues.append(
                "视频片段 '{}' 无法读取帧范围（{}）。".format(entry["name"], exc)
            )
            clips_to_mark.append(clip)
            continue
        if entry["end"] <= entry["start"]:
            issues.append(
                "视频片段 '{}' 的范围无效（{} - {}）。".format(
                    entry["name"], entry["start"], entry["end"]
                )
            )
            clips_to_mark.append(clip)
            continue
        video_entries.append(entry)

    video_blocks = build_contiguous_video_blocks(video_entries)
    used_blocks = set()

    for audio in active_audio:
        exact_block_index = None
        for block_index, block in enumerate(video_blocks):
            if block_index in used_blocks:
                continue
            if block["start"] == audio["start"] and block["end"] == audio["end"]:
                exact_block_index = block_index
                break

        if exact_block_index is not None:
            used_blocks.add(exact_block_index)
            exact_block = video_blocks[exact_block_index]
            disabled_video = [
                item for item in exact_block["clips"] if not item["enabled"]
            ]
            if not disabled_video:
                continue

            issues.append(
                "编号 {:02d}：A{} 与 V{} 边界一致（{} - {}），但画面区间包含 {} 个已禁用视频片段。".format(
                    audio["index"],
                    settings["audio_track"],
                    settings["video_track"],
                    audio["start"],
                    audio["end"],
                    len(disabled_video),
                )
            )
            clips_to_mark.append(audio["clip"])
            clips_to_mark.extend(item["clip"] for item in exact_block["clips"])
            continue

        overlapping_blocks = [
            block
            for block in video_blocks
            if ranges_overlap(
                audio["start"], audio["end"], block["start"], block["end"]
            )
        ]
        if overlapping_blocks:
            picture_ranges = "、".join(
                "{} - {}".format(block["start"], block["end"])
                for block in overlapping_blocks
            )
        else:
            picture_ranges = "该音频范围内没有画面"

        issues.append(
            "编号 {:02d}：A{} 音频为 {} - {}，V{} 画面为 {}，起止帧没有完全对齐。".format(
                audio["index"],
                settings["audio_track"],
                audio["start"],
                audio["end"],
                settings["video_track"],
                picture_ranges,
            )
        )
        clips_to_mark.append(audio["clip"])
        for block in overlapping_blocks:
            clips_to_mark.extend(item["clip"] for item in block["clips"])

    for block_index, block in enumerate(video_blocks):
        if block_index in used_blocks:
            continue
        overlaps_disabled_task = any(
            ranges_overlap(
                block["start"], block["end"], audio["start"], audio["end"]
            )
            for audio in disabled_audio
        )
        if overlaps_disabled_task:
            continue
        overlaps_reported_audio = any(
            ranges_overlap(
                block["start"], block["end"], audio["start"], audio["end"]
            )
            for audio in active_audio
        )
        if overlaps_reported_audio:
            continue

        issues.append(
            "V{} 画面区间 {} - {} 没有对应的启用 A{} 音频片段。".format(
                settings["video_track"],
                block["start"],
                block["end"],
                settings["audio_track"],
            )
        )
        clips_to_mark.extend(item["clip"] for item in block["clips"])

    marked, mark_failed = mark_preflight_clips(clips_to_mark)
    return {
        "issues": issues,
        "marked": marked,
        "mark_failed": mark_failed,
        "active_audio_count": len(active_audio),
        "video_block_count": len(video_blocks),
    }


def format_preflight_report(settings, result):
    lines = [
        "导出前预检查未通过，整个导出流程已经停止。",
        "本次没有创建任何渲染任务，也没有创建输出目录。",
        "",
        "检查范围：V{} 画面区间 <-> A{} 背景音乐片段".format(
            settings["video_track"], settings["audio_track"]
        ),
        "启用音频：{} 个；连续画面区间：{} 个；发现问题：{} 个。".format(
            result["active_audio_count"],
            result["video_block_count"],
            len(result["issues"]),
        ),
        "问题片段已标为{}：成功 {} 个，失败 {} 个。".format(
            PREFLIGHT_ERROR_COLOR_LABEL,
            result["marked"],
            result["mark_failed"],
        ),
        "",
        "问题明细：",
    ]
    lines.extend(
        "{}. {}".format(index, issue)
        for index, issue in enumerate(result["issues"], start=1)
    )
    lines.extend(["", "请修正时间线后重新运行脚本。"])
    return "\n".join(lines)


def show_message_dialog(ui, title, message):
    dispatcher = bmd.UIDispatcher(ui)
    window = dispatcher.AddWindow(
        {
            "ID": "BatchExportMessageWin",
            "WindowTitle": title,
            "Geometry": [420, 220, 760, 460],
        },
        ui.VGroup(
            {"Spacing": 8, "Margin": 10},
            [
                ui.TextEdit(
                    {
                        "ID": "MessageText",
                        "PlainText": message,
                        "ReadOnly": True,
                        "Weight": 1,
                    }
                ),
                ui.HGroup(
                    [
                        ui.Label({"Text": ""}),
                        ui.Button({"ID": "MessageOk", "Text": "知道了，去检查", "MinimumSize": [130, 30]}),
                    ]
                ),
            ],
        ),
    )

    def close_message(_event=None):
        dispatcher.ExitLoop()

    window.On.BatchExportMessageWin.Close = close_message
    window.On.MessageOk.Clicked = close_message
    window.Show()
    dispatcher.RunLoop()
    window.Hide()


def execute_export(project, timeline, settings, ui=None):
    audio_items = sort_timeline_items(
        timeline.GetItemListInTrack("audio", settings["audio_track"])
    )
    if not audio_items:
        raise RuntimeError("音频轨道 {} 中没有任何片段。".format(settings["audio_track"]))

    if settings.get("preflight_alignment", True):
        preflight_result = check_video_audio_alignment(timeline, audio_items, settings)
        if preflight_result["issues"]:
            report = format_preflight_report(settings, preflight_result)
            print(report)
            if ui is not None:
                try:
                    show_message_dialog(ui, "导出已停止：发现片段未对齐", report)
                except Exception as exc:
                    print("警告: 无法显示预检查信息框: {}".format(exc))
            return []
        print(
            "预检查通过: A{} 的 {} 个启用音频片段与 V{} 的 {} 个连续画面区间完全对齐。".format(
                settings["audio_track"],
                preflight_result["active_audio_count"],
                settings["video_track"],
                preflight_result["video_block_count"],
            )
        )

    if not project.LoadRenderPreset(settings["render_preset"]):
        raise RuntimeError("找不到渲染预设: {}".format(settings["render_preset"]))

    timeline_name = timeline.GetName()
    print("发现 {} 个音频片段，开始创建渲染任务。".format(len(audio_items)))

    job_ids = []
    disabled_count = 0
    invalid_count = 0
    last_index_number = settings["begin_index"] - 1
    for idx, clip in enumerate(audio_items, start=1):
        index_number = idx + settings["begin_index"] - 1
        last_index_number = index_number
        clip_name = clip.GetName()

        # 禁用片段仍占用当前编号，但不产生空目录或渲染任务。
        if not is_clip_enabled(clip):
            disabled_count += 1
            print(
                "跳过已禁用片段: 编号 {:02d} / {} "
                "(保留文件夹编号，不创建渲染任务)".format(
                    index_number, clip_name
                )
            )
            continue

        start = int(clip.GetStart())
        end = int(clip.GetEnd())
        if end <= start:
            invalid_count += 1
            print(
                "跳过无效片段: 编号 {:02d} / {} ({} - {})".format(
                    index_number, clip_name, start, end
                )
            )
            continue

        output_dir = make_output_dir(settings, timeline_name, index_number)
        custom_name = make_custom_name(settings, timeline_name, clip_name, index_number)
        os.makedirs(output_dir, exist_ok=True)

        ok = project.SetRenderSettings(
            {
                "SelectAllFrames": False,
                "MarkIn": start,
                "MarkOut": end,
                "TargetDir": output_dir,
                "CustomName": custom_name,
            }
        )
        if not ok:
            raise RuntimeError("设置渲染参数失败: {} ({} - {})".format(custom_name, start, end))

        job_id = project.AddRenderJob()
        if not job_id:
            raise RuntimeError("添加渲染任务失败: {} ({} - {})".format(custom_name, start, end))

        job_ids.append(job_id)
        print("已添加任务: {} ({} - {}) -> {}".format(custom_name, start, end, output_dir))

    print(
        "任务统计: 已添加 {} 个，禁用跳过 {} 个，无效跳过 {} 个，"
        "编号已推进到 {:02d}。".format(
            len(job_ids), disabled_count, invalid_count, last_index_number
        )
    )

    if not job_ids:
        if disabled_count or invalid_count:
            print("没有创建渲染任务；所有片段均已跳过，编号顺序仍已保留。")
            return []
        raise RuntimeError("没有创建任何渲染任务。")

    if settings["auto_start_render"]:
        if not project.StartRendering(job_ids, True):
            raise RuntimeError("渲染任务已创建，但自动开始渲染失败。")
        print("已开始渲染 {} 个任务。".format(len(job_ids)))
    else:
        print("全部任务已加入渲染队列，请在 Deliver 页面手动开始渲染。")

    return job_ids


def main():
    try:
        _resolve, project, timeline, ui, dispatcher = get_resolve_objects()
        settings = build_dialog_compact(ui, dispatcher, timeline)
        if not settings:
            print("已取消。")
            return
        execute_export(project, timeline, settings, ui=ui)
    except Exception:
        print("批量导出失败:")
        print(traceback.format_exc())


if __name__ == "__main__":
    main()

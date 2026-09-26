#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DaVinci Resolve Track Fill Helper

Read clip blocks from an audio or video reference track, then fill those
blocks with the first media item placed on a selected audio or video track.
"""

import os
import sys
import traceback


try:
    if sys.platform.startswith("win"):
        RESOLVE_SCRIPT_PATH = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"
    elif sys.platform == "darwin":
        RESOLVE_SCRIPT_PATH = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
    else:
        RESOLVE_SCRIPT_PATH = "/opt/resolve/Developer/Scripting/Modules"

    if RESOLVE_SCRIPT_PATH not in sys.path:
        sys.path.append(RESOLVE_SCRIPT_PATH)
    import DaVinciResolveScript as dvr_script
except Exception as exc:
    print("ERROR: Cannot import DaVinci Resolve API: {}".format(exc))
    sys.exit(1)


MODES = [
    (
        "original_helper",
        "原版 DaVinciMusicHelper.py（推荐）",
        "原版铺设逻辑，当前推荐模式。",
    ),
    (
        "precise_continuous",
        "2号脚本：精确换算循环",
        "2号换算逻辑，可能不稳定，仅作备用。",
    ),
    (
        "fast_continuous",
        "快速连续循环",
        "按素材帧数快速循环。",
    ),
    (
        "restart_each_block",
        "每个区域从头开始",
        "每个连续区域都从模板开头铺设。",
    ),
]


SOURCE_TYPES = [
    (
        "audio",
        "音频轨道",
        "读取所选音频轨道上的片段区间。",
    ),
    (
        "video",
        "视频轨道",
        "读取所选视频轨道上的片段区间；视频空隙不会铺设。",
    ),
]


TARGET_TYPE_DESCRIPTIONS = {
    "audio": "使用目标音频轨的第一个片段作为模板，只铺设音频。视频参照模式下默认 V1 → A1。",
    "video": "使用目标视频轨的第一个片段作为模板，只铺设画面。默认使用 V2，避免覆盖参照 V1。",
}


DEFAULT_SETTINGS = {
    "operation": "track_fill",
    "mode": "original_helper",
    "source_type": "audio",
    "source_track": 1,
    "target_type": "audio",
    "target_track": 2,
    "merge_tolerance": 5,
    "mark_loop_start": True,
    "clear_target_track": True,
    "media_file_path": "",
}


WATERMARK_DEFAULTS = {
    "source_track": 1,
    "start_track": 0,
    "tolerance": 5,
    "clear_tracks": True,
    "continuous_source": False,
    "main": True,
    "suno": False,
    "whatsapp": False,
    "lzx_r": False,
    "xh_c": False,
    "custom_names": "",
    "template_tracks": "",
}


PRESET_WATERMARKS = [
    ("main", "版权水印"),
    ("suno", "版权水印 suno"),
    ("whatsapp", "MX306-XH"),
    ("lzx_r", "LZX-R"),
    ("xh_c", "XH-C"),
]


CORRECTION_CLIP_COLOR = "Purple"


def get_resolve_objects():
    resolve = dvr_script.scriptapp("Resolve")
    if not resolve:
        return None, None, None
    project_manager = resolve.GetProjectManager()
    if not project_manager:
        return resolve, None, None
    project = project_manager.GetCurrentProject()
    if not project:
        return resolve, None, None
    timeline = project.GetCurrentTimeline()
    return resolve, project, timeline


def mode_label(mode_key):
    for key, label, _description in MODES:
        if key == mode_key:
            return label
    return mode_key


def source_type_label(source_type):
    for key, label, _description in SOURCE_TYPES:
        if key == source_type:
            return label
    return source_type


def track_id(track_type, track_index):
    prefix = "V" if track_type == "video" else "A"
    return "{}{}".format(prefix, track_index)


def parse_positive_int(value, default_value, minimum=0):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default_value
    if parsed < minimum:
        return default_value
    return parsed


def parse_string_list(value):
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        normalized = str(value or "")
        for separator in ("，", "、", "；", ";", "\r", "\n"):
            normalized = normalized.replace(separator, ",")
        raw_values = normalized.split(",")

    result = []
    seen = set()
    for value_item in raw_values:
        item = str(value_item).strip()
        key = item.lower()
        if item and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def parse_track_list(value):
    tracks = []
    seen = set()
    for token in parse_string_list(value):
        values = []
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start, end = int(parts[0].strip()), int(parts[1].strip())
            except Exception:
                continue
            if start > 0 and end >= start and end - start <= 63:
                values = list(range(start, end + 1))
        else:
            try:
                values = [int(token)]
            except Exception:
                continue
        for track in values:
            if track > 0 and track not in seen:
                tracks.append(track)
                seen.add(track)
    return tracks


def get_fusion_ui(resolve):
    fusion_app = globals().get("fusion")
    if not fusion_app and resolve:
        try:
            fusion_app = resolve.Fusion()
        except Exception:
            fusion_app = None
    if not fusion_app:
        try:
            fusion_app = dvr_script.scriptapp("Fusion")
        except Exception:
            fusion_app = None
    if not fusion_app:
        return None, None

    ui = getattr(fusion_app, "UIManager", None)
    dispatcher_factory = getattr(dvr_script, "UIDispatcher", None)
    if not dispatcher_factory:
        bmd_module = globals().get("bmd")
        dispatcher_factory = getattr(bmd_module, "UIDispatcher", None) if bmd_module else None
    if not ui or not dispatcher_factory:
        return None, None
    return ui, dispatcher_factory(ui)


def read_line_edit(item, default_value):
    try:
        return str(item.Text)
    except Exception:
        return str(default_value)


def read_checkbox(item, default_value):
    try:
        return bool(item.Checked)
    except Exception:
        return bool(default_value)


def show_settings_dialog(defaults, resolve=None):
    ui, dispatcher = get_fusion_ui(resolve)
    if not ui or not dispatcher:
        print("WARNING: Fusion UIManager is unavailable. Using default settings.")
        return dict(defaults)

    mode_labels = [item[1] for item in MODES]
    mode_keys = [item[0] for item in MODES]
    default_mode_index = mode_keys.index(defaults["mode"]) if defaults["mode"] in mode_keys else 0
    track_type_labels = [item[1] for item in SOURCE_TYPES]
    track_type_keys = [item[0] for item in SOURCE_TYPES]
    default_source_type_index = track_type_keys.index(defaults.get("source_type", "audio"))
    default_target_type = defaults.get("target_type", "audio")
    default_target_type_index = track_type_keys.index(default_target_type)
    result = {"settings": None}

    track_fill_page = ui.VGroup(
        {"Spacing": 3},
        [
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "铺设模式", "MinimumSize": [80, 24], "Weight": 0}),
                    ui.ComboBox({"ID": "ModeCombo", "MinimumSize": [0, 24], "Weight": 1}),
                ],
            ),
            ui.Label({
                "ID": "ModeDescription",
                "Text": MODES[default_mode_index][2],
                "WordWrap": True,
                "MinimumSize": [0, 24],
                "Weight": 0,
            }),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "参照轨道", "MinimumSize": [80, 24], "Weight": 0}),
                    ui.ComboBox({"ID": "SourceTypeCombo", "MinimumSize": [0, 24], "Weight": 1}),
                    ui.Label({"Text": "编号", "MinimumSize": [34, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "SourceTrack", "Text": str(defaults["source_track"]), "MinimumSize": [48, 24], "Weight": 0}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "铺设轨道", "MinimumSize": [80, 24], "Weight": 0}),
                    ui.ComboBox({"ID": "TargetTypeCombo", "MinimumSize": [0, 24], "Weight": 1}),
                    ui.Label({"Text": "编号", "MinimumSize": [34, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "TargetTrack", "Text": str(defaults["target_track"]), "MinimumSize": [48, 24], "Weight": 0}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "合并容差(帧)", "MinimumSize": [96, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "Tolerance", "Text": str(defaults["merge_tolerance"]), "MinimumSize": [48, 24], "Weight": 0}),
                    ui.Label({"Text": "相邻片段间隔不超过该值时视为连续。", "Weight": 1}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "素材文件", "MinimumSize": [80, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "MediaPath", "Text": defaults["media_file_path"], "MinimumSize": [0, 24], "Weight": 1}),
                ],
            ),
            ui.Label({
                "Text": "文件留空时使用铺设轨道的第一个片段；试铺成功后才会清空目标轨。",
                "WordWrap": True,
                "MinimumSize": [0, 24],
                "Weight": 0,
            }),
            ui.CheckBox({"ID": "ClearTrack", "Text": "铺设前清空目标轨道", "Checked": defaults["clear_target_track"], "Weight": 0}),
            ui.CheckBox({"ID": "MarkLoopStart", "Text": "每次模板从头循环时标粉色", "Checked": defaults["mark_loop_start"], "Weight": 0}),
            ui.Label({"Text": "", "Weight": 1}),
        ],
    )

    watermark_page = ui.VGroup(
        {"Spacing": 3},
        [
            ui.Label({
                "Text": "根据视频轨道区间批量铺设多个水印，每个水印使用一条独立视频轨。",
                "WordWrap": True,
                "MinimumSize": [0, 24],
                "Weight": 0,
            }),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "参照视频轨", "MinimumSize": [74, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "WaterSourceTrack", "Text": str(WATERMARK_DEFAULTS["source_track"]), "MinimumSize": [48, 24], "Weight": 0}),
                    ui.Label({"Text": "起始水印轨", "MinimumSize": [74, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "WaterStartTrack", "Text": "", "PlaceholderText": "自动", "MinimumSize": [64, 24], "Weight": 0}),
                    ui.Label({"Text": "容差(帧)", "MinimumSize": [58, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "WaterTolerance", "Text": str(WATERMARK_DEFAULTS["tolerance"]), "MinimumSize": [48, 24], "Weight": 0}),
                    ui.Label({"Text": "", "Weight": 1}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.CheckBox({"ID": "MainWatermark", "Text": "版权水印", "Checked": WATERMARK_DEFAULTS["main"], "Weight": 0}),
                    ui.CheckBox({"ID": "SunoWatermark", "Text": "版权水印 suno", "Checked": WATERMARK_DEFAULTS["suno"], "Weight": 0}),
                    ui.CheckBox({"ID": "WhatsappWatermark", "Text": "MX306-XH", "Checked": WATERMARK_DEFAULTS["whatsapp"], "Weight": 0}),
                    ui.CheckBox({"ID": "LzxWatermark", "Text": "LZX-R", "Checked": WATERMARK_DEFAULTS["lzx_r"], "Weight": 0}),
                    ui.CheckBox({"ID": "XhWatermark", "Text": "XH-C", "Checked": WATERMARK_DEFAULTS["xh_c"], "Weight": 0}),
                    ui.Label({"Text": "", "Weight": 1}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "媒体池素材名", "MinimumSize": [86, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "CustomNames", "Text": WATERMARK_DEFAULTS["custom_names"], "PlaceholderText": "多个名称用逗号分隔", "MinimumSize": [0, 24], "Weight": 1}),
                ],
            ),
            ui.HGroup(
                {"Weight": 0, "Spacing": 6},
                [
                    ui.Label({"Text": "模板视频轨", "MinimumSize": [86, 24], "Weight": 0}),
                    ui.LineEdit({"ID": "TemplateTracks", "Text": WATERMARK_DEFAULTS["template_tracks"], "PlaceholderText": "例如 2,3,4：取各轨首片段并在原轨重铺", "MinimumSize": [0, 24], "Weight": 1}),
                ],
            ),
            ui.CheckBox({"ID": "ClearWaterTracks", "Text": "添加前清空目标水印轨", "Checked": WATERMARK_DEFAULTS["clear_tracks"], "Weight": 0}),
            ui.CheckBox({"ID": "ContinuousWatermark", "Text": "跨区间连续读取模板素材；关闭时每个区间从头开始", "Checked": WATERMARK_DEFAULTS["continuous_source"], "Weight": 0}),
            ui.Label({
                "Text": "起始轨留空时，媒体池水印会自动添加到当前最高视频轨上方；模板轨始终在原轨重铺。",
                "WordWrap": True,
                "MinimumSize": [0, 32],
                "Weight": 0,
            }),
            ui.Label({"Text": "", "Weight": 1}),
        ],
    )

    window = dispatcher.AddWindow(
        {
            "ID": "TrackFillHelperWin",
            "WindowTitle": "DaVinci 轨道铺设与水印助手",
            "Geometry": [240, 100, 550, 400],
        },
        ui.VGroup(
            {"Spacing": 4, "Margin": 6},
            [
                ui.TabBar({"ID": "MainTabs", "MinimumSize": [0, 28], "Weight": 0}),
                ui.Stack({"ID": "MainStack", "Weight": 1}, [track_fill_page, watermark_page]),
                ui.HGroup(
                    {"Weight": 0, "Spacing": 6},
                    [
                        ui.Label({"Text": "", "Weight": 1}),
                        ui.Button({"ID": "CancelButton", "Text": "取消", "MinimumSize": [80, 28], "Weight": 0}),
                        ui.Button({"ID": "RunButton", "Text": "开始铺设", "MinimumSize": [100, 28], "Weight": 0}),
                    ],
                ),
            ],
        ),
    )

    items = window.GetItems()
    items["MainTabs"].AddTab("轨道铺设")
    items["MainTabs"].AddTab("批量水印")
    items["MainTabs"].CurrentIndex = 0
    items["MainStack"].CurrentIndex = 0

    for label in mode_labels:
        items["ModeCombo"].AddItem(label)
    items["ModeCombo"].CurrentIndex = default_mode_index
    for label in track_type_labels:
        items["SourceTypeCombo"].AddItem(label)
        items["TargetTypeCombo"].AddItem(label)
    items["SourceTypeCombo"].CurrentIndex = default_source_type_index
    items["TargetTypeCombo"].CurrentIndex = default_target_type_index

    def combo_index(item_id, default_index, item_count):
        try:
            index = int(items[item_id].CurrentIndex)
        except Exception:
            index = default_index
        return index if 0 <= index < item_count else default_index

    def selected_mode_index():
        return combo_index("ModeCombo", default_mode_index, len(MODES))

    def selected_source_type_index():
        return combo_index("SourceTypeCombo", default_source_type_index, len(SOURCE_TYPES))

    def selected_target_type_index():
        return combo_index("TargetTypeCombo", default_target_type_index, len(SOURCE_TYPES))

    def update_mode(_event=None):
        items["ModeDescription"].Text = MODES[selected_mode_index()][2]

    def update_source_type(_event=None):
        index = selected_source_type_index()
        source_type = SOURCE_TYPES[index][0]
        target_type = SOURCE_TYPES[selected_target_type_index()][0]
        items["SourceTrack"].Text = "1"
        if target_type == "audio":
            items["TargetTrack"].Text = "1" if source_type == "video" else "2"
        elif read_line_edit(items["TargetTrack"], "2").strip() == "1":
            items["TargetTrack"].Text = "2"

    def update_target_type(_event=None):
        target_type = SOURCE_TYPES[selected_target_type_index()][0]
        source_type = SOURCE_TYPES[selected_source_type_index()][0]
        items["TargetTrack"].Text = "2" if target_type == "video" else ("1" if source_type == "video" else "2")

    def switch_page(event=None):
        try:
            index = int(event.get("Index", items["MainTabs"].CurrentIndex)) if event else int(items["MainTabs"].CurrentIndex)
        except Exception:
            index = 0
        index = 1 if index == 1 else 0
        items["MainStack"].CurrentIndex = index
        items["RunButton"].Text = "添加水印" if index == 1 else "开始铺设"

    def close_window(_event=None):
        result["settings"] = None
        dispatcher.ExitLoop()

    def run(_event=None):
        try:
            operation_index = int(items["MainTabs"].CurrentIndex)
        except Exception:
            operation_index = 0

        if operation_index == 1:
            start_text = read_line_edit(items["WaterStartTrack"], "").strip()
            result["settings"] = {
                "operation": "watermark_batch",
                "source_track": parse_positive_int(read_line_edit(items["WaterSourceTrack"], 1), 1, minimum=1),
                "start_track": parse_positive_int(start_text, 0, minimum=1) if start_text else 0,
                "tolerance": parse_positive_int(read_line_edit(items["WaterTolerance"], 5), 5, minimum=0),
                "clear_tracks": read_checkbox(items["ClearWaterTracks"], True),
                "continuous_source": read_checkbox(items["ContinuousWatermark"], False),
                "main": read_checkbox(items["MainWatermark"], True),
                "suno": read_checkbox(items["SunoWatermark"], False),
                "whatsapp": read_checkbox(items["WhatsappWatermark"], False),
                "lzx_r": read_checkbox(items["LzxWatermark"], False),
                "xh_c": read_checkbox(items["XhWatermark"], False),
                "custom_names": parse_string_list(read_line_edit(items["CustomNames"], "")),
                "template_tracks": parse_track_list(read_line_edit(items["TemplateTracks"], "")),
            }
            dispatcher.ExitLoop()
            return

        source_type = SOURCE_TYPES[selected_source_type_index()][0]
        target_type = SOURCE_TYPES[selected_target_type_index()][0]
        source_track = parse_positive_int(read_line_edit(items["SourceTrack"], defaults["source_track"]), defaults["source_track"], minimum=1)
        target_track = parse_positive_int(read_line_edit(items["TargetTrack"], defaults["target_track"]), defaults["target_track"], minimum=1)
        if source_type == target_type and source_track == target_track:
            print("ERROR: Reference track and target track cannot be the same.")
            return
        path = read_line_edit(items["MediaPath"], "").strip().strip('"')
        if path and not os.path.exists(path):
            print("ERROR: Media file path does not exist: {}".format(path))
            return
        result["settings"] = {
            "operation": "track_fill",
            "mode": MODES[selected_mode_index()][0],
            "source_type": source_type,
            "source_track": source_track,
            "target_type": target_type,
            "target_track": target_track,
            "merge_tolerance": parse_positive_int(read_line_edit(items["Tolerance"], defaults["merge_tolerance"]), defaults["merge_tolerance"], minimum=0),
            "mark_loop_start": read_checkbox(items["MarkLoopStart"], defaults["mark_loop_start"]),
            "clear_target_track": read_checkbox(items["ClearTrack"], defaults["clear_target_track"]),
            "media_file_path": path,
        }
        dispatcher.ExitLoop()

    window.On.TrackFillHelperWin.Close = close_window
    window.On.CancelButton.Clicked = close_window
    window.On.RunButton.Clicked = run
    window.On.ModeCombo.CurrentIndexChanged = update_mode
    window.On.SourceTypeCombo.CurrentIndexChanged = update_source_type
    window.On.TargetTypeCombo.CurrentIndexChanged = update_target_type
    window.On.MainTabs.CurrentChanged = switch_page

    window.Show()
    dispatcher.RunLoop()
    window.Hide()
    return result["settings"]


def get_timeline_fps(project):
    try:
        return float(project.GetSetting("timelineFrameRate"))
    except Exception:
        return 24.0


def parse_timecode_duration(duration_text, fps):
    if not duration_text or ":" not in duration_text:
        return 0
    try:
        h, m, s, f = [int(part) for part in str(duration_text).split(":")[:4]]
    except Exception:
        return 0
    return int((h * 3600 + m * 60 + s) * fps + f)


def get_clip_duration_frames(media_pool_item, project_fps):
    props = media_pool_item.GetClipProperty() or {}
    for key in ("Frames", "Frame Count"):
        value = str(props.get(key, "")).strip()
        if value.isdigit() and int(value) > 0:
            return int(value)

    duration_frames = parse_timecode_duration(props.get("Duration", ""), project_fps)
    if duration_frames > 0:
        return duration_frames
    return 0


def get_media_source_fps(media_pool_item, fallback_fps):
    props = media_pool_item.GetClipProperty() or {}
    for key in ("Shot Frame Rate", "Frame Rate", "FPS"):
        raw = str(props.get(key, "")).strip()
        if not raw:
            continue
        try:
            fps = float(raw)
        except Exception:
            continue
        if fps > 0:
            return fps
    return fallback_fps


def merge_connected_clips(clips_info, tolerance=5):
    if not clips_info:
        return []

    normalized = []
    for clip in clips_info:
        try:
            start = int(clip["start"])
            duration = int(clip["duration"])
        except Exception:
            continue
        if duration <= 0:
            continue
        normalized.append(
            {
                "start": start,
                "duration": duration,
                "end_pos": start + duration,
                "name": clip.get("name", ""),
            }
        )

    if not normalized:
        return []

    normalized.sort(key=lambda item: item["start"])
    merged = [normalized[0].copy()]
    for clip in normalized[1:]:
        current = merged[-1]
        if clip["start"] <= current["end_pos"] + tolerance:
            current["end_pos"] = max(current["end_pos"], clip["end_pos"])
            current["duration"] = current["end_pos"] - current["start"]
        else:
            merged.append(clip.copy())
    return merged


def get_source_blocks(timeline, source_type, source_track, tolerance):
    source_clips = timeline.GetItemListInTrack(source_type, source_track)
    if not source_clips:
        return []
    raw_clips = [
        {"start": clip.GetStart(), "duration": clip.GetDuration(), "name": clip.GetName()}
        for clip in source_clips
    ]
    return merge_connected_clips(raw_clips, tolerance=tolerance)


def media_type_code(track_type):
    return 1 if track_type == "video" else 2


def get_fill_source(timeline, project, target_type, target_track, media_file_path):
    media_pool = project.GetMediaPool()
    if media_file_path:
        imported = media_pool.ImportMedia([media_file_path])
        if imported:
            return imported[0]
        return None

    existing = timeline.GetItemListInTrack(target_type, target_track)
    if existing:
        return existing[0].GetMediaPoolItem()
    return None


def delete_track_items(timeline, track_type, track_index):
    existing_clips = timeline.GetItemListInTrack(track_type, track_index)
    if not existing_clips:
        return True
    try:
        return bool(timeline.DeleteClips(existing_clips))
    except Exception:
        return False


def append_segment(
    media_pool,
    fill_source,
    target_type,
    target_track,
    source_start,
    source_frames,
    record_frame,
):
    if source_frames <= 0:
        return None
    clip_info = {
        "mediaPoolItem": fill_source,
        "startFrame": int(source_start),
        # Tested watermark and audio media use this as the end boundary.
        # Using "- 1" makes both targets one frame short; the exact append
        # helper below measures and calibrates media with different behavior.
        "endFrame": int(source_start + source_frames),
        "mediaType": media_type_code(target_type),
        "trackIndex": int(target_track),
        "recordFrame": int(record_frame),
    }
    result = media_pool.AppendToTimeline([clip_info])
    if result and len(result) > 0:
        return result[0]
    return None


def mark_clip_if_needed(clip, should_mark):
    if not clip or not should_mark:
        return
    try:
        clip.SetClipColor("Pink")
    except Exception:
        pass


def mark_clip_for_manual_correction(clip):
    if not clip:
        return
    try:
        clip.SetClipColor(CORRECTION_CLIP_COLOR)
    except Exception:
        pass


def timeline_item_duration(clip):
    try:
        duration = clip.GetDuration()
        if duration is not None:
            return int(round(float(duration)))
    except Exception:
        pass
    try:
        return int(round(float(clip.GetEnd()) - float(clip.GetStart())))
    except Exception:
        return 0


def remove_created_clips(timeline, clips):
    if not clips:
        return True
    try:
        return bool(timeline.DeleteClips(clips, False))
    except Exception:
        return False


def append_exact_segment(
    timeline,
    media_pool,
    fill_source,
    target_type,
    target_track,
    source_start,
    source_frames,
    expected_timeline_frames,
    record_frame,
    max_source_frames=None,
    max_attempts=12,
):
    """Append a segment and calibrate its source boundary to the exact duration."""
    expected = int(expected_timeline_frames)
    requested = int(source_frames)
    if expected < 1 or requested < 1:
        raise RuntimeError("Exact append requires a positive frame duration.")

    maximum = int(max_source_frames) if max_source_frames else None
    candidate = min(requested, maximum) if maximum else requested
    tried = set()
    measurements = []
    best_approximation = None

    for _attempt in range(max_attempts):
        candidate = max(1, candidate)
        if maximum:
            candidate = min(candidate, maximum)

        if candidate in tried:
            replacement = None
            search_limit = max(max_attempts, 4)
            for distance in range(1, search_limit + 1):
                for value in (candidate + distance, candidate - distance):
                    if value < 1 or value in tried:
                        continue
                    if maximum and value > maximum:
                        continue
                    replacement = value
                    break
                if replacement is not None:
                    break
            if replacement is None:
                break
            candidate = replacement

        tried.add(candidate)
        clip = append_segment(
            media_pool,
            fill_source,
            target_type,
            target_track,
            source_start,
            candidate,
            record_frame,
        )
        if not clip:
            measurements.append((candidate, None))
            candidate += 1
            continue

        actual = timeline_item_duration(clip)
        measurements.append((candidate, actual))
        if actual == expected:
            if len(measurements) > 1:
                print(
                    "Frame boundary calibrated at timeline frame {}: source span {} -> "
                    "timeline duration {} ({} attempt(s)).".format(
                        record_frame, candidate, actual, len(measurements)
                    )
                )
            return clip, candidate, actual, True

        if actual > 0:
            approximation_score = (
                abs(actual - expected),
                0 if actual < expected else 1,
            )
            if (
                best_approximation is None
                or approximation_score < best_approximation[0]
            ):
                best_approximation = (
                    approximation_score,
                    candidate,
                    actual,
                )

        if not remove_created_clips(timeline, [clip]):
            if actual > 0:
                mark_clip_for_manual_correction(clip)
                print(
                    "WARNING: A frame-calibration trial clip could not be removed at "
                    "timeline frame {}. It was kept at {} frame(s) and marked {} for "
                    "manual correction.".format(
                        record_frame, actual, CORRECTION_CLIP_COLOR
                    )
                )
                return clip, candidate, actual, False
            raise RuntimeError(
                "Cannot remove an unusable frame-calibration trial clip at timeline "
                "frame {}. Previously added clips were kept.".format(record_frame)
            )

        error = expected - actual
        source_per_timeline = requested / float(expected)
        correction = int(round(error * source_per_timeline))
        if correction == 0:
            correction = 1 if error > 0 else -1
        candidate += correction

    details = ", ".join(
        "{}->{}".format(source_count, actual if actual is not None else "failed")
        for source_count, actual in measurements
    )

    if best_approximation is not None:
        _score, best_source_frames, measured_duration = best_approximation
        fallback_clip = append_segment(
            media_pool,
            fill_source,
            target_type,
            target_track,
            source_start,
            best_source_frames,
            record_frame,
        )
        if fallback_clip:
            fallback_duration = timeline_item_duration(fallback_clip)
            if fallback_duration > 0:
                mark_clip_for_manual_correction(fallback_clip)
                print(
                    "WARNING: Exact frame calibration was not possible at timeline frame {}. "
                    "Kept the closest segment: target={}, actual={}, source span={}. "
                    "The clip was marked {} for manual correction. Attempts: {}".format(
                        record_frame,
                        expected,
                        fallback_duration,
                        best_source_frames,
                        CORRECTION_CLIP_COLOR,
                        details,
                    )
                )
                return (
                    fallback_clip,
                    best_source_frames,
                    fallback_duration,
                    False,
                )

    raise RuntimeError(
        "Resolve could not create any usable {}-frame segment at timeline frame {} "
        "after automatic boundary calibration (source span -> actual: {}). "
        "Previously added clips were kept.".format(
            expected, record_frame, details or "no valid attempt"
        )
    )


def get_timeline_end_frame(timeline):
    try:
        return int(timeline.GetEndFrame())
    except Exception:
        return 0


def probe_fill_media(timeline, project, fill_source, target_type, target_track):
    """Verify the requested media-only append before deleting target clips."""
    media_pool = project.GetMediaPool()
    record_frame = max(get_timeline_end_frame(timeline) + 1000, 1000)
    test_clips = media_pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": fill_source,
                "mediaType": media_type_code(target_type),
                "trackIndex": int(target_track),
                "recordFrame": int(record_frame),
            }
        ]
    )
    if not test_clips:
        return 0

    try:
        duration = int(test_clips[0].GetDuration() or 0)
    except Exception:
        duration = 0

    try:
        deleted = bool(timeline.DeleteClips(test_clips))
    except Exception:
        deleted = False
    if not deleted:
        print("ERROR: Cannot remove the test clip. Existing target track was not cleared.")
        return 0
    return duration


def measure_fill_duration_on_timeline(
    timeline, project, fill_source, target_type, target_track
):
    media_pool = project.GetMediaPool()
    record_frame = max(get_timeline_end_frame(timeline) + 1000, 1000)
    test_clip = media_pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": fill_source,
                "mediaType": media_type_code(target_type),
                "trackIndex": int(target_track),
                "recordFrame": int(record_frame),
            }
        ]
    )
    if not test_clip:
        return 0

    timeline_clip = test_clip[0]
    try:
        duration = int(timeline_clip.GetDuration())
    except Exception:
        duration = 0

    timeline.DeleteClips([timeline_clip])
    return duration


def fill_fast_mode(timeline, project, blocks, bgm_source, settings):
    media_pool = project.GetMediaPool()
    fps = get_timeline_fps(project)
    bgm_length = settings.get("measured_source_length", 0) or get_clip_duration_frames(bgm_source, fps)
    if bgm_length < 1:
        print("ERROR: Cannot read template duration.")
        return 0

    total_added = 0
    bgm_cursor = 0
    restart_each_block = settings["mode"] == "restart_each_block"

    for block_index, block in enumerate(blocks, start=1):
        timeline_pos = int(block["start"])
        remaining = int(block["duration"])
        if restart_each_block:
            bgm_cursor = 0

        print("Block {}: start={}, duration={}".format(block_index, timeline_pos, remaining))
        while remaining > 0:
            if bgm_cursor >= bgm_length:
                bgm_cursor = 0

            available = bgm_length - bgm_cursor
            take_frames = min(remaining, available)
            if take_frames < 1:
                break

            try:
                clip, used_source_frames, _actual_duration, is_exact = append_exact_segment(
                    timeline,
                    media_pool,
                    bgm_source,
                    settings["target_type"],
                    settings["target_track"],
                    bgm_cursor,
                    take_frames,
                    take_frames,
                    timeline_pos,
                    max_source_frames=available,
                )
            except RuntimeError:
                raise

            if is_exact:
                mark_clip_if_needed(clip, settings["mark_loop_start"] and bgm_cursor == 0)
            timeline_pos += take_frames
            remaining -= take_frames
            bgm_cursor += used_source_frames
            total_added += 1

        if remaining != 0:
            raise RuntimeError(
                "Block {} is still short by {} frame(s).".format(
                    block_index, remaining
                )
            )

    return total_added


def fill_original_helper_mode(timeline, project, blocks, bgm_source, settings):
    """Preserve the behavior of the original DaVinciMusicHelper.py."""
    media_pool = project.GetMediaPool()
    fps = get_timeline_fps(project)
    bgm_length = settings.get("measured_source_length", 0) or get_clip_duration_frames(bgm_source, fps)
    if bgm_length < 10:
        print("ERROR: Template duration looks invalid: {} frames.".format(bgm_length))
        return 0

    total_added = 0
    bgm_cursor = 0

    for block_index, block in enumerate(blocks, start=1):
        timeline_pos = int(block["start"])
        remaining = int(block["duration"])
        print("Block {}: start={}, duration={}".format(block_index, timeline_pos, remaining))

        while remaining > 0:
            if bgm_cursor >= bgm_length:
                bgm_cursor = 0

            # Preserve the original helper rule: restart if this block would
            # cross the end of the template media.
            if bgm_cursor + remaining >= bgm_length:
                bgm_cursor = 0

            available = bgm_length - bgm_cursor
            take_frames = min(remaining, available)
            if take_frames < 1:
                break

            try:
                clip, used_source_frames, _actual_duration, is_exact = append_exact_segment(
                    timeline,
                    media_pool,
                    bgm_source,
                    settings["target_type"],
                    settings["target_track"],
                    bgm_cursor,
                    take_frames,
                    take_frames,
                    timeline_pos,
                    max_source_frames=available,
                )
            except RuntimeError:
                raise

            if is_exact:
                mark_clip_if_needed(clip, settings["mark_loop_start"] and bgm_cursor == 0)
            timeline_pos += take_frames
            remaining -= take_frames
            bgm_cursor += used_source_frames
            total_added += 1

        if remaining != 0:
            raise RuntimeError(
                "Block {} is still short by {} frame(s).".format(
                    block_index, remaining
                )
            )

    return total_added


def fill_precise_mode(timeline, project, blocks, bgm_source, settings):
    media_pool = project.GetMediaPool()
    timeline_fps = get_timeline_fps(project)
    source_fps = get_media_source_fps(bgm_source, timeline_fps)
    fps_ratio = source_fps / timeline_fps if timeline_fps else 1.0

    bgm_timeline_length = settings.get("measured_source_length", 0)
    if bgm_timeline_length < 1:
        bgm_timeline_length = measure_fill_duration_on_timeline(
            timeline,
            project,
            bgm_source,
            settings["target_type"],
            settings["target_track"],
        )
    if bgm_timeline_length < 1:
        print("ERROR: Cannot measure template timeline duration.")
        return 0

    bgm_source_length = int(bgm_timeline_length * fps_ratio + 0.5)
    if bgm_source_length < 1:
        print("ERROR: Template source duration is invalid.")
        return 0

    print("Timeline FPS: {}".format(timeline_fps))
    print("Template source FPS: {}".format(source_fps))
    print("Template timeline length: {} frames".format(bgm_timeline_length))
    print("Template source length: {} frames".format(bgm_source_length))

    total_added = 0
    source_cursor = 0

    for block_index, block in enumerate(blocks, start=1):
        block_start = int(block["start"])
        block_end = int(block["end_pos"])
        pos = block_start
        clip_count = 0

        print("Block {}: start={}, duration={}".format(block_index, block_start, block["duration"]))
        while pos < block_end:
            if source_cursor >= bgm_source_length:
                source_cursor = 0

            remaining_timeline = block_end - pos
            remaining_source = bgm_source_length - source_cursor
            source_timeline_capacity = max(
                1, int(remaining_source / fps_ratio + 0.5)
            )
            expected_duration = min(
                remaining_timeline, source_timeline_capacity
            )
            use_source_frames = int(expected_duration * fps_ratio + 0.5)
            use_source_frames = min(use_source_frames, remaining_source)
            if use_source_frames < 1:
                use_source_frames = 1

            try:
                clip, used_source_frames, _actual_duration, is_exact = append_exact_segment(
                    timeline,
                    media_pool,
                    bgm_source,
                    settings["target_type"],
                    settings["target_track"],
                    source_cursor,
                    use_source_frames,
                    expected_duration,
                    pos,
                    max_source_frames=remaining_source,
                )
            except RuntimeError:
                raise

            if is_exact:
                mark_clip_if_needed(clip, settings["mark_loop_start"] and source_cursor == 0)
            pos += expected_duration
            source_cursor += used_source_frames
            clip_count += 1
            total_added += 1

            if clip_count > 1000:
                raise RuntimeError(
                    "Safety limit reached in block {} before exact alignment.".format(
                        block_index
                    )
                )

        if pos != block_end:
            raise RuntimeError(
                "Block {} ended at frame {} instead of {}.".format(
                    block_index, pos, block_end
                )
            )

    return total_added


def match_media_to_clips(timeline, project, settings):
    source_type = settings.get("source_type", "audio")
    target_type = settings.get("target_type", "audio")
    target_track = settings.get("target_track", 2)
    print("\n" + "=" * 60)
    print("DaVinci Track Fill Helper")
    print("Mode: {}".format(mode_label(settings["mode"])))
    print("Reference: {}".format(track_id(source_type, settings["source_track"])))
    print("Target: {}".format(track_id(target_type, target_track)))
    print("=" * 60)

    if source_type == target_type and settings["source_track"] == target_track:
        print("ERROR: Reference track and target track cannot be the same.")
        return False

    try:
        source_track_count = int(timeline.GetTrackCount(source_type) or 0)
    except Exception:
        source_track_count = None
    if source_track_count is not None and settings["source_track"] > source_track_count:
        print(
            "ERROR: {} {} does not exist. Current timeline has {} {} track(s).".format(
                source_type_label(source_type),
                settings["source_track"],
                source_track_count,
                source_type_label(source_type),
            )
        )
        return False

    try:
        target_track_count = int(timeline.GetTrackCount(target_type) or 0)
    except Exception:
        target_track_count = None
    if target_track_count is not None and target_track > target_track_count:
        print(
            "ERROR: Target {} does not exist. Current timeline has {} {} track(s).".format(
                track_id(target_type, target_track),
                target_track_count,
                source_type_label(target_type),
            )
        )
        return False

    blocks = get_source_blocks(
        timeline,
        source_type,
        settings["source_track"],
        settings["merge_tolerance"],
    )
    if not blocks:
        print(
            "ERROR: Source {} {} is empty.".format(
                source_type_label(source_type), settings["source_track"]
            )
        )
        return False
    print("Detected {} continuous {} block(s).".format(len(blocks), source_type))

    media_file_path = settings.get("media_file_path", "")
    fill_source = get_fill_source(
        timeline,
        project,
        target_type,
        target_track,
        media_file_path,
    )
    if not fill_source:
        target_items = timeline.GetItemListInTrack(target_type, target_track) or []
        if media_file_path:
            print("ERROR: Cannot import the selected media file: {}".format(media_file_path))
        elif target_items:
            print(
                "ERROR: The first clip on {} has no MediaPoolItem. Resolve cannot duplicate "
                "some timeline-only titles, generators, effects, or adjustment clips through "
                "the public scripting API.".format(track_id(target_type, target_track))
            )
        else:
            print(
                "ERROR: {} is empty. Place one template clip on it or select a media file.".format(
                    track_id(target_type, target_track)
                )
            )
        return False
    print("Template media: {}".format(fill_source.GetName()))

    measured_source_length = probe_fill_media(
        timeline,
        project,
        fill_source,
        target_type,
        target_track,
    )
    if measured_source_length < 1:
        print(
            "ERROR: The template on {} cannot be appended as {}. The target track was not "
            "cleared.".format(track_id(target_type, target_track), target_type)
        )
        return False
    print("Template probe succeeded: {} timeline frames.".format(measured_source_length))

    runtime_settings = dict(settings)
    runtime_settings["target_type"] = target_type
    runtime_settings["target_track"] = target_track
    runtime_settings["measured_source_length"] = measured_source_length

    if settings.get("clear_target_track", True):
        if not delete_track_items(timeline, target_type, target_track):
            print(
                "ERROR: Cannot clear {}. Check whether the track is locked.".format(
                    track_id(target_type, target_track)
                )
            )
            return False

    if settings["mode"] == "original_helper":
        total_added = fill_original_helper_mode(timeline, project, blocks, fill_source, runtime_settings)
    elif settings["mode"] == "precise_continuous":
        total_added = fill_precise_mode(timeline, project, blocks, fill_source, runtime_settings)
    else:
        total_added = fill_fast_mode(timeline, project, blocks, fill_source, runtime_settings)

    print("Done. Added {} clip(s) to {}.".format(total_added, track_id(target_type, target_track)))
    print("=" * 60 + "\n")
    return total_added > 0


def get_highest_video_track(timeline, max_tracks=64):
    highest = 0
    try:
        track_count = int(timeline.GetTrackCount("video") or 0)
    except Exception:
        track_count = max_tracks
    for index in range(1, max(track_count, 0) + 1):
        try:
            clips = timeline.GetItemListInTrack("video", index)
        except Exception:
            clips = None
        if clips:
            highest = index
    return highest


def ensure_video_track(timeline, track_index):
    try:
        current = int(timeline.GetTrackCount("video") or 0)
    except Exception:
        current = 0
    while current < track_index:
        if not timeline.AddTrack("video"):
            raise RuntimeError("无法创建视频轨 V{}。".format(current + 1))
        current += 1


def iter_folder_clips(folder):
    try:
        clips = folder.GetClipList() or []
    except Exception:
        clips = []
    for clip in clips:
        yield clip

    try:
        subfolders = folder.GetSubFolderList() or []
    except Exception:
        subfolders = []
    for subfolder in subfolders:
        for clip in iter_folder_clips(subfolder):
            yield clip


def find_media_pool_item(project, name):
    media_pool = project.GetMediaPool()
    root_folder = media_pool.GetRootFolder()
    needle = str(name).strip().lower()
    partial_match = None
    for clip in iter_folder_clips(root_folder):
        clip_name = str(clip.GetName())
        lower_name = clip_name.lower()
        if lower_name == needle:
            return clip
        if partial_match is None and needle in lower_name:
            partial_match = clip
    return partial_match


def selected_watermark_names(settings):
    names = []
    seen = set()
    for key, name in PRESET_WATERMARKS:
        if settings.get(key) and name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())
    for name in parse_string_list(settings.get("custom_names", [])):
        if name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())
    return names


def collect_watermark_specs(timeline, project, settings):
    source_track = int(settings["source_track"])
    template_tracks = parse_track_list(settings.get("template_tracks", []))
    names = selected_watermark_names(settings)
    if not template_tracks and not names:
        raise RuntimeError("没有选择水印，也没有填写模板视频轨。")

    specs = []
    occupied_tracks = set()
    for track_index in template_tracks:
        if track_index == source_track:
            raise RuntimeError("模板轨 V{} 不能与参照轨相同。".format(track_index))
        clips = timeline.GetItemListInTrack("video", track_index) or []
        if not clips:
            raise RuntimeError("模板轨 V{} 为空，请先放入一个水印片段。".format(track_index))
        media_item = clips[0].GetMediaPoolItem()
        if not media_item:
            raise RuntimeError(
                "模板轨 V{} 的首片段没有 MediaPoolItem；纯标题、生成器或调整片段无法通过公共 API 复制。".format(
                    track_index
                )
            )
        specs.append(
            {
                "label": "V{} / {}".format(track_index, media_item.GetName()),
                "media_item": media_item,
                "target_track": track_index,
                "measured_length": 0,
            }
        )
        occupied_tracks.add(track_index)

    start_track = int(settings.get("start_track", 0) or 0)
    if names and start_track <= 0:
        start_track = get_highest_video_track(timeline) + 1

    missing_names = []
    for offset, name in enumerate(names):
        target_track = start_track + offset
        if target_track == source_track:
            raise RuntimeError("水印目标轨 V{} 不能与参照轨相同。".format(target_track))
        if target_track in occupied_tracks:
            raise RuntimeError("水印目标轨 V{} 与模板轨重复，请调整起始轨。".format(target_track))
        media_item = find_media_pool_item(project, name)
        if not media_item:
            missing_names.append(name)
            continue
        specs.append(
            {
                "label": str(name),
                "media_item": media_item,
                "target_track": target_track,
                "measured_length": 0,
            }
        )
        occupied_tracks.add(target_track)

    if missing_names:
        raise RuntimeError("媒体池中未找到水印素材: {}".format("、".join(missing_names)))
    return specs


def run_watermark_batch(timeline, project, settings):
    source_track = int(settings["source_track"])
    blocks = get_source_blocks(
        timeline,
        "video",
        source_track,
        int(settings.get("tolerance", 5)),
    )
    if not blocks:
        raise RuntimeError("参照视频轨 V{} 没有可用片段。".format(source_track))

    specs = collect_watermark_specs(timeline, project, settings)
    for spec in specs:
        ensure_video_track(timeline, spec["target_track"])

    print("识别到 {} 个视频区域，准备铺设 {} 个水印。".format(len(blocks), len(specs)))
    for spec in specs:
        measured_length = probe_fill_media(
            timeline,
            project,
            spec["media_item"],
            "video",
            spec["target_track"],
        )
        if measured_length < 1:
            raise RuntimeError(
                "水印 '{}' 无法作为视频追加到 V{}；任何目标轨都尚未清空。".format(
                    spec["label"], spec["target_track"]
                )
            )
        spec["measured_length"] = measured_length

    total_added = 0
    for spec in specs:
        target_track = spec["target_track"]
        if settings.get("clear_tracks", True):
            if not delete_track_items(timeline, "video", target_track):
                raise RuntimeError("无法清空水印轨 V{}，请检查轨道是否锁定。".format(target_track))

        runtime_settings = {
            "mode": "fast_continuous" if settings.get("continuous_source") else "restart_each_block",
            "target_type": "video",
            "target_track": target_track,
            "mark_loop_start": False,
            "measured_source_length": spec["measured_length"],
        }
        added = fill_fast_mode(
            timeline,
            project,
            blocks,
            spec["media_item"],
            runtime_settings,
        )
        if added < 1:
            raise RuntimeError("水印 '{}' 铺设失败。".format(spec["label"]))
        total_added += added
        print("水印 '{}' 已添加到 V{}，共 {} 个片段。".format(spec["label"], target_track, added))

    print("批量水印完成，共添加 {} 个片段。".format(total_added))
    return total_added


def main():
    resolve, project, timeline = get_resolve_objects()
    if not project or not timeline:
        print("ERROR: Cannot get current DaVinci Resolve project/timeline.")
        return

    settings = show_settings_dialog(DEFAULT_SETTINGS, resolve)
    if settings is None:
        print("Cancelled.")
        return

    try:
        if settings.get("operation") == "watermark_batch":
            run_watermark_batch(timeline, project, settings)
        else:
            match_media_to_clips(timeline, project, settings)
    except RuntimeError as exc:
        print("ERROR: {}".format(exc))
    except Exception:
        print("ERROR: Unexpected failure.")
        print(traceback.format_exc())


if __name__ == "__main__":
    main()

"""Pure timeline mapping used by the smart-edit review UI.

The exported video is assembled from several source files and, optionally,
several kept ranges inside each source.  Keeping this mapping independent of
Qt makes seeking, issue markers and later timeline editing testable.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clip_ranges(clip):
    start = max(0.0, _number(clip.get("trim_start")))
    original_duration = max(start, _number(clip.get("original_duration")))
    end = _number(clip.get("trim_end"), original_duration)
    if end <= start:
        end = original_duration
    ranges = []
    for value in clip.get("kept_ranges", []) or []:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        range_start = max(start, _number(value[0]))
        range_end = min(end, _number(value[1]))
        if range_end > range_start + 0.001:
            ranges.append((range_start, range_end))
    if not ranges and end > start + 0.001:
        ranges.append((start, end))
    return ranges


def build_task_timeline(task):
    """Return final-output source ranges in their actual export order."""
    indexed = [
        (index, clip)
        for index, clip in enumerate(task.get("clips", []) or [])
        if clip.get("included", True)
    ]
    indexed.sort(key=lambda value: (
        int(_number(value[1].get("export_order"), value[0] + 1)),
        int(_number(value[1].get("source_index"), value[0])),
        value[0],
    ))
    cursor = 0.0
    segments = []
    for clip_index, clip in indexed:
        ranges = _clip_ranges(clip)
        for range_index, (source_start, source_end) in enumerate(ranges):
            duration = source_end - source_start
            segments.append({
                "clip_index": clip_index,
                "range_index": range_index,
                "source": str(clip.get("source") or ""),
                "file_name": str(clip.get("file_name") or ""),
                "status": str(clip.get("status") or "green"),
                "review_acknowledged": bool(clip.get("review_acknowledged")),
                "duplicate_group_id": str(
                    clip.get("duplicate_group_id") or ""
                ),
                "duplicate_group_size": int(
                    clip.get("duplicate_group_size") or 0
                ),
                "duplicate_selected": bool(clip.get("duplicate_selected")),
                "auto_excluded_duplicate": bool(
                    clip.get("auto_excluded_duplicate")
                ),
                "source_start": source_start,
                "source_end": source_end,
                "timeline_start": cursor,
                "timeline_end": cursor + duration,
            })
            cursor += duration
    return segments, cursor


def _merged_deleted_ranges(clip, duration):
    if not clip.get("included", True):
        return [(0.0, duration, "整段不导出")]
    trim_start = max(0.0, min(duration, _number(clip.get("trim_start"))))
    trim_end = max(
        trim_start,
        min(duration, _number(clip.get("trim_end"), duration)),
    )
    values = []
    if trim_start > 0.001:
        values.append((0.0, trim_start, "片头气口"))
    for value in clip.get("pause_removals", []) or []:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        left = max(trim_start, min(trim_end, _number(value[0])))
        right = max(left, min(trim_end, _number(value[1])))
        if right > left + 0.001:
            values.append((left, right, "句内气口"))
    if trim_end < duration - 0.001:
        values.append((trim_end, duration, "片尾气口"))
    return sorted(values, key=lambda value: (value[0], value[1]))


def build_task_review_timeline(task):
    """Build an edit timeline that also retains regions removed on export."""
    indexed = list(enumerate(task.get("clips", []) or []))
    indexed.sort(key=lambda value: (
        int(_number(value[1].get("export_order"), value[0] + 1)),
        int(_number(value[1].get("source_index"), value[0])),
        value[0],
    ))
    cursor = 0.0
    segments = []
    for clip_index, clip in indexed:
        duration = max(
            0.0,
            _number(clip.get("original_duration")),
            _number(clip.get("trim_end")),
            max(
                (_number(word.get("end")) for word in clip.get("words", []) or []),
                default=0.0,
            ),
        )
        if duration <= 0.001:
            continue
        deleted = _merged_deleted_ranges(clip, duration)
        boundaries = {0.0, duration}
        for left, right, _reason in deleted:
            boundaries.update((left, right))
        boundaries = sorted(boundaries)
        for range_index, (source_start, source_end) in enumerate(
            zip(boundaries, boundaries[1:])
        ):
            if source_end <= source_start + 0.001:
                continue
            middle = (source_start + source_end) / 2.0
            deletion = next(
                (
                    (left, right, reason)
                    for left, right, reason in deleted
                    if left - 0.0005 <= middle <= right + 0.0005
                ),
                None,
            )
            span = source_end - source_start
            segments.append({
                "clip_index": clip_index,
                "range_index": range_index,
                "source": str(clip.get("source") or ""),
                "file_name": str(clip.get("file_name") or ""),
                "status": str(clip.get("status") or "green"),
                "review_acknowledged": bool(clip.get("review_acknowledged")),
                "duplicate_group_id": str(
                    clip.get("duplicate_group_id") or ""
                ),
                "duplicate_group_size": int(
                    clip.get("duplicate_group_size") or 0
                ),
                "duplicate_selected": bool(clip.get("duplicate_selected")),
                "auto_excluded_duplicate": bool(
                    clip.get("auto_excluded_duplicate")
                ),
                "source_start": source_start,
                "source_end": source_end,
                "timeline_start": cursor,
                "timeline_end": cursor + span,
                "is_removed": deletion is not None,
                "remove_reason": deletion[2] if deletion else "",
                "included": bool(clip.get("included", True)),
            })
            cursor += span
    return segments, cursor


def _normalized_text(value):
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", str(value or ""))
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _severity_rank(value):
    return {"green": 0, "orange": 1, "pink": 2}.get(str(value), 0)


def _block_quality(clip, start, end, text, comparison_text):
    severity = "green"
    related = []
    for issue in clip.get("issues", []) or []:
        issue_severity = str(issue.get("severity") or "info")
        if issue_severity == "info":
            continue
        left = _number(issue.get("start"))
        right = max(left + 0.001, _number(issue.get("end"), left))
        if min(end, right) > max(start, left) - 0.001:
            related.append(issue)
            if _severity_rank(issue_severity) > _severity_rank(severity):
                severity = issue_severity
    left_text = _normalized_text(text)
    right_text = _normalized_text(comparison_text)
    ratio = 1.0
    if left_text or right_text:
        ratio = SequenceMatcher(None, left_text, right_text).ratio()
    if ratio < 0.52:
        severity = "pink"
    elif ratio < 0.78 and _severity_rank(severity) < 1:
        severity = "orange"
    return severity, ratio, related


def _recognized_text(clip, start, end):
    return " ".join(
        str(word.get("text") or "").strip()
        for word in clip.get("words", []) or []
        if str(word.get("text") or "").strip()
        and _number(word.get("end")) > start
        and _number(word.get("start")) < end
    ).strip()


def _aligned_text(clip, start, end):
    return " ".join(
        str(word.get("raw") or "").strip()
        for word in clip.get("word_timeline", []) or []
        if str(word.get("raw") or "").strip()
        and _number(word.get("end")) > start
        and _number(word.get("start")) < end
    ).strip()


def _subtitle_block(clip, clip_index, segments, kind, start, end, text):
    timeline_start = output_time_for_source(segments, clip_index, start)
    timeline_end = output_time_for_source(segments, clip_index, end)
    if timeline_start is None or timeline_end is None:
        return None
    comparison = (
        _aligned_text(clip, start, end)
        if kind == "recognized"
        else _recognized_text(clip, start, end)
    )
    severity, ratio, related = _block_quality(
        clip, start, end, text, comparison
    )
    if severity == "green":
        suggestion = "两条字幕在这一处基本对齐，通常不需要处理。"
    elif kind == "recognized":
        suggestion = (
            f"建议以任务文本轨为准核对。该处模型识别为「{text}」，"
            f"对齐到的任务原文为「{comparison or '未找到'}」。"
        )
    else:
        suggestion = (
            f"建议试听这一处。任务原文为「{text}」，模型听到「{comparison or '未识别到'}」；"
            "如果视频确实读错应重录或跳过，读音正确则可人工通过。"
        )
    return {
        "kind": kind,
        "clip_index": clip_index,
        "source_start": start,
        "source_end": end,
        "timeline_start": min(timeline_start, timeline_end),
        "timeline_end": max(timeline_start + 0.04, timeline_end),
        "text": str(text or "").strip(),
        "comparison_text": comparison,
        "severity": severity,
        "similarity": ratio,
        "related_issues": related,
        "suggestion": suggestion,
    }


def _group_subtitle_words(
    words,
    text_key,
    max_words=0,
    max_chars=0,
    split_on_line=False,
):
    groups = []
    current = []
    for word in words:
        text = str(word.get(text_key) or "").strip()
        if not text:
            continue
        candidate = " ".join(
            str(value.get(text_key) or "").strip()
            for value in current + [word]
            if str(value.get(text_key) or "").strip()
        )
        pause = (
            _number(word.get("start")) - _number(current[-1].get("end"))
            if current else 0.0
        )
        line_changed = bool(
            split_on_line
            and current
            and word.get("line_index") != current[-1].get("line_index")
        )
        if current and (
            (max_words > 0 and len(current) >= max_words)
            or (max_chars > 0 and len(candidate) > max_chars)
            or pause > 1.0
            or line_changed
        ):
            groups.append(current)
            current = []
        current.append(word)
        if re.search(r"[.!?。！？…]\s*$", text):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _apply_track_gap(blocks, block_gap_ms):
    try:
        block_gap_ms = int(block_gap_ms)
    except (TypeError, ValueError):
        return
    if block_gap_ms < 0:
        return
    gap = block_gap_ms / 1000.0
    ordered = sorted(blocks, key=lambda item: item["timeline_start"])
    for current, following in zip(ordered, ordered[1:]):
        current["timeline_end"] = max(
            current["timeline_start"] + 0.04,
            following["timeline_start"] - gap,
        )


def build_subtitle_tracks(
    task,
    segments,
    recognized_words_per_block=6,
    settings=None,
):
    """Return model-generated and task-text-aligned subtitle blocks."""
    settings = settings if isinstance(settings, dict) else None
    max_words = (
        max(0, int(settings.get("srt_max_words_per_block", 0)))
        if settings is not None else max(1, int(recognized_words_per_block))
    )
    max_chars = (
        max(0, int(settings.get("srt_max_chars_per_block", 0)))
        if settings is not None else 0
    )
    recognized_blocks = []
    aligned_blocks = []
    for clip_index, clip in enumerate(task.get("clips", []) or []):
        words = [
            word for word in clip.get("words", []) or []
            if str(word.get("text") or "").strip()
        ]
        for group in _group_subtitle_words(
            words, "text", max_words=max_words, max_chars=max_chars
        ):
            start = _number(group[0].get("start"))
            end = max(start + 0.04, _number(group[-1].get("end"), start))
            text = " ".join(str(value.get("text") or "").strip() for value in group)
            block = _subtitle_block(
                clip, clip_index, segments, "recognized", start, end, text
            )
            if block:
                recognized_blocks.append(block)

        aligned_words = list(clip.get("word_timeline", []) or [])
        aligned_groups = _group_subtitle_words(
            aligned_words,
            "raw",
            max_words=max_words,
            max_chars=max_chars,
            split_on_line=True,
        )
        for group in aligned_groups:
            start = _number(group[0].get("start"))
            end = max(start + 0.04, _number(group[-1].get("end"), start))
            text = " ".join(
                str(value.get("raw") or "").strip() for value in group
            )
            block = _subtitle_block(
                clip,
                clip_index,
                segments,
                "aligned",
                start,
                end,
                text,
            )
            if block:
                aligned_blocks.append(block)
        if not aligned_groups:
            cues = clip.get("cues", []) or []
            if not cues and str(clip.get("expected_text") or "").strip():
                cues = [{
                    "start": clip.get("trim_start", 0.0),
                    "end": clip.get("trim_end", clip.get("original_duration", 0.0)),
                    "text": clip.get("expected_text", ""),
                }]
            for cue in cues:
                start = _number(cue.get("start"))
                end = max(start + 0.04, _number(cue.get("end"), start))
                block = _subtitle_block(
                    clip,
                    clip_index,
                    segments,
                    "aligned",
                    start,
                    end,
                    cue.get("text", ""),
                )
                if block:
                    aligned_blocks.append(block)

    # A confirmed missing script block has no audio timestamp of its own, so
    # the regular per-clip cue loop cannot display it.  Put the exact missing
    # source text directly on the forced-alignment track at the boundary
    # between its previous/following clips.  This keeps the most important
    # failure visible in the timeline instead of requiring a hunt through the
    # issue table.
    by_file_name = {}
    for segment in segments:
        by_file_name.setdefault(segment.get("file_name", ""), []).append(segment)
    timeline_duration = max(
        (float(segment.get("timeline_end") or 0.0) for segment in segments),
        default=0.0,
    )
    for missing_index, missing in enumerate(task.get("missing_blocks", []) or []):
        text = str(missing.get("text") or "").strip()
        if not text:
            continue
        previous = by_file_name.get(str(missing.get("previous_clip") or ""), [])
        following = by_file_name.get(str(missing.get("following_clip") or ""), [])
        if previous:
            anchor = float(previous[-1]["timeline_end"])
            clip_index = int(previous[-1]["clip_index"])
        elif following:
            anchor = float(following[0]["timeline_start"])
            clip_index = int(following[0]["clip_index"])
        elif segments:
            anchor = 0.0
            clip_index = int(segments[0]["clip_index"])
        else:
            continue

        display_span = min(6.0, max(2.0, 1.4 + len(text) * 0.035))
        start = max(0.0, anchor - display_span / 2.0)
        end = min(timeline_duration, start + display_span)
        if end - start < min(0.5, timeline_duration):
            start = max(0.0, end - display_span)
        aligned_blocks.append({
            "kind": "aligned",
            "clip_index": clip_index,
            "source_start": 0.0,
            "source_end": 0.0,
            "timeline_start": start,
            "timeline_end": max(start + 0.04, end),
            "text": f"⛔ 缺段：{text}",
            "missing_text": text,
            "comparison_text": "未找到对应的视频语音",
            "severity": "pink",
            "similarity": 0.0,
            "related_issues": [],
            "is_missing": True,
            "missing_block_index": missing_index,
            "missing_block": missing,
            "suggestion": (
                f"确认缺少任务原文「{text}」。请补拍对应视频；"
                "如果视频实际已经读出这段内容，请在缺段页人工确认误报。"
            ),
        })
    block_gap_ms = settings.get("srt_block_gap_ms", -1) if settings else -1
    _apply_track_gap(recognized_blocks, block_gap_ms)
    _apply_track_gap(aligned_blocks, block_gap_ms)
    return recognized_blocks, aligned_blocks


def source_location_for_output(segments, output_time):
    """Map final-output seconds to one source file/range."""
    if not segments:
        return None
    value = max(0.0, _number(output_time))
    segment = segments[-1]
    for candidate in segments:
        if value < candidate["timeline_end"] - 0.0005:
            segment = candidate
            break
    offset = max(
        0.0,
        min(
            segment["source_end"] - segment["source_start"],
            value - segment["timeline_start"],
        ),
    )
    return segment, segment["source_start"] + offset


def output_time_for_source(segments, clip_index, source_time):
    """Map a source timestamp to final-output seconds.

    If the timestamp lies in a removed pause, return the closest retained edge
    so an issue remains visible instead of disappearing from the review.
    """
    candidates = [
        segment for segment in segments
        if int(segment["clip_index"]) == int(clip_index)
    ]
    if not candidates:
        return None
    value = _number(source_time)
    for segment in candidates:
        if segment["source_start"] <= value <= segment["source_end"]:
            return segment["timeline_start"] + value - segment["source_start"]
    nearest = min(
        candidates,
        key=lambda segment: min(
            abs(value - segment["source_start"]),
            abs(value - segment["source_end"]),
        ),
    )
    if value < nearest["source_start"]:
        return nearest["timeline_start"]
    return nearest["timeline_end"]


def build_issue_markers(task, segments):
    markers = []
    for clip_index, clip in enumerate(task.get("clips", []) or []):
        if not clip.get("included", True):
            continue
        for issue_index, issue in enumerate(clip.get("issues", []) or []):
            severity = str(issue.get("severity") or "info")
            if severity == "info":
                continue
            source_start = _number(issue.get("start"))
            source_end = max(source_start, _number(issue.get("end"), source_start))
            output_time = output_time_for_source(segments, clip_index, source_start)
            if output_time is None:
                continue
            markers.append({
                "kind": "issue",
                "clip_index": clip_index,
                "issue_index": issue_index,
                "time": output_time,
                "source_start": source_start,
                "source_end": source_end,
                "severity": severity,
                "title": str(issue.get("title") or issue.get("kind") or "问题"),
                "detail": str(issue.get("detail") or ""),
                "expected": str(issue.get("expected") or ""),
                "recognized": str(issue.get("recognized") or ""),
            })

    clip_name_positions = {}
    for segment in segments:
        clip_name_positions.setdefault(segment["file_name"], []).append(segment)
    for severity, blocks in (
        ("pink", task.get("missing_blocks", []) or []),
        ("orange", task.get("unverified_blocks", []) or []),
    ):
        for block_index, block in enumerate(blocks):
            previous = str(block.get("previous_clip") or "")
            following = str(block.get("following_clip") or "")
            if previous in clip_name_positions:
                time_value = clip_name_positions[previous][-1]["timeline_end"]
            elif following in clip_name_positions:
                time_value = clip_name_positions[following][0]["timeline_start"]
            else:
                time_value = 0.0
            markers.append({
                "kind": "missing" if severity == "pink" else "unverified",
                "clip_index": None,
                "issue_index": block_index,
                "time": time_value,
                "source_start": 0.0,
                "source_end": 0.0,
                "severity": severity,
                "title": "确认缺段" if severity == "pink" else "识别边界待核对",
                "detail": str(block.get("issue_reason") or ""),
                "expected": str(block.get("text") or ""),
                "recognized": str(block.get("edge_recognized_text") or ""),
                "block": block,
            })
    markers.sort(key=lambda marker: (marker["time"], marker["severity"] != "pink"))
    return markers

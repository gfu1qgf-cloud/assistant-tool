"""Whisper-assisted clip verification, breath trimming, and SRT export.

This module deliberately does not import ``stable_whisper`` or create a Whisper
model.  The application owns one preloaded faster-whisper model and passes it to
the worker, preserving the startup/import order required by this project.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from qt_compat import QtCore


SMART_VIDEO_EDITOR_CONFIG_KEY = "smart_video_editor"
SMART_VIDEO_PENDING_CONFIG_KEY = "smart_video_pending_reviews"
SMART_VIDEO_EDITOR_REPORT_NAME = "智能剪辑审核.json"
SMART_VIDEO_EDITOR_TEXT_REPORT_NAME = "智能剪辑问题报告.txt"
BREATH_CUT_OUTPUT_FOLDER_NAME = "气口剪辑结果"
SMART_VIDEO_ANALYSIS_CACHE_VERSION = 9
VOICE_ACTIVITY_CACHE_VERSION = 1
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts"}
BREATH_DETECTION_MODES = (
    "db_only",
    "voice_only",
    "union",
    "intersection",
    "voice_refined",
)

_ANALYSIS_SETTING_KEYS = (
    "lead_padding_ms",
    "tail_padding_ms",
    "silence_detection_enabled",
    "silence_threshold_db",
    "min_silence_ms",
    "voice_detection_enabled",
    "vad_threshold",
    "vad_neg_threshold",
    "vad_min_speech_ms",
    "vad_speech_pad_ms",
    "breath_detection_mode",
    "boundary_search_ms",
    "compress_internal_pauses",
    "internal_pause_mode",
    "pause_threshold_ms",
    "retained_pause_ms",
    "pause_keep_before_ms",
    "pause_keep_after_ms",
    "pass_similarity_percent",
    "severe_similarity_percent",
    "whisper_model_size",
    "srt_max_words_per_block",
    "srt_max_chars_per_block",
    "srt_include_line_breaks",
    "srt_block_gap_ms",
)

DEFAULT_SMART_VIDEO_EDITOR_SETTINGS = {
    "lead_padding_ms": 120,
    "tail_padding_ms": 220,
    "silence_detection_enabled": True,
    "silence_threshold_db": -35,
    "min_silence_ms": 350,
    "voice_detection_enabled": True,
    # 0.50 + 400 ms is the conservative faster-whisper transcription default,
    # but it classifies too much background sound as protected speech for an
    # editor.  These defaults still protect syllables while exposing useful
    # no-voice gaps to the breath cutter.
    "vad_threshold": 0.65,
    "vad_neg_threshold": 0.50,
    "vad_min_speech_ms": 120,
    "vad_speech_pad_ms": 120,
    # VAD is authoritative by default so sustained room noise is not mistaken
    # for speech merely because it is louder than the dB threshold.  An
    # overlapping dB interval may only refine the already proven no-voice edge.
    "breath_detection_mode": "voice_refined",
    "boundary_search_ms": 1800,
    # Normal operation only removes dead air at clip boundaries.  Cutting a
    # silence inside a sentence is inherently unsafe when ASR misses a word,
    # so the experimental mode is opt-in and versioned separately.
    "compress_internal_pauses": False,
    "internal_pause_mode": "off",
    "pause_threshold_ms": 1000,
    "retained_pause_ms": 320,
    "pause_keep_before_ms": 160,
    "pause_keep_after_ms": 160,
    "pass_similarity_percent": 74,
    "severe_similarity_percent": 52,
    "whisper_model_size": "base",
    "use_main_subtitle_settings": True,
    "srt_max_words_per_block": 8,
    "srt_max_chars_per_block": 50,
    "srt_include_line_breaks": False,
    "srt_block_gap_ms": -1,
    "output_folder_name": "智能剪辑结果",
    "ffmpeg_path": "",
    "existing_output": "version",
    "auto_export_clean": True,
}


def _int_value(value, default, minimum, maximum):
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float_value(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_smart_video_editor_settings(value=None):
    source = value if isinstance(value, dict) else {}
    result = copy.deepcopy(DEFAULT_SMART_VIDEO_EDITOR_SETTINGS)
    result["lead_padding_ms"] = _int_value(
        source.get("lead_padding_ms"), 120, 0, 3000
    )
    result["tail_padding_ms"] = _int_value(
        source.get("tail_padding_ms"), 220, 0, 3000
    )
    result["silence_detection_enabled"] = bool(
        source.get("silence_detection_enabled", True)
    )
    result["silence_threshold_db"] = _int_value(
        source.get("silence_threshold_db"), -35, -80, -5
    )
    result["min_silence_ms"] = _int_value(
        source.get("min_silence_ms"), 350, 80, 5000
    )
    result["voice_detection_enabled"] = bool(
        source.get("voice_detection_enabled", True)
    )
    result["vad_threshold"] = _float_value(
        source.get("vad_threshold"), 0.65, 0.1, 0.95
    )
    result["vad_neg_threshold"] = _float_value(
        source.get("vad_neg_threshold"), 0.50, 0.01, 0.94
    )
    if result["vad_neg_threshold"] >= result["vad_threshold"]:
        result["vad_neg_threshold"] = max(
            0.01, round(result["vad_threshold"] - 0.05, 2)
        )
    result["vad_min_speech_ms"] = _int_value(
        source.get("vad_min_speech_ms"), 120, 0, 2000
    )
    result["vad_speech_pad_ms"] = _int_value(
        source.get("vad_speech_pad_ms"), 120, 0, 1500
    )
    detection_mode = str(
        source.get("breath_detection_mode") or "voice_refined"
    ).strip().lower()
    result["breath_detection_mode"] = (
        detection_mode
        if detection_mode in BREATH_DETECTION_MODES
        else "voice_refined"
    )
    result["boundary_search_ms"] = _int_value(
        source.get("boundary_search_ms"), 1800, 100, 10000
    )
    pause_mode = str(source.get("internal_pause_mode") or "off").lower()
    result["internal_pause_mode"] = (
        "experimental" if pause_mode == "experimental" else "off"
    )
    result["compress_internal_pauses"] = bool(
        source.get("compress_internal_pauses", False)
        and result["internal_pause_mode"] == "experimental"
    )
    result["pause_threshold_ms"] = _int_value(
        source.get("pause_threshold_ms"), 1000, 300, 10000
    )
    result["retained_pause_ms"] = _int_value(
        source.get("retained_pause_ms"), 320, 0, 5000
    )
    legacy_retained = result["retained_pause_ms"]
    result["pause_keep_before_ms"] = _int_value(
        source.get("pause_keep_before_ms"),
        legacy_retained // 2,
        0,
        2500,
    )
    result["pause_keep_after_ms"] = _int_value(
        source.get("pause_keep_after_ms"),
        legacy_retained - legacy_retained // 2,
        0,
        2500,
    )
    maximum_kept = max(0, result["pause_threshold_ms"] - 100)
    kept_total = (
        result["pause_keep_before_ms"]
        + result["pause_keep_after_ms"]
    )
    if kept_total > maximum_kept and kept_total:
        ratio = maximum_kept / float(kept_total)
        result["pause_keep_before_ms"] = int(round(
            result["pause_keep_before_ms"] * ratio
        ))
        result["pause_keep_after_ms"] = max(
            0, maximum_kept - result["pause_keep_before_ms"]
        )
    result["retained_pause_ms"] = (
        result["pause_keep_before_ms"]
        + result["pause_keep_after_ms"]
    )
    result["pass_similarity_percent"] = _int_value(
        source.get("pass_similarity_percent"), 74, 40, 100
    )
    result["severe_similarity_percent"] = _int_value(
        source.get("severe_similarity_percent"), 52, 0, 95
    )
    if result["severe_similarity_percent"] >= result["pass_similarity_percent"]:
        result["severe_similarity_percent"] = max(
            0, result["pass_similarity_percent"] - 5
        )
    whisper_model_size = str(
        source.get("whisper_model_size") or "base"
    ).strip().lower()
    result["whisper_model_size"] = (
        whisper_model_size
        if whisper_model_size in {"base", "small", "medium", "large-v3"}
        else "base"
    )
    result["use_main_subtitle_settings"] = bool(
        source.get("use_main_subtitle_settings", True)
    )
    result["srt_max_words_per_block"] = _int_value(
        source.get("srt_max_words_per_block"), 8, 0, 50
    )
    result["srt_max_chars_per_block"] = _int_value(
        source.get("srt_max_chars_per_block"), 50, 0, 500
    )
    result["srt_include_line_breaks"] = bool(
        source.get("srt_include_line_breaks", False)
    )
    result["srt_block_gap_ms"] = _int_value(
        source.get("srt_block_gap_ms"), -1, -1, 60_000
    )
    output_name = str(source.get("output_folder_name") or "").strip()
    result["output_folder_name"] = output_name or "智能剪辑结果"
    result["ffmpeg_path"] = str(source.get("ffmpeg_path") or "").strip()
    existing_output = str(source.get("existing_output") or "version").lower()
    result["existing_output"] = (
        existing_output
        if existing_output in {"version", "overwrite", "skip"}
        else "version"
    )
    result["auto_export_clean"] = bool(
        source.get("auto_export_clean", True)
    )
    return result


def _safe_child_name(name, fallback):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "").strip())
    value = value.rstrip(" .")
    return value or fallback


def _natural_key(value):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


def _explicit_sequence_number(path):
    """Return a deliberate short sequence prefix such as ``1_`` or ``第2段``.

    Long leading values are normally dates or generated ids, so they must not
    silently take control of clip order.
    """
    stem = Path(path).stem.strip()
    match = re.match(r"^(\d{1,3})(?=$|[\s_.()（）\-\[\]])", stem)
    if match is None:
        match = re.match(r"^第\s*(\d{1,3})(?:段|条|个|集|部分|片)?", stem)
    return int(match.group(1)) if match is not None else None


def _has_complete_explicit_sequence(paths):
    numbers = [_explicit_sequence_number(path) for path in paths]
    return (
        len(numbers) > 1
        and all(number is not None for number in numbers)
        and len(set(numbers)) == len(numbers)
    )


def _filename_sequence_key(clip):
    number = _explicit_sequence_number(clip.get("source"))
    return (
        number if number is not None else 10 ** 9,
        _natural_key(clip.get("file_name", "")),
        clip.get("source_index", 0),
    )


def discover_task_videos(task_dir, settings=None):
    """Return eligible source videos without ever re-importing generated output."""
    settings = normalize_smart_video_editor_settings(settings)
    root = Path(task_dir)
    if not root.is_dir():
        return []
    output_name = settings["output_folder_name"].casefold()
    result = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        relative_parts = [part.casefold() for part in path.relative_to(root).parts[:-1]]
        if output_name in relative_parts or ".smart_edit_cache" in relative_parts:
            continue
        stem = path.stem.casefold()
        if "_智能剪辑" in stem or stem.endswith("智能剪辑"):
            continue
        result.append(path)
    return sorted(result, key=lambda item: _natural_key(str(item.relative_to(root))))


def _is_cjk(character):
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def text_units(text):
    """Case-fold text into comparable units while ignoring punctuation."""
    units = []
    buffer = []

    def flush():
        if buffer:
            token = "".join(buffer).casefold()
            token = "".join(
                character
                for character in token
                if unicodedata.category(character)[0] in {"L", "N"}
            )
            if token:
                units.append(token)
            buffer.clear()

    for character in unicodedata.normalize("NFKC", str(text or "")):
        category = unicodedata.category(character)
        if _is_cjk(character):
            flush()
            units.append(character.casefold())
        elif category[0] in {"L", "N"} or character in {"'", "’", "-"}:
            buffer.append(character)
        else:
            flush()
    flush()
    return [unit for unit in units if unit]


def _token_spans(text):
    spans = []
    start = None
    for index, character in enumerate(str(text or "")):
        category = unicodedata.category(character)
        token_character = category[0] in {"L", "N"} or character in {"'", "’", "-"}
        if _is_cjk(character):
            if start is not None:
                spans.append((start, index))
                start = None
            spans.append((index, index + 1))
        elif token_character:
            if start is None:
                start = index
        elif start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(str(text or ""))))
    return spans


def split_script_blocks(text, target_count=0, weights=None):
    text = str(text or "").strip()
    if not text:
        return []
    blocks = [
        item.strip()
        for item in re.split(r"\r?\n+|(?<=[.!?。！？])\s+", text)
        if item.strip()
    ]
    target_count = max(0, int(target_count or 0))
    if target_count <= len(blocks) or target_count <= 1:
        return blocks

    spans = _token_spans(text)
    if len(spans) < target_count:
        return blocks
    normalized_weights = []
    if isinstance(weights, Sequence) and len(weights) == target_count:
        for weight in weights:
            try:
                normalized_weights.append(max(1.0, float(weight)))
            except (TypeError, ValueError):
                normalized_weights.append(1.0)
    else:
        normalized_weights = [1.0] * target_count
    total_weight = sum(normalized_weights)
    token_boundaries = [0]
    cumulative_weight = 0.0
    for index in range(1, target_count):
        cumulative_weight += normalized_weights[index - 1]
        ideal = round(len(spans) * cumulative_weight / total_weight)
        minimum = token_boundaries[-1] + 1
        maximum = len(spans) - (target_count - index)
        token_boundaries.append(max(minimum, min(maximum, ideal)))
    token_boundaries.append(len(spans))
    character_boundaries = [0]
    character_boundaries.extend(
        spans[token_index][0] for token_index in token_boundaries[1:-1]
    )
    character_boundaries.append(len(text))
    balanced = []
    for index in range(target_count):
        chunk = text[
            character_boundaries[index]:character_boundaries[index + 1]
        ].strip()
        if chunk:
            balanced.append(chunk)
    return balanced or blocks


def build_script_word_records(text):
    """Flatten the authoritative script while retaining exact line slices."""
    lines = [
        line.strip()
        for line in str(text or "").replace("\r\n", "\n").split("\n")
        if line.strip()
    ]
    if not lines and str(text or "").strip():
        lines = [str(text).strip()]
    words = []
    for line_index, line in enumerate(lines):
        spans = _token_spans(line)
        for line_word_index, (start, end) in enumerate(spans):
            raw = line[start:end]
            normalized = text_units(raw)
            if not normalized:
                continue
            # CJK spans are one character; spaced-language spans are one word.
            words.append({
                "raw": raw,
                "norm": "".join(normalized),
                "line_index": line_index,
                "char_start": start,
                "char_end": end,
                "is_first_in_line": line_word_index == 0,
                "is_last_in_line": line_word_index == len(spans) - 1,
                "word_index": len(words),
            })
    return lines, words


def script_text_from_word_records(records, lines):
    output = []
    group = []

    def flush():
        if not group:
            return
        first = group[0]
        last = group[-1]
        source = lines[first["line_index"]]
        start = 0 if first["is_first_in_line"] else first["char_start"]
        end = len(source) if last["is_last_in_line"] else last["char_end"]
        if not last["is_last_in_line"]:
            # A max-word subtitle split must not silently throw away commas or
            # sentence punctuation between two word groups.
            while end < len(source):
                character = source[end]
                if unicodedata.category(character)[0] in {"L", "N"}:
                    break
                end += 1
        value = source[start:end].strip()
        if value:
            output.append(value)
        group.clear()

    for word in records:
        if group and word["line_index"] != group[-1]["line_index"]:
            flush()
        group.append(word)
    flush()
    return "\n".join(output)


def _recognized_word_records(words):
    records = []
    for source_index, word in enumerate(words):
        normalized = text_units(word.get("text", ""))
        if not normalized:
            continue
        records.append({
            **word,
            "raw": str(word.get("text") or "").strip(),
            "norm": "".join(normalized),
            "source_index": source_index,
        })
    return records


def _alignment_unit_records(records):
    """Expand CJK ASR tokens into comparable characters without losing time.

    Whisper may return an entire Chinese/Japanese phrase as one timestamped
    token, while the task script is flattened character by character.  Keep
    spaced-language words intact, but distribute a CJK token's timestamp over
    its characters so both matching and SRT timing remain usable.
    """
    expanded = []
    for record_index, record in enumerate(records):
        units = text_units(record.get("norm") or record.get("raw") or "")
        if not units:
            continue
        start = float(record.get("start") or 0.0)
        end = max(start, float(record.get("end") or start))
        duration = end - start
        for unit_index, unit in enumerate(units):
            unit_start = start + duration * unit_index / len(units)
            unit_end = start + duration * (unit_index + 1) / len(units)
            expanded.append({
                "norm": unit,
                "record_index": record_index,
                "source_index": record.get("source_index", record_index),
                "start": unit_start,
                "end": unit_end,
            })
    return expanded


def _edit_similarity(left, right):
    left = str(left or "")
    right = str(right or "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1]
                + (0 if left_character == right_character else 1),
            ))
        previous = current
    return 1.0 - previous[-1] / max(len(left), len(right), 1)


def _diacritic_fold(value):
    """Fold accent-only spelling variants for ASR word alignment.

    Whisper may transcribe a borrowed or devotional word with the local
    accent (for example ``Amen`` -> ``Ámen``).  That is useful text, but it
    must not turn an actually recognized edge word into an unanchored word and
    consequently disable an otherwise proven breath cut.
    """
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(character)
    )


def _compact_record_similarity(expected_records, observed_records):
    expected = "".join(
        str(item.get("norm") or "") for item in expected_records
    )
    observed = "".join(
        str(item.get("norm") or "") for item in observed_records
    )
    if not expected or not observed:
        return 0.0
    # Whisper frequently drops the final consonant of a very short boundary
    # word (for example Slovak ``Ak`` -> ``A``). Treat this as strong *edge
    # audio evidence* only; the ordinary word aligner remains conservative and
    # the item still goes through manual review instead of becoming exact.
    if (
        len(expected_records) == 1
        and len(observed_records) == 1
        and min(len(expected), len(observed)) >= 1
        and max(len(expected), len(observed)) <= 3
        and abs(len(expected) - len(observed)) == 1
        and (expected.startswith(observed) or observed.startswith(expected))
    ):
        return 0.82
    length_ratio = min(len(expected), len(observed)) / max(
        len(expected), len(observed), 1
    )
    if length_ratio < 0.55:
        return 0.0
    return _edit_similarity(expected, observed)


def _token_equivalent(left, right):
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    if _diacritic_fold(left) == _diacritic_fold(right):
        return True
    length_ratio = min(len(left), len(right)) / max(len(left), len(right))
    return length_ratio >= 0.72 and _edit_similarity(left, right) >= 0.78


def _token_lcs_pairs(left, right):
    rows = len(left)
    columns = len(right)
    table = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            if _token_equivalent(left[row - 1], right[column - 1]):
                table[row][column] = table[row - 1][column - 1] + 1
            else:
                table[row][column] = max(
                    table[row - 1][column], table[row][column - 1]
                )
    pairs = []
    row, column = rows, columns
    while row > 0 and column > 0:
        if _token_equivalent(left[row - 1], right[column - 1]):
            pairs.append((row - 1, column - 1))
            row -= 1
            column -= 1
        elif table[row - 1][column] >= table[row][column - 1]:
            row -= 1
        else:
            column -= 1
    pairs.reverse()
    return pairs


def _sequence_score(expected, observed):
    """VideoKit-style tolerant word score with a compact-text fallback."""
    if not expected or not observed:
        return 0.0
    pairs = _token_lcs_pairs(expected, observed)
    matched = len(pairs)
    precision = matched / max(1, len(observed))
    recall = matched / max(1, len(expected))
    word_f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    compact_score = _edit_similarity("".join(expected), "".join(observed))
    return max(word_f1, word_f1 * 0.72 + compact_score * 0.28)


def best_unit_window(expected_units, observed_units):
    """Find the contiguous observed window that best represents expected text."""
    expected = list(expected_units)
    observed = list(observed_units)
    if not expected or not observed:
        return {"start": 0, "end": 0, "score": 0.0}
    target = len(expected)
    minimum = max(1, int(target * 0.55) - 2)
    maximum = min(len(observed), int(target * 1.55) + 4)
    if minimum > maximum:
        minimum = maximum
    best = {"start": 0, "end": min(len(observed), target), "score": -1.0}
    for length in range(minimum, maximum + 1):
        for start in range(0, len(observed) - length + 1):
            end = start + length
            score = _sequence_score(expected, observed[start:end])
            length_penalty = abs(length - target) / max(target, length)
            score -= min(0.08, length_penalty * 0.08)
            if score > best["score"]:
                best = {"start": start, "end": end, "score": max(0.0, score)}
    return best


def _word_units(words):
    units = []
    unit_word_indexes = []
    for word_index, word in enumerate(words):
        for unit in text_units(word.get("text", "")):
            units.append(unit)
            unit_word_indexes.append(word_index)
    return units, unit_word_indexes


def match_text_to_words(expected_text, words, start_word=0, end_word=None):
    end_word = len(words) if end_word is None else min(len(words), end_word)
    start_word = max(0, min(end_word, start_word))
    subset = words[start_word:end_word]
    units, unit_word_indexes = _word_units(subset)
    match = best_unit_window(text_units(expected_text), units)
    if not unit_word_indexes or match["end"] <= match["start"]:
        return {
            "start_word": start_word,
            "end_word": start_word,
            "score": 0.0,
        }
    first = unit_word_indexes[match["start"]]
    last = unit_word_indexes[match["end"] - 1]
    return {
        "start_word": start_word + first,
        "end_word": start_word + last + 1,
        "score": match["score"],
    }


def _record_window_candidates(records, target_text, limit=12):
    """Locate a clip transcript anywhere in the complete script.

    This mirrors VideoKit's multilingual V2 matcher: word-level fuzzy LCS,
    length agreement, and explicit first/last-word boundary bonuses.
    """
    target = text_units(target_text)
    expanded = _alignment_unit_records(records)
    source = [item["norm"] for item in expanded]
    if not target or not source:
        return []
    target_length = len(target)
    minimum = max(1, int(target_length * 0.45))
    maximum = min(
        len(source), max(target_length + 5, int(math.ceil(target_length * 1.8)))
    )
    candidates = []
    seen_ranges = set()

    # Most correctly spoken clips take this fast path.  Besides being exact,
    # it avoids an expensive fuzzy scan over a long task script.
    if target_length <= len(source):
        for unit_start in range(len(source) - target_length + 1):
            unit_end = unit_start + target_length
            if source[unit_start:unit_end] != target:
                continue
            start = expanded[unit_start]["record_index"]
            end = expanded[unit_end - 1]["record_index"]
            key = (start, end)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            candidates.append({
                "start": start,
                "end": end,
                "length": target_length,
                "similarity": 1.0,
                "adjusted_score": 1.0,
            })
        if candidates:
            return candidates[:max(1, int(limit))]

    # The former exhaustive scan evaluated every possible start *and* every
    # possible length with two dynamic-programming scorers.  On a long script
    # and 10+ clips this grows into millions of Python list allocations.  Use
    # rare matching words and SequenceMatcher blocks to identify plausible
    # starts first; fuzzy scoring still makes the final decision.
    source_positions = {}
    for source_index, token in enumerate(source):
        source_positions.setdefault(token, []).append(source_index)
    unique_target_indexes = []
    seen_target_tokens = set()
    for target_index, token in enumerate(target):
        if token in seen_target_tokens:
            continue
        seen_target_tokens.add(token)
        unique_target_indexes.append(target_index)
    unique_target_indexes.sort(
        key=lambda index: (len(source_positions.get(target[index], ())), index)
    )
    start_votes = {}
    neighborhood = max(2, min(8, target_length // 8 + 1))

    def vote(start, weight):
        start = max(0, min(len(source) - 1, int(start)))
        start_votes[start] = start_votes.get(start, 0.0) + float(weight)

    for target_index in unique_target_indexes[:6]:
        positions = source_positions.get(target[target_index], ())
        frequency_weight = 2.0 / max(1, len(positions))
        for source_index in positions:
            base = source_index - target_index
            for delta in range(-neighborhood, neighborhood + 1):
                vote(base + delta, frequency_weight / (1 + abs(delta)))

    matcher = difflib.SequenceMatcher(None, target, source, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size:
            base = block.b - block.a
            for delta in range(-neighborhood, neighborhood + 1):
                vote(base + delta, (1.0 + block.size) / (1 + abs(delta)))

    if not start_votes:
        maximum_start = max(0, len(source) - minimum)
        stride = max(1, int(math.ceil((maximum_start + 1) / 160.0)))
        for start in range(0, maximum_start + 1, stride):
            vote(start, 0.1)
    candidate_starts = [
        start
        for start, _score in sorted(
            start_votes.items(),
            key=lambda item: (-item[1], item[0]),
        )[:240]
    ]

    length_span = maximum - minimum
    if length_span <= 28:
        candidate_lengths = list(range(minimum, maximum + 1))
    else:
        candidate_lengths = {
            minimum,
            maximum,
            target_length,
            *(
                target_length + delta
                for delta in (-10, -7, -5, -3, -2, -1, 1, 2, 3, 5, 7, 10)
            ),
        }
        candidate_lengths.update(
            minimum + int(round(length_span * step / 18.0))
            for step in range(19)
        )
        candidate_lengths = sorted(
            length for length in candidate_lengths
            if minimum <= length <= maximum
        )

    for start in candidate_starts:
        for length in candidate_lengths:
            end = start + length
            if end > len(source):
                continue
            candidate = source[start:end]
            similarity = _sequence_score(target, candidate)
            length_ratio = min(len(candidate), target_length) / max(
                len(candidate), target_length, 1
            )
            boundary_score = (
                (0.5 if _token_equivalent(target[0], candidate[0]) else 0.0)
                + (0.5 if _token_equivalent(target[-1], candidate[-1]) else 0.0)
            )
            adjusted = similarity * 0.78 + length_ratio * 0.14 + boundary_score * 0.08
            if adjusted < 0.40:
                continue
            record_start = expanded[start]["record_index"]
            record_end = expanded[end - 1]["record_index"]
            candidates.append({
                "start": record_start,
                "end": record_end,
                "length": end - start,
                "similarity": similarity,
                "adjusted_score": adjusted,
            })
    candidates.sort(key=lambda item: (
        -item["adjusted_score"],
        -item["similarity"],
        item["length"],
        item["start"],
    ))
    unique = []
    seen_ranges.clear()
    for candidate in candidates:
        key = (candidate["start"], candidate["end"])
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        unique.append(candidate)
        if len(unique) >= max(1, int(limit)):
            break
    return unique


def _overlap_size(start, end, ranges):
    overlap = 0
    for used_start, used_end in ranges:
        overlap_start = max(start, used_start)
        overlap_end = min(end, used_end)
        if overlap_end >= overlap_start:
            overlap += overlap_end - overlap_start + 1
    return overlap


def _select_global_script_matches(
    script_words,
    transcriptions,
    minimum_score=0.58,
    candidate_sets=None,
):
    """Assign clips independently of filenames, then resolve script conflicts.

    High-confidence clips claim their script ranges first.  This prevents an
    early, poorly recognised file from shifting every later file, while still
    allowing unordered filenames to be sorted by spoken content.
    """
    if candidate_sets is None:
        candidate_sets = [
            _record_window_candidates(script_words, item.get("text", ""))
            for item in transcriptions
        ]
    assignment_order = sorted(
        range(len(transcriptions)),
        key=lambda index: (
            -(candidate_sets[index][0]["adjusted_score"] if candidate_sets[index] else 0.0),
            -len(text_units(transcriptions[index].get("text", ""))),
            index,
        ),
    )
    assignments = [None] * len(transcriptions)
    used_ranges = []
    for clip_index in assignment_order:
        best = None
        for candidate in candidate_sets[clip_index]:
            length = max(1, candidate["end"] - candidate["start"] + 1)
            overlap_ratio = _overlap_size(
                candidate["start"], candidate["end"], used_ranges
            ) / length
            rank = candidate["adjusted_score"] - min(0.75, overlap_ratio * 0.75)
            if best is None or rank > best[0]:
                best = (rank, overlap_ratio, candidate)
        if best is None:
            continue
        rank, overlap_ratio, candidate = best
        # Never invent a script position for low-confidence audio.  A complete
        # overlap is usually a duplicate take and is safer in manual review.
        if candidate["adjusted_score"] < minimum_score or rank < 0.38:
            continue
        selected = dict(candidate)
        selected["overlap_ratio"] = overlap_ratio
        selected["assignment_score"] = rank
        assignments[clip_index] = selected
        used_ranges.append((selected["start"], selected["end"]))
    return assignments


def _content_order_evidence(
    script_words,
    transcriptions,
    minimum_score=0.48,
    candidate_sets=None,
):
    """Find script positions that are safe for ordering, not for approval.

    A clip can be clearly located near the beginning of a script while still
    containing enough ASR errors to require manual review.  Treating those two
    decisions as one threshold was the reason a 57.6% position match became
    completely "unmatched" and was moved behind later clips.
    """
    result = []
    for transcription_index, transcription in enumerate(transcriptions):
        candidates = (
            candidate_sets[transcription_index]
            if candidate_sets is not None
            else _record_window_candidates(
                script_words, transcription.get("text", ""), limit=24
            )
        )
        if not candidates:
            result.append({
                "reliable": False,
                "start": -1,
                "end": -1,
                "score": 0.0,
                "location_margin": 0.0,
            })
            continue
        best = candidates[0]
        best_length = max(1, int(best["end"]) - int(best["start"]) + 1)
        alternative = None
        for candidate in candidates[1:]:
            candidate_length = max(
                1, int(candidate["end"]) - int(candidate["start"]) + 1
            )
            overlap = _overlap_size(
                int(best["start"]),
                int(best["end"]),
                [(int(candidate["start"]), int(candidate["end"]))],
            )
            overlap_ratio = overlap / max(1, min(best_length, candidate_length))
            if overlap_ratio < 0.25:
                alternative = candidate
                break
        margin = (
            float(best.get("adjusted_score") or 0.0)
            - float(alternative.get("adjusted_score") or 0.0)
            if alternative is not None
            else float(best.get("adjusted_score") or 0.0)
        )
        reliable = (
            float(best.get("adjusted_score") or 0.0) >= minimum_score
            and (alternative is None or margin >= 0.035)
        )
        result.append({
            "reliable": reliable,
            "start": int(best["start"]),
            "end": int(best["end"]),
            "score": round(float(best.get("adjusted_score") or 0.0), 4),
            "similarity": round(float(best.get("similarity") or 0.0), 4),
            "location_margin": round(margin, 4),
        })
    return result


def _content_order_is_complete(evidence):
    if not evidence or not all(item.get("reliable") for item in evidence):
        return False
    ordered = sorted(evidence, key=lambda item: (item["start"], item["end"]))
    for previous, following in zip(ordered, ordered[1:]):
        previous_length = max(1, previous["end"] - previous["start"] + 1)
        following_length = max(1, following["end"] - following["start"] + 1)
        overlap = _overlap_size(
            previous["start"],
            previous["end"],
            [(following["start"], following["end"])],
        )
        if overlap / max(1, min(previous_length, following_length)) >= 0.65:
            return False
    return True


def _range_overlap_ratio(left, right):
    if not left.get("reliable") or not right.get("reliable"):
        return 0.0
    left_start, left_end = int(left["start"]), int(left["end"])
    right_start, right_end = int(right["start"]), int(right["end"])
    left_length = max(1, left_end - left_start + 1)
    right_length = max(1, right_end - right_start + 1)
    overlap = _overlap_size(
        left_start, left_end, [(right_start, right_end)]
    )
    return overlap / max(1, min(left_length, right_length))


def _duplicate_clip_quality(clip):
    probabilities = [
        float(word.get("probability") or 0.0)
        for word in clip.get("words", [])
        if word.get("probability") is not None
    ]
    average_probability = (
        sum(probabilities) / len(probabilities) if probabilities else 0.0
    )
    return (
        int(int(clip.get("script_word_start", -1)) >= 0),
        float(clip.get("similarity") or 0.0),
        float(clip.get("content_order_score") or 0.0),
        average_probability,
        -int(clip.get("source_index", 0)),
    )


def _token_multiset_similarity(left_units, right_units):
    """Compare spoken words without assuming Whisper kept sentence order.

    Generated takes occasionally move a short title from the beginning to the
    end.  SequenceMatcher then underrates what is still the same take.  The
    max-length denominator deliberately prevents a short excerpt from being
    mistaken for a duplicate of a longer clip.
    """
    if not left_units or not right_units:
        return 0.0
    left_counts = Counter(left_units)
    right_counts = Counter(right_units)
    shared = sum((left_counts & right_counts).values())
    return shared / max(len(left_units), len(right_units))


_DUPLICATE_CANDIDATE_STATE_FIELDS = (
    "included",
    "status",
    "issue_reason",
    "issues",
    "similarity",
    "script_word_start",
    "script_word_end",
    "script_start_line",
    "script_end_line",
    "expected_text",
    "matched_text",
    "recognized_match_start",
    "recognized_match_end",
    "word_timeline",
    "cues",
    "alignment_issues",
    "boundary_warnings",
    "boundary_decisions",
    "trim_start",
    "trim_end",
    "automatic_trim_start",
    "automatic_trim_end",
    "pause_removals",
    "automatic_pause_removals",
    "kept_ranges",
    "removed_seconds",
    "boundary_removed_seconds",
    "internal_removed_seconds",
    "review_acknowledged",
    "manual_text_override",
)


def _capture_duplicate_candidate_state(clip):
    return {
        key: copy.deepcopy(clip[key])
        for key in _DUPLICATE_CANDIDATE_STATE_FIELDS
        if key in clip
    }


def _restore_duplicate_candidate_state(clip):
    state = clip.get("duplicate_candidate_state")
    if not isinstance(state, dict):
        return
    for key in _DUPLICATE_CANDIDATE_STATE_FIELDS:
        if key in state:
            clip[key] = copy.deepcopy(state[key])
        else:
            clip.pop(key, None)


def _exclude_duplicate_candidate(clip, selected):
    reason = (
        f"与 {selected.get('file_name', '已选片段')} 属于同一组重复素材，"
        "当前未选中，不会参与生成。"
    )
    clip.update({
        "included": False,
        "duplicate_selected": False,
        "auto_excluded_duplicate": True,
        "duplicate_of": selected.get("file_name", ""),
        "status": "orange",
        "issue_reason": reason,
        "issues": [{
            "severity": "orange",
            "kind": "duplicate_clip_auto_excluded",
            "start": 0.0,
            "end": float(clip.get("original_duration") or 0.0),
            "title": "重复片段候选（当前未选）",
            "detail": reason,
            "expected": selected.get("expected_text", ""),
            "recognized": clip.get("recognized_text", ""),
        }],
    })


def _mark_duplicate_clips(clips, transcriptions, order_evidence):
    """Exclude repeated takes only when file/audio/location evidence agrees."""
    count = len(clips)
    if count < 2:
        return 0
    parents = list(range(count))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    stable_fingerprints = []
    normalized_paths = []
    for clip in clips:
        source = Path(clip.get("source") or "")
        normalized_paths.append(os.path.normcase(os.path.abspath(str(source))))
        try:
            stable_fingerprints.append(_stable_fingerprint(source))
        except OSError:
            stable_fingerprints.append(None)

    for left in range(count):
        left_units = text_units(transcriptions[left].get("text", ""))
        left_duration = max(
            0.01, float(transcriptions[left].get("duration") or 0.0)
        )
        for right in range(left + 1, count):
            same_path = normalized_paths[left] == normalized_paths[right]
            right_units = text_units(transcriptions[right].get("text", ""))
            transcript_similarity = max(
                _sequence_score(left_units, right_units),
                _token_multiset_similarity(left_units, right_units),
            )
            right_duration = max(
                0.01, float(transcriptions[right].get("duration") or 0.0)
            )
            duration_ratio = min(left_duration, right_duration) / max(
                left_duration, right_duration
            )
            same_sample = (
                stable_fingerprints[left] is not None
                and stable_fingerprints[left] == stable_fingerprints[right]
            )
            same_script_location = (
                _range_overlap_ratio(
                    order_evidence[left], order_evidence[right]
                ) >= 0.80
            )
            spoken_duplicate = (
                min(len(left_units), len(right_units)) >= 3
                and transcript_similarity >= 0.94
                and duration_ratio >= 0.72
                and same_script_location
            )
            copied_file = (
                same_sample
                and transcript_similarity >= 0.90
                and duration_ratio >= 0.90
            )
            if same_path or copied_file or spoken_duplicate:
                union(left, right)

    groups = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)
    duplicate_count = 0
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        winner_index = max(indexes, key=lambda index: _duplicate_clip_quality(clips[index]))
        winner = clips[winner_index]
        group_seed = "|".join(sorted(normalized_paths[index] for index in indexes))
        group_id = hashlib.sha1(
            group_seed.encode("utf-8", "replace")
        ).hexdigest()[:12]
        for index in indexes:
            candidate = clips[index]
            candidate.update({
                "duplicate_group_id": group_id,
                "duplicate_group_size": len(indexes),
                "duplicate_selected": index == winner_index,
                "duplicate_candidate_state": _capture_duplicate_candidate_state(
                    candidate
                ),
            })
        winner["auto_excluded_duplicate"] = False
        winner["duplicate_of"] = ""
        for index in indexes:
            if index == winner_index:
                continue
            duplicate = clips[index]
            _exclude_duplicate_candidate(duplicate, winner)
            duplicate_count += 1
    return duplicate_count


def select_duplicate_group_clip(task, selected_clip_index, settings=None):
    """Keep exactly one member of a detected duplicate group.

    The chosen take inherits the group's script position and export slot, but
    retains its own audio, ASR timings and trimming evidence.  This prevents a
    replacement take from jumping to the end of the finished video.
    """
    clips = task.get("clips", []) or []
    selected_clip_index = int(selected_clip_index)
    if selected_clip_index < 0 or selected_clip_index >= len(clips):
        raise IndexError("重复片段索引无效")
    selected = clips[selected_clip_index]
    group_id = str(selected.get("duplicate_group_id") or "")
    if not group_id:
        raise ValueError("所选片段不属于重复片段组")
    group_indexes = [
        index for index, clip in enumerate(clips)
        if str(clip.get("duplicate_group_id") or "") == group_id
    ]
    if len(group_indexes) < 2:
        raise ValueError("重复片段组至少需要两个候选")

    current_index = next(
        (
            index for index in group_indexes
            if clips[index].get("duplicate_selected")
        ),
        next(
            (index for index in group_indexes if clips[index].get("included", True)),
            group_indexes[0],
        ),
    )
    current = clips[current_index]
    if current.get("duplicate_selected") and not current.get(
        "auto_excluded_duplicate"
    ):
        current["duplicate_candidate_state"] = (
            _capture_duplicate_candidate_state(current)
        )

    role_start = int(current.get("script_word_start", -1))
    role_end = int(current.get("script_word_end", -1))
    if role_start < 0 or role_end < role_start:
        role_start = int(current.get("duplicate_role_script_start", -1))
        role_end = int(current.get("duplicate_role_script_end", -1))
    # The current winner may have been moved manually after an earlier switch.
    # Its live export order is therefore the authoritative group slot.
    role_order = int(current.get("export_order", current_index + 1))
    selected_previous_order = int(
        selected.get("export_order", selected_clip_index + 1)
    )
    for index in group_indexes:
        clips[index]["duplicate_role_script_start"] = role_start
        clips[index]["duplicate_role_script_end"] = role_end
        clips[index]["duplicate_role_export_order"] = role_order

    _restore_duplicate_candidate_state(selected)
    selected.update({
        "included": True,
        "duplicate_selected": True,
        "auto_excluded_duplicate": False,
        "duplicate_of": "",
        "export_order": role_order,
    })
    if current_index != selected_clip_index:
        current["export_order"] = selected_previous_order

    script_words = task.get("script_words", []) or []
    script_lines = task.get("script_lines", []) or []
    if (
        role_start >= 0
        and role_end >= role_start
        and role_end < len(script_words)
        and script_lines
    ):
        previous_range = (
            int(selected.get("script_word_start", -1)),
            int(selected.get("script_word_end", -1)),
        )
        selected["script_word_start"] = role_start
        selected["script_word_end"] = role_end
        _refresh_clip_after_script_range(
            selected,
            script_words,
            script_lines,
            normalize_smart_video_editor_settings(settings),
            previous_script_range=previous_range,
        )
        score = _sequence_score(
            text_units(selected.get("expected_text", "")),
            text_units(selected.get("recognized_text", "")),
        )
        selected["similarity"] = round(score, 4)
        status, reason = _classify(
            score,
            selected.get("expected_text", ""),
            selected.get("recognized_text", ""),
            normalize_smart_video_editor_settings(settings),
        )
        serious = [
            issue for issue in selected.get("issues", [])
            if issue.get("severity") in {"pink", "orange"}
        ]
        if any(issue.get("severity") == "pink" for issue in serious):
            status = "pink"
            reason = _issue_summary(next(
                issue for issue in serious if issue.get("severity") == "pink"
            ))
        elif serious and status == "green":
            status = "orange"
            reason = _issue_summary(serious[0])
        selected["status"] = status
        selected["issue_reason"] = reason

    selected["duplicate_candidate_state"] = _capture_duplicate_candidate_state(
        selected
    )
    for index in group_indexes:
        if index != selected_clip_index:
            _exclude_duplicate_candidate(clips[index], selected)
    task["duplicate_count"] = sum(
        max(0, len([
            clip for clip in clips
            if clip.get("duplicate_group_id") == candidate_group
        ]) - 1)
        for candidate_group in {
            clip.get("duplicate_group_id") for clip in clips
            if clip.get("duplicate_group_id")
        }
    )
    return group_indexes


def _interpolate_script_word_times(script_slice, clip_slice):
    """Map authoritative words onto real ASR word times using fuzzy LCS anchors."""
    if not script_slice or not clip_slice:
        return []
    script_norms = [word["norm"] for word in script_slice]
    alignment_clip = _alignment_unit_records(clip_slice)
    clip_norms = [word["norm"] for word in alignment_clip]
    pairs = _token_lcs_pairs(script_norms, clip_norms)
    mapped = [None] * len(script_slice)
    for script_index, clip_index in pairs:
        clip_word = alignment_clip[clip_index]
        mapped[script_index] = {
            "start": float(clip_word.get("start") or 0.0),
            "end": float(clip_word.get("end") or 0.0),
            "source_word_index": int(clip_word.get("source_index", clip_index)),
            "anchor": True,
        }

    if not pairs:
        # Very weak alignment remains review-only, but a proportional timeline
        # makes manual inspection useful without creating zero-length flashes.
        for script_index in range(len(script_slice)):
            clip_index = min(
                len(alignment_clip) - 1,
                round(
                    script_index * (len(alignment_clip) - 1)
                    / max(1, len(script_slice) - 1)
                ),
            )
            clip_word = alignment_clip[clip_index]
            mapped[script_index] = {
                "start": float(clip_word.get("start") or 0.0),
                "end": float(clip_word.get("end") or 0.0),
                "source_word_index": int(clip_word.get("source_index", clip_index)),
                "anchor": False,
            }
    else:
        index = 0
        while index < len(mapped):
            if mapped[index] is not None:
                index += 1
                continue
            gap_start = index
            while index < len(mapped) and mapped[index] is None:
                index += 1
            gap_end = index - 1
            previous_end = (
                mapped[gap_start - 1]["end"]
                if gap_start > 0 and mapped[gap_start - 1] is not None
                else float(alignment_clip[0].get("start") or 0.0)
            )
            next_start = (
                mapped[index]["start"]
                if index < len(mapped) and mapped[index] is not None
                else float(alignment_clip[-1].get("end") or previous_end)
            )
            available = max(0.0, next_start - previous_end)
            count = gap_end - gap_start + 1
            step = available / (count + 1)
            for offset, script_index in enumerate(range(gap_start, gap_end + 1)):
                start = previous_end + step * (offset + 0.10)
                end = previous_end + step * (offset + 0.90)
                if end <= start:
                    end = start + 0.05
                mapped[script_index] = {
                    "start": start,
                    "end": end,
                    "source_word_index": None,
                    "anchor": False,
                }

    timeline = []
    for script_word, timing in zip(script_slice, mapped):
        timeline.append({
            "script_word_index": script_word["word_index"],
            "line_index": script_word["line_index"],
            "raw": script_word["raw"],
            "start": round(float(timing["start"]), 3),
            "end": round(float(timing["end"]), 3),
            "source_word_index": timing["source_word_index"],
            "anchor": bool(timing["anchor"]),
        })
    return timeline


def _effective_pass_threshold(unit_count, settings):
    configured = settings["pass_similarity_percent"] / 100.0
    if unit_count <= 3:
        return min(configured, 0.58)
    if unit_count <= 7:
        return min(configured, 0.68)
    if unit_count <= 12:
        return min(configured, 0.72)
    return configured


def _classify(score, expected_text, recognized_text, settings):
    severe = settings["severe_similarity_percent"] / 100.0
    passed = _effective_pass_threshold(len(text_units(expected_text)), settings)
    if not recognized_text.strip():
        return "pink", "未识别到有效语音，必须人工核对"
    if score < severe:
        return "pink", "文案与视频差异较大，必须人工核对"
    if score < passed:
        return "orange", "存在明显单词差异，请核对后再生成"
    return "green", "仅有轻微差异或标点/大小写差异"


def _parse_silence_ranges(stderr, duration):
    """Parse FFmpeg silencedetect events in their emitted order."""
    ranges = []
    current_start = None
    pattern = re.compile(r"silence_(start|end):\s*(-?[0-9.]+)")
    for match in pattern.finditer(str(stderr or "")):
        value = max(0.0, float(match.group(2)))
        if match.group(1) == "start":
            current_start = value
        elif current_start is not None:
            end = min(max(current_start, value), max(0.0, float(duration)))
            if end > current_start:
                ranges.append([round(current_start, 3), round(end, 3)])
            current_start = None
    if current_start is not None and duration > current_start:
        ranges.append([round(current_start, 3), round(float(duration), 3)])
    return ranges


def detect_silence_ranges(source, ffmpeg, duration, settings):
    """Use actual audio level, not ASR gaps, to locate quiet intervals."""
    if not settings.get("silence_detection_enabled", True):
        return [], "静音分贝检测已关闭"
    threshold = int(settings["silence_threshold_db"])
    minimum = settings["min_silence_ms"] / 1000.0
    args = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-vn",
        "-af",
        f"silencedetect=noise={threshold}dB:d={minimum:.3f}",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [], f"FFmpeg 分贝检测失败：{error}"
    ranges = _parse_silence_ranges(completed.stderr, duration)
    if completed.returncode not in {0, 255} and not ranges:
        tail = "\n".join(completed.stderr.strip().splitlines()[-3:])
        return [], "FFmpeg 分贝检测没有完成" + (f"：{tail}" if tail else "")
    return ranges, ""


def _merge_time_ranges(ranges, duration, join_gap=0.01):
    duration = max(0.0, float(duration or 0.0))
    normalized = []
    for value in ranges or []:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        start = max(0.0, min(duration, float(value[0])))
        end = max(start, min(duration, float(value[1])))
        if end - start >= 0.001:
            normalized.append([start, end])
    normalized.sort(key=lambda item: (item[0], item[1]))
    merged = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + join_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[round(start, 3), round(end, 3)] for start, end in merged]


def _complement_time_ranges(ranges, duration):
    duration = max(0.0, float(duration or 0.0))
    result = []
    cursor = 0.0
    for start, end in _merge_time_ranges(ranges, duration):
        if start > cursor + 0.001:
            result.append([round(cursor, 3), round(start, 3)])
        cursor = max(cursor, end)
    if duration > cursor + 0.001:
        result.append([round(cursor, 3), round(duration, 3)])
    return result


def intersect_time_ranges(left_ranges, right_ranges, duration):
    """Return time covered by both normalized range sets."""
    left = _merge_time_ranges(left_ranges, duration)
    right = _merge_time_ranges(right_ranges, duration)
    result = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        start = max(left[left_index][0], right[right_index][0])
        end = min(left[left_index][1], right[right_index][1])
        if end - start >= 0.001:
            result.append([start, end])
        if left[left_index][1] <= right[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return _merge_time_ranges(result, duration)


def subtract_time_ranges(source_ranges, excluded_ranges, duration):
    """Subtract excluded time from source ranges."""
    source = _merge_time_ranges(source_ranges, duration)
    excluded = _merge_time_ranges(excluded_ranges, duration)
    result = []
    for source_start, source_end in source:
        cursor = source_start
        for excluded_start, excluded_end in excluded:
            if excluded_end <= cursor:
                continue
            if excluded_start >= source_end:
                break
            if excluded_start > cursor + 0.001:
                result.append([cursor, min(source_end, excluded_start)])
            cursor = max(cursor, excluded_end)
            if cursor >= source_end:
                break
        if cursor < source_end - 0.001:
            result.append([cursor, source_end])
    return _merge_time_ranges(result, duration)


def detect_voice_absence_ranges(source, duration, settings):
    """Locate intervals without human speech, even when background noise exists."""
    if not settings.get("voice_detection_enabled", True):
        return [], "人声活动检测已关闭"
    try:
        # faster-whisper is imported before PyQt by the application entrypoint.
        # Import these helpers lazily here so the engine does not create a
        # second Whisper model or add another runtime dependency.
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        sampling_rate = 16000
        audio = decode_audio(str(source), sampling_rate=sampling_rate)
        if getattr(audio, "size", 0) <= 0:
            return [], "视频没有可分析的音频"
        audio_duration = len(audio) / float(sampling_rate)
        effective_duration = max(0.0, float(duration or audio_duration))
        speech_chunks = get_speech_timestamps(
            audio,
            VadOptions(
                threshold=float(settings.get("vad_threshold", 0.65)),
                neg_threshold=float(
                    settings.get("vad_neg_threshold", 0.50)
                ),
                min_speech_duration_ms=int(
                    settings.get("vad_min_speech_ms", 120)
                ),
                min_silence_duration_ms=int(
                    settings.get("min_silence_ms", 350)
                ),
                speech_pad_ms=int(settings.get("vad_speech_pad_ms", 120)),
            ),
            sampling_rate=sampling_rate,
        )
        speech_ranges = [
            [
                max(0.0, float(item.get("start", 0)) / sampling_rate),
                min(
                    effective_duration,
                    float(item.get("end", 0)) / sampling_rate,
                ),
            ]
            for item in speech_chunks
        ]
        return _complement_time_ranges(
            speech_ranges, effective_duration
        ), ""
    except Exception as error:
        return [], f"人声活动检测失败：{type(error).__name__}: {error}"


def combine_breath_gap_ranges(
    duration,
    silence_ranges,
    voice_absence_ranges,
    mode="voice_refined",
    db_detection_available=True,
    voice_detection_available=True,
    boundary_extension_s=0.55,
):
    """Combine dB and VAD evidence using a user-selected set operation.

    ``voice_refined`` keeps VAD authoritative and only lets an overlapping dB
    interval refine its padded boundary. ``union`` is deliberately aggressive;
    ``intersection`` is deliberately conservative.
    """
    mode = str(mode or "voice_refined").strip().lower()
    if mode not in BREATH_DETECTION_MODES:
        mode = "voice_refined"
    db_ranges = (
        _merge_time_ranges(silence_ranges, duration)
        if db_detection_available else []
    )
    voice_ranges = (
        _merge_time_ranges(voice_absence_ranges, duration)
        if voice_detection_available else []
    )
    if mode == "db_only":
        return db_ranges
    if mode == "voice_only":
        return voice_ranges
    if mode == "union":
        return _merge_time_ranges(db_ranges + voice_ranges, duration)
    if mode == "intersection":
        if not db_detection_available or not voice_detection_available:
            return []
        return intersect_time_ranges(db_ranges, voice_ranges, duration)
    if not voice_detection_available:
        return db_ranges
    if not db_detection_available:
        return voice_ranges
    if not voice_ranges:
        return []
    extension = max(0.0, float(boundary_extension_s or 0.0))
    refined = [list(value) for value in voice_ranges]
    for db_start, db_end in db_ranges:
        for voice_range in refined:
            voice_start, voice_end = voice_range
            if db_end < voice_start or db_start > voice_end:
                continue
            voice_range[0] = min(
                voice_start, max(db_start, voice_start - extension)
            )
            voice_range[1] = max(
                voice_end, min(db_end, voice_end + extension)
            )
    return _merge_time_ranges(refined, duration)


def _combined_detection_error(settings, silence_error, voice_error):
    mode = str(settings.get("breath_detection_mode") or "voice_refined")
    db_enabled = bool(settings.get("silence_detection_enabled", True))
    voice_enabled = bool(settings.get("voice_detection_enabled", True))
    db_error = str(silence_error or "") if db_enabled else "分贝检测已关闭"
    vad_error = str(voice_error or "") if voice_enabled else "人声检测已关闭"
    db_ok = db_enabled and not db_error
    voice_ok = voice_enabled and not vad_error
    if mode == "db_only":
        return "" if db_ok else db_error
    if mode == "voice_only":
        return "" if voice_ok else vad_error
    if mode == "intersection":
        missing = []
        if not db_ok:
            missing.append(db_error)
        if not voice_ok:
            missing.append(vad_error)
        return "；".join(missing)
    if db_ok or voice_ok:
        return ""
    return "；".join(value for value in (db_error, vad_error) if value)


def refine_trim_boundaries(
    first_word_start,
    last_word_end,
    duration,
    silence_ranges,
    settings,
    detection_error="",
):
    """Choose cuts only inside measured silence surrounding matched speech.

    If a suitable quiet interval is unavailable, preserve that source edge.
    Keeping a little extra footage is preferable to cutting a spoken syllable.
    """
    duration = max(0.0, float(duration))
    first_word_start = max(0.0, min(duration, float(first_word_start)))
    last_word_end = max(first_word_start, min(duration, float(last_word_end)))
    lead = settings["lead_padding_ms"] / 1000.0
    tail = settings["tail_padding_ms"] / 1000.0
    search = settings["boundary_search_ms"] / 1000.0
    trim_start = 0.0
    trim_end = duration
    decisions = []
    warnings = []

    if not settings.get("silence_detection_enabled", True):
        return trim_start, trim_end, decisions, warnings
    if detection_error:
        warnings.append({
            "severity": "orange",
            "kind": "silence_detection_failed",
            "start": 0.0,
            "end": duration,
            "title": "气口检测失败",
            "detail": detection_error + "；已保守保留原片首尾。",
        })
        return trim_start, trim_end, decisions, warnings

    before = [
        item for item in silence_ranges
        if item[1] <= first_word_start + 0.001
        and first_word_start - item[1] <= search
    ]
    if before:
        quiet = max(before, key=lambda item: item[1])
        trim_start = max(0.0, min(first_word_start, quiet[1] - lead))
        if trim_start >= 0.08:
            decisions.append({
                "severity": "info",
                "kind": "head_cut",
                "start": 0.0,
                "end": trim_start,
                "title": "片头气口裁切",
                "detail": (
                    f"在 {quiet[0]:.3f}-{quiet[1]:.3f} 秒检测到无人声或"
                    "低音量气口，保留 "
                    f"{settings['lead_padding_ms']} ms 前留白。"
                ),
            })
    elif first_word_start > lead + 0.25:
        warnings.append({
            "severity": "orange",
            "kind": "head_boundary_unconfirmed",
            "start": 0.0,
            "end": first_word_start,
            "title": "片头切点没有气口证据",
            "detail": "没有在首个匹配单词前找到可靠的无人声或低音量区间，片头未自动裁切。",
        })

    after = [
        item for item in silence_ranges
        if item[0] >= last_word_end - 0.001
        and item[0] - last_word_end <= search
    ]
    if after:
        quiet = min(after, key=lambda item: item[0])
        trim_end = min(duration, max(last_word_end, quiet[0] + tail))
        if duration - trim_end >= 0.08:
            decisions.append({
                "severity": "info",
                "kind": "tail_cut",
                "start": trim_end,
                "end": duration,
                "title": "片尾气口裁切",
                "detail": (
                    f"在 {quiet[0]:.3f}-{quiet[1]:.3f} 秒检测到无人声或"
                    "低音量气口，保留 "
                    f"{settings['tail_padding_ms']} ms 后留白。"
                ),
            })
    elif duration - last_word_end > tail + 0.35:
        warnings.append({
            "severity": "orange",
            "kind": "tail_boundary_unconfirmed",
            "start": last_word_end,
            "end": duration,
            "title": "片尾切点没有气口证据",
            "detail": "没有在最后匹配单词后找到可靠的无人声或低音量区间，片尾未自动裁切。",
        })
    return trim_start, trim_end, decisions, warnings


def find_pause_removals(
    words, trim_start, trim_end, settings, silence_ranges=None
):
    if not settings["compress_internal_pauses"]:
        return []
    # Whisper may miss a quietly spoken word.  A word timestamp gap alone is
    # never sufficient evidence for deleting audio; require measured silence.
    if not silence_ranges:
        return []
    threshold = settings["pause_threshold_ms"] / 1000.0
    keep_before = settings["pause_keep_before_ms"] / 1000.0
    keep_after = settings["pause_keep_after_ms"] / 1000.0
    inside = [
        word for word in words
        if word.get("end", 0.0) > trim_start and word.get("start", 0.0) < trim_end
    ]
    removals = []
    for left, right in zip(inside, inside[1:]):
        gap_start = float(left.get("end", 0.0))
        gap_end = float(right.get("start", gap_start))
        gap = gap_end - gap_start
        if gap < threshold:
            continue
        measured = [
            [max(gap_start, quiet_start), min(gap_end, quiet_end)]
            for quiet_start, quiet_end in silence_ranges
            if min(gap_end, quiet_end) - max(gap_start, quiet_start) >= threshold
        ]
        if not measured:
            continue
        quiet_start, quiet_end = max(
            measured, key=lambda item: item[1] - item[0]
        )
        remove_start = max(trim_start, quiet_start + keep_before)
        remove_end = min(trim_end, quiet_end - keep_after)
        if remove_end - remove_start >= 0.08:
            removals.append([round(remove_start, 3), round(remove_end, 3)])
    return removals


def kept_ranges(trim_start, trim_end, removals):
    trim_start = max(0.0, float(trim_start))
    trim_end = max(trim_start, float(trim_end))
    result = []
    cursor = trim_start
    for start, end in sorted(removals):
        start = max(trim_start, min(trim_end, float(start)))
        end = max(start, min(trim_end, float(end)))
        if start > cursor + 0.001:
            result.append([round(cursor, 3), round(start, 3)])
        cursor = max(cursor, end)
    if trim_end > cursor + 0.001:
        result.append([round(cursor, 3), round(trim_end, 3)])
    return result


def breath_cut_plan(duration, silence_ranges, settings):
    """Build conservative standalone cuts from verified breath gaps."""
    duration = max(0.0, float(duration or 0.0))
    normalized = []
    for value in silence_ranges or []:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        start = max(0.0, min(duration, float(value[0])))
        end = max(start, min(duration, float(value[1])))
        if end - start >= 0.03:
            normalized.append([start, end])
    normalized.sort(key=lambda value: (value[0], value[1]))
    merged = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    trim_start = 0.0
    trim_end = duration
    lead = max(0.0, float(settings.get("lead_padding_ms", 120))) / 1000.0
    tail = max(0.0, float(settings.get("tail_padding_ms", 220))) / 1000.0
    edge_tolerance = 0.08
    if merged and merged[0][0] <= edge_tolerance:
        trim_start = max(0.0, merged[0][1] - lead)
    if merged and merged[-1][1] >= duration - edge_tolerance:
        trim_end = min(duration, merged[-1][0] + tail)

    removals = []
    if settings.get("compress_internal_pauses", False):
        threshold = max(
            0.08, float(settings.get("pause_threshold_ms", 1000)) / 1000.0
        )
        keep_left = max(
            0.0, float(settings.get("pause_keep_before_ms", 160)) / 1000.0
        )
        keep_right = max(
            0.0, float(settings.get("pause_keep_after_ms", 160)) / 1000.0
        )
        for quiet_start, quiet_end in merged:
            quiet_start = max(trim_start, quiet_start)
            quiet_end = min(trim_end, quiet_end)
            if quiet_end - quiet_start < threshold:
                continue
            remove_start = quiet_start + keep_left
            remove_end = quiet_end - keep_right
            if remove_end - remove_start >= 0.08:
                removals.append([
                    round(remove_start, 3), round(remove_end, 3)
                ])

    # Do not turn a silent or malformed source into an empty output.  Keeping
    # the original is safer and lets the user inspect it on the timeline.
    planned_kept = kept_ranges(trim_start, trim_end, removals)
    if sum(end - start for start, end in planned_kept) < 0.08:
        trim_start, trim_end, removals = 0.0, duration, []
        planned_kept = kept_ranges(trim_start, trim_end, removals)
    return (
        round(trim_start, 3),
        round(trim_end, 3),
        removals,
        planned_kept,
    )


def apply_manual_breath_overrides(
    clip,
    automatic_trim_start=None,
    automatic_trim_end=None,
    automatic_removals=None,
):
    """Apply durable user edits on top of the latest automatic cut plan.

    Automatic analysis and manual review are stored separately. This lets a
    user re-run dB/VAD detection without losing ranges they explicitly restored
    or added on the timeline.
    """
    duration = max(0.0, float(clip.get("original_duration") or 0.0))
    if automatic_trim_start is None:
        automatic_trim_start = clip.get(
            "automatic_trim_start", clip.get("trim_start", 0.0)
        )
    if automatic_trim_end is None:
        automatic_trim_end = clip.get(
            "automatic_trim_end", clip.get("trim_end", duration)
        )
    if automatic_removals is None:
        automatic_removals = clip.get(
            "automatic_pause_removals", clip.get("pause_removals", [])
        )

    automatic_trim_start = max(
        0.0, min(duration, float(automatic_trim_start or 0.0))
    )
    automatic_trim_end = max(
        automatic_trim_start,
        min(duration, float(automatic_trim_end or duration)),
    )
    automatic_removals = _merge_time_ranges(
        automatic_removals or [], duration
    )
    clip["automatic_trim_start"] = round(automatic_trim_start, 3)
    clip["automatic_trim_end"] = round(automatic_trim_end, 3)
    clip["automatic_pause_removals"] = automatic_removals

    deleted = list(automatic_removals)
    if automatic_trim_start > 0.001:
        deleted.append([0.0, automatic_trim_start])
    if automatic_trim_end < duration - 0.001:
        deleted.append([automatic_trim_end, duration])
    deleted.extend(clip.get("manual_delete_ranges", []) or [])
    deleted = _merge_time_ranges(deleted, duration)

    restored = list(clip.get("manual_keep_ranges", []) or [])
    if clip.get("manual_keep_head"):
        restored.append([0.0, automatic_trim_start])
    if clip.get("manual_keep_tail"):
        restored.append([automatic_trim_end, duration])
    deleted = subtract_time_ranges(deleted, restored, duration)

    trim_start = 0.0
    trim_end = duration
    internal = []
    for left, right in deleted:
        if left <= 0.001:
            trim_start = max(trim_start, right)
        elif right >= duration - 0.001:
            trim_end = min(trim_end, left)
        else:
            internal.append([left, right])
    if trim_end <= trim_start + 0.08:
        # A bad pair of manual marks must never create an empty export.
        trim_start, trim_end, internal = 0.0, duration, []
    else:
        internal = _merge_time_ranges([
            [max(trim_start, left), min(trim_end, right)]
            for left, right in internal
            if min(trim_end, right) - max(trim_start, left) >= 0.08
        ], duration)

    clip["trim_start"] = round(trim_start, 3)
    clip["trim_end"] = round(trim_end, 3)
    clip["pause_removals"] = internal
    clip["kept_ranges"] = kept_ranges(trim_start, trim_end, internal)
    boundary_removed = trim_start + max(0.0, duration - trim_end)
    internal_removed = sum(right - left for left, right in internal)
    clip["boundary_removed_seconds"] = round(boundary_removed, 3)
    clip["internal_removed_seconds"] = round(internal_removed, 3)
    clip["removed_seconds"] = round(
        boundary_removed + internal_removed, 3
    )
    return clip


def source_time_in_kept_ranges(value, ranges):
    value = float(value)
    elapsed = 0.0
    for start, end in ranges:
        if value <= start:
            return elapsed
        if value < end:
            return elapsed + value - start
        elapsed += end - start
    return elapsed


def _cue_plans_from_timeline(timeline, script_words, script_lines):
    cues = []
    by_index = {item["script_word_index"]: item for item in timeline}
    line_indexes = sorted({item["line_index"] for item in timeline})
    for line_index in line_indexes:
        line_records = [
            word for word in script_words
            if word["line_index"] == line_index and word["word_index"] in by_index
        ]
        if not line_records:
            continue
        timings = [by_index[word["word_index"]] for word in line_records]
        cues.append({
            "text": script_text_from_word_records(line_records, script_lines),
            "start": min(item["start"] for item in timings),
            "end": max(item["end"] for item in timings),
            "script_word_start": line_records[0]["word_index"],
            "script_word_end": line_records[-1]["word_index"],
        })
    return cues


def _alignment_issues(script_slice, clip_slice, script_lines):
    """Return human-readable word differences with source time positions."""
    if not script_slice:
        return []
    clip_units = _alignment_unit_records(clip_slice)
    script_norms = [word["norm"] for word in script_slice]
    clip_norms = [word["norm"] for word in clip_units]
    pairs = _token_lcs_pairs(script_norms, clip_norms)
    issues = []
    previous_script = -1
    previous_clip = -1
    sentinels = pairs + [(len(script_slice), len(clip_units))]
    for script_index, clip_index in sentinels:
        expected_records = script_slice[previous_script + 1:script_index]
        observed_units = clip_units[previous_clip + 1:clip_index]
        if expected_records or observed_units:
            observed_record_indexes = []
            for unit in observed_units:
                record_index = int(unit["record_index"])
                if not observed_record_indexes or observed_record_indexes[-1] != record_index:
                    observed_record_indexes.append(record_index)
            observed_records = [
                clip_slice[index]
                for index in observed_record_indexes
                if 0 <= index < len(clip_slice)
            ]
            expected_text = script_text_from_word_records(
                expected_records, script_lines
            ) if expected_records else ""
            recognized_text = " ".join(
                str(record.get("raw") or "").strip()
                for record in observed_records
            ).strip()
            if observed_units:
                start = min(unit["start"] for unit in observed_units)
                end = max(unit["end"] for unit in observed_units)
            else:
                previous_end = (
                    clip_units[previous_clip]["end"]
                    if 0 <= previous_clip < len(clip_units)
                    else 0.0
                )
                next_start = (
                    clip_units[clip_index]["start"]
                    if 0 <= clip_index < len(clip_units)
                    else previous_end
                )
                start, end = previous_end, max(previous_end, next_start)
            expected_count = len(expected_records)
            observed_count = len(observed_units)
            affected = max(expected_count, observed_count)
            severity = (
                "pink"
                if affected >= 4
                or expected_count / max(1, len(script_slice)) >= 0.35
                else "orange"
            )
            if expected_records and observed_units:
                title = "单词不一致"
                detail = "正确文案与视频在此处出现替换或错读。"
                kind = "word_difference"
            elif expected_records:
                title = "疑似漏读"
                detail = "任务文案中的这些单词没有找到可靠读音。"
                kind = "missing_words"
            else:
                title = "疑似多读"
                detail = "视频中识别到任务文案没有的额外内容。"
                kind = "extra_words"
            issues.append({
                "severity": severity,
                "kind": kind,
                "script_word_start": (
                    int(expected_records[0]["word_index"])
                    if expected_records else None
                ),
                "script_word_end": (
                    int(expected_records[-1]["word_index"])
                    if expected_records else None
                ),
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "line_start": (
                    int(expected_records[0]["line_index"]) + 1
                    if expected_records else None
                ),
                "line_end": (
                    int(expected_records[-1]["line_index"]) + 1
                    if expected_records else None
                ),
                "title": title,
                "expected": expected_text,
                "recognized": recognized_text,
                "detail": detail,
            })
        previous_script = script_index
        previous_clip = clip_index
    return issues


def _issue_summary(issue):
    start = float(issue.get("start") or 0.0)
    end = float(issue.get("end") or start)
    time_text = f"{start:.3f}-{end:.3f} 秒"
    expected = str(issue.get("expected") or "").strip()
    recognized = str(issue.get("recognized") or "").strip()
    comparison = ""
    if expected or recognized:
        comparison = f"；应为「{expected or '（无）'}」，识别为「{recognized or '（无）'}」"
    return f"{time_text} {issue.get('title', '问题')}：{issue.get('detail', '')}{comparison}"


def _recognized_text_in_range(words, start, end):
    values = [
        str(word.get("text") or "").strip()
        for word in words
        if float(word.get("end") or 0.0) > float(start)
        and float(word.get("start") or 0.0) < float(end)
        and str(word.get("text") or "").strip()
    ]
    return " ".join(values)


def _build_clip_plan(
    source,
    transcription,
    assignment,
    script_words,
    script_lines,
    settings,
    source_index,
    silence_ranges=None,
    silence_detection_error="",
    voice_absence_ranges=None,
    voice_detection_error="",
):
    words = transcription.get("words", [])
    recognized_words = _recognized_word_records(words)
    recognized_text = transcription.get("text", "").strip()
    duration = max(
        float(transcription.get("duration") or 0.0),
        max((float(word.get("end") or 0.0) for word in words), default=0.0),
    )
    expected_text = ""
    script_slice = []
    clip_slice = []
    word_timeline = []
    score = 0.0
    script_start = -1
    script_end = -1
    overlap_ratio = 0.0
    matched_text = ""
    recognized_match_start = -1
    recognized_match_end = -1

    if assignment is not None:
        script_start = int(assignment["start"])
        script_end = int(assignment["end"])
        script_slice = script_words[script_start:script_end + 1]
        expected_text = script_text_from_word_records(script_slice, script_lines)
        cut_candidates = _record_window_candidates(
            recognized_words, expected_text, limit=1
        )
        cut_match = cut_candidates[0] if cut_candidates else None
        if cut_match is not None:
            recognized_match_start = int(cut_match["start"])
            recognized_match_end = int(cut_match["end"])
            clip_slice = recognized_words[
                cut_match["start"]:cut_match["end"] + 1
            ]
            matched_text = " ".join(item["raw"] for item in clip_slice)
            word_timeline = _interpolate_script_word_times(
                script_slice, clip_slice
            )
            score = max(
                float(assignment.get("similarity") or 0.0),
                float(cut_match.get("similarity") or 0.0),
            )
        overlap_ratio = float(assignment.get("overlap_ratio") or 0.0)

    silence_ranges = list(silence_ranges or [])
    voice_absence_ranges = list(voice_absence_ranges or [])
    breath_gap_ranges = combine_breath_gap_ranges(
        duration,
        silence_ranges,
        voice_absence_ranges,
        mode=settings.get("breath_detection_mode", "voice_refined"),
        db_detection_available=(
            settings.get("silence_detection_enabled", True)
            and not silence_detection_error
        ),
        voice_detection_available=(
            settings.get("voice_detection_enabled", True)
            and not voice_detection_error
        ),
        boundary_extension_s=min(
            0.55,
            settings.get("vad_speech_pad_ms", 120) / 1000.0 + 0.1,
        ),
    )
    detection_error = _combined_detection_error(
        settings, silence_detection_error, voice_detection_error
    )
    boundary_decisions = []
    boundary_warnings = []
    # Script matching and breath trimming are separate confidence decisions.
    # A foreign-language spelling/tokenisation error can prevent this clip
    # from being assigned to the authoritative script even though Whisper has
    # still located the spoken audio accurately.  In that case keep the clip
    # pink, but do not retain an otherwise proven multi-second head/tail gap:
    # recognized word timestamps + measured dB silence are sufficient for an
    # edge cut.  They are deliberately *not* used to make internal cuts here.
    # Trimming is a physical-audio decision, so its outer boundary must use
    # every word actually heard by ASR. A fuzzy script window may omit a
    # differently spelled final word even though it is present in the audio;
    # that shorter window can move the search origin too far left and miss a
    # real multi-second tail gap.
    boundary_words = recognized_words or clip_slice
    if boundary_words:
        first_word = boundary_words[0]
        last_word = boundary_words[-1]
        trim_start, trim_end, boundary_decisions, boundary_warnings = (
            refine_trim_boundaries(
                float(first_word.get("start") or 0.0),
                float(last_word.get("end") or duration),
                duration,
                breath_gap_ranges,
                settings,
                detection_error,
            )
        )
        # Missing anchors at an outer boundary mean ASR did not prove where the
        # utterance starts/ends. Preserve that source edge even if a nearby
        # quiet interval exists.
        first_recognized_start = min(
            (float(item.get("start") or 0.0) for item in recognized_words),
            default=float(first_word.get("start") or 0.0),
        )
        last_recognized_end = max(
            (float(item.get("end") or 0.0) for item in recognized_words),
            default=float(last_word.get("end") or duration),
        )
        head_no_voice_proven = any(
            float(left) <= 0.03
            and float(right) <= first_recognized_start + 0.05
            for left, right in voice_absence_ranges
        )
        tail_no_voice_proven = any(
            float(right) >= duration - 0.03
            and float(left) >= last_recognized_end - 0.05
            for left, right in voice_absence_ranges
        )
        if (
            clip_slice
            and word_timeline
            and not word_timeline[0].get("anchor", False)
            and not head_no_voice_proven
        ):
            trim_start = 0.0
            boundary_warnings.append({
                "severity": "pink",
                "kind": "unconfirmed_first_word",
                "start": 0.0,
                "end": float(first_word.get("start") or 0.0),
                "title": "首词时间未确认",
                "detail": "首个文案单词没有直接识别锚点，片头已保留，请人工核对。",
            })
        if (
            clip_slice
            and word_timeline
            and not word_timeline[-1].get("anchor", False)
            and not tail_no_voice_proven
        ):
            trim_end = duration
            boundary_warnings.append({
                "severity": "pink",
                "kind": "unconfirmed_last_word",
                "start": float(last_word.get("end") or duration),
                "end": duration,
                "title": "尾词时间未确认",
                "detail": "最后一个文案单词没有直接识别锚点，片尾已保留，请人工核对。",
            })
    else:
        trim_start, trim_end = 0.0, duration
    if trim_end <= trim_start:
        trim_start, trim_end = 0.0, duration

    removals = find_pause_removals(
        words, trim_start, trim_end, settings, breath_gap_ranges
    )
    ranges = kept_ranges(trim_start, trim_end, removals)
    alignment_issues = _alignment_issues(
        script_slice, clip_slice, script_lines
    )
    removal_decisions = [
        {
            "severity": "info",
            "kind": "internal_silence_cut",
            "start": start,
            "end": end,
            "title": "句内静音压缩",
            "detail": (
                "此区间同时满足词间隔和无人声/低音量证据，"
                "将按实验设置压缩。"
            ),
        }
        for start, end in removals
    ]
    for decision in boundary_decisions + removal_decisions:
        decision["expected"] = ""
        decision["recognized"] = _recognized_text_in_range(
            words, decision["start"], decision["end"]
        )
        if decision["recognized"]:
            decision["detail"] += (
                f" 删除区间识别内容：「{decision['recognized']}」。"
            )
    issues = alignment_issues + boundary_warnings + boundary_decisions + removal_decisions
    if assignment is None:
        status, reason = "pink", "无法在完整任务文案中可靠定位，未自动猜测顺序"
        issues.insert(0, {
            "severity": "pink",
            "kind": "script_position_unmatched",
            "start": 0.0,
            "end": duration,
            "title": "无法定位文案顺序",
            "detail": reason,
            "expected": "",
            "recognized": recognized_text,
        })
    else:
        status, reason = _classify(score, expected_text, recognized_text, settings)
        anchor_count = sum(item.get("anchor", False) for item in word_timeline)
        anchor_ratio = anchor_count / max(1, len(script_slice))
        if anchor_ratio < 0.40:
            status = "pink"
            reason = "可确认的对应单词太少，必须人工核对"
        elif anchor_ratio < 0.68 and status == "green":
            status = "orange"
            reason = "部分单词时间由相邻读音推算，请试听核对"
        if overlap_ratio >= 0.45:
            status = "pink"
            reason = "与其他视频匹配到同一段文案，可能是重复片段"
        elif overlap_ratio > 0.0 and status == "green":
            status = "orange"
            reason = "与相邻片段存在少量重复读音，请核对边界"
        issue_severities = {item.get("severity") for item in issues}
        if "pink" in issue_severities:
            status = "pink"
            reason = next(
                _issue_summary(item) for item in issues
                if item.get("severity") == "pink"
            )
        elif "orange" in issue_severities and status == "green":
            status = "orange"
            reason = next(
                _issue_summary(item) for item in issues
                if item.get("severity") == "orange"
            )
        if status in {"orange", "pink"} and not any(
            item.get("severity") in {"orange", "pink"} for item in issues
        ):
            issues.insert(0, {
                "severity": status,
                "kind": "match_quality",
                "start": trim_start,
                "end": trim_end,
                "title": "文案匹配需要核对",
                "detail": reason,
                "expected": expected_text,
                "recognized": recognized_text,
            })

    cues = _cue_plans_from_timeline(
        word_timeline, script_words, script_lines
    )
    return {
        "source": str(source),
        "source_index": int(source_index),
        "export_order": int(source_index),
        "file_name": Path(source).name,
        "expected_text": expected_text,
        "recognized_text": recognized_text,
        "matched_text": matched_text,
        "recognized_match_start": recognized_match_start,
        "recognized_match_end": recognized_match_end,
        "similarity": round(score, 4),
        "status": status,
        "issue_reason": reason,
        "script_word_start": script_start,
        "script_word_end": script_end,
        "script_start_line": script_slice[0]["line_index"] if script_slice else -1,
        "script_end_line": script_slice[-1]["line_index"] if script_slice else -1,
        "word_timeline": word_timeline,
        "trim_start": round(trim_start, 3),
        "trim_end": round(trim_end, 3),
        "automatic_trim_start": round(trim_start, 3),
        "automatic_trim_end": round(trim_end, 3),
        "original_duration": round(duration, 3),
        "pause_removals": removals,
        "automatic_pause_removals": removals,
        "kept_ranges": ranges,
        "removed_seconds": round(
            trim_start
            + max(0.0, duration - trim_end)
            + sum(end - start for start, end in removals),
            3,
        ),
        "cues": cues,
        "words": words,
        "silence_threshold_db": settings["silence_threshold_db"],
        "min_silence_ms": settings["min_silence_ms"],
        "silence_ranges": silence_ranges,
        "voice_absence_ranges": voice_absence_ranges,
        "breath_gap_ranges": breath_gap_ranges,
        "silence_detection_error": detection_error,
        "db_silence_detection_error": silence_detection_error,
        "voice_detection_error": voice_detection_error,
        "alignment_issues": alignment_issues,
        "boundary_warnings": boundary_warnings,
        "boundary_decisions": boundary_decisions,
        "issues": issues,
        "boundary_removed_seconds": round(
            trim_start + max(0.0, duration - trim_end), 3
        ),
        "internal_removed_seconds": round(
            sum(end - start for start, end in removals), 3
        ),
        "included": True,
        "from_cache": bool(transcription.get("from_cache")),
    }


def _fingerprint(path):
    path = Path(path)
    stat = path.stat()
    digest = hashlib.sha256()
    sample_size = min(64 * 1024, stat.st_size)
    if sample_size:
        with path.open("rb") as handle:
            digest.update(handle.read(sample_size))
            if stat.st_size > sample_size:
                handle.seek(max(0, stat.st_size - sample_size))
                digest.update(handle.read(sample_size))
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def _fingerprints_match(cached, current):
    """Compare file content evidence without treating a touched mtime as a change."""
    if not isinstance(cached, dict) or not isinstance(current, dict):
        return False
    cached_digest = str(cached.get("sample_sha256") or "")
    current_digest = str(current.get("sample_sha256") or "")
    if cached_digest and current_digest:
        return (
            int(cached.get("size", -1)) == int(current.get("size", -2))
            and cached_digest == current_digest
        )
    return cached == current


def _stable_fingerprint(path):
    value = _fingerprint(path)
    return {
        "size": int(value.get("size", 0)),
        "sample_sha256": str(value.get("sample_sha256") or ""),
    }


def _cache_path(task_dir, source, settings):
    output_dir = Path(task_dir) / settings["output_folder_name"]
    digest = hashlib.sha256(
        os.path.normcase(os.path.abspath(str(source))).encode("utf-8", "replace")
    ).hexdigest()[:20]
    return output_dir / ".smart_edit_cache" / f"{digest}.json"


def _silence_cache_path(task_dir, source, settings):
    return _cache_path(task_dir, source, settings).with_suffix(".silence.json")


def _voice_activity_cache_path(task_dir, source, settings):
    return _cache_path(task_dir, source, settings).with_suffix(".voice.json")


def analyze_source_silence(
    source,
    task_dir,
    duration,
    settings,
    ffmpeg=None,
    progress=None,
    log_prefix="[智能剪辑]",
):
    """Read or create cached FFmpeg dB-level silence analysis."""
    progress = progress or (lambda _message: None)
    signature = {
        "source_fingerprint": _fingerprint(source),
        "silence_threshold_db": settings["silence_threshold_db"],
        "min_silence_ms": settings["min_silence_ms"],
    }
    cache_path = _silence_cache_path(task_dir, source, settings)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        cached = None
    if (
        isinstance(cached, dict)
        and _fingerprints_match(
            cached.get("source_fingerprint"), signature["source_fingerprint"]
        )
        and cached.get("silence_threshold_db") == signature["silence_threshold_db"]
        and cached.get("min_silence_ms") == signature["min_silence_ms"]
    ):
        progress(f"{log_prefix} 分贝静音缓存命中：{Path(source).name}")
        return list(cached.get("ranges") or []), str(cached.get("error") or "")

    if not settings.get("silence_detection_enabled", True):
        return [], "静音分贝检测已关闭"
    if not ffmpeg:
        return [], "没有找到 FFmpeg，无法执行分贝静音检测"
    progress(
        f"{log_prefix} 检测音量气口：{Path(source).name} "
        f"({settings['silence_threshold_db']} dB / {settings['min_silence_ms']} ms)"
    )
    ranges, error = detect_silence_ranges(
        source, ffmpeg, duration, settings
    )
    if not error:
        _write_json_atomic(cache_path, {
            **signature,
            "ranges": ranges,
            "error": "",
            "created_at": int(time.time()),
        })
    return ranges, error


def analyze_source_voice_absence(
    source,
    task_dir,
    duration,
    settings,
    progress=None,
    log_prefix="[智能剪辑]",
):
    """Read or create cached Silero VAD no-speech intervals."""
    progress = progress or (lambda _message: None)
    signature = {
        "version": VOICE_ACTIVITY_CACHE_VERSION,
        "source_fingerprint": _fingerprint(source),
        "vad_threshold": settings["vad_threshold"],
        "vad_neg_threshold": settings["vad_neg_threshold"],
        "vad_min_speech_ms": settings["vad_min_speech_ms"],
        "min_silence_ms": settings["min_silence_ms"],
        "vad_speech_pad_ms": settings["vad_speech_pad_ms"],
    }
    cache_path = _voice_activity_cache_path(task_dir, source, settings)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("version") == signature["version"]
        and _fingerprints_match(
            cached.get("source_fingerprint"),
            signature["source_fingerprint"],
        )
        and cached.get("vad_threshold") == signature["vad_threshold"]
        and cached.get("vad_neg_threshold") == signature["vad_neg_threshold"]
        and cached.get("vad_min_speech_ms") == signature["vad_min_speech_ms"]
        and cached.get("min_silence_ms") == signature["min_silence_ms"]
        and cached.get("vad_speech_pad_ms") == signature["vad_speech_pad_ms"]
    ):
        progress(f"{log_prefix} 人声检测缓存命中：{Path(source).name}")
        return list(cached.get("ranges") or []), str(cached.get("error") or "")

    if not settings.get("voice_detection_enabled", True):
        return [], "人声活动检测已关闭"
    progress(
        f"{log_prefix} 检测人声气口：{Path(source).name} "
        f"(灵敏度 {settings['vad_threshold']:.2f})"
    )
    ranges, error = detect_voice_absence_ranges(source, duration, settings)
    if not error:
        _write_json_atomic(cache_path, {
            **signature,
            "ranges": ranges,
            "error": "",
            "created_at": int(time.time()),
        })
    return ranges, error


def _read_transcription_cache(
    cache_path, source, language, model_name="base"
):
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    if not _fingerprints_match(
        value.get("source_fingerprint"), _fingerprint(source)
    ):
        return None
    if str(value.get("language") or "") != str(language or ""):
        return None
    cached_model = str(value.get("whisper_model_size") or "base").lower()
    if cached_model != str(model_name or "base").lower():
        return None
    value["from_cache"] = True
    return value


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp_path, path)


def _task_analysis_signature(job, sources, settings):
    payload = {
        "version": SMART_VIDEO_ANALYSIS_CACHE_VERSION,
        "script": str(job.get("script") or ""),
        "language": str(job.get("language") or ""),
        "sources": [
            {
                "path": os.path.normcase(os.path.abspath(str(source))),
                "fingerprint": _stable_fingerprint(source),
            }
            for source in sources
        ],
        "settings": {
            key: settings.get(key) for key in _ANALYSIS_SETTING_KEYS
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task_report_path(job, settings):
    return (
        Path(job["task_dir"])
        / settings["output_folder_name"]
        / SMART_VIDEO_EDITOR_REPORT_NAME
    )


def _load_cached_task_analysis(job, sources, settings, signature=None):
    report_path = _task_report_path(job, settings)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    task = report.get("task") if isinstance(report, dict) else None
    if not isinstance(task, dict):
        return None
    signature = signature or _task_analysis_signature(job, sources, settings)
    if (
        int(task.get("analysis_cache_version") or 0)
        != SMART_VIDEO_ANALYSIS_CACHE_VERSION
        or str(task.get("analysis_signature") or "") != signature
    ):
        return None

    task = copy.deepcopy(task)
    task_dir = Path(job["task_dir"])
    output_dir = task_dir / settings["output_folder_name"]
    task.update({
        "task_id": str(job.get("task_id") or ""),
        "task_name": str(job.get("task_name") or ""),
        "label": str(job.get("label") or job.get("task_id") or ""),
        "task_dir": str(task_dir),
        "script": str(job.get("script") or ""),
        "language": str(job.get("language") or ""),
        "output_dir": str(output_dir),
        "report_path": str(output_dir / SMART_VIDEO_EDITOR_REPORT_NAME),
        "text_report_path": str(output_dir / SMART_VIDEO_EDITOR_TEXT_REPORT_NAME),
        "analysis_signature": signature,
        "analysis_cache_version": SMART_VIDEO_ANALYSIS_CACHE_VERSION,
        "analysis_from_cache": True,
    })
    for clip in task.get("clips", []):
        clip["analysis_from_cache"] = True
        clip["from_cache"] = True
    return task


def smart_video_jobs_require_model(jobs, settings=None):
    """Return whether any selected source lacks reusable analysis/ASR cache."""
    settings = normalize_smart_video_editor_settings(settings)
    for job in jobs or []:
        try:
            task_dir = Path(job["task_dir"])
            sources = [Path(path) for path in job.get("sources", [])]
            signature = _task_analysis_signature(job, sources, settings)
            if _load_cached_task_analysis(
                job, sources, settings, signature=signature
            ) is not None:
                continue
            for source in sources:
                if _read_transcription_cache(
                    _cache_path(task_dir, source, settings),
                    source,
                    job.get("language"),
                    settings.get("whisper_model_size", "base"),
                ) is None:
                    return True
        except (OSError, ValueError, TypeError, KeyError):
            return True
    return False


def transcribe_source(model, source, language, task_dir, settings, progress=None):
    progress = progress or (lambda _message: None)
    cache_path = _cache_path(task_dir, source, settings)
    cached = _read_transcription_cache(
        cache_path,
        source,
        language,
        settings.get("whisper_model_size", "base"),
    )
    if cached is not None:
        progress(f"[智能剪辑] 识别缓存命中：{Path(source).name}")
        return cached

    if model is None:
        raise RuntimeError(
            f"{Path(source).name} 没有可复用的识别缓存，Whisper 模型尚未初始化"
        )

    progress(f"[智能剪辑] 正在识别：{Path(source).name}")
    kwargs = {
        "word_timestamps": True,
        "vad_filter": True,
        "beam_size": 5,
    }
    normalized_language = str(language or "").strip().lower()
    if normalized_language and normalized_language not in {"unknown", "auto"}:
        kwargs["language"] = normalized_language.split("-")[0]
    segments, info = model.transcribe(str(source), **kwargs)
    words = []
    transcript_parts = []
    for segment in segments:
        segment_text = str(getattr(segment, "text", "") or "").strip()
        if segment_text:
            transcript_parts.append(segment_text)
        for word in getattr(segment, "words", None) or []:
            word_text = str(getattr(word, "word", "") or "").strip()
            if not word_text:
                continue
            words.append({
                "text": word_text,
                "start": round(float(getattr(word, "start", 0.0) or 0.0), 3),
                "end": round(float(getattr(word, "end", 0.0) or 0.0), 3),
                "probability": round(
                    float(getattr(word, "probability", 0.0) or 0.0), 4
                ),
            })
    value = {
        "source": str(source),
        "source_fingerprint": _fingerprint(source),
        "language": language or "",
        "whisper_model_size": settings.get("whisper_model_size", "base"),
        "detected_language": str(getattr(info, "language", "") or ""),
        "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 3),
        "text": " ".join(transcript_parts).strip(),
        "words": words,
        "created_at": int(time.time()),
        "from_cache": False,
    }
    _write_json_atomic(cache_path, value)
    return value


def _ordering_report_text(task):
    mode = task.get("ordering_mode")
    labels = {
        "content_verified": "语音内容独立识别",
        "content_verified_filename": "语音内容识别（与文件编号一致）",
        "content_override_filename": "语音内容识别（与文件编号冲突，待人工确认）",
        "filename_fallback": "文件编号临时兜底（语音证据不足，待人工确认）",
        "content_partial": "语音内容部分识别（未定位片段置后，待人工确认）",
        # Backward compatibility with reports created by the previous build.
        "filename_sequence": "文件名前缀编号",
        "script_position": "语音在完整文案中的位置",
    }
    return labels.get(mode, "语音在完整文案中的位置")


def render_task_problem_report(task, settings=None):
    settings = normalize_smart_video_editor_settings(settings)
    lines = [
        "智能剪辑可读问题报告",
        "=" * 36,
        f"任务：{task.get('label') or task.get('task_id') or ''}",
        f"片段排序依据：{_ordering_report_text(task)}",
        (
            "气口检测参数："
            f"{settings['silence_threshold_db']} dB，"
            f"最短静音 {settings['min_silence_ms']} ms，"
            f"边界搜索 {settings['boundary_search_ms']} ms"
        ),
        "",
    ]
    if task.get("ordering_conflict"):
        lines[-1:-1] = [
            "文件编号顺序：" + " → ".join(task.get("filename_order", [])),
            "语音建议顺序：" + " → ".join(
                task.get("content_suggested_order", [])
            ),
        ]
    clips = sorted(
        task.get("clips", []),
        key=lambda clip: (
            int(clip.get("export_order", 10 ** 9)),
            int(clip.get("source_index", 10 ** 9)),
        ),
    )
    for clip in clips:
        lines.extend([
            (
                f"[{clip.get('export_order', '-')}] {clip.get('file_name', '')} "
                f"- {STATUS_REPORT_TEXT.get(clip.get('status'), clip.get('status', ''))}"
            ),
            f"  裁切：{float(clip.get('trim_start') or 0):.3f} - "
            f"{float(clip.get('trim_end') or 0):.3f} 秒 / "
            f"原片 {float(clip.get('original_duration') or 0):.3f} 秒",
            f"  正确文案：{str(clip.get('expected_text') or '').replace(chr(10), ' / ')}",
            f"  视频识别：{str(clip.get('recognized_text') or '').replace(chr(10), ' / ')}",
        ])
        problems = [
            issue for issue in clip.get("issues", [])
            if issue.get("severity") != "info"
        ]
        decisions = [
            issue for issue in clip.get("issues", [])
            if issue.get("severity") == "info"
        ]
        if problems:
            lines.append("  发现的问题：")
            lines.extend(f"    - {_issue_summary(issue)}" for issue in problems)
        else:
            lines.append("  发现的问题：无")
        if decisions:
            lines.append("  自动裁切依据：")
            lines.extend(f"    - {_issue_summary(issue)}" for issue in decisions)
        lines.append("")
    missing = task.get("missing_blocks", [])
    if missing:
        review_decision = smart_video_task_review_decision(task)
        review_text = {
            "approved": "已人工确认内容完整，允许导出",
            "skipped": "已人工选择暂缓，本次不导出",
        }.get(review_decision, "尚未处理，当前禁止导出")
        lines.append(f"确认缺失的任务原文（严重：{review_text}）：")
        for block in missing:
            lines.append(
                f"  - 第 {int(block.get('script_start_line', 0)) + 1}-"
                f"{int(block.get('script_end_line', 0)) + 1} 行："
                f"{str(block.get('text') or '').replace(chr(10), ' / ')}"
            )
            if block.get("previous_clip") or block.get("following_clip"):
                lines.append(
                    "    相邻片段："
                    f"前={block.get('previous_clip') or '无'}；"
                    f"后={block.get('following_clip') or '无'}"
                )
    unverified = task.get("unverified_blocks", [])
    if unverified:
        lines.append("语音识别未完全覆盖的边界词（不一定缺段，必须试听核对）：")
        for block in unverified:
            lines.append(
                f"  - 第 {int(block.get('script_start_line', 0)) + 1}-"
                f"{int(block.get('script_end_line', 0)) + 1} 行："
                f"{str(block.get('text') or '').replace(chr(10), ' / ')}"
            )
    return "\n".join(lines).rstrip() + "\n"


STATUS_REPORT_TEXT = {
    "green": "通过",
    "orange": "需要核对",
    "pink": "严重异常",
}


def save_analysis_reports(bundle):
    for task in bundle.get("tasks", []):
        report_path = task.get("report_path")
        if report_path:
            _write_json_atomic(report_path, {
                "version": 1,
                "created_at": bundle.get("created_at", int(time.time())),
                "settings": bundle.get("settings", {}),
                "task": task,
            })
        text_report_path = task.get("text_report_path")
        if text_report_path:
            path = Path(text_report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                render_task_problem_report(task, bundle.get("settings")),
                encoding="utf-8-sig",
            )


def _refresh_clip_after_script_range(
    clip,
    script_words,
    script_lines,
    settings,
    previous_script_range=None,
):
    start = int(clip.get("script_word_start", -1))
    end = int(clip.get("script_word_end", -1))
    if start < 0 or end < start:
        return
    script_slice = script_words[start:end + 1]
    clip["expected_text"] = script_text_from_word_records(
        script_slice, script_lines
    )
    clip["script_start_line"] = script_slice[0]["line_index"]
    clip["script_end_line"] = script_slice[-1]["line_index"]
    recognized_words = _recognized_word_records(clip.get("words", []))
    candidates = _record_window_candidates(
        recognized_words, clip["expected_text"], limit=1
    )
    clip_slice = []
    if candidates:
        match = candidates[0]
        match_start = int(match["start"])
        match_end = int(match["end"])
        if previous_script_range is not None:
            previous_start, previous_end = previous_script_range
            if start < previous_start and match_start > 0:
                added_prefix = script_words[
                    start:min(previous_start, end + 1)
                ]
                if _compact_record_similarity(
                    added_prefix, recognized_words[:match_start]
                ) >= 0.72:
                    match_start = 0
            if end > previous_end and match_end + 1 < len(recognized_words):
                added_suffix = script_words[
                    max(start, previous_end + 1):end + 1
                ]
                if _compact_record_similarity(
                    added_suffix, recognized_words[match_end + 1:]
                ) >= 0.72:
                    match_end = len(recognized_words) - 1
        clip["recognized_match_start"] = match_start
        clip["recognized_match_end"] = match_end
        clip_slice = recognized_words[match_start:match_end + 1]
    elif recognized_words:
        # A manually numbered clip is still authoritative for order.  Retain
        # all ASR timings as interpolation bounds while keeping the clip pink.
        clip["recognized_match_start"] = 0
        clip["recognized_match_end"] = len(recognized_words) - 1
        clip_slice = recognized_words
    clip["word_timeline"] = _interpolate_script_word_times(
        script_slice, clip_slice
    )
    if not clip["word_timeline"]:
        duration = max(0.05, float(clip.get("original_duration") or 0.05))
        step = duration / max(1, len(script_slice))
        clip["word_timeline"] = [
            {
                "script_word_index": word["word_index"],
                "line_index": word["line_index"],
                "raw": word["raw"],
                "start": round(index * step, 3),
                "end": round(min(duration, (index + 1) * step), 3),
                "source_word_index": None,
                "anchor": False,
            }
            for index, word in enumerate(script_slice)
        ]
    clip["cues"] = _cue_plans_from_timeline(
        clip["word_timeline"], script_words, script_lines
    )
    if clip["word_timeline"]:
        timeline = clip["word_timeline"]
        duration = float(clip.get("original_duration") or 0.0)
        trim_start, trim_end, decisions, warnings = refine_trim_boundaries(
            min(item["start"] for item in timeline),
            max(item["end"] for item in timeline),
            duration,
            clip.get("breath_gap_ranges", clip.get("silence_ranges", [])),
            settings,
            clip.get("silence_detection_error", ""),
        )
        if not timeline[0].get("anchor", False):
            trim_start = 0.0
        if not timeline[-1].get("anchor", False):
            trim_end = duration
        clip["automatic_trim_start"] = round(trim_start, 3)
        clip["automatic_trim_end"] = round(trim_end, 3)
        clip["boundary_decisions"] = decisions
        clip["boundary_warnings"] = warnings
    automatic_removals = [
        removal for removal in clip.get(
            "automatic_pause_removals", clip.get("pause_removals", [])
        )
        if removal[0] >= clip.get("automatic_trim_start", 0.0)
        and removal[1] <= clip.get("automatic_trim_end", duration)
    ]
    apply_manual_breath_overrides(
        clip,
        clip.get("automatic_trim_start", clip.get("trim_start", 0.0)),
        clip.get("automatic_trim_end", clip.get("trim_end", duration)),
        automatic_removals,
    )
    clip["alignment_issues"] = _alignment_issues(
        script_slice, clip_slice, script_lines
    )
    removal_decisions = [
        {
            "severity": "info",
            "kind": "internal_silence_cut",
            "start": left,
            "end": right,
            "title": "句内静音压缩",
            "detail": (
                "此区间同时满足词间隔和无人声/低音量证据，"
                "将按实验设置压缩。"
            ),
        }
        for left, right in clip["pause_removals"]
    ]
    clip["issues"] = (
        clip["alignment_issues"]
        + clip.get("boundary_warnings", [])
        + clip.get("boundary_decisions", [])
        + removal_decisions
    )
    duration = float(clip.get("original_duration") or 0.0)
    internal_removed = sum(
        right - left for left, right in clip["pause_removals"]
    )
    clip["boundary_removed_seconds"] = round(
        clip["trim_start"] + max(0.0, duration - clip["trim_end"]), 3
    )
    clip["internal_removed_seconds"] = round(internal_removed, 3)
    clip["removed_seconds"] = round(
        clip["boundary_removed_seconds"] + internal_removed, 3
    )


def _unmatched_recognized_edges(clip):
    recognized_count = len(_recognized_word_records(clip.get("words", [])))
    anchored_indexes = [
        int(item["source_word_index"])
        for item in clip.get("word_timeline", [])
        if item.get("anchor") and item.get("source_word_index") is not None
    ]
    if recognized_count > 0 and anchored_indexes:
        return (
            max(0, min(anchored_indexes)),
            max(0, recognized_count - max(anchored_indexes) - 1),
        )
    match_start = int(clip.get("recognized_match_start", -1))
    match_end = int(clip.get("recognized_match_end", -1))
    if recognized_count <= 0 or match_start < 0 or match_end < match_start:
        return 0, 0
    return match_start, max(0, recognized_count - match_end - 1)


def _edge_audio_evidence_for_script_gap(records, clips):
    """Return ASR edge evidence that a script gap was spoken, but mis-tokenized.

    A short sentence at a clip boundary must not become a missing-segment
    blocker merely because Whisper split or spelled its opening/ending words
    differently. Only use recognized words outside the clip's anchored match,
    and require a strong compact-text match to the exact adjacent script gap.
    """
    if not records:
        return None
    gap_start = int(records[0]["word_index"])
    gap_end = int(records[-1]["word_index"])
    candidates = []

    def add_candidate(clip, edge_records, side):
        if not clip or not edge_records:
            return
        similarity = _compact_record_similarity(records, edge_records)
        if similarity >= 0.72:
            candidates.append({
                "clip": clip,
                "side": side,
                "similarity": similarity,
                "recognized": " ".join(
                    str(item.get("raw") or "").strip()
                    for item in edge_records
                    if str(item.get("raw") or "").strip()
                ),
            })

    for clip in clips:
        anchor_indexes = [
            int(item["script_word_index"])
            for item in clip.get("word_timeline", [])
            if item.get("anchor", True)
            and item.get("script_word_index") is not None
        ]
        if not anchor_indexes:
            continue
        recognized = _recognized_word_records(clip.get("words", []))
        head_count, tail_count = _unmatched_recognized_edges(clip)
        if min(anchor_indexes) == gap_end + 1:
            add_candidate(clip, recognized[:head_count], "head")
        if max(anchor_indexes) == gap_start - 1:
            edge_records = recognized[-tail_count:] if tail_count else []
            add_candidate(clip, edge_records, "tail")

    if not candidates:
        return None
    return max(candidates, key=lambda item: item["similarity"])


def _allocate_authoritative_script_ranges(
    clips, script_words, script_lines, settings, include_unmatched=False
):
    """Give ordered clips continuous, complete slices of the task script.

    ASR decides timings and raises review issues; it must never be allowed to
    delete words from the authoritative task text.  Explicit filename numbers
    additionally allow an unmatched clip to receive its intended position.
    """
    if not clips or not script_words:
        return
    ordered = list(clips)
    if include_unmatched:
        active = ordered
    else:
        active = [
            clip for clip in ordered
            if int(clip.get("script_word_start", -1)) >= 0
            and int(clip.get("script_word_end", -1))
            >= int(clip.get("script_word_start", -1))
        ]
    if not active:
        return

    original_ranges = {
        id(clip): (
            int(clip.get("script_word_start", -1)),
            int(clip.get("script_word_end", -1)),
        )
        for clip in active
    }
    word_count = len(script_words)
    boundaries = []
    previous_boundary = -1
    for index, (left, right) in enumerate(zip(active, active[1:])):
        left_start, left_end = original_ranges[id(left)]
        right_start, right_end = original_ranges[id(right)]
        left_valid = left_start >= 0 and left_end >= left_start
        right_valid = right_start >= 0 and right_end >= right_start
        left_head, left_tail = _unmatched_recognized_edges(left)
        right_head, right_tail = _unmatched_recognized_edges(right)
        del left_head, right_tail

        if right_valid:
            # A leading ASR word omitted by fuzzy matching (for example Vaša /
            # Váša) belongs to the following numbered clip, not its predecessor.
            boundary = right_start - min(right_head, max(0, right_start)) - 1
            if left_valid and left_tail > 0:
                left_suggestion = left_end + left_tail
                boundary = max(boundary, min(right_start - 1, left_suggestion))
        elif left_valid:
            boundary = left_end + left_tail
        else:
            weights = [
                max(1, len(_recognized_word_records(clip.get("words", []))))
                for clip in active
            ]
            consumed_weight = sum(weights[:index + 1])
            boundary = round(word_count * consumed_weight / sum(weights)) - 1

        # Keep every remaining clip non-empty when the script is long enough.
        minimum = previous_boundary + 1
        remaining_clips = len(active) - index - 1
        maximum = word_count - remaining_clips - 1
        boundary = max(minimum, min(maximum, int(boundary)))
        boundaries.append(boundary)
        previous_boundary = boundary

    starts = [0] + [boundary + 1 for boundary in boundaries]
    ends = boundaries + [word_count - 1]
    for clip, start, end in zip(active, starts, ends):
        old_start, old_end = original_ranges[id(clip)]
        changed = start != old_start or end != old_end
        clip["script_word_start"] = start
        clip["script_word_end"] = end
        _refresh_clip_after_script_range(
            clip,
            script_words,
            script_lines,
            settings,
            previous_script_range=(old_start, old_end),
        )
        if not changed:
            continue
        issue = {
            "severity": "pink",
            "kind": "authoritative_script_range_inferred",
            "start": float(clip.get("trim_start") or 0.0),
            "end": float(clip.get("trim_end") or 0.0),
            "title": "原文首尾已补齐",
            "detail": (
                "语音识别漏掉了部分首尾词，字幕已按任务原文补齐；"
                "对应时间为推算值，请试听核对。"
            ),
            "expected": clip.get("expected_text", ""),
            "recognized": clip.get("recognized_text", ""),
        }
        clip.setdefault("issues", []).insert(0, issue)
        clip["status"] = "pink"
        clip["issue_reason"] = _issue_summary(issue)


def _trim_boundary_overlaps(clips, script_words, script_lines, settings):
    """Assign a shared boundary reading to the later script-position clip."""
    matched = [
        clip for clip in clips
        if clip.get("script_word_start", -1) >= 0
        and clip.get("script_word_end", -1) >= clip.get("script_word_start", -1)
    ]
    matched.sort(key=lambda clip: (
        clip["script_word_start"], clip.get("source_index", 0)
    ))
    for previous, following in zip(matched, matched[1:]):
        following_start = following["script_word_start"]
        if not (
            previous["script_word_start"] < following_start
            <= previous["script_word_end"]
        ):
            continue
        previous["script_word_end"] = following_start - 1
        _refresh_clip_after_script_range(
            previous, script_words, script_lines, settings
        )
        if previous["status"] == "green":
            previous["status"] = "orange"
            previous["issue_reason"] = "已自动移除与下一片段重复朗读的边界词，请试听确认"
        overlap_issue = {
            "severity": "orange",
            "kind": "boundary_overlap",
            "start": float(previous.get("trim_end") or 0.0),
            "end": float(previous.get("trim_end") or 0.0),
            "title": "相邻片段文案重叠",
            "detail": previous["issue_reason"],
        }
        previous.setdefault("issues", []).insert(0, overlap_issue)


def _missing_script_blocks(script_words, script_lines, clips):
    """Return uncovered script ranges and distinguish gaps from ASR edges.

    A missing complete script line/sentence, or three consecutive uncovered
    words, is strong enough to mean that a source segment may genuinely be
    absent.  One or two words at a matched clip boundary are kept as review
    warnings because Whisper can omit short openings/endings even when the
    audio is present.
    """
    covered = set()
    for clip in clips:
        if "word_timeline" in clip:
            covered.update(
                int(item["script_word_index"])
                for item in clip.get("word_timeline", [])
                if item.get("anchor", True)
                and item.get("script_word_index") is not None
            )
            # A substitution is a serious word error, but it proves that audio
            # exists at this position.  Keep it in the pink/orange review flow
            # instead of mislabelling it as an absent video segment.
            for issue in clip.get("alignment_issues", []):
                if issue.get("kind") != "word_difference":
                    continue
                issue_start = issue.get("script_word_start")
                issue_end = issue.get("script_word_end")
                if issue_start is None or issue_end is None:
                    continue
                covered.update(range(int(issue_start), int(issue_end) + 1))
            continue
        # Backward compatibility for manually constructed or older reports
        # that predate per-word anchor data.
        start = int(clip.get("script_word_start", -1))
        end = int(clip.get("script_word_end", -1))
        if start >= 0 and end >= start:
            covered.update(range(start, end + 1))
    missing = []
    index = 0
    while index < len(script_words):
        if index in covered:
            index += 1
            continue
        start = index
        while index < len(script_words) and index not in covered:
            index += 1
        end = index - 1
        records = script_words[start:end + 1]
        fully_missing_lines = []
        for line_index in sorted({item["line_index"] for item in records}):
            line_word_indexes = [
                item["word_index"]
                for item in script_words
                if item["line_index"] == line_index
            ]
            if (
                line_word_indexes
                and start <= line_word_indexes[0]
                and end >= line_word_indexes[-1]
            ):
                fully_missing_lines.append(line_index)
        complete_sentence = False
        if records[0]["line_index"] == records[-1]["line_index"]:
            source_line = script_lines[records[0]["line_index"]]
            before_gap = source_line[:records[0]["char_start"]]
            after_word = source_line[records[-1]["char_end"]:]
            start_is_sentence_boundary = bool(
                records[0]["is_first_in_line"]
                or re.search(r"[.!?。！？]\s*$", before_gap)
            )
            end_is_sentence_boundary = bool(
                records[-1]["is_last_in_line"]
                or re.match(r"^[^\w]*[.!?。！？]", after_word)
            )
            complete_sentence = (
                start_is_sentence_boundary and end_is_sentence_boundary
            )
        previous_candidates = [
            clip for clip in clips
            if int(clip.get("script_word_end", -1)) < start
            and int(clip.get("script_word_end", -1)) >= 0
        ]
        following_candidates = [
            clip for clip in clips
            if int(clip.get("script_word_start", -1)) > end
        ]
        previous_clip = (
            max(
                previous_candidates,
                key=lambda clip: int(clip.get("script_word_end", -1)),
            ).get("file_name", "")
            if previous_candidates else ""
        )
        following_clip = (
            min(
                following_candidates,
                key=lambda clip: int(clip.get("script_word_start", 10 ** 9)),
            ).get("file_name", "")
            if following_candidates else ""
        )
        edge_audio_evidence = _edge_audio_evidence_for_script_gap(
            records,
            clips,
        )
        blocks_export = bool(
            fully_missing_lines or complete_sentence or len(records) >= 3
        ) and edge_audio_evidence is None
        before_records = script_words[max(0, start - 5):start]
        after_records = script_words[end + 1:min(len(script_words), end + 6)]
        if blocks_export:
            issue_reason = (
                "这段任务原文没有被任何保留的视频片段覆盖，已判定为缺段；"
                "必须补齐或重新选择视频并再次分析，当前禁止生成。"
            )
        elif edge_audio_evidence is not None:
            issue_reason = (
                "相邻片段未分配的边界读音与原文高度近似，已判定为 "
                "Whisper 拼写或分词偏差，不作为缺段；请人工试听核对。"
            )
        else:
            issue_reason = (
                "只有少量边界词没有形成可靠语音锚点，可能是 Whisper 漏识别；"
                "相邻音频已保留，请人工试听核对。"
            )
        missing.append({
            "script_word_start": start,
            "script_word_end": end,
            "script_start_line": records[0]["line_index"],
            "script_end_line": records[-1]["line_index"],
            "text": script_text_from_word_records(records, script_lines),
            "word_count": len(records),
            "fully_missing_lines": fully_missing_lines,
            "complete_sentence": complete_sentence,
            "blocks_export": blocks_export,
            "before_text": script_text_from_word_records(
                before_records, script_lines
            ),
            "after_text": script_text_from_word_records(
                after_records, script_lines
            ),
            "previous_clip": str(previous_clip or ""),
            "following_clip": str(following_clip or ""),
            "edge_audio_supported": edge_audio_evidence is not None,
            "edge_audio_similarity": round(
                float((edge_audio_evidence or {}).get("similarity") or 0.0),
                4,
            ),
            "edge_recognized_text": str(
                (edge_audio_evidence or {}).get("recognized") or ""
            ),
            "issue_reason": issue_reason,
        })
    return missing


def _task_missing_findings(task):
    """Return hard gaps for the task's current, actually included clips."""
    included_clips = [
        clip for clip in task.get("clips", [])
        if clip.get("included", True)
    ]
    return [
        block for block in _missing_script_blocks(
            task.get("script_words", []),
            task.get("script_lines", []),
            included_clips,
        )
        if block.get("blocks_export")
    ]


def _signature_int(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _task_missing_review_signature(task, findings=None):
    """Bind a human decision to the exact clips, edits and gaps reviewed."""
    findings = _task_missing_findings(task) if findings is None else list(findings)
    clips = []
    for clip in task.get("clips", []):
        if not clip.get("included", True):
            continue
        clips.append({
            "source": os.path.normcase(os.path.abspath(str(clip.get("source") or ""))),
            "export_order": _signature_int(clip.get("export_order"), 0),
            "trim_start": round(float(clip.get("trim_start") or 0.0), 4),
            "trim_end": round(float(clip.get("trim_end") or 0.0), 4),
            "script_word_start": _signature_int(clip.get("script_word_start")),
            "script_word_end": _signature_int(clip.get("script_word_end")),
            "expected_text": str(clip.get("expected_text") or ""),
            "anchors": [
                _signature_int(item.get("script_word_index"))
                for item in clip.get("word_timeline", [])
                if item.get("anchor", True)
            ],
        })
    payload = {
        "task_id": str(task.get("task_id") or ""),
        "script": str(task.get("script") or ""),
        "clips": clips,
        "findings": [{
            "start": _signature_int(item.get("script_word_start")),
            "end": _signature_int(item.get("script_word_end")),
            "text": str(item.get("text") or ""),
        } for item in findings],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def set_smart_video_missing_review(task, decision):
    """Record a human decision for all current hard gaps in one task."""
    decision = str(decision or "").strip().lower()
    if not decision:
        task.pop("missing_review", None)
        return None
    if decision not in {"approved", "skipped"}:
        raise ValueError(f"未知的缺段处理决定：{decision}")
    findings = _task_missing_findings(task)
    if not findings:
        raise ValueError("这个任务当前没有需要人工处理的确认缺段。")
    review = {
        "decision": decision,
        "signature": _task_missing_review_signature(task, findings),
        "reviewed_at": int(time.time()),
        "finding_count": len(findings),
    }
    task["missing_review"] = review
    return review


def smart_video_task_review_decision(task, findings=None):
    """Return a decision only while it still matches the current edit state."""
    review = task.get("missing_review")
    if not isinstance(review, dict):
        return ""
    decision = str(review.get("decision") or "").strip().lower()
    if decision not in {"approved", "skipped"}:
        return ""
    findings = _task_missing_findings(task) if findings is None else list(findings)
    if not findings:
        return ""
    if str(review.get("signature") or "") != _task_missing_review_signature(
        task, findings
    ):
        return ""
    return decision


def smart_video_missing_findings(bundle):
    """List every current hard gap, including valid human decisions."""
    findings = []
    for task_index, task in enumerate(bundle.get("tasks", [])):
        task_findings = _task_missing_findings(task)
        decision = smart_video_task_review_decision(task, task_findings)
        review = task.get("missing_review") if decision else {}
        for block in task_findings:
            findings.append({
                **block,
                "task_index": task_index,
                "task_id": str(task.get("task_id") or ""),
                "task_label": str(
                    task.get("label") or task.get("task_id") or "任务"
                ),
                "review_decision": decision,
                "reviewed_at": int((review or {}).get("reviewed_at") or 0),
            })
    return findings


def smart_video_export_blockers(bundle):
    """Return only hard gaps which still lack a valid human decision."""
    return [
        block for block in smart_video_missing_findings(bundle)
        if block.get("review_decision") not in {"approved", "skipped"}
    ]


def _pending_record_id(task):
    identity = "\n".join([
        os.path.normcase(os.path.abspath(str(task.get("task_dir") or ""))),
        str(task.get("task_id") or ""),
    ])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def normalize_smart_video_pending_reviews(value):
    """Normalize the compact, JSON-safe list used by the Tools menu."""
    rows = value if isinstance(value, list) else []
    normalized = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        record_id = str(raw.get("record_id") or "").strip()
        task_dir = str(raw.get("task_dir") or "").strip()
        if not record_id or not task_dir or record_id in seen:
            continue
        seen.add(record_id)
        normalized.append({
            "record_id": record_id,
            "status": "skipped" if raw.get("status") == "skipped" else "pending",
            "updated_at": int(raw.get("updated_at") or 0),
            "task_id": str(raw.get("task_id") or ""),
            "task_name": str(raw.get("task_name") or ""),
            "label": str(raw.get("label") or raw.get("task_id") or "任务"),
            "task_dir": task_dir,
            "script": str(raw.get("script") or ""),
            "language": str(raw.get("language") or ""),
            "sources": [
                str(path) for path in raw.get("sources", [])
                if str(path).strip()
            ],
            "missing_blocks": [
                dict(item) for item in raw.get("missing_blocks", [])
                if isinstance(item, dict)
            ],
        })
        if len(normalized) >= 1000:
            break
    return normalized


def update_smart_video_pending_reviews(existing, bundle):
    """Merge this review run into the persistent pending-task list."""
    records = {
        item["record_id"]: item
        for item in normalize_smart_video_pending_reviews(existing)
    }
    now = int(time.time())
    for task in bundle.get("tasks", []):
        record_id = _pending_record_id(task)
        findings = _task_missing_findings(task)
        decision = smart_video_task_review_decision(task, findings)
        if not findings or decision == "approved":
            records.pop(record_id, None)
            continue
        records[record_id] = {
            "record_id": record_id,
            "status": "skipped" if decision == "skipped" else "pending",
            "updated_at": now,
            "task_id": str(task.get("task_id") or ""),
            "task_name": str(task.get("task_name") or ""),
            "label": str(task.get("label") or task.get("task_id") or "任务"),
            "task_dir": str(task.get("task_dir") or ""),
            "script": str(task.get("script") or ""),
            "language": str(task.get("language") or ""),
            "sources": [
                str(clip.get("source") or "")
                for clip in task.get("clips", [])
                if str(clip.get("source") or "").strip()
            ],
            "missing_blocks": findings,
        }
    return sorted(
        records.values(), key=lambda item: int(item.get("updated_at") or 0),
        reverse=True,
    )[:1000]


def format_smart_video_export_blockers(blockers, limit=12):
    blockers = list(blockers or [])
    lines = []
    for block in blockers[:max(1, int(limit or 1))]:
        start_line = int(block.get("script_start_line", 0)) + 1
        end_line = int(block.get("script_end_line", 0)) + 1
        line_text = (
            f"第 {start_line} 行"
            if start_line == end_line
            else f"第 {start_line}-{end_line} 行"
        )
        neighbor_parts = []
        if block.get("previous_clip"):
            neighbor_parts.append(f"前一片段：{block['previous_clip']}")
        if block.get("following_clip"):
            neighbor_parts.append(f"后一片段：{block['following_clip']}")
        neighbor_text = (
            "；" + "；".join(neighbor_parts) if neighbor_parts else ""
        )
        lines.append(
            f"• {block.get('task_label', '任务')}｜{line_text}{neighbor_text}\n"
            f"  缺失原文：{str(block.get('text') or '').replace(chr(10), ' / ')}"
        )
    if len(blockers) > len(lines):
        lines.append(f"• 另有 {len(blockers) - len(lines)} 段，请在核对窗口查看。")
    return "\n".join(lines)


def validate_smart_video_bundle_for_export(bundle):
    blockers = smart_video_export_blockers(bundle)
    if not blockers:
        return []
    raise ValueError(
        "检测到任务视频缺段，已禁止生成。请补齐视频后重新分析：\n"
        + format_smart_video_export_blockers(blockers)
    )


def _rebuild_clip_ranges_and_removed(clip):
    start = float(clip.get("trim_start") or 0.0)
    end = float(clip.get("trim_end") or start)
    duration = float(clip.get("original_duration") or end)
    removals = [
        [max(start, float(left)), min(end, float(right))]
        for left, right in clip.get("pause_removals", [])
        if min(end, float(right)) - max(start, float(left)) >= 0.08
    ]
    clip["pause_removals"] = removals
    clip["kept_ranges"] = kept_ranges(start, end, removals)
    internal_removed = sum(right - left for left, right in removals)
    boundary_removed = start + max(0.0, duration - end)
    clip["boundary_removed_seconds"] = round(boundary_removed, 3)
    clip["internal_removed_seconds"] = round(internal_removed, 3)
    clip["removed_seconds"] = round(boundary_removed + internal_removed, 3)


def _edge_has_proven_no_voice(clip, side, minimum_seconds=0.35):
    """Return whether VAD proves an edge is dead air beyond anchored speech."""
    if clip.get("voice_detection_error"):
        return False
    duration = float(clip.get("original_duration") or 0.0)
    if duration <= 0.0:
        return False
    anchors = [
        item for item in clip.get("word_timeline", [])
        if item.get("anchor", False)
    ]
    if not anchors:
        return False
    for left, right in clip.get("voice_absence_ranges", []) or []:
        left = max(0.0, float(left))
        right = min(duration, float(right))
        if right - left < float(minimum_seconds):
            continue
        if side == "tail":
            last_word_end = max(float(item.get("end") or 0.0) for item in anchors)
            if right >= duration - 0.03 and left >= last_word_end - 0.05:
                return True
        else:
            first_word_start = min(float(item.get("start") or 0.0) for item in anchors)
            if left <= 0.03 and right <= first_word_start + 0.05:
                return True
    return False


def _protect_edges_adjacent_to_missing_script(clips, missing_blocks):
    """Preserve only uncertain edges where ASR may have missed real speech.

    A supported pronunciation mismatch is not a missing recording. Likewise,
    retaining a source edge cannot recover a word when VAD already proves that
    the whole edge after/before anchored speech contains no human voice.
    """
    matched = [
        clip for clip in clips
        if int(clip.get("script_word_start", -1)) >= 0
        and int(clip.get("script_word_end", -1)) >= int(clip.get("script_word_start", -1))
    ]
    for block in missing_blocks:
        if block.get("edge_audio_supported"):
            continue
        missing_start = int(block["script_word_start"])
        missing_end = int(block["script_word_end"])
        previous_candidates = [
            clip for clip in matched
            if int(clip["script_word_end"]) < missing_start
        ]
        following_candidates = [
            clip for clip in matched
            if int(clip["script_word_start"]) > missing_end
        ]
        protected = []
        if previous_candidates:
            previous = max(
                previous_candidates, key=lambda clip: int(clip["script_word_end"])
            )
            if not _edge_has_proven_no_voice(previous, "tail"):
                old_trim_end = float(previous.get("trim_end") or 0.0)
                previous["trim_end"] = float(previous.get("original_duration") or 0.0)
                previous["boundary_decisions"] = [
                    item for item in previous.get("boundary_decisions", [])
                    if item.get("kind") != "tail_cut"
                ]
                protected.append((previous, "tail", old_trim_end))
        if following_candidates:
            following = min(
                following_candidates, key=lambda clip: int(clip["script_word_start"])
            )
            if not _edge_has_proven_no_voice(following, "head"):
                old_trim_start = float(following.get("trim_start") or 0.0)
                following["trim_start"] = 0.0
                following["boundary_decisions"] = [
                    item for item in following.get("boundary_decisions", [])
                    if item.get("kind") != "head_cut"
                ]
                protected.append((following, "head", old_trim_start))
        for clip, side, old_boundary in protected:
            issue = {
                "severity": "pink",
                "kind": "missing_script_boundary_guard",
                "start": (
                    old_boundary if side == "tail" else 0.0
                ),
                "end": (
                    float(clip.get("original_duration") or 0.0)
                    if side == "tail" else old_boundary
                ),
                "title": "漏匹配文案边界保护",
                "detail": (
                    "相邻任务文案没有匹配到视频，可能是 Whisper 漏识别。"
                    f"已禁止裁切此片段{'片尾' if side == 'tail' else '片头'}，请试听确认。"
                ),
                "expected": str(block.get("text") or ""),
                "recognized": "",
            }
            clip.setdefault("boundary_warnings", []).append(issue)
            clip["issues"] = (
                clip.get("alignment_issues", [])
                + clip.get("boundary_warnings", [])
                + clip.get("boundary_decisions", [])
                + [
                    item for item in clip.get("issues", [])
                    if item.get("kind") == "internal_silence_cut"
                ]
            )
            clip["status"] = "pink"
            clip["issue_reason"] = _issue_summary(issue)
            _rebuild_clip_ranges_and_removed(clip)


def _add_order_warning(clips, title, detail, kind):
    for clip in clips:
        issue = {
            "severity": "pink",
            "kind": kind,
            "start": float(clip.get("trim_start") or 0.0),
            "end": float(clip.get("trim_end") or 0.0),
            "title": title,
            "detail": detail,
            "expected": clip.get("expected_text", ""),
            "recognized": clip.get("recognized_text", ""),
        }
        clip.setdefault("issues", []).insert(0, issue)
        clip["status"] = "pink"
        clip["issue_reason"] = _issue_summary(issue)


def analyze_smart_video_jobs(jobs, model, settings=None, progress=None, cancelled=None):
    settings = normalize_smart_video_editor_settings(settings)
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    silence_ffmpeg = None
    silence_ffmpeg_error = ""
    if settings.get("silence_detection_enabled", True):
        try:
            silence_ffmpeg = _resolve_ffmpeg(settings)
        except ValueError as error:
            silence_ffmpeg_error = str(error)
    bundle = {
        "version": 1,
        "created_at": int(time.time()),
        "settings": settings,
        "tasks": [],
    }
    task_cache_count = 0
    transcription_cache_count = 0
    for task_index, job in enumerate(jobs, 1):
        if cancelled():
            raise InterruptedError("用户已请求停止智能剪辑分析")
        task_dir = Path(job["task_dir"])
        sources = [Path(path) for path in job.get("sources", [])]
        analysis_signature = _task_analysis_signature(job, sources, settings)
        progress(
            f"[智能剪辑] 分析任务 {task_index}/{len(jobs)}：{job.get('label', job.get('task_id', ''))}"
        )
        cached_task = _load_cached_task_analysis(
            job, sources, settings, signature=analysis_signature
        )
        if cached_task is not None:
            progress(
                "[智能剪辑] 完整分析缓存命中："
                f"{job.get('label', job.get('task_id', ''))}"
                "（识别、排序、缺段核对和气口计划均已复用）"
            )
            bundle["tasks"].append(cached_task)
            task_cache_count += 1
            continue
        transcriptions = []
        silence_analyses = []
        voice_analyses = []
        for source_index, source in enumerate(sources, 1):
            if cancelled():
                raise InterruptedError("用户已请求停止智能剪辑分析")
            progress(
                f"[智能剪辑] 视频 {source_index}/{len(sources)}：{source.name}"
            )
            transcription = transcribe_source(
                model,
                source,
                job.get("language"),
                task_dir,
                settings,
                progress,
            )
            transcriptions.append(transcription)
            transcription_cache_count += int(
                bool(transcription.get("from_cache"))
            )
            duration = max(
                float(transcription.get("duration") or 0.0),
                max(
                    (
                        float(word.get("end") or 0.0)
                        for word in transcription.get("words", [])
                    ),
                    default=0.0,
                ),
            )
            if silence_ffmpeg_error:
                silence_analyses.append(([], silence_ffmpeg_error))
            else:
                silence_analyses.append(analyze_source_silence(
                    source,
                    task_dir,
                    duration,
                    settings,
                    silence_ffmpeg,
                    progress,
                ))
            voice_analyses.append(analyze_source_voice_absence(
                source,
                task_dir,
                duration,
                settings,
                progress,
            ))
        script_lines, script_words = build_script_word_records(
            job.get("script", "")
        )
        candidate_sets = [
            _record_window_candidates(
                script_words,
                item.get("text", ""),
                limit=24,
            )
            for item in transcriptions
        ]
        assignments = _select_global_script_matches(
            script_words,
            transcriptions,
            minimum_score=max(
                0.58, settings["severe_similarity_percent"] / 100.0
            ),
            candidate_sets=candidate_sets,
        )
        order_evidence = _content_order_evidence(
            script_words,
            transcriptions,
            candidate_sets=candidate_sets,
        )
        clips = []
        for source_index, (
            source,
            transcription,
            assignment,
            silence_analysis,
            voice_analysis,
        ) in enumerate(zip(
            sources,
            transcriptions,
            assignments,
            silence_analyses,
            voice_analyses,
        )):
            silence_ranges, silence_error = silence_analysis
            voice_absence_ranges, voice_error = voice_analysis
            clips.append(_build_clip_plan(
                source,
                transcription,
                assignment,
                script_words,
                script_lines,
                settings,
                source_index,
                silence_ranges,
                silence_error,
                voice_absence_ranges,
                voice_error,
            ))
        for clip, evidence in zip(clips, order_evidence):
            clip["content_order_start"] = int(evidence.get("start", -1))
            clip["content_order_end"] = int(evidence.get("end", -1))
            clip["content_order_score"] = float(evidence.get("score", 0.0))
            clip["content_order_reliable"] = bool(evidence.get("reliable"))

        duplicate_count = _mark_duplicate_clips(
            clips, transcriptions, order_evidence
        )
        active_clips = [
            clip for clip in clips if clip.get("included", True)
        ]
        duplicate_clips = [
            clip for clip in clips if not clip.get("included", True)
        ]
        _trim_boundary_overlaps(
            active_clips, script_words, script_lines, settings
        )
        filename_sequence = _has_complete_explicit_sequence([
            Path(clip["source"]) for clip in active_clips
        ])
        filename_sorted = sorted(active_clips, key=_filename_sequence_key)
        active_order_evidence = [
            order_evidence[int(clip.get("source_index", 0))]
            for clip in active_clips
        ]
        content_order_complete = _content_order_is_complete(
            active_order_evidence
        )
        content_sorted = sorted(active_clips, key=lambda clip: (
            int(clip.get("content_order_start", 10 ** 9)),
            -float(clip.get("content_order_score") or 0.0),
            int(clip.get("source_index", 0)),
        ))
        order_warning = None
        ordering_conflict = False
        if content_order_complete:
            active_clips = content_sorted
            ordering_mode = "content_verified"
            if filename_sequence:
                content_names = [clip["file_name"] for clip in active_clips]
                filename_names = [clip["file_name"] for clip in filename_sorted]
                if content_names != filename_names:
                    ordering_conflict = True
                    ordering_mode = "content_override_filename"
                    order_warning = (
                        "编号与语音顺序冲突",
                        (
                            "文件名编号与语音在完整文案中的位置不一致。"
                            "当前采用语音建议顺序，必须人工核对后再生成。"
                        ),
                        "filename_content_order_conflict",
                    )
                else:
                    ordering_mode = "content_verified_filename"
        elif filename_sequence:
            active_clips = filename_sorted
            ordering_mode = "filename_fallback"
            order_warning = (
                "语音顺序证据不足",
                (
                    "部分片段无法仅凭语音可靠确定位置，当前暂按文件名编号排列。"
                    "编号可能有误，必须人工核对顺序后再生成。"
                ),
                "filename_order_fallback",
            )
        else:
            active_clips.sort(key=lambda clip: (
                clip["script_word_start"]
                if clip["script_word_start"] >= 0
                else 10 ** 9,
                -float(clip.get("similarity") or 0.0),
                clip.get("source_index", 0),
            ))
            ordering_mode = "content_partial"
        clips = active_clips + sorted(
            duplicate_clips,
            key=lambda clip: int(clip.get("source_index", 0)),
        )
        for export_order, clip in enumerate(clips, 1):
            clip["export_order"] = export_order
        uncovered_blocks = _missing_script_blocks(
            script_words, script_lines, active_clips
        )
        missing_blocks = [
            block for block in uncovered_blocks if block.get("blocks_export")
        ]
        unverified_blocks = [
            block for block in uncovered_blocks if not block.get("blocks_export")
        ]
        _protect_edges_adjacent_to_missing_script(active_clips, uncovered_blocks)
        # Never stretch an existing clip across a confirmed missing segment.
        # For one/two uncertain boundary words, retaining the authoritative
        # script is still useful and mirrors the proven subtitle workflow.
        if not missing_blocks:
            _allocate_authoritative_script_ranges(
                active_clips,
                script_words,
                script_lines,
                settings,
                include_unmatched=(content_order_complete or filename_sequence),
            )
            # Range allocation can turn a boundary ASR spelling/tokenization
            # mismatch into an ordinary word-difference issue. Recalculate
            # coverage so the pre-allocation gap is not left in the report.
            uncovered_blocks = _missing_script_blocks(
                script_words, script_lines, active_clips
            )
            missing_blocks = [
                block for block in uncovered_blocks if block.get("blocks_export")
            ]
            unverified_blocks = [
                block for block in uncovered_blocks if not block.get("blocks_export")
            ]
        if order_warning is not None:
            _add_order_warning(active_clips, *order_warning)
        task_output_dir = task_dir / settings["output_folder_name"]
        task_result = {
            "task_id": str(job.get("task_id") or ""),
            "task_name": str(job.get("task_name") or ""),
            "label": str(job.get("label") or job.get("task_id") or ""),
            "task_dir": str(task_dir),
            "script": str(job.get("script") or ""),
            "script_lines": script_lines,
            "script_words": script_words,
            "language": str(job.get("language") or ""),
            "ordering_mode": ordering_mode,
            "ordering_conflict": ordering_conflict,
            "filename_order": [clip["file_name"] for clip in filename_sorted],
            "content_suggested_order": [
                clip["file_name"] for clip in content_sorted
                if clip.get("content_order_reliable")
            ],
            "clips": clips,
            "missing_blocks": missing_blocks,
            "unverified_blocks": unverified_blocks,
            "duplicate_count": duplicate_count,
            "output_dir": str(task_output_dir),
            "report_path": str(task_output_dir / SMART_VIDEO_EDITOR_REPORT_NAME),
            "text_report_path": str(
                task_output_dir / SMART_VIDEO_EDITOR_TEXT_REPORT_NAME
            ),
            "analysis_signature": analysis_signature,
            "analysis_cache_version": SMART_VIDEO_ANALYSIS_CACHE_VERSION,
            "analysis_from_cache": False,
        }
        bundle["tasks"].append(task_result)
    save_analysis_reports(bundle)
    clips = [clip for task in bundle["tasks"] for clip in task["clips"]]
    missing_count = sum(
        len(task.get("missing_blocks", [])) for task in bundle["tasks"]
    )
    unverified_count = sum(
        len(task.get("unverified_blocks", [])) for task in bundle["tasks"]
    )
    problem_count = sum(
        issue.get("severity") != "info"
        for clip in clips
        for issue in clip.get("issues", [])
    )
    cut_decision_count = sum(
        issue.get("severity") == "info"
        for clip in clips
        for issue in clip.get("issues", [])
    )
    duplicate_count = sum(
        int(task.get("duplicate_count") or 0) for task in bundle["tasks"]
    )
    bundle["summary"] = {
        "task_count": len(bundle["tasks"]),
        "clip_count": len(clips),
        "green_count": sum(clip["status"] == "green" for clip in clips),
        "orange_count": sum(clip["status"] == "orange" for clip in clips),
        "pink_count": sum(clip["status"] == "pink" for clip in clips),
        "missing_count": missing_count,
        "unverified_count": unverified_count,
        "export_blocked": missing_count > 0,
        "problem_count": problem_count,
        "cut_decision_count": cut_decision_count,
        "duplicate_count": duplicate_count,
        "task_cache_count": task_cache_count,
        "transcription_cache_count": transcription_cache_count,
        "needs_review": (
            any(clip["status"] != "green" for clip in clips)
            or missing_count > 0
            or unverified_count > 0
            or cut_decision_count > 0
        ),
    }
    save_analysis_reports(bundle)
    return bundle


def apply_clip_review(clip, included, trim_start, trim_end, expected_text=None):
    clip["included"] = bool(included)
    duration = max(0.0, float(clip.get("original_duration") or 0.0))
    start = max(0.0, min(duration, float(trim_start)))
    end = max(start, min(duration, float(trim_end)))
    if end - start < 0.08:
        raise ValueError(f"{clip.get('file_name', '视频')} 的结束时间必须晚于开始时间")
    clip["trim_start"] = round(start, 3)
    clip["trim_end"] = round(end, 3)
    if expected_text is not None and str(expected_text).strip():
        reviewed_text = str(expected_text).strip()
        if reviewed_text != str(clip.get("expected_text") or "").strip():
            clip["manual_text_override"] = True
        clip["expected_text"] = reviewed_text
    removals = [
        [max(start, float(left)), min(end, float(right))]
        for left, right in clip.get("pause_removals", [])
        if min(end, float(right)) - max(start, float(left)) >= 0.08
    ]
    clip["pause_removals"] = removals
    clip["kept_ranges"] = kept_ranges(start, end, removals)
    internal_removed = sum(right - left for left, right in removals)
    boundary_removed = start + max(0.0, duration - end)
    clip["boundary_removed_seconds"] = round(boundary_removed, 3)
    clip["internal_removed_seconds"] = round(internal_removed, 3)
    clip["removed_seconds"] = round(boundary_removed + internal_removed, 3)
    return clip


def _resolve_ffmpeg(settings):
    configured = str(settings.get("ffmpeg_path") or "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        found = shutil.which(configured)
        if found:
            return found
        raise FileNotFoundError(f"智能剪辑编码器不存在：{configured}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError(
        "没有找到 FFmpeg。请在“程序设置 → 智能剪辑”中选择 ffmpeg.exe。"
    )


def _run_process(args, progress, cancelled):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stderr = ""
    # FFmpeg writes continuously to stderr. A PIPE can fill up and deadlock on
    # long videos, so drain it through a temporary file while polling cancel.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as error_file:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=error_file,
            creationflags=creationflags,
        )
        while process.poll() is None:
            if cancelled():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise InterruptedError("用户已请求停止智能剪辑导出")
            time.sleep(0.1)
        error_file.seek(0)
        stderr = error_file.read()
    if process.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-12:])
        progress(f"[智能剪辑] 编码器返回错误：{tail}")
        raise RuntimeError(tail or f"FFmpeg 退出代码 {process.returncode}")


def _media_shape(path):
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        try:
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        finally:
            capture.release()
    except Exception:
        width, height, fps = 0, 0, 0.0
    width = width if width > 1 else 1080
    height = height if height > 1 else 1920
    width -= width % 2
    height -= height % 2
    fps = fps if 1.0 <= fps <= 240.0 else 30.0
    return width, height, fps


def _media_duration(path, ffmpeg=""):
    """Read media duration without loading a speech model."""
    ffmpeg_path = Path(str(ffmpeg or "")) if ffmpeg else None
    candidates = []
    if ffmpeg_path:
        candidates.extend([
            ffmpeg_path.with_name("ffprobe.exe"),
            ffmpeg_path.with_name("ffprobe"),
        ])
    system_probe = shutil.which("ffprobe")
    if system_probe:
        candidates.append(Path(system_probe))
    for probe in candidates:
        if not probe.is_file():
            continue
        try:
            completed = subprocess.run(
                [
                    str(probe), "-v", "error", "-show_entries",
                    "format=duration", "-of",
                    "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            duration = float(completed.stdout.strip())
            if completed.returncode == 0 and duration > 0.0:
                return duration
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            pass
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        try:
            frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        finally:
            capture.release()
        if frame_count > 0.0 and fps > 0.0:
            return frame_count / fps
    except Exception:
        pass
    raise ValueError(f"无法读取视频时长：{Path(path).name}")


def _versioned_output(path, behavior):
    path = Path(path)
    if not path.exists() or behavior == "overwrite":
        return path
    if behavior == "skip":
        return None
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"无法为输出文件生成可用版本号：{path}")


def _format_srt_time(seconds):
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _split_cue_text(text, max_words):
    text = str(text or "").strip()
    spans = _token_spans(text)
    if not text or max_words <= 0 or len(spans) <= max_words:
        return [text] if text else []
    token_boundaries = list(range(0, len(spans), max_words)) + [len(spans)]
    character_boundaries = [0]
    character_boundaries.extend(
        spans[token_index][0] for token_index in token_boundaries[1:-1]
    )
    character_boundaries.append(len(text))
    chunks = [
        text[character_boundaries[index]:character_boundaries[index + 1]].strip()
        for index in range(len(character_boundaries) - 1)
    ]
    return [chunk for chunk in chunks if chunk]


def _complete_timeline_for_srt(clip, records, timeline_by_word):
    """Guarantee a usable time entry for every authoritative script word."""
    missing = [
        word for word in records
        if int(word["word_index"]) not in timeline_by_word
    ]
    if not missing:
        return timeline_by_word
    rebuilt = _interpolate_script_word_times(
        records, _recognized_word_records(clip.get("words", []))
    )
    if rebuilt:
        return {
            int(item["script_word_index"]): item
            for item in rebuilt
        }

    ranges = clip.get("kept_ranges", [])
    source_start = (
        float(ranges[0][0]) if ranges else float(clip.get("trim_start") or 0.0)
    )
    source_end = (
        float(ranges[-1][1])
        if ranges
        else float(clip.get("trim_end") or source_start)
    )
    source_end = max(source_start + 0.05, source_end)
    step = (source_end - source_start) / max(1, len(records))
    return {
        int(word["word_index"]): {
            "script_word_index": int(word["word_index"]),
            "line_index": int(word["line_index"]),
            "raw": word["raw"],
            "start": source_start + index * step,
            "end": source_start + (index + 1) * step,
            "source_word_index": None,
            "anchor": False,
        }
        for index, word in enumerate(records)
    }


def build_srt_cues(task, settings):
    cues = []
    timeline_offset = 0.0
    max_words = settings["srt_max_words_per_block"]
    max_chars = settings.get("srt_max_chars_per_block", 50)
    script_words = task.get("script_words", [])
    script_lines = task.get("script_lines", [])
    ordered_clips = sorted(
        task.get("clips", []),
        key=lambda clip: (
            int(clip.get("export_order", 10 ** 9)),
            int(clip.get("source_index", 10 ** 9)),
        ),
    )
    for clip in ordered_clips:
        if not clip.get("included", True):
            continue
        ranges = clip.get("kept_ranges", [])
        clip_duration = sum(end - start for start, end in ranges)
        timeline_by_word = {
            int(item["script_word_index"]): item
            for item in clip.get("word_timeline", [])
        }
        start_index = int(clip.get("script_word_start", -1))
        end_index = int(clip.get("script_word_end", -1))
        clip_script_words = [
            word for word in script_words
            if start_index <= int(word.get("word_index", -1)) <= end_index
        ]
        timeline_by_word = _complete_timeline_for_srt(
            clip, clip_script_words, timeline_by_word
        )
        record_groups = []
        current = []
        for word in clip_script_words:
            candidate_text = " ".join(
                str(item.get("raw") or "").strip()
                for item in current + [word]
                if str(item.get("raw") or "").strip()
            )
            if (
                current
                and (
                    word["line_index"] != current[-1]["line_index"]
                    or (max_words > 0 and len(current) >= max_words)
                    or (max_chars > 0 and len(candidate_text) > max_chars)
                )
            ):
                record_groups.append(current)
                current = []
            current.append(word)
        if current:
            record_groups.append(current)

        for records in record_groups:
            timings = [timeline_by_word[word["word_index"]] for word in records]
            source_start = min(item["start"] for item in timings)
            source_end = max(item["end"] for item in timings)
            start = timeline_offset + source_time_in_kept_ranges(
                source_start, ranges
            )
            end = timeline_offset + source_time_in_kept_ranges(
                source_end, ranges
            )
            if end <= start + 0.05:
                continue
            cues.append({
                "start": start,
                "end": end,
                "text": script_text_from_word_records(records, script_lines),
            })

        # A reviewer may deliberately replace the auto-matched text.  In that
        # case it is no longer safe to pretend that individual words have exact
        # anchors, so keep one clearly visible cue across the reviewed range.
        if clip.get("manual_text_override"):
            cues = [
                cue for cue in cues
                if not (
                    timeline_offset <= cue["start"]
                    and cue["end"] <= timeline_offset + clip_duration
                )
            ]
            text = str(clip.get("expected_text") or "").strip()
            if text and clip_duration > 0.05:
                cues.append({
                    "start": timeline_offset,
                    "end": timeline_offset + clip_duration,
                    "text": text,
                })
        timeline_offset += clip_duration
    cues.sort(key=lambda cue: (cue["start"], cue["end"]))
    for index in range(1, len(cues)):
        previous = cues[index - 1]
        current = cues[index]
        if current["start"] >= previous["end"]:
            continue
        boundary = (current["start"] + previous["end"]) / 2.0
        boundary = max(previous["start"] + 0.05, boundary)
        boundary = min(current["end"] - 0.05, boundary)
        if boundary > previous["start"] and boundary < current["end"]:
            previous["end"] = boundary
            current["start"] = boundary
    block_gap_ms = int(settings.get("srt_block_gap_ms", -1))
    if block_gap_ms >= 0:
        gap = block_gap_ms / 1000.0
        for index in range(len(cues) - 1):
            current = cues[index]
            following = cues[index + 1]
            current["end"] = max(
                current["start"] + 0.05,
                following["start"] - gap,
            )
    return [cue for cue in cues if cue["end"] > cue["start"] + 0.05]


def _write_srt(path, cues):
    lines = []
    for index, cue in enumerate(cues, 1):
        start = max(0.0, float(cue["start"]))
        end = max(start + 0.05, float(cue["end"]))
        lines.extend([
            str(index),
            f"{_format_srt_time(start)} --> {_format_srt_time(end)}",
            str(cue["text"]).strip(),
            "",
        ])
    Path(path).write_text("\n".join(lines), encoding="utf-8-sig")


def subtitle_text_for_export(task):
    """Return the reviewed text that is actually present in the exported clips.

    The original task script is not always safe here: a reviewer may exclude a
    bad take or correct one clip's expected text.  Joining the included clips in
    export order gives stable-whisper exactly the text present in the final
    media, while retaining the task script as a compatibility fallback for old
    analysis reports.
    """
    all_clips = list(task.get("clips", []))
    clips = sorted(
        (
            clip for clip in all_clips
            if clip.get("included", True)
        ),
        key=lambda clip: (
            int(clip.get("export_order", 10 ** 9)),
            int(clip.get("source_index", 10 ** 9)),
        ),
    )
    original_script = str(task.get("script") or "").strip()
    if original_script and smart_video_task_review_decision(task) == "approved":
        # A human explicitly confirmed that the detector produced a false
        # missing-segment alarm.  Align against the authoritative full script,
        # not the detector's incomplete per-clip text ranges.
        return original_script
    relevant_clips = [
        clip for clip in all_clips
        if not clip.get("auto_excluded_duplicate")
    ]
    all_clips_included = bool(relevant_clips) and len(clips) == len(relevant_clips)
    has_manual_text = any(clip.get("manual_text_override") for clip in clips)
    if original_script and all_clips_included and not has_manual_text:
        # This also repairs old analysis reports whose per-clip fuzzy ranges
        # accidentally omitted the first or last words while every take is
        # still being exported.
        return original_script
    parts = [
        str(clip.get("expected_text") or "").strip()
        for clip in clips
        if str(clip.get("expected_text") or "").strip()
    ]
    if parts:
        return "\n".join(parts)
    if original_script:
        return original_script
    script_words = task.get("script_words", [])
    if script_words:
        return script_text_from_word_records(
            script_words, task.get("script_lines", [])
        ).strip()
    return ""


def analyze_breath_cut_files(
    sources, settings=None, progress=None, cancelled=None
):
    """Analyze arbitrary videos for a standalone, timeline-reviewed breath cut."""
    settings = normalize_smart_video_editor_settings(settings)
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    ffmpeg = _resolve_ffmpeg(settings)
    analysis_settings = dict(settings)
    analysis_settings["output_folder_name"] = BREATH_CUT_OUTPUT_FOLDER_NAME
    unique_sources = []
    seen = set()
    for source in sources or []:
        path = Path(source)
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen or not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        seen.add(key)
        unique_sources.append(path)
    if not unique_sources:
        raise ValueError("没有可用于气口剪辑的视频文件")

    tasks = []
    for index, source in enumerate(unique_sources, 1):
        if cancelled():
            raise InterruptedError("用户已请求停止气口分析")
        progress(
            f"[气口剪辑] 分析 {index}/{len(unique_sources)}：{source.name}"
        )
        duration = _media_duration(source, ffmpeg)
        silence_ranges, silence_error = analyze_source_silence(
            source,
            source.parent,
            duration,
            analysis_settings,
            ffmpeg=ffmpeg,
            progress=progress,
            log_prefix="[气口剪辑]",
        )
        voice_absence_ranges, voice_error = analyze_source_voice_absence(
            source,
            source.parent,
            duration,
            analysis_settings,
            progress=progress,
            log_prefix="[气口剪辑]",
        )
        breath_gap_ranges = combine_breath_gap_ranges(
            duration,
            silence_ranges,
            voice_absence_ranges,
            mode=settings.get("breath_detection_mode", "voice_refined"),
            db_detection_available=(
                settings.get("silence_detection_enabled", True)
                and not silence_error
            ),
            voice_detection_available=(
                settings.get("voice_detection_enabled", True)
                and not voice_error
            ),
            boundary_extension_s=min(
                0.55,
                settings.get("vad_speech_pad_ms", 120) / 1000.0 + 0.1,
            ),
        )
        detection_error = _combined_detection_error(
            settings, silence_error, voice_error
        )
        trim_start, trim_end, removals, planned_kept = breath_cut_plan(
            duration, breath_gap_ranges, settings
        )
        issues = []
        if detection_error:
            issues.append({
                "severity": "orange",
                "kind": "silence_detection_failed",
                "start": 0.0,
                "end": duration,
                "title": "气口检测失败",
                "detail": detection_error,
            })
        removed_seconds = max(0.0, trim_start) + max(0.0, duration - trim_end)
        removed_seconds += sum(end - start for start, end in removals)
        clip = {
            "source": str(source),
            "file_name": source.name,
            "source_index": 0,
            "export_order": 1,
            "included": True,
            "status": "orange" if detection_error else "green",
            "original_duration": round(duration, 3),
            "trim_start": trim_start,
            "trim_end": trim_end,
            "automatic_trim_start": trim_start,
            "automatic_trim_end": trim_end,
            "silence_ranges": silence_ranges,
            "voice_absence_ranges": voice_absence_ranges,
            "breath_gap_ranges": breath_gap_ranges,
            "silence_detection_error": detection_error,
            "db_silence_detection_error": silence_error,
            "voice_detection_error": voice_error,
            "pause_removals": removals,
            "automatic_pause_removals": removals,
            "kept_ranges": planned_kept,
            "boundary_removed_seconds": round(
                max(0.0, trim_start) + max(0.0, duration - trim_end), 3
            ),
            "internal_removed_seconds": round(
                sum(end - start for start, end in removals), 3
            ),
            "removed_seconds": round(removed_seconds, 3),
            "words": [],
            "word_timeline": [],
            "cues": [],
            "expected_text": "",
            "recognized_text": "",
            "issues": issues,
        }
        tasks.append({
            "task_id": source.stem,
            "task_name": source.stem,
            "label": source.name,
            "task_dir": str(source.parent),
            "output_dir": str(source.parent / BREATH_CUT_OUTPUT_FOLDER_NAME),
            "clips": [clip],
            "missing_blocks": [],
            "script_words": [],
            "script_lines": [],
        })
    return {
        "workflow": "breath_cut",
        "settings": settings,
        "tasks": tasks,
        "summary": {
            "file_count": len(tasks),
            "removed_seconds": round(sum(
                task["clips"][0]["removed_seconds"] for task in tasks
            ), 3),
            "error_count": sum(
                bool(task["clips"][0].get("silence_detection_error"))
                for task in tasks
            ),
        },
    }


def _generate_final_aligned_srt(
    output_video, output_srt, task, settings, progress, alignment_model=None
):
    """Generate SRT from the finished edit through the proven subtitle path.

    Import lazily so SmartVideoEditor itself keeps the project's safe startup
    order.  A temporary file prevents a failed alignment from leaving a partial
    SRT beside an otherwise valid video.
    """
    from model.SubtitleHelper import generate_srt_whisper_only

    subtitle_text = subtitle_text_for_export(task)
    if not subtitle_text:
        raise ValueError("没有可用于生成字幕的任务文案")
    language = str(task.get("language") or "").strip().lower()
    if language in {"", "unknown", "und", "none"}:
        language = "sk"
    if language.startswith("zh"):
        language = "zh"

    # stable-whisper selects the writer from the final extension, therefore the
    # transactional temporary file must still end in .srt.
    temporary_srt = output_srt.with_name(
        f".{output_srt.stem}.aligning{output_srt.suffix}"
    )
    temporary_srt.unlink(missing_ok=True)
    progress(
        f"[智能剪辑] 正在用原字幕功能重新对齐最终成片：{output_video.name}"
    )
    try:
        generate_srt_whisper_only(
            str(output_video),
            subtitle_text,
            str(temporary_srt),
            language=language,
            include_line_breaks=bool(settings["srt_include_line_breaks"]),
            max_words_per_block=int(settings["srt_max_words_per_block"]),
            max_chars_per_block=int(settings["srt_max_chars_per_block"]),
            block_gap_ms=(
                None
                if int(settings["srt_block_gap_ms"]) < 0
                else int(settings["srt_block_gap_ms"])
            ),
            model=alignment_model,
        )
        if not temporary_srt.is_file() or temporary_srt.stat().st_size <= 0:
            raise RuntimeError("原字幕功能没有生成有效的 SRT 文件")
        os.replace(temporary_srt, output_srt)
    finally:
        temporary_srt.unlink(missing_ok=True)
    progress(f"[智能剪辑] 最终成片字幕对齐完成：{output_srt.name}")


def _export_task(
    task, settings, ffmpeg, progress, cancelled, alignment_model=None
):
    clips = sorted(
        (
            clip for clip in task.get("clips", [])
            if clip.get("included", True)
        ),
        key=lambda clip: (
            int(clip.get("export_order", 10 ** 9)),
            int(clip.get("source_index", 10 ** 9)),
        ),
    )
    ranges = [
        (clip, range_value)
        for clip in clips
        for range_value in clip.get("kept_ranges", [])
        if range_value[1] - range_value[0] >= 0.08
    ]
    if not ranges:
        raise ValueError("没有勾选可导出的有效视频片段")

    output_dir = Path(task["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _safe_child_name(
        task.get("task_id") or task.get("task_name"), "智能剪辑"
    ) + "_智能剪辑"
    output_video = _versioned_output(
        output_dir / f"{base_name}.mp4", settings["existing_output"]
    )
    if output_video is None:
        return {
            "task_id": task.get("task_id", ""),
            "status": "skipped",
            "message": "输出已存在，按设置跳过",
        }
    output_srt = output_video.with_suffix(".srt")
    width, height, fps = _media_shape(ranges[0][0]["source"])

    with tempfile.TemporaryDirectory(prefix="smart_edit_", dir=str(output_dir)) as temp:
        temp_dir = Path(temp)
        segment_paths = []
        for index, (clip, (start, end)) in enumerate(ranges, 1):
            if cancelled():
                raise InterruptedError("用户已请求停止智能剪辑导出")
            segment_path = temp_dir / f"segment_{index:05d}.mkv"
            frame_count = max(1, int(round((end - start) * fps)))
            frame_duration = frame_count / fps
            progress(
                f"[智能剪辑] 裁剪 {index}/{len(ranges)}：{clip['file_name']} "
                f"{start:.2f}-{end:.2f} 秒"
            )
            video_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps:.3f},setpts=PTS-STARTPTS"
            )
            args = [
                ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", clip["source"],
                "-t", f"{frame_duration:.9f}",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", video_filter,
                "-af", (
                    f"apad,atrim=duration={frame_duration:.9f},"
                    "asetpts=PTS-STARTPTS"
                ),
                "-frames:v", str(frame_count),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-bf", "0",
                # AAC adds encoder padding to every independently encoded file.
                # When those files are stream-concatenated, the padding shifts
                # every following video segment and Resolve sees a longer clip.
                # Keep intermediate audio sample-accurate and encode AAC once,
                # only after all segments have been joined.
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                "-shortest", str(segment_path),
            ]
            _run_process(args, progress, cancelled)
            segment_paths.append(segment_path)

        concat_path = temp_dir / "concat.txt"
        concat_path.write_text(
            "\n".join(
                "file '{}'".format(str(path).replace("'", "'\\''"))
                for path in segment_paths
            ),
            encoding="utf-8",
        )
        partial = temp_dir / "final.mp4"
        progress(f"[智能剪辑] 正在合并：{output_video.name}")
        _run_process([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(partial),
        ], progress, cancelled)
        shutil.move(str(partial), str(output_video))

    _generate_final_aligned_srt(
        output_video,
        output_srt,
        task,
        settings,
        progress,
        alignment_model=alignment_model,
    )
    return {
        "task_id": task.get("task_id", ""),
        "status": "completed",
        "video": str(output_video),
        "srt": str(output_srt),
        "subtitle_alignment": "final_media_stable_whisper",
        "removed_seconds": round(sum(
            float(clip.get("removed_seconds") or 0.0) for clip in clips
        ), 3),
    }


def _export_breath_cut_task(task, settings, ffmpeg, progress, cancelled):
    clip = next(
        (
            value for value in task.get("clips", [])
            if value.get("included", True)
        ),
        None,
    )
    if clip is None:
        return {
            "task_id": task.get("task_id", ""),
            "status": "skipped",
            "message": "用户取消勾选，未导出",
        }
    ranges = [
        [float(start), float(end)]
        for start, end in clip.get("kept_ranges", []) or []
        if float(end) - float(start) >= 0.08
    ]
    if not ranges:
        raise ValueError("没有可导出的保留区间")
    source = Path(clip["source"])
    output_dir = Path(task["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _safe_child_name(source.stem, "视频") + "_气口剪辑"
    output_video = _versioned_output(
        output_dir / f"{base_name}.mp4", settings["existing_output"]
    )
    if output_video is None:
        return {
            "task_id": task.get("task_id", ""),
            "status": "skipped",
            "message": "输出已存在，按设置跳过",
        }
    width, height, fps = _media_shape(source)
    with tempfile.TemporaryDirectory(prefix="breath_cut_", dir=str(output_dir)) as temp:
        temp_dir = Path(temp)
        segment_paths = []
        for index, (start, end) in enumerate(ranges, 1):
            if cancelled():
                raise InterruptedError("用户已请求停止气口剪辑导出")
            segment_path = temp_dir / f"segment_{index:05d}.mkv"
            frame_count = max(1, int(round((end - start) * fps)))
            frame_duration = frame_count / fps
            progress(
                f"[气口剪辑] 裁剪 {index}/{len(ranges)}：{source.name} "
                f"{start:.2f}-{end:.2f} 秒"
            )
            video_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps:.3f},setpts=PTS-STARTPTS"
            )
            _run_process([
                ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source),
                "-t", f"{frame_duration:.9f}",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", video_filter,
                "-af", (
                    f"apad,atrim=duration={frame_duration:.9f},"
                    "asetpts=PTS-STARTPTS"
                ),
                "-frames:v", str(frame_count),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-bf", "0", "-c:a", "pcm_s16le", "-ar", "48000",
                "-ac", "2", "-shortest", str(segment_path),
            ], progress, cancelled)
            segment_paths.append(segment_path)
        concat_path = temp_dir / "concat.txt"
        concat_path.write_text(
            "\n".join(
                "file '{}'".format(str(path).replace("'", "'\\''"))
                for path in segment_paths
            ),
            encoding="utf-8",
        )
        partial = temp_dir / "final.mp4"
        progress(f"[气口剪辑] 正在合并：{output_video.name}")
        _run_process([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i",
            str(concat_path), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar",
            "48000", "-ac", "2", "-movflags", "+faststart", str(partial),
        ], progress, cancelled)
        shutil.move(str(partial), str(output_video))
    return {
        "task_id": task.get("task_id", ""),
        "status": "completed",
        "video": str(output_video),
        "source": str(source),
        "removed_seconds": round(float(clip.get("removed_seconds") or 0.0), 3),
    }


def export_breath_cut_bundle(
    bundle, settings=None, progress=None, cancelled=None
):
    settings = normalize_smart_video_editor_settings(
        settings or bundle.get("settings")
    )
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    tasks = list(bundle.get("tasks", []) or [])
    if not tasks:
        raise ValueError("没有可导出的气口剪辑任务")
    ffmpeg = _resolve_ffmpeg(settings)
    result = {"completed": [], "failed": [], "skipped": []}
    for index, task in enumerate(tasks, 1):
        progress(
            f"[气口剪辑] 导出 {index}/{len(tasks)}：{task.get('label', '')}"
        )
        try:
            item = _export_breath_cut_task(
                task, settings, ffmpeg, progress, cancelled
            )
        except InterruptedError:
            raise
        except Exception as error:
            result["failed"].append({
                "task_id": task.get("task_id", ""),
                "label": task.get("label", ""),
                "task_dir": task.get("task_dir", ""),
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            })
        else:
            target = "skipped" if item.get("status") == "skipped" else "completed"
            result[target].append(item)
    return result


def export_smart_video_bundle(
    bundle,
    settings=None,
    progress=None,
    cancelled=None,
    alignment_model=None,
):
    validate_smart_video_bundle_for_export(bundle)
    settings = normalize_smart_video_editor_settings(
        settings or bundle.get("settings")
    )
    progress = progress or (lambda _message: None)
    cancelled = cancelled or (lambda: False)
    tasks = list(bundle.get("tasks", []))
    export_tasks = [
        task for task in tasks
        if smart_video_task_review_decision(task) != "skipped"
    ]
    ffmpeg = _resolve_ffmpeg(settings) if export_tasks else ""
    result = {"completed": [], "failed": [], "skipped": []}
    for index, task in enumerate(tasks, 1):
        if smart_video_task_review_decision(task) == "skipped":
            progress(
                f"[智能剪辑] 暂缓任务 {index}/{len(tasks)}：{task.get('label', '')}"
            )
            result["skipped"].append({
                "task_id": task.get("task_id", ""),
                "label": task.get("label", ""),
                "status": "skipped_missing_segment",
                "message": "人工选择暂缓：检测到缺段，本次未导出",
                "task_dir": str(task.get("task_dir") or ""),
                "report_path": str(task.get("report_path") or ""),
            })
            continue
        progress(
            f"[智能剪辑] 导出任务 {index}/{len(tasks)}：{task.get('label', '')}"
        )
        try:
            item = _export_task(
                task,
                settings,
                ffmpeg,
                progress,
                cancelled,
                alignment_model=alignment_model,
            )
        except InterruptedError:
            raise
        except Exception as error:
            result["failed"].append({
                "task_id": task.get("task_id", ""),
                "label": task.get("label", ""),
                "task_dir": task.get("task_dir", ""),
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            })
        else:
            result["completed"].append(item)
    return result


class SmartVideoEditorThread(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(
        self,
        mode,
        settings,
        jobs=None,
        model=None,
        model_loader=None,
        bundle=None,
        files=None,
        parent=None,
    ):
        super().__init__(parent)
        self.mode = str(mode)
        self.settings = normalize_smart_video_editor_settings(settings)
        self.jobs = list(jobs or [])
        self.model = model
        self.model_loader = model_loader
        self.bundle = bundle
        self.files = list(files or [])

    def _load_requested_model(self):
        if self.model is not None or self.model_loader is None:
            return self.model
        model_name = self.settings.get("whisper_model_size", "base")
        self.log.emit(f"[智能剪辑] 正在后台加载 {model_name} 模型……")
        self.model = self.model_loader(model_name)
        self.log.emit(f"[智能剪辑] {model_name} 模型加载完成。")
        return self.model

    def run(self):
        try:
            if self.mode == "analyze":
                self._load_requested_model()
                result = analyze_smart_video_jobs(
                    self.jobs,
                    self.model,
                    self.settings,
                    progress=self.log.emit,
                    cancelled=self.isInterruptionRequested,
                )
            elif self.mode == "export":
                if not isinstance(self.bundle, dict):
                    raise ValueError("没有可导出的智能剪辑分析结果")
                self._load_requested_model()
                result = export_smart_video_bundle(
                    self.bundle,
                    self.settings,
                    progress=self.log.emit,
                    cancelled=self.isInterruptionRequested,
                    alignment_model=self.model,
                )
            elif self.mode == "breath_analyze":
                result = analyze_breath_cut_files(
                    self.files,
                    self.settings,
                    progress=self.log.emit,
                    cancelled=self.isInterruptionRequested,
                )
            elif self.mode == "breath_export":
                if not isinstance(self.bundle, dict):
                    raise ValueError("没有可导出的气口剪辑分析结果")
                result = export_breath_cut_bundle(
                    self.bundle,
                    self.settings,
                    progress=self.log.emit,
                    cancelled=self.isInterruptionRequested,
                )
            else:
                raise ValueError(f"未知的智能剪辑阶段：{self.mode}")
        except (Exception, SystemExit) as error:
            self.failed.emit(
                f"{type(error).__name__}: {error}",
                traceback.format_exc(),
            )
            return
        self.completed.emit(result)

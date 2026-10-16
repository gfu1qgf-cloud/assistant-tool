"""Deterministic, punctuation-aware Flow copy segmentation. No model/network."""

import math
import re
import unicodedata

CONFIG_KEY = "script_segmenter"
DEFAULT_SETTINGS = {"durations": [6, 8, 10], "preferred_seconds": 10,
                    "words_per_minute": 150, "characters_per_minute": 240,
                    "pause_seconds": 0.3, "padding_seconds": 0.4,
                    "boundary_strategy": "fewest"}
ABBREVIATIONS = {"dr", "prof", "mr", "mrs", "ms", "sv", "napr", "tzv", "resp", "str", "č", "atď"}
SPECIAL = re.compile(r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[\w-]+", re.UNICODE)


def _number(value, default, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result)) if math.isfinite(result) else default


def normalize_settings(value):
    value = value if isinstance(value, dict) else {}
    raw = value.get("durations", [6, 8, 10])
    durations = sorted({int(x) for x in raw if x in (6, 8, 10)}) if isinstance(raw, (list, tuple)) else []
    if not durations:
        durations = [6, 8, 10]
    preferred = value.get("preferred_seconds", 10)
    preferred = preferred if preferred in durations else max(durations)
    return {"durations": durations, "preferred_seconds": preferred,
            "words_per_minute": _number(value.get("words_per_minute"), 150, 40, 300),
            "characters_per_minute": _number(value.get("characters_per_minute"), 240, 80, 600),
            "pause_seconds": _number(value.get("pause_seconds"), .3, 0, 2),
            "padding_seconds": _number(value.get("padding_seconds"), .4, 0, 2),
            "boundary_strategy": "punctuation_first" if value.get("boundary_strategy") == "punctuation_first" else "fewest"}


def _word_character(character):
    return character.isalnum() or character == "_" or unicodedata.category(character).startswith("M")


def _cjk(character):
    return ("\u3400" <= character <= "\u9fff" or "\u3040" <= character <= "\u30ff"
            or "\uac00" <= character <= "\ud7af" or "\U00020000" <= character <= "\U0003134f")


def _reading_word_units(word):
    """Conservative timing weights; never rewrite or split the source token.

    A year/date/chapter reference can take several spoken words or syllables,
    even when written as one token. Count every digit, plus numeric connectors,
    rather than treating an entire number as a single 0.4-second short word.
    This is a timing estimate, not language-specific number normalization.
    """
    digits = sum(character.isdigit() for character in word)
    letters = sum(character.isalnum() and not character.isdigit() and not _cjk(character) for character in word)
    connectors = sum(character in ".,:/\u2010\u2011-" and word[index-1].isdigit() and word[index+1].isdigit()
                     for index, character in enumerate(word) if 0 < index < len(word)-1)
    return digits + connectors + (max(1, letters / 12) if letters else 0)


def word_spans(text):
    """Accents, inner apostrophes/hyphens, decimal numbers and URLs stay whole.

    Continuous CJK text is kept as a unit, not guessed apart without a language
    tokenizer. Long units are flagged instead of silently cutting a word.
    """
    spans, index = [], 0
    while index < len(text):
        special = SPECIAL.match(text, index)
        if special:
            end = special.end()
            while end > index and text[end - 1] in ".,!?;:。！？；，)]»”":
                end -= 1
            spans.append((index, end))
            index = end
            continue
        if not _word_character(text[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < len(text):
            character = text[index]
            if _word_character(character):
                index += 1
            elif (character in "'’\u2010\u2011-" and index + 1 < len(text)
                  and _word_character(text[index + 1])):
                index += 1
            elif (character in ".,:/" and text[index - 1].isdigit()
                  and index + 1 < len(text) and text[index + 1].isdigit()):
                index += 1
            else:
                break
        spans.append((start, index))
    return spans


def _boundary(text, left_end, right_start):
    gap = text[left_end:right_start]
    # Do not strand an opening quote/bracket at the previous segment's end.
    opening = re.search(r"\s([\"„“«‹(\[]+)\s*$", gap)
    return left_end + opening.start(1) if opening else right_start


def _pause_kind(word, gap):
    if "\n" in gap or "\r" in gap:
        return "换行", 0.15, 1.0
    if re.search(r"[!?。！？]", gap):
        return "句末", 0.0, 1.0
    if "." in gap and word.casefold() not in ABBREVIATIONS:
        return "句末", 0.0, 1.0
    if re.search(r"[;；:：]", gap):
        return "分号/冒号", .7, .8
    if re.search(r"[,，、]", gap):
        return "逗号", 1.0, .55
    if re.search(r"[—–]", gap):
        return "破折号", 1.2, .65
    return "词间", 3.0, 0.0


def _numbered_item_links(text, spans):
    """Keep a paragraph's list marker with its following whole word.

    Only a line-leading short ordinal with list punctuation is protected;
    years, decimals, times and chapter references retain their normal timing.
    No source characters or spoken number units are removed.
    """
    indices = {start: index for index, (start, _end) in enumerate(spans)}
    marker = re.compile(r"(?:^|(?<=[\r\n]))[ \t]*(?:\([ \t]*(\d{1,3})[ \t]*\)|(\d{1,3})[.)）、:：])(?=\s|[^\W\d_])")
    links = set()
    for match in marker.finditer(text):
        position = match.start(1) if match.group(1) is not None else match.start(2)
        index = indices.get(position)
        if index is not None and index + 1 < len(spans):
            links.add(index + 1)
    return links


def _first_unit_end(start, links):
    end = start + 1
    while end in links:
        end += 1
    return end


def _oversize_warning(start, end):
    if end > start + 1:
        return "段落序号与后续完整词合计超时；请调整语速或正文，未拆开它们。"
    return "单个完整词或连续无空格文本超时；请补充停顿标点或调整语速，未切开它。"


def _punctuation_first_choices(prefix, boundaries, settings, canceled, links):
    """Fill complete punctuation-delimited phrases, then retreat if needed.

    Never start the next phrase merely to fill unused time. If the first phrase
    itself is too long, fall back to whole words; the original text is untouched.
    """
    count, start = len(boundaries), 0
    maximum, padding = max(settings["durations"]), settings["padding_seconds"]
    choices = [None] * count
    while start < count:
        if canceled and canceled():
            from model.BackgroundTasks import BackgroundTaskCanceled
            raise BackgroundTaskCanceled("已取消文案分段。")
        first_end = _first_unit_end(start, links)
        last_word, last_pause = None, None
        for end in range(start + 1, count + 1):
            if prefix[end] - prefix[start] + padding > maximum + 1e-9:
                break
            if end in links:
                continue
            last_word = end
            if end == count or boundaries[end - 1][0] != "词间":
                last_pause = end
        end = last_pause or last_word or first_end
        estimated = prefix[end] - prefix[start]
        fits = [seconds for seconds in settings["durations"] if estimated + padding <= seconds + 1e-9]
        warning = "" if fits else _oversize_warning(start, end)
        # In automatic mode a shorter complete phrase may only need 6/8 sec.
        # Fixed 6/8/10-second modes still use exactly the user's chosen length.
        choices[start] = (end, min(fits) if fits else maximum, estimated, warning)
        start = end
    return choices


def split_copy(text, settings=None, canceled=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("没有文案，请粘贴文字或填写任务语音文案。")
    if len(text) > 120000:
        raise ValueError("单篇文案超过12万字符，请分批处理。")
    settings = normalize_settings(settings)
    spans = word_spans(text)
    if not spans:
        raise ValueError("文案中没有可朗读的文字。")
    maximum = max(settings["durations"])
    cuts = [0] + [_boundary(text, spans[i - 1][1], spans[i][0]) for i in range(1, len(spans))] + [len(text)]
    links = _numbered_item_links(text, spans)
    prefix, word_units, boundaries = [0.0], [0.0], []
    for index, (start, end) in enumerate(spans):
        word = text[start:end]
        cjk_count = sum(_cjk(character) for character in word)
        spoken = cjk_count * 60 / settings["characters_per_minute"]
        units = _reading_word_units(word)
        spoken += units * 60 / settings["words_per_minute"]
        word_units.append(word_units[-1] + units)
        gap = text[end:spans[index + 1][0] if index + 1 < len(spans) else len(text)]
        # A list marker's dot is not a spoken sentence-ending pause.
        kind, penalty, pause = ("词间", 3.0, 0.0) if index + 1 in links else _pause_kind(word, gap)
        boundaries.append((kind, penalty))
        prefix.append(prefix[-1] + spoken + pause * settings["pause_seconds"])
    count = len(spans)
    if settings["boundary_strategy"] == "punctuation_first":
        choices = _punctuation_first_choices(prefix, boundaries, settings, canceled, links)
    else:
        # Keep the existing minimum-video-count policy unchanged by default.
        costs, choices = [(math.inf, math.inf)] * (count + 1), [None] * count
        costs[count] = (0, 0.0)
        for start in range(count - 1, -1, -1):
            if canceled and canceled():
                from model.BackgroundTasks import BackgroundTaskCanceled
                raise BackgroundTaskCanceled("已取消文案分段。")
            first_end = _first_unit_end(start, links)
            for end in range(first_end, count + 1):
                if end in links:
                    continue
                estimated = prefix[end] - prefix[start]
                fits = [seconds for seconds in settings["durations"]
                        if estimated + settings["padding_seconds"] <= seconds + 1e-9]
                if not fits:
                    if end > first_end:
                        break
                    seconds, score, warning = maximum, 1000.0, _oversize_warning(start, end)
                else:
                    seconds = (settings["preferred_seconds"] if end < count and settings["preferred_seconds"] in fits
                               else min(fits))
                    score = 0 if end == count else 8 * ((seconds - estimated - settings["padding_seconds"]) / seconds) ** 2
                    warning = ""
                if end < count:
                    score += boundaries[end - 1][1] * 1.6
                total = (1 + costs[end][0], score + costs[end][1])
                if total < costs[start]:
                    costs[start] = total
                    choices[start] = (end, seconds, estimated, warning)
                if not fits:
                    break
    segments, start = [], 0
    while start < count:
        end, seconds, estimated, warning = choices[start]
        segments.append({"index": len(segments) + 1, "seconds": seconds,
                         "estimated_seconds": round(estimated, 3), "text": text[cuts[start]:cuts[end]].strip(),
                         "start": cuts[start], "end": cuts[end], "word_count": end - start,
                         "spoken_word_count": round(word_units[end] - word_units[start], 3),
                         "boundary": boundaries[end - 1][0] if end < count else "文案结束",
                         "warning": warning})
        start = end
    # Every source character belongs to exactly one interval, including gaps.
    if "".join(text[item["start"]:item["end"]] for item in segments) != text:
        raise RuntimeError("文案完整性校验失败，未生成结果。")
    return {"source": text, "settings": settings, "segments": segments,
            "estimated_seconds": round(prefix[-1], 3),
            "video_seconds": sum(item["seconds"] for item in segments),
            "warning_count": sum(bool(item["warning"]) for item in segments)}


def format_result(result, indices=None, with_labels=True):
    selected = set(indices) if indices is not None else None
    blocks = []
    for segment in result.get("segments", []):
        if selected is not None and segment["index"] not in selected:
            continue
        label = (f"第{segment['index']}段｜{segment['seconds']}秒"
                 if not segment["warning"] else f"第{segment['index']}段｜超出{segment['seconds']}秒，需人工处理")
        blocks.append(f"【{label}】\n{segment['text']}" if with_labels else segment["text"])
    return "\n\n".join(blocks)


def run_segmentation(payload, run):
    results = []
    for index, entry in enumerate(payload["entries"]):
        run.check_canceled()
        try:
            result = split_copy(entry["text"], payload["settings"], canceled=run.is_canceled)
            results.append({"key": entry["key"], "result": result})
        except ValueError as error:
            results.append({"key": entry["key"], "error": str(error)})
        run.report(f"文案分段 {index + 1}/{len(payload['entries'])}", int((index + 1) * 100 / len(payload["entries"])))
    return {"entries": results}

"""Use real SRT/ASR speech ranges, then force-align the authoritative text.

No equal-duration word interpolation, transcript replacement or media writes.
"""
import copy
from difflib import SequenceMatcher
from itertools import groupby
import math
import re
import unicodedata


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def check_windows(windows, text, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('语句窗口的音频时长无效。')
    if not 0 < len(windows) <= 128 or compact(''.join(w['text'] for w in windows)) != compact(text):
        raise ValueError('语句窗口未完整覆盖原文，未丢弃任何文案。')
    previous = 0.
    for window in windows:
        start, end = float(window['start']), float(window['end'])
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                or start < previous - .02 or end > duration + .001 or end - start > 28
                or len(window['text']) > 800):
            raise ValueError('语句窗口太长、相互重叠或超出音频；不能作为可靠的逐词对齐范围。')
        previous = end
    return windows


def subtitle_windows(words, text, duration):
    if not words or not all('cue' in word for word in words):
        raise ValueError('同名SRT缺少语句编号。')
    if compact(''.join(w['text'] for w in words)) != compact(text):
        raise ValueError('同名SRT与当前原文不一致，不能借用时间范围。')
    cues = []
    for _, items in groupby(words, key=lambda word: word['cue']):
        items = list(items); start, end = items[0]['start'], items[0]['end']
        if any(w['start'] != start or w['end'] != end for w in items):
            raise ValueError('同一SRT块存在不同时间范围。')
        cues.append({'start': start, 'end': end, 'text': ' '.join(w['text'] for w in items)})
    windows = []
    for cue in cues:
        if (windows and cue['start'] >= windows[-1]['end'] - .02
                and cue['end'] - windows[-1]['start'] <= 12
                and len(windows[-1]['text']) + len(cue['text']) < 400):
            windows[-1]['end'] = cue['end']; windows[-1]['text'] += ' ' + cue['text']
        else:
            windows.append(copy.deepcopy(cue))
    # Preserve the original ranges for symmetric boundary padding.
    bounds = [(w['start'], w['end']) for w in windows]
    for i, window in enumerate(windows):
        lower = 0 if i == 0 else (bounds[i-1][1] + bounds[i][0]) / 2
        upper = duration if i == len(windows) - 1 else (bounds[i][1] + bounds[i+1][0]) / 2
        window['start'] = max(lower, window['start'] - .12)
        window['end'] = min(upper, window['end'] + .12)
    return check_windows(windows, text, duration)


def normalized_token(text):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFC', text).casefold())


def recognition_windows(words, text, duration):
    """Exact monotonic matches locate ranges; only supplied text is rendered."""
    tokens = re.findall(r'\S+', text)
    expected = [(i, normalized_token(word)) for i, word in enumerate(tokens) if normalized_token(word)]
    observed, previous = [], -1.
    for word in words:
        value = normalized_token(word['text']); start, end = float(word['start']), float(word['end'])
        if not value or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration + .25 or start < previous:
            continue
        observed.append({**word, 'norm': value, 'start': start, 'end': min(duration, end)}); previous = start
    if not expected or not observed:
        raise ValueError('本地识别没有可靠的语句定位词。')
    matcher = SequenceMatcher(None, [w[1] for w in expected], [w['norm'] for w in observed], autojunk=False)
    pairs = []
    for block in matcher.get_matching_blocks():
        if block.size < min(3, len(expected)): continue
        pairs.extend((expected[block.a + k][0], observed[block.b + k]) for k in range(block.size))
    if (not pairs or len(pairs) < .65 * len(expected) or pairs[0][0] > 5 or len(tokens) - 1 - pairs[-1][0] > 5
            or any(right[0] - left[0] > 12 for left, right in zip(pairs, pairs[1:]))):
        raise ValueError('本地识别与原文的可靠匹配不足，不能定位全部语句；未猜测整段时间。')
    # When the selected text is only the body, do not include spoken title or
    # other recognizer context preceding/following an exactly matched edge.
    first = pairs[0][1] if pairs[0][0] == 0 else observed[0]
    last = pairs[-1][1] if pairs[-1][0] == len(tokens)-1 else observed[-1]
    cuts = [(0, max(0., first['start'] - .12))]
    for index, anchor in pairs:
        if index > cuts[-1][0] and (anchor['start'] - cuts[-1][1] >= 8 or index - cuts[-1][0] >= 40):
            cuts.append((index, max(cuts[-1][1] + .02, anchor['start'] - .06)))
    cuts.append((len(tokens), min(duration, last['end'] + .12)))
    windows = [{'start': a[1], 'end': b[1], 'text': ' '.join(tokens[a[0]:b[0]])} for a, b in zip(cuts, cuts[1:])]
    return check_windows(windows, text, duration)


def recognize_anchors(model, voice, language, check):
    if not callable(getattr(model, 'transcribe', None)):
        raise ValueError('当前字幕模型不支持本地语句定位。')
    segments, _ = model.transcribe(str(voice), language=language, word_timestamps=True,
                                   beam_size=5, vad_filter=True, condition_on_previous_text=False)
    words = []
    try:
        for segment in segments:
            check()
            words.extend({'text': w.word.strip(), 'start': w.start, 'end': w.end} for w in segment.words or ())
            if len(words) > 12000: raise ValueError('识别定位词数超过安全上限。')
    finally:
        close = getattr(segments, 'close', None)
        if callable(close): close()
    check(); return words


def align_in_windows(model, voice, text, language, duration, check, report, original_words, repair, validate, coarse_loader=None):
    from stable_whisper.alignment import align_words
    windows = None
    if coarse_loader is not None:
        try:
            check(); windows = subtitle_windows(coarse_loader(), text, duration)
            report(f'动态字幕：同名任务SRT全文核对通过，使用{len(windows)}个真实语句窗口重新逐词对齐；不会均分词时间。')
        except InterruptedError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            report('动态字幕：同名任务SRT不可用或未通过全文／时间核对，改用本地识别定位语句窗口。')
    if windows is None:
        report('动态字幕：正在本地识别人声，仅提取可靠语句时间范围；字幕文字仍使用完整原文。')
        windows = recognition_windows(recognize_anchors(model, voice, language, check), text, duration)
    check()
    result = align_words(model, str(voice), result=windows, language=language, normalize_text=False,
                         regroup=False, suppress_silence=False, verbose=None, progress_callback=lambda *_: check())
    check()
    if result is None: raise ValueError('短窗口对齐未返回结果。')
    words = [{'text': w.word.strip(), 'start': w.start, 'end': w.end} for w in result.all_words() if w.word.strip()]
    words = validate(repair(original_words(words, text, language), duration, report,
                            edge_bounds=(windows[0]['start'], windows[-1]['end'])), duration)
    report(f'动态字幕：短窗口逐词重对齐完成，保留全部{len(words)}个词；没有替换原文或均分整段时间。')
    return words

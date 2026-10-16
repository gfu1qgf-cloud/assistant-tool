"""Verbatim forced-alignment adapter; never substitute a model transcription.

Only this model adapter can repair bounded timing defects. User SRT validation
remains strict and untouched. Large/unanchored failures are never guessed.
"""
import bisect
import copy
import math
import re
import unicodedata


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def original_words(words, text, language='sk'):
    """Restore original whitespace-delimited words from token/punctuation pieces."""
    if not isinstance(words, list) or not words:
        raise ValueError('本地对齐没有返回任何词时间戳。')
    pieces, ends, offset = [], [], 0
    for word in words:
        value = compact(str(word.get('text', '')))
        if not value:
            continue
        pieces.append({**word, 'text': value})
        offset += len(value); ends.append(offset)
    expected, actual = compact(text), ''.join(w['text'] for w in pieces)
    if expected != actual:
        prefix = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b), min(len(expected), len(actual)))
        raise ValueError(f'本地对齐结果缺少或改变了任务文字（第{prefix + 1}个非空白字符附近）；'
                         '这是对齐结果校验失败，不代表已确认音频有问题。')
    if language in {'zh', 'ja', 'th', 'lo', 'my'}:
        return pieces
    tokens = re.findall(r'\S+', text)
    result, offset = [], 0
    for token in tokens:
        length = len(compact(token))
        first, last = bisect.bisect_right(ends, offset), bisect.bisect_left(ends, offset + length)
        relevant = pieces[first:last + 1]
        if not relevant:
            raise ValueError('本地对齐没有完整覆盖任务文案。')
        # A standalone punctuation token may have zero duration. Preserve it on
        # the original word, rather than treating it as a separate failed word.
        result.append({'text': token, 'start': min(float(w['start']) for w in relevant),
                       'end': max(float(w['end']) for w in relevant)})
        offset += length
    return result


def repair_model_timings(words, duration, report, edge_bounds=None):
    """After a real retry, repair only short, internally anchored zero words.

    At most two adjacent words / 5 percent overall. A single <=3-letter edge
    word needs a separately verified speech window; missing blocks stay errors.
    """
    result = copy.deepcopy(words)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('人声音频时长无效。')
    previous = -1.0
    zeros = []
    for index, word in enumerate(result):
        start, end = float(word['start']), float(word['end'])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError('本地对齐返回了非有限的时间戳。')
        if start < -.02 or end < start - .005 or start < previous - .02 or end > duration + .25 or start > duration:
            raise ValueError(f'本地对齐“{word["text"][:40]}”时间顺序或范围异常：{start:.3f}–{end:.3f}秒。')
        start, end = max(0., previous, start), min(duration, max(end, start))
        word.update(start=start, end=end); previous = start
        if end - start < .005:
            zeros.append(index)
    if not zeros:
        return result
    if len(zeros) > max(2, math.ceil(len(result) * .05)):
        raise ValueError(f'本地对齐仍有{len(zeros)}/{len(result)}个零时长词，未猜测整段时间。')
    runs = []
    for index in zeros:
        if runs and index == runs[-1][-1] + 1: runs[-1].append(index)
        else: runs.append([index])
    for run in runs:
        if len(run) == 1 and edge_bounds is not None and len(result) > 1:
            index = run[0]; token = result[index]['text'].strip('.,!?;:„“”"\'()[]')
            lower_edge, upper_edge = edge_bounds
            short = token.isalpha() and len(token) <= 3
            if (index == 0 and short and 0 <= lower_edge < upper_edge <= duration
                    and abs(result[0]['start'] - lower_edge) <= .1):
                after = result[1]
                if lower_edge - .02 <= after['start'] <= lower_edge + .25:
                    finish = min(lower_edge + .08, max(lower_edge + .04, after['start']))
                    if after['end'] - max(after['start'], finish) >= .02:
                        result[0].update(start=lower_edge, end=finish)
                        after['start'] = max(after['start'], finish)
                        continue
            if (index == len(result)-1 and short and 0 <= lower_edge < upper_edge <= duration
                    and abs(result[index]['start'] - upper_edge) <= .1):
                before = result[index-1]
                begin = max(before['start'] + .02, min(before['end'], upper_edge - .04))
                if 0 < upper_edge - begin <= .08 and max(0., before['end'] - begin) <= .08:
                    before['end'] = min(before['end'], begin)
                    result[index].update(start=begin, end=upper_edge)
                    continue
        if len(run) > 2 or run[0] == 0 or run[-1] == len(result) - 1:
            raise ValueError('本地对齐仍有未定位的开头／末尾或连续零时长词，未删词、未猜测整段时间。')
        before, after = result[run[0] - 1], result[run[-1] + 1]
        anchor = result[run[0]]['start']
        lower, upper = before['start'] + .02, after['start']
        required = .03 * len(run)
        if upper - lower < required or not lower - .1 <= anchor <= upper + .1:
            raise ValueError('零时长词缺少可靠的相邻对齐窗口，未猜测时间。')
        begin = max(lower, min(anchor, upper - required))
        finish = min(upper, begin + .08 * len(run))
        # Do not move an adjacent healthy word by more than 160 ms.
        if before['end'] > begin and before['end'] - begin > .16:
            raise ValueError('局部时间修整会影响过多相邻内容，已停止。')
        if before['end'] > begin: before['end'] = begin
        step = (finish - begin) / len(run)
        for index, item in enumerate(run):
            result[item].update(start=begin + index * step, end=begin + (index + 1) * step)
    report(f'动态字幕：局部修整{len(zeros)}个零时长词（只使用相邻对齐／已核对语句窗口，局部时刻为近似值）；全文未删改。')
    return result


def align_verbatim(model, voice, text, language, duration, check, report, validate, coarse_loader=None):
    from stable_whisper.alignment import align
    last_error = None
    for attempt in range(2):
        check()
        if attempt:
            report('动态字幕：首次对齐存在文字／时间戳异常，正在使用更长上下文并关闭静音压缩重新对齐；不会删除词。')
        result = align(model, str(voice), text=text, language=language,
                       remove_instant_words=False, regroup=False, verbose=None,
                       token_step=0 if attempt else 100,
                       suppress_silence=not bool(attempt),
                       progress_callback=lambda *_: check())
        check()
        try:
            if result is None: raise ValueError('本地模型没有返回词级对齐结果。')
            words = [{'text': word.word.strip(), 'start': word.start, 'end': word.end}
                     for word in result.all_words() if word.word.strip()]
            words = original_words(words, text, language)
            if attempt: words = repair_model_timings(words, duration, report)
            return validate(words, duration)
        except (ValueError, TypeError, KeyError) as error:
            last_error = error
    try:
        from .caption_windows import align_in_windows
        return align_in_windows(model, voice, text, language, duration, check, report,
                                original_words, repair_model_timings, validate, coarse_loader)
    except (ValueError, TypeError, KeyError) as error:
        last_error = ValueError(str(last_error) + '；短窗口：' + str(error))
    raise ValueError('本地字幕整段与短窗口对齐仍未通过：' + str(last_error) +
                     ' 请查看对齐详情；如接受近似时刻，可主动选择“匀速动画”。没有认定音频损坏。') from last_error

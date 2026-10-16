"""Additive, fingerprint-bound word timings; never writes the original SRT."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import unicodedata

from model.AppDataStorage import data_file

MAX_WORDS = 6000
_WRITE_LOCK = threading.RLock()


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def validate_words(words, duration):
    if not isinstance(words, list) or not 0 < len(words) <= MAX_WORDS:
        raise ValueError('ELE 没有返回有效的逐词时间戳。')
    result, previous = [], -1.
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('音频时长无效。')
    for item in words:
        text = item.get('text')
        start, end = item.get('start'), item.get('end')
        if (not isinstance(text, str) or not text.strip() or len(text) > 2000
                or type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= duration + .05 or start < previous):
            raise ValueError('ELE 返回了无效的逐词时间；没有猜测时刻或改写任务字幕。')
        end = min(end, duration)
        if end <= start: raise ValueError('ELE 词时间超出音频。')
        result.append(dict(text=text.strip(), start=float(start), end=float(end)))
        previous = start
    return result


def character_words(alignment):
    chars = field(alignment, 'characters')
    starts = field(alignment, 'character_start_times_seconds')
    ends = field(alignment, 'character_end_times_seconds')
    if (not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list)
            or not 0 < len(chars) <= 200000 or len(chars) != len(starts) or len(chars) != len(ends)):
        raise ValueError('ELE 没有提供完整的字符时间戳。')
    text, times = '', []
    for c, start, end in zip(chars, starts, ends):
        if (not isinstance(c, str) or not c or len(c) > 16 or type(start) not in (int, float)
                or type(end) not in (int, float) or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= end):
            raise ValueError('ELE 字符时间戳无效。')
        text += c; times.extend([(float(start), float(end))] * len(c))
    # Audio tags can have zero-length character timings; remove only bracketed
    # regions for which the service supplied no spoken duration at all.
    ignored = set()
    for m in re.finditer(r'\[[^\]\n]{1,100}\]', text):
        if all(a == b for a, b in times[m.start():m.end()]): ignored.update(range(m.start(),m.end()))
    visible = ''.join(' ' if i in ignored else c for i, c in enumerate(text))
    result = []
    for match in re.finditer(r'\S+', visible):
        span = times[match.start():match.end()]
        result.append(dict(text=match.group(), start=min(a for a, _ in span), end=max(b for _, b in span)))
    if not result or len(result) > MAX_WORDS: raise ValueError('ELE 字符时间无法组成有效单词。')
    return result


def normalize_token(text):
    text = unicodedata.normalize('NFC', text).casefold()
    letters = ''.join(c for c in text if c.isalnum())
    return letters or text


def matching_words(words, text):
    """Match one exact contiguous token span; preserve the user's spelling."""
    tokens = re.findall(r'\S+', str(text or ''))
    if not tokens: return [dict(w) for w in words]
    source = [normalize_token(w['text']) for w in words]
    wanted = [normalize_token(t) for t in tokens]
    matches = [i for i in range(len(source)-len(wanted)+1) if source[i:i+len(wanted)] == wanted]
    if len(matches) == 1:
        return [dict(word, text=token) for word, token in zip(words[matches[0]:matches[0]+len(tokens)], tokens)]
    if not matches:
        result = _punctuation_words(words, text)
        if result is not None:
            return result
    raise ValueError('逐词记录与当前文案不唯一匹配，未借用其他文案的时间戳。')


def _punctuation_words(words, text):
    """Allow missing spaces after punctuation, never invent word times.

    The audio-bound records can split ``bolesť.Po`` into two timed words.
    Only a unique complete source-word span is accepted. Splits must already
    exist in the service's words and be separated by punctuation in the text;
    ``therapist`` must not match ``the rapist`` or a substring inside a word.
    """
    text = unicodedata.normalize('NFC', str(text))
    letters, positions = [], []
    for token in re.finditer(r'\S+',text):
        punctuation_only = not any(c.isalnum() for c in token.group())
        for index in range(token.start(),token.end()):
            for folded in text[index].casefold():
                if folded.isalnum() or punctuation_only:
                    letters.append(folded); positions.append(index)
    wanted = ''.join(letters)
    source = [normalize_token(w['text']) for w in words]
    if not wanted or not all(source):
        return None
    offsets = [0]
    for token in source: offsets.append(offsets[-1]+len(token))
    boundaries = set(offsets)
    combined = ''.join(source)
    candidates, cursor = [], 0
    while (start := combined.find(wanted, cursor)) >= 0:
        end = start+len(wanted)
        if start in boundaries and end in boundaries: candidates.append((start,end))
        cursor = start+1
    if len(candidates) != 1:
        return None
    start,end = candidates[0]
    first,last = offsets.index(start),offsets.index(end)
    supplied = {offset-start for offset in offsets[first:last+1]}
    requested, count = {0}, 0
    for token in re.findall(r'\S+', text):
        key = normalize_token(token)
        if not key: return None
        count += len(key); requested.add(count)
    if not requested <= supplied:
        return None  # Splitting one timed source word would require guessing.
    for boundary in supplied-requested:
        gap = text[positions[boundary-1]+1:positions[boundary]]
        if (not any(unicodedata.category(c).startswith('P') for c in gap)
                or any(not c.isspace() and not unicodedata.category(c).startswith('P') for c in gap)):
            return None
    result, cursor = [], 0
    for index in range(first,last):
        boundary = offsets[index+1]-start
        stop = positions[boundary] if boundary < len(positions) else len(text)
        token = text[cursor:stop].strip()
        if len(re.findall(r'\S+',token)) != 1: return None
        result.append(dict(words[index], text=token))
        cursor = stop
    return result


def fingerprint(path, cancel=None):
    path = Path(path); before = path.stat(); digest = hashlib.sha256()
    with data_file(path).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            if cancel and cancel.is_set(): raise InterruptedError('已取消音频校验。')
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('音频在校验期间发生变化，未保存或使用旧时间戳。')
    return {'sha256':digest.hexdigest(), 'size':after.st_size}


def artifact_files(audio):
    audio = Path(audio)
    return sorted((p for p in audio.parent.iterdir() if p.name.startswith(audio.stem+'[动态')
        and p.suffix == '.json' and p.is_file()), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:200]


def load_timings(audio, text, duration=None, cancel=None, *, report=None):
    files = artifact_files(audio)
    if not files: return None
    stamp = fingerprint(audio, cancel)
    rejected = None
    for file in files:
        try:
            if file.stat().st_size > 2*1024*1024: continue
            value = data_file(file).read_json()
            if not isinstance(value,dict): continue
            if value.get('schema') != 1 or value.get('audio') != stamp: continue
            srt = file.with_suffix('.srt')
            if (not srt.is_file() or srt.stat().st_size > 2*1024*1024
                    or hashlib.sha256(data_file(srt).read_bytes()).hexdigest() != value.get('srt_sha256')):
                continue
            words = validate_words(value['words'], value['duration'] if duration is None else duration)
            return matching_words(words, text)
        except (OSError, ValueError, TypeError, KeyError) as error:
            rejected = str(error)
            continue
    if report:
        report('动态字幕：发现 ELE 动态记录，但未通过音频／字幕校验或文案唯一匹配，未使用它。' +
               ('原因：'+rejected[:220] if rejected else '请确认该记录与当前人声属于同一次生成。'))
    return None


def to_srt(words):
    def clock(ms):
        h, r = divmod(ms,3600000); m, r = divmod(r,60000); s, ms = divmod(r,1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    chunks = []
    for i, word in enumerate(words,1):
        start = round(word['start']*1000); end = max(start+1,round(word['end']*1000))
        chunks.append(f"{i}\n{clock(start)} --> {clock(end)}\n{word['text']}\n")
    return '\n'.join(chunks)


def save_timings(audio, words, duration, *, source, cancel=None, expected=None):
    audio = Path(audio); words = validate_words(words,duration)
    stamp = fingerprint(audio,cancel)
    if expected is not None and stamp != expected: raise ValueError('对齐期间音频已改变，未保存旧结果。')
    raw = to_srt(words).encode('utf8')
    with _WRITE_LOCK:
        for number in range(1,1001):
            label = '动态' if number == 1 else '动态'+str(number)
            srt = audio.with_name(audio.stem+'['+label+'].srt'); metadata = srt.with_suffix('.json')
            if not srt.exists() and not metadata.exists(): break
        else: raise ValueError('该音频已有1000份动态字幕，请先整理；未覆盖任何文件。')
        # Metadata is the commit marker. An incomplete pair is never consumed.
        data_file(srt).write_bytes(raw)
        data_file(metadata).write_json({'schema':1,'audio':stamp,'duration':duration,'source':source,
            'srt_sha256':hashlib.sha256(raw).hexdigest(),'words':words},indent=2)
    return srt


@dataclass
class TimedAudio:
    chunks: object
    words: object = None
    warning: str = ''
    source: str = 'elevenlabs_tts'

    def __iter__(self): return iter(self.chunks)

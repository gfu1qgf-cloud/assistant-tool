"""Per-material A–B loops; native concat metadata, never a media cache."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import tempfile

KEY = 'media_loop_regions'


def identity(path):
    # Pure path comparison: normalization of a 782-item plan must not stat all
    # pool members. The selected source is checked only when actually used.
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def normalize(rules):
    if not isinstance(rules, list) or len(rules) > 1000:
        raise ValueError('循环区间记录无效（最多1000个素材）。')
    seen = set(); result = []
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('path'), str) or not rule['path'].strip():
            raise ValueError('循环区间的素材路径无效。')
        if any(c in rule['path'] for c in '\x00\r\n'): raise ValueError('循环素材路径包含无效字符。')
        token = identity(rule['path'])
        if token in seen: raise ValueError('同一素材存在重复循环区间。')
        seen.add(token)
        for key in ('start', 'end', 'duration'):
            value = rule.get(key)
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 86400:
                raise ValueError('循环区间时间必须是有效秒数。')
        if not 0 <= rule['start'] < rule['end'] <= rule['duration'] + 1e-6:
            raise ValueError('循环起点必须小于终点，且不能超出素材时长。')
        for key in ('size', 'mtime_ns'):
            if type(rule.get(key)) is not int or rule[key] < 0: raise ValueError('循环素材校验信息无效。')
        result.append({key: rule[key] for key in ('path', 'start', 'end', 'duration', 'size', 'mtime_ns')})
    return result


def stamp(path):
    value = Path(path).stat()
    return dict(size=value.st_size, mtime_ns=value.st_mtime_ns)


def original_path(layer):
    return layer.get('_matting_binding', {}).get('source', {}).get('path') or layer.get('path', '')


def selected(layer):
    if not layer.get('media_loop', True): return None
    path = original_path(layer)
    if not path: return None
    token = identity(path)
    return next((rule for rule in layer.get(KEY, []) if identity(rule['path']) == token), None)


def validate(rule, info, path=None):
    if stamp(rule['path']) != {key: rule[key] for key in ('size', 'mtime_ns')}:
        raise ValueError('素材已改变，请重新确认循环区间：'+Path(rule['path']).name)
    # A saved transparent result must retain the original source clock.
    if rule['end'] > info['duration'] + 1e-5:
        raise ValueError('循环终点超出当前视频时长，请重新设置循环区间。')
    if rule['end']-rule['start'] < 1/info['fps'] - 1e-6:
        raise ValueError('循环区间至少需要一帧。')
    return rule


class PlaylistInput(str):
    """String remains the original local path for graph/input validation.

    Inputs expands it into a tiny, private, per-encoder-attempt ffconcat file.
    Frames stay inside FFmpeg, including ProRes/QTRLE/WebM alpha channels.
    """
    def __new__(cls, path, rule, duration, start_time=0.):
        value = super().__new__(cls, path)
        value.rule, value.duration, value.start_time = dict(rule), duration, start_time
        length = rule['end']-rule['start']
        value.repeats = max(0, math.ceil(max(0., duration-rule['end'])/length))+1
        if value.repeats > 20000: raise ValueError('循环区间过短，请加长区间或缩短成品时长。')
        return value

    def __deepcopy__(self, memo): return self


@contextmanager
def playlist(spec):
    if stamp(spec.rule['path']) != {key: spec.rule[key] for key in ('size', 'mtime_ns')}:
        raise ValueError('循环素材已改变，请重新设置区间。')
    path = Path(str(spec)).resolve().as_posix().replace("'", "'\\''")
    if any(c in path for c in '\x00\r\n'): raise ValueError('循环素材路径包含无效字符。')
    segments = [(0., spec.rule['end'])] + [(spec.rule['start'], spec.rule['end'])]*spec.repeats
    content = 'ffconcat version 1.0\n'
    for a, b in segments:
        content += (f"file '{path}'\ninpoint {a+spec.start_time:.9f}\n"
                    f"outpoint {b+spec.start_time:.9f}\nduration {b-a:.9f}\n")
    with tempfile.TemporaryDirectory(prefix='visual-loop-') as directory:
        script = Path(directory)/'loop.ffconcat'
        script.write_text(content, encoding='utf-8')
        yield ['-f', 'concat', '-safe', '0', '-segment_time_metadata', '1', '-i', str(script)]


def sources(layer):
    """Only this visual control's sources; no global background pool lookup."""
    from .visual_media import is_animated
    kind = layer.get('source', 'fixed')
    entries = layer.get('sequence_items', []) if kind in {'pool', 'sequence'} else []
    paths = [entry.get('path', '') if isinstance(entry, dict) else str(entry) for entry in entries]
    if not paths: paths = [layer.get('source_file') if kind == 'file' else original_path(layer)]
    result, seen = [], set()
    for path in paths:
        if path and is_animated(path) and identity(path) not in seen:
            seen.add(identity(path)); result.append(path)
    return result

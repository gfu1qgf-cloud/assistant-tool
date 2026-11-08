"""Project-owned background ranges, original paths and source files intact."""
import math
from .visual_loop_region import identity, normalize as normalize_rules, validate, PlaylistInput

KEY = 'background_trims'


def normalized(settings):
    try: return normalize_rules(settings.get(KEY, []))
    except ValueError as error: raise ValueError(str(error).replace('循环', '裁切')) from error


def lookup(settings):
    return {identity(rule['path']): rule for rule in settings.get(KEY, [])}


def selected(settings, path):
    if not path: return None
    return lookup(settings).get(identity(path))


def apply(media, path, settings):
    rule = selected(settings, path)
    if rule is None: return media
    from .matting_video import describe
    try: validate(rule, describe(media))
    except ValueError as error: raise ValueError(str(error).replace('循环', '裁切')) from error
    return dict(media, duration=rule['end']-rule['start'], _background_trim=dict(rule))


class TrimInput(PlaylistInput):
    def __new__(cls, path, rule, duration, start_time=0.):
        # Override the first interval too; unlike a repeat-region control, a
        # background trim excludes the discarded beginning on its first pass.
        value = str.__new__(cls, path)
        value.rule, value.duration, value.start_time = dict(rule), duration, start_time
        value.first_start = rule['start']
        value.clock_anchor = rule['start'] > 0
        value.repeats = max(0, math.ceil(duration/(rule['end']-rule['start'])-1e-9)-1)
        if value.repeats > 20000: raise ValueError('裁切区间过短，请加长区间或缩短成品时长。')
        return value


def input_for(path, media, duration):
    rule = media.get('_background_trim')
    return TrimInput(path, rule, duration, float(media['video'].get('start_time') or 0)) if rule else path


def prefix(settings):
    rule = settings.get('_background_trim')
    if not rule: return ''
    anchor = 'trim=start=0.000001,' if rule['start'] > 0 else ''
    return 'select=concatdec_select,'+anchor+'setpts=PTS-STARTPTS,'


def display(path, name, rules):
    rule = rules.get(identity(path))
    if not rule: return name, path
    interval = f"{rule['start']:.3f}–{rule['end']:.3f} 秒"
    return '✂ 已裁切 · '+name, path+'\n裁切：'+interval+'（当前项目的背景使用此区间）'

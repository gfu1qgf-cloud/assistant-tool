"""Project-owned narration effects; dry speech timing is always preserved."""
import math

VERSION = 'voice-space-v1'
DEFAULTS = dict(voice_fx_preset='none', voice_fx_amount=100., voice_fx_decay=0.,
                voice_fx_predelay=-1, voice_fx_width=100.)
PRESETS = {
    'none': dict(name='关闭 · 原声', note='保留原来的人声。'),
    'holy_light': dict(name='圣光 · 轻柔神圣', rt=1.2, delay=25, wet=.20, tone=7000, width=.65,
        seed=141, note='轻柔、明亮，空间感适中，适合日常祷告和清楚的旁白。'),
    'chapel': dict(name='小礼拜堂 · 温暖祷告', rt=1.9, delay=32, wet=.28, tone=4300, width=.75,
        seed=271, note='温暖的小教堂，尾音柔和，适合亲近、安静的祈祷。'),
    'church': dict(name='教堂 · 庄严朗读', rt=3.1, delay=48, wet=.34, tone=5200, width=.90,
        seed=389, note='中大型教堂的庄严空间感，适合经文、祝福和正式朗读。'),
    'cathedral': dict(name='大教堂 · 宏大圣咏', rt=5.2, delay=70, wet=.44, tone=5800, width=1.,
        seed=521, note='宽阔、绵长的大教堂尾音，适合慢速、留白较多的圣咏或宣告。'),
    'heaven': dict(name='天穹 · 空灵长尾', rt=6.3, delay=85, wet=.48, tone=8500, width=1.,
        seed=653, note='明亮、飘逸的长尾音，适合梦幻、神圣氛围；快速长句可降低强度。'),
    'oracle': dict(name='神谕 · 层叠回声', rt=2.8, delay=60, wet=.34, tone=5600, width=.95,
        seed=787, echoes=((.26, .30), (.52, .17), (.78, .09)),
        note='在空间尾音中加入可辨认的重复回声，适合强调句、神谕和短促宣告。'),
    'warm_hall': dict(name='殿堂 · 温厚讲述', rt=2.4, delay=38, wet=.30, tone=3200, width=.80,
        seed=919, note='偏温厚、少刺耳高频的殿堂感，适合沉稳、低声的讲述。'),
}


def normalized(settings):
    values = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    if not isinstance(values['voice_fx_preset'], str) or values['voice_fx_preset'] not in PRESETS:
        raise ValueError('人声特效预设无效。')
    for key, low, high in (('voice_fx_amount', 0, 150), ('voice_fx_decay', 0, 8),
                           ('voice_fx_predelay', -1, 200), ('voice_fx_width', 0, 100)):
        value = values[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('人声特效参数超出范围：'+key)
    if type(values['voice_fx_predelay']) is not int: raise ValueError('人声尾音间隔须为整数毫秒。')
    return values


def active(settings):
    p = normalized(settings)
    return p['voice_fx_preset'] != 'none' and p['voice_fx_amount'] > 0


def resolved(settings):
    p = normalized(settings)
    if not active(p): return None
    preset = dict(PRESETS[p['voice_fx_preset']])
    preset.update(rt=p['voice_fx_decay'] or preset['rt'],
                  delay=preset['delay'] if p['voice_fx_predelay'] == -1 else p['voice_fx_predelay'],
                  wet=preset['wet']*p['voice_fx_amount']/100,
                  width=preset['width']*p['voice_fx_width']/100)
    return preset


def append_effect(graph, settings, ir_index, duration, *, sidechain=False):
    p = resolved(settings)
    if p is None: return '[voice]'
    if ir_index is None: raise ValueError('人声空间特效缺少空间响应，请从文案视频重新预览／导出。')
    if sidechain:
        # AFIR pulls a different block cadence. Equal full blocks on the dry
        # detector and music prevent sidechaincompress dropping the final
        # partial music block under different filter scheduling. Extra zero
        # samples are removed by the final duration trim.
        graph.append('[voice]asetnsamples=n=1024:p=1[voice_fx_clock]')
    split = '[voice_fx_clock]asplit=3[voice_fx_dry][voice_fx_send][voice_side]' if sidechain else '[voice]asplit=2[voice_fx_dry][voice_fx_send]'
    graph.append(split)
    # afir's dry/wet mean input/output gains, NOT a dry/wet blend. Keep an
    # explicit, unchanged dry branch to avoid delaying the narrated words.
    graph.append(f'[{ir_index}:a]aresample=48000,aformat=channel_layouts=stereo[voice_fx_ir]')
    fade = min(.15, duration/4)
    graph.append(f'[voice_fx_send][voice_fx_ir]afir=dry=1:wet=1:irnorm=-1:irfmt=input:'
                 f'minp=256:maxp=4096,afade=t=out:st={duration-fade:.9f}:d={fade:.9f}[voice_fx_wet]')
    graph.append(f"[voice_fx_dry][voice_fx_wet]amix=inputs=2:weights='1 {p['wet']:.9f}':"
                 'duration=longest:normalize=0[voice_fx_mix]')
    return '[voice_fx_mix]'

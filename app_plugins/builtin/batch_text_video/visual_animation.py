"""Validated image motion, compiled to FFmpeg (no Python per-frame rendering)."""
import math

DEFAULTS = {
    "visual_animation": "none", "animation_strength": 8., "animation_period": 2.,
    "animation_start_delay": 0., "animation_end_early": 0.,
}
CHOICES = (("无动画", "none"), ("律动 · 节奏缩放", "pulse"), ("呼吸 · 平滑缩放", "breathe"),
           ("轻浮动 · 上下", "float"), ("缓慢推近", "zoom_in"), ("缓慢拉远", "zoom_out"))


def normalize_animation(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    if layer["visual_animation"] not in {value for _, value in CHOICES}:
        raise ValueError("图片动画类型无效。")
    for key, low, high in (("animation_strength", 0, 100), ("animation_period", .2, 120),
                           ("animation_start_delay", 0, 86400), ("animation_end_early", 0, 86400)):
        value = layer[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("图片动画参数超出范围："+key)
        layer[key] = float(value)


def timing(layer, duration):
    normalize_animation(layer)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("图片动画需要有效的视频时长。")
    start, finish = layer["animation_start_delay"], duration-layer["animation_end_early"]
    if finish-start < .001:
        raise ValueError(f"{layer.get('name', '图片')}：动画开始延迟＋提前结束必须小于最终视频时长（{duration:.3f} 秒）。")
    return start, finish


def motion_at(layer, duration, time):
    """Numeric counterpart of expressions, useful for predictable motion tests."""
    start, finish = timing(layer, duration)
    t = max(0., min(finish-start, time-start))
    amount, period = layer["animation_strength"]/100, layer["animation_period"]
    mode, scale, dy = layer["visual_animation"], 1., 0.
    if mode == "pulse":
        phase = (t % period)/period
        scale += amount*(math.sin(math.pi*phase/.45)**2 if phase < .45 else 0.)
    elif mode == "breathe":
        scale += amount*(1-math.cos(2*math.pi*t/period))/2
    elif mode == "float":
        dy = amount*math.sin(2*math.pi*t/period)
    elif mode in {"zoom_in", "zoom_out"}:
        progress = t/(finish-start)
        scale += amount*(progress if mode == "zoom_in" else 1-progress)
    return scale, dy


def expressions(layer, duration):
    """Only numeric validated values enter the FFmpeg expression language."""
    start, finish = timing(layer, duration)
    local = f"clip(t-{start:.9f},0,{finish-start:.9f})"
    amount, period = layer["animation_strength"]/100, layer["animation_period"]
    mode = layer["visual_animation"]
    scale, dy = "1", "0"
    if mode == "pulse":
        phase = f"mod({local},{period:.9f})"
        wave = f"if(lt({phase},{period*.45:.9f}),pow(sin(PI*{phase}/{period*.45:.9f}),2),0)"
        scale = f"1+{amount:.9f}*({wave})"
    elif mode == "breathe":
        scale = f"1+{amount:.9f}*(1-cos(2*PI*({local})/{period:.9f}))/2"
    elif mode == "float":
        dy = f"{amount:.9f}*sin(2*PI*({local})/{period:.9f})"
    elif mode in {"zoom_in", "zoom_out"}:
        progress = f"({local})/{finish-start:.9f}"
        scale = f"1+{amount:.9f}*({progress if mode == 'zoom_in' else '1-'+progress})"
    return scale, dy

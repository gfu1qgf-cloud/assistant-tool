"""A bounded show window applied to an isolated alpha plane, never the base video."""
from dataclasses import dataclass
import math


DEFAULTS = {"start": 0., "end": 0., "enter": "fade", "exit": "fade",
            "enter_duration": .4, "exit_duration": .4, "distance": 12., "clock": "auto"}
EFFECTS = frozenset({"cut", "fade", "left", "right", "up", "down", "small", "large"})
ENTER_CHOICES = (("cut", "直接出现"), ("fade", "淡入"), ("left", "从左滑入＋淡入"),
                 ("right", "从右滑入＋淡入"), ("up", "从上滑入＋淡入"),
                 ("down", "从下滑入＋淡入"), ("small", "由小放大＋淡入"),
                 ("large", "由大缩小＋淡入"))
EXIT_CHOICES = (("cut", "直接消失"), ("fade", "淡出"), ("left", "向左滑出＋淡出"),
                ("right", "向右滑出＋淡出"), ("up", "向上滑出＋淡出"),
                ("down", "向下滑出＋淡出"), ("small", "缩小＋淡出"), ("large", "放大＋淡出"))


def normalize_timing(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("显示时段设置格式无效。")
    result = {**DEFAULTS, **value}
    for key, maximum in (("start", 86400), ("end", 86400), ("enter_duration", 60),
                         ("exit_duration", 60), ("distance", 80)):
        number = result[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= maximum:
            raise ValueError("显示时段的时间／运动幅度必须是范围内的有限数值。")
        result[key] = float(number)
    if result["end"] and result["end"] <= result["start"]:
        raise ValueError("显示时段的结束秒必须大于开始秒；0表示直到视频结束。")
    if result["enter"] not in EFFECTS or result["exit"] not in EFFECTS:
        raise ValueError("显示时段的出入场方式无效。")
    if result["clock"] not in {"auto", "restart", "global"}:
        raise ValueError("显示时段的素材时钟无效。")
    return result


@dataclass(frozen=True)
class ShowWindow:
    start: float
    end: float
    enter: str
    exit: str
    enter_duration: float
    exit_duration: float
    distance: float
    clock: str

    def source_duration(self, video_duration):
        return self.end - self.start if self.clock == "restart" else video_duration


def resolve_window(value, duration, *, automatic_clock="restart", report=None):
    settings = normalize_timing(value)
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError("显示时段缺少有效的视频时长。")
    end = min(settings["end"] or duration, duration)
    start = settings["start"]
    if start >= end:
        raise ValueError(f"显示开始为{start:g}秒，但视频只有{duration:g}秒，没有可显示的时段。")
    if settings["end"] > duration and report:
        report(f"显示时段：结束超过视频长度，按视频末尾{duration:g}秒结束。", None)
    enter = 0. if settings["enter"] == "cut" else settings["enter_duration"]
    exit = 0. if settings["exit"] == "cut" else settings["exit_duration"]
    span = end - start
    if enter + exit > span:
        # Keep a short fully-visible hold even when the caller supplied a tiny window.
        factor = span * .9 / (enter + exit)
        enter, exit = enter * factor, exit * factor
        if report:
            report("显示时段太短，已按比例缩短出入场动画，保留完整显示阶段。", None)
    clock = automatic_clock if settings["clock"] == "auto" else settings["clock"]
    if clock not in {"restart", "global"}:
        raise ValueError("显示时段的默认素材时钟无效。")
    return ShowWindow(start, end, settings["enter"], settings["exit"], enter, exit,
                      settings["distance"] / 100, clock)


def add_timed_plane(filters, plane, label, width, height, duration, fps, window, *, center=None):
    """Input is a full-canvas RGBA layer on its source clock; output is global RGBA.

    Entrance/exit affect only this plane. The full-canvas output continues before
    and after it, so disabling/fading an element cannot blacken or trim the video.
    Animation expressions are compiled from enums and checked numbers, not user code.
    """
    if not isinstance(window, ShowWindow):
        raise TypeError("出入场需要已解析的显示时段。")
    cx, cy = center or (width / 2, height / 2)
    if not all(math.isfinite(n) for n in (cx, cy)):
        raise ValueError("出入场中心坐标无效。")
    effect = [f"setpts=PTS-STARTPTS+{window.start if window.clock == 'restart' else 0:.9f}/TB",
              "format=rgba"]
    if window.enter_duration:
        effect.append(f"fade=t=in:st={window.start:.9f}:d={window.enter_duration:.9f}:alpha=1")
    if window.exit_duration:
        effect.append(f"fade=t=out:st={window.end-window.exit_duration:.9f}:d={window.exit_duration:.9f}:alpha=1")
    # Cubic ease-out for entering; ease-in for exiting. Fade itself stays linear.
    incoming = (f"pow(1-clip((t-{window.start:.9f})/{window.enter_duration:.9f},0,1),3)"
                if window.enter_duration else "0")
    outgoing = (f"pow(clip((t-{window.end-window.exit_duration:.9f})/{window.exit_duration:.9f},0,1),3)"
                if window.exit_duration else "0")
    x, y, scale = [], [], []
    for mode, progress in ((window.enter, incoming), (window.exit, outgoing)):
        if mode in {"left", "right"}:
            sign = -1 if mode == "left" else 1
            x.append(f"{sign*width*window.distance:.9f}*({progress})")
        elif mode in {"up", "down"}:
            sign = -1 if mode == "up" else 1
            y.append(f"{sign*height*window.distance:.9f}*({progress})")
        elif mode in {"small", "large"}:
            sign = -1 if mode == "small" else 1
            scale.append(f"{sign*window.distance:.9f}*({progress})")
    if scale and window.distance:
        factor = "1+" + "+".join(scale)
        effect.append(f"scale=w='max(2,round({width}*({factor})/2)*2)':"
                      f"h='max(2,round({height}*({factor})/2)*2)':eval=frame")
    filters.append(f"[{plane}]" + ",".join(effect) + f"[{label}_moving]")
    filters.append(f"color=c=black@0:s={width}x{height}:r={float(fps):.9f}:d={duration:.9f},"
                   f"format=rgba[{label}_blank]")
    # Compensate scaling around the object's center, not the video corner.
    xx = f"{cx:.9f}*(1-overlay_w/{width})" + ("+" + "+".join(x) if x else "")
    yy = f"{cy:.9f}*(1-overlay_h/{height})" + ("+" + "+".join(y) if y else "")
    enabled = f"gte(t,{window.start:.9f})*lt(t,{window.end:.9f})"
    filters.append(f"[{label}_blank][{label}_moving]overlay=x='{xx}':y='{yy}':eval=frame:"
                   f"enable='{enabled}':format=auto:eof_action=pass:repeatlast=0[{label}]")
    return label

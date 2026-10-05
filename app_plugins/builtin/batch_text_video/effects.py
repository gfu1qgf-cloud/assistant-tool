"""Background-only effects shared by still previews and video exports."""
import math


def blur_strength(settings):
    value = settings.get("background_blur", 0)
    if isinstance(value, bool):
        raise ValueError("背景模糊必须是0–40的数值。")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("背景模糊必须是0–40的数值。") from None
    if not math.isfinite(value) or not 0 <= value <= 40:
        raise ValueError("背景模糊必须是0–40的数值。")
    return value


def blur_filter(settings, width):
    value = blur_strength(settings)
    return f"gblur=sigma={value*width/1080:.6f}:steps=2" if value else ""

"""Bounded size contrast, preserving the previous RNG sequence at strength 1."""
import math


def contrast_unit(value, contrast):
    if type(contrast) not in (int, float) or not math.isfinite(contrast) or not 0 <= contrast <= 4:
        raise ValueError('字幕字号对比度应为0–4倍。')
    if contrast == 1: return value
    return min(1., max(0., .5 + (value - .5) * contrast))


def size_between(rng, low, high, contrast):
    return low + (high - low) * contrast_unit(rng.random(), contrast)


def size_contrast(layer):
    """Turning randomness off must not overwrite its saved strength/seed."""
    return layer.get('caption_size_contrast', 1.) if supports_random_size(layer) and layer.get('caption_random_size', False) else 0.


def supports_random_size(layer):
    # A preset selects values, never the component's available capabilities.
    return layer.get('kind','caption') == 'caption' or bool(layer.get('_caption_style_design'))

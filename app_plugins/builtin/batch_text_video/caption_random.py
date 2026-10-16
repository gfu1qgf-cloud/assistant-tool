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
    if layer.get('caption_preset') == 'pop':
        return True
    from .caption_styles import is_catalog, effects
    if not is_catalog(layer): return False
    style=effects(layer)
    return style['anim_in_type'] in {'word_pop_random','word_pop_random_pulse','scatter_pop','word_random_position'} or style['block_typography_enabled']

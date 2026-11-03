"""One master switch for every visual matte path; stored options stay intact."""
import os


def enabled(layer):
    return layer.get('matting_enabled', True)


def original(layer):
    """Return a render-only copy, never mutate the user's saved refinements."""
    if enabled(layer): return layer
    result = dict(layer)
    binding = result.pop('_matting_binding', None)
    if binding:
        # A previously resolved saved cutout may already have replaced path.
        # Restore only that binding's path, not an independently retargeted pool.
        source = binding.get('source', {}).get('path', '')
        saved = binding.get('result', {}).get('path', source)
        token = lambda p: os.path.normcase(os.path.abspath(p)) if p else ''
        if source and token(result.get('path','')) in {token(source), token(saved)}:
            result.update(path=source, thumbnail='')
    for key in ('_matting_snapshot', '_matting_post_options'): result.pop(key, None)
    result.update(key_mode='none', key_method='normal', use_saved_matte=False,
                  key_despill=False, key_cleanup=False, key_matte_grow=0.,
                  key_alpha_low=0., key_alpha_high=100., key_mask_path='')
    return result

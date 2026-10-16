"""GUI preparation returns only a host credential-session identifier."""
def prepare_binding(settings, services):
    needed = any(layer.get('kind') == 'caption' and layer.get('enabled', True) and layer.get('opacity', 100)
        and layer.get('caption_timing') == 'elevenlabs' for layer in settings.get('layers', []))
    if not needed: return None
    prepare = getattr(services, 'prepare_elevenlabs', None)
    if not callable(prepare): raise ValueError('ELE 云端对齐需要从主程序打开插件。')
    return prepare()

"""One in-memory migration path for an editor or a lazily accessed project."""
import copy


def normalize_project(state, defaults):
    if not isinstance(state, dict) or not isinstance(state.get('jobs'), list) or not isinstance(state.get('music'), list):
        raise ValueError('项目任务或音乐记录格式异常，未覆盖原记录。')
    settings = state.get('settings', {})
    if not isinstance(settings, dict):
        raise ValueError('项目设置格式异常，未覆盖原记录。')
    # Migrate renamed settings before defaults would hide the old values.
    from .image_background import normalized as normalize_background
    if 'background_visual' not in settings and 'image_background_motion' in settings:
        settings.update(normalize_background(settings))
    settings = state['settings'] = {**copy.deepcopy(defaults), **settings}
    if 'color_rules' in settings:
        from .palette_rules import normalize
        settings['color_rules'] = normalize(settings['color_rules'])
    from .export_destination import normalized as normalize_destination
    from .canvas_settings import normalized as normalize_canvas
    from .voice_effects import normalized as normalize_voice
    from .adaptation import normalize_duration
    from model.MediaEncoding import normalize_export_settings
    from .effects import blur_strength
    from .layers import normalize_layers
    for normalizer in (normalize_destination, normalize_canvas, normalize_voice, normalize_background):
        settings.update(normalizer(settings))
    normalize_duration(settings)
    normalize_export_settings(settings)
    settings['background_blur'] = blur_strength(settings)
    settings['layers'] = normalize_layers(settings.get('layers'))
    state.setdefault('backgrounds', [])
    from .pools import migrate_pools
    migrate_pools(state)
    from .task_render_config import normalize as normalize_configs
    normalize_configs(state)
    return state


def profile_data(state, identifier, defaults):
    """Read only this project's own fields; never inherit another project's pools."""
    profile = next((p for p in state['profiles'] if p['id'] == identifier), None)
    if profile is None:
        raise ValueError('项目已不存在，请重新选择。')
    from .pools import RESOURCE_KEYS, snapshot
    if identifier == state['active_profile']:
        data = {key: copy.deepcopy(state[key]) for key in RESOURCE_KEYS if key in state}
    else:
        if not isinstance(profile.get('data'), dict):
            raise ValueError('所选配置尚无保存内容。')
        data = copy.deepcopy(profile['data'])
    normalize_project(data, defaults)
    data['settings'].update(copy.deepcopy(state['render_settings']))
    return snapshot(data)

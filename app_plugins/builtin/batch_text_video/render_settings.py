"""One encoding policy shared by all saved projects in this plugin."""
import copy

KEYS = ('video_encoder', 'encoder_fallback', 'export_workers', 'export_quality', 'ffmpeg_path', 'overwrite', 'keep_media_cache')


def normalized(values, defaults):
    from model.MediaEncoding import normalize_export_settings
    result = {key: copy.deepcopy(values.get(key, defaults[key])) for key in KEYS}
    normalize_export_settings(result)
    if (not isinstance(result['ffmpeg_path'], str) or type(result['overwrite']) is not bool
            or type(result['keep_media_cache']) is not bool):
        raise ValueError('全局渲染路径或覆盖选项无效。')
    result['ffmpeg_path'] = result['ffmpeg_path'].strip()
    return result


def apply_global(state, defaults):
    values = state.get('render_settings')
    if values is None:
        values = {key: copy.deepcopy(state['settings'].get(key, defaults[key])) for key in KEYS}
        values['export_workers'] = max(2, values['export_workers'])
    state['render_settings'] = normalized(values, defaults)
    state['settings'].update(copy.deepcopy(state['render_settings']))
    return state

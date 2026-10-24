"""The fixed bottom visual owns animation; the project owns its background pool."""
import copy
from qt_compat import QtWidgets
from .visual_animation import DEFAULTS, normalize_animation

IDENTIFIER = '__bottom_background__'


def control(settings):
    values = settings.get('background_visual')
    if values is None:
        values = {}
        if 'image_background_motion' in settings:  # Migrate the short-lived 1.30 project fields.
            values.update(visual_animation=settings['image_background_motion'],
                animation_strength=settings.get('image_background_strength', 8),
                animation_period=min(120, settings.get('image_background_seconds', 8)),
                animation_zoom_seconds=settings.get('image_background_seconds', 8),
                animation_direction=settings.get('image_background_direction', 'outward'))
    if not isinstance(values, dict): raise ValueError('底层视觉控件参数无效。')
    layer = dict(copy.deepcopy(values), id=IDENTIFIER, kind='background', name=values.get('name', '底层背景'),
                 enabled=True, opacity=100, x=50, y=50, width=100, height=100, path='')
    normalize_animation(layer)
    if layer['visual_animation'] == 'float': raise ValueError('底层背景不支持上下浮动，请使用缩放动画。')
    return layer


def values(layer):
    return {key: copy.deepcopy(layer[key]) for key in ('name', *DEFAULTS)}


def background_job(job, state):
    """An explicitly chosen global pool overrides row backgrounds, by queue position."""
    identifier = state['settings'].get('background_pool_id', '')
    if not identifier: return dict(job)
    if identifier == 'none': return dict(job, path='')
    if identifier == 'builtin:background':
        paths = state.get('backgrounds', [])
    else:
        from .custom_pools import get_pool
        pool = get_pool(state, identifier)
        if pool['kind'] not in {'image', 'video', 'visual'}: raise ValueError('背景池必须是图片、视频或视觉池。')
        paths = [item['path'] for item in pool['items']]
    if not paths: return dict(job, path='')
    index = next((index for index, item in enumerate(state.get('jobs', [])) if item.get('id') == job.get('id')), 0)
    index = job.get('_background_pool_position', index)
    if type(index) is not int or not 0 <= index <= 1_000_000: raise ValueError('背景池取用位置无效。')
    # Quick export may append a row after previewing a copy. Keep the visible
    # pool position when its queue ID/position is assigned later.
    return dict(job, path=paths[index % len(paths)], _background_pool_position=index)


def background_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    from .visual_media import VisualProperties
    panel = ComponentPropertyPanel(mode=mode)
    source = QtWidgets.QLabel('来源：项目设置 → 背景池（全项目共用）')
    source.setWordWrap(True); panel.body.addWidget(source)
    visual = VisualProperties()
    visual.basic_form.parentWidget().hide()
    visual.sections.sections['visual'].hide()
    visual.sections.sections['appearance'].hide()
    visual.sections.sections['animation'].set_expanded(True)
    mode_control = visual.controls['visual_animation']
    mode_control.removeItem(mode_control.findData('float'))
    panel.visual_properties = panel.add_bound('visual', '', visual, fields=set(DEFAULTS), section=False)
    panel.sections['animation'] = visual.sections.sections['animation']
    return panel

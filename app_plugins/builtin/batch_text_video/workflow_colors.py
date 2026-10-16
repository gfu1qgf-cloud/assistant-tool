"""Typed color overrides; use the existing typography renderer and opacity fields."""
from model.node_workflow.contracts import ColorValue, NodePort

COLOR_FIELDS = (('color', '文字颜色'), ('outline_color', '描边颜色'),
                ('shadow_color', '阴影颜色'), ('background_color', '底色'))
COLOR_PORTS = tuple(NodePort(key, title + ' · 覆盖参数', 'color', required=False) for key, title in COLOR_FIELDS)


def apply_color_inputs(layer, inputs):
    layer = dict(layer)
    for key, _ in COLOR_FIELDS:
        value = inputs.get(key)
        if value is None:
            continue
        if not isinstance(value, ColorValue):
            raise TypeError('颜色输入需要 RGB／RGBA 颜色对象。')
        # QColor uses #AARRGGBB, unlike the public hex conversion's #RRGGBBAA.
        if key == 'color':
            layer[key] = f'#{round(value.alpha * 255):02x}' + value.hex_rgb[1:]
        else:
            layer[key] = value.hex_rgb
            layer[key.replace('_color', '_opacity')] = value.alpha * 100
        if key == 'color':
            layer['color_mode'] = 'fixed'
    return layer

"""Independent component snapshots for the OS clipboard; media stays in place."""
import copy
import json
import uuid

MIME = 'application/x-lzx-batch-video-components'
MAX_BYTES = 16*1024*1024
SINGLETONS = {'title', 'body', 'watermark'}


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if not key.startswith('_')}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return copy.deepcopy(value)


def capture(layer, state, scope):
    if not layer:
        raise ValueError('请先选择要复制的控件。')
    layer = clean(layer)
    if layer['kind'] == 'background':
        from .background_visual import control
        layer = control({'background_visual': layer})
    else:
        from .layers import normalize_layers
        from .pools import bind_pool_layers
        layer = normalize_layers([layer])[0]
        # Snapshot the selected component's actual library contents, never a
        # whole project or references to an old project's private pool IDs.
        layer = bind_pool_layers([layer], state)[0] if state else layer
    return dict(version=1, profile=state.get('active_profile', ''), scope=str(scope), layer=clean(layer))


def encode(payload):
    value = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf8')
    if len(value) > MAX_BYTES:
        raise ValueError('控件的轮换内容过大，请减少轮换项后再复制。')
    return value


def decode(value):
    if len(value) > MAX_BYTES:
        raise ValueError('剪贴板控件记录过大。')
    try:
        def invalid(_value): raise ValueError('Non-finite value')
        payload = json.loads(value, parse_constant=invalid)
        if (not isinstance(payload, dict) or payload.get('version') != 1
                or not isinstance(payload.get('profile'), str) or not isinstance(payload.get('scope'), str)
                or not isinstance(payload.get('layer'), dict)):
            raise ValueError('Invalid component snapshot')
        layer = payload['layer'] = clean(payload['layer'])
        if layer.get('kind') == 'background':
            from .background_visual import control
            payload['layer'] = control({'background_visual': layer})
        else:
            from .layers import normalize_layers
            payload['layer'] = normalize_layers([layer])[0]
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise ValueError('剪贴板里的控件记录无效，请重新复制。') from error
    return payload


def prepare(layers, background, payload, *, profile='', scope=''):
    """Validate the complete edit before a widget, saved project or file changes."""
    from .layers import normalize_layers
    from .components import walk_components
    from .background_visual import control, values
    current = normalize_layers(layers)
    clone = copy.deepcopy(payload['layer'])
    notices, replacements = [], []
    if clone['kind'] == 'background':
        new_background = values(control({'background_visual': clone}))
        return dict(layers=current, background=new_background, selected=clone['id'],
                    replacements=['底层背景'] if new_background != background else [], notices=[])
    same_project = bool(profile and profile == payload['profile'] and str(scope) == payload['scope'])
    existing = next((x for x in current if x['kind'] == clone['kind']), None) if clone['kind'] in SINGLETONS else None
    old_id = clone['id']
    clone['id'] = existing['id'] if existing else uuid.uuid4().hex
    if same_project and not existing:
        clone['name'] = clone.get('name', '控件')+' · 副本'
    for component in walk_components([clone]):
        if component.get('source') == 'pool' and not same_project:
            task_text = (component['kind'] in {'title','body','text'} and not component.get('pool_id')
                         and component.get('copy_binding', 'task') == 'task')
            if not task_text:
                component.update(source='sequence', pool_id='')
                if component['kind'] in {'title','body','text'}: component['copy_binding'] = 'rotate'
                notices.append('外部资源池已保存为控件内轮换副本')
        if component.get('source') == 'sequence':
            for item in component.get('sequence_items', []): item['id'] = uuid.uuid4().hex
        if component.get('wrap_targets'):
            targets = [clone['id'] if key == old_id else key for key in component['wrap_targets']
                       if same_project or key == old_id]
            if len(targets) != len(component['wrap_targets']):
                notices.append('未复制的其他控件不再作为环绕避让对象')
            component['wrap_targets'] = targets
            if not targets and not component.get('wrap_polygons'): component['wrap_enabled'] = False
    # Watermark parts keep all four sources/styles/positions; normalize_layers
    # maintains their private canonical IDs, independent of the outer group ID.
    if existing:
        at = current.index(existing)
        if clean(existing) != clone: replacements.append(existing['name'])
        current[at] = clone
    else:
        current.append(clone)
    result = normalize_layers(current)  # All limits checked atomically.
    return dict(layers=result, background=copy.deepcopy(background), selected=clone['id'],
                replacements=replacements, notices=list(dict.fromkeys(notices)))

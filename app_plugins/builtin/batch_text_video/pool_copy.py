"""Clipboard snapshots of pool records; never duplicate or alter media files."""
import copy
import json
from pathlib import Path
import uuid

from .custom_pools import MAX_POOL_ITEMS, POOL_TYPES, accepted_suffixes

MIME = 'application/x-lzx-batch-pool-records'
MAX_CLIPBOARD_BYTES = 16 * 1024 * 1024


def libraries(state):
    result = {
        'backgrounds': ('默认背景池', 'background', state.get('backgrounds', [])),
        'copy_pool': ('默认文案池', 'copy', state.get('copy_pool', [])),
        'image_pool': ('默认图片池', 'visual', state.get('image_pool', [])),
        'music': ('默认音乐池', 'music', state.get('music', [])),
    }
    result.update({'pool:'+p['id']: (p['name'], p['kind'], p['items'])
                   for p in state.get('resource_pools', [])})
    return result


def capture(state, key, identifiers):
    pools = libraries(state)
    if key not in pools:
        raise ValueError('请在资源池中选择素材。')
    wanted = set(identifiers)
    if not wanted:
        raise ValueError('请先选择要复制的素材。')
    records = [copy.deepcopy(item) if isinstance(item, dict) else {'id': item, 'path': item}
               for item in pools[key][2]
               if (item if key == 'backgrounds' else item['id']) in wanted]
    if {item['id'] for item in records} != wanted:
        raise ValueError('来源素材已发生变化，请重新选择。')
    paths = {item.get('path', '') for item in records}
    return dict(version=1, kind=pools[key][1], profile=state.get('active_profile', ''),
                records=records, trims=copy.deepcopy([r for r in state.get('settings', {}).get('background_trims', [])
                                                     if r['path'] in paths]))


def encode(payload):
    value = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf8')
    if len(value) > MAX_CLIPBOARD_BYTES:
        raise ValueError('所选记录过多，请分批复制。')
    return value


def decode(value):
    if len(value) > MAX_CLIPBOARD_BYTES:
        raise ValueError('剪贴板记录过大，请分批复制。')
    try:
        def require(condition):
            if not condition:
                raise ValueError('Invalid clipboard schema')
        def invalid_constant(_value):
            raise ValueError('Non-finite clipboard number')
        payload = json.loads(value, parse_constant=invalid_constant)
        require(isinstance(payload, dict) and payload.get('version') == 1)
        require(payload.get('kind') in set(POOL_TYPES) | {'background', 'copy'})
        require(isinstance(payload.get('profile'), str))
        records = payload['records']
        require(isinstance(records, list) and 0 < len(records) <= MAX_POOL_ITEMS)
        for item in records:
            require(isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'])
            if payload['kind'] == 'copy':
                require(all(isinstance(item.get(f, ''), str) for f in ('title', 'body')))
            else:
                require(isinstance(item.get('path'), str) and item['path'])
        from .background_trim import normalized
        payload['trims'] = normalized({'background_trims': payload.get('trims', [])})
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise ValueError('剪贴板里的素材记录无效，请重新复制。') from error
    return payload


def suffixes(kind):
    if kind == 'background':
        from .engine import VIDEO_SUFFIXES
        from .layers import IMAGE_SUFFIXES
        return VIDEO_SUFFIXES | IMAGE_SUFFIXES
    return accepted_suffixes(kind)


def _origin(item, profile):
    value = item.get('_pool_copy_origin', {})
    return (str(value.get('profile', profile)), str(value.get('id', item['id']))) if isinstance(value, dict) else (profile, item['id'])


def validate_target(state, target, payload):
    pool = libraries(state).get(target)
    if pool is None:
        raise ValueError('请先打开要粘贴的资源池。')
    _, kind, destination = pool
    records = payload['records']
    inline = payload['kind'] == 'copy'
    if inline != (kind == 'copy') or not inline and any(
            Path(item['path']).suffix.casefold() not in suffixes(kind) for item in records):
        raise ValueError('所选素材与当前池的类型不兼容，本次未粘贴。')
    return pool


def paste_records(state, target, payload):
    """Return an independent updated project, validating the entire paste first."""
    from .store import file_identity
    _, kind, destination = validate_target(state, target, payload)
    records = payload['records']
    inline = payload['kind'] == 'copy'
    origin = lambda item: _origin(item, payload['profile'])
    known = ({_origin(item, state.get('active_profile', '')) for item in destination} if inline else
             {file_identity(item if isinstance(item, str) else item['path']) for item in destination})
    additions, skipped = [], 0
    for item in records:
        identity = origin(item) if inline else file_identity(item['path'])
        if identity in known:
            skipped += 1
            continue
        known.add(identity)
        if target == 'backgrounds':
            clone = item['path']
        else:
            clone = copy.deepcopy(item)
            clone['id'] = uuid.uuid4().hex
            if inline:
                clone['_pool_copy_origin'] = dict(zip(('profile', 'id'), origin(item)))
            elif kind == 'music':
                clone.update(cursor=0.0, last_used='', audio_key='')
            else:
                for field in ('cursor', 'last_used', 'audio_key'):
                    clone.pop(field, None)
        additions.append(clone)
    if len(destination) + len(additions) > MAX_POOL_ITEMS:
        raise ValueError(f'目标池最多 {MAX_POOL_ITEMS} 项，本次未粘贴。')
    updated = copy.deepcopy(state)
    libraries(updated)[target][2].extend(additions)
    # Carry per-file background cuts across projects, without changing an existing cut.
    if kind in {'background', 'image', 'video', 'visual'}:
        settings = updated.setdefault('settings', {})
        rules = settings.setdefault('background_trims', [])
        existing = {file_identity(r['path']) for r in rules}
        added_paths = {file_identity(item if isinstance(item, str) else item['path']) for item in additions}
        rules.extend(copy.deepcopy(r) for r in payload.get('trims', [])
                     if file_identity(r['path']) in added_paths - existing)
        from .background_trim import normalized
        settings['background_trims'] = normalized(settings)
    from .pool_order import normalize
    normalize(updated)
    return updated, {'added': len(additions), 'skipped': skipped}


def rebind(layers, state, target):
    """Refresh only bindings to the edited pool, retaining all playheads."""
    from .pools import bind_pool_layers
    result = copy.deepcopy(layers)
    for layer in result:
        if layer['kind'] == 'watermark':
            layer['corners'] = dict(zip(layer['corners'], rebind(list(layer['corners'].values()), state, target)))
        elif layer.get('source') == 'pool':
            pool_id = layer.get('pool_id', '')
            key = 'pool:'+pool_id if pool_id else ('image_pool' if layer['kind'] in {'image', 'caption'} else 'copy_pool')
            if key == target:
                layer.update(bind_pool_layers([layer], state)[0])
    return result

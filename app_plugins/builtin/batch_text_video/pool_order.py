"""Persist one usage permutation; restoration never resurrects removed media."""
import random

BUILTINS = ('backgrounds', 'copy_pool', 'image_pool', 'music')


def libraries(state):
    return {**{key: state.get(key, []) for key in BUILTINS},
            **{'pool:'+pool['id']: pool['items'] for pool in state.get('resource_pools', [])}}


def token(item):
    return item if isinstance(item, str) else item['id']


def normalize(state):
    rules = state.get('resource_orders', {})
    if not isinstance(rules, dict):
        raise ValueError('资源池随机顺序记录无效。')
    result = {}
    for key, items in libraries(state).items():
        if key not in rules or not items:
            continue
        original = rules[key]
        if not isinstance(original, list) or any(not isinstance(i, str) for i in original):
            raise ValueError('资源池原始顺序记录无效。')
        present = {token(item) for item in items}
        seen = set()
        ordered = []
        for identifier in original + [token(item) for item in items]:
            if identifier in present and identifier not in seen:
                ordered.append(identifier)
                seen.add(identifier)
        result[key] = ordered
    state['resource_orders'] = result
    return result


def active(state, key):
    return key in state.get('resource_orders', {})


def reorder(state, key, *, restore=False, rng=None):
    """Change actual allocation order, keeping IDs, audio cursors and jobs intact."""
    pools = libraries(state)
    if key not in pools:
        return False
    items = pools[key]
    if restore:
        if not active(state, key):
            return False
        normalize(state)
        original = state['resource_orders'].pop(key, [])
        positions = {identifier: i for i, identifier in enumerate(original)}
        items.sort(key=lambda item: positions.get(token(item), len(positions)))
    else:
        if len(items) < 2:
            return False
        normalize(state)
        state['resource_orders'].setdefault(key, [token(item) for item in items])
        before = [token(item) for item in items]
        (rng or random.SystemRandom()).shuffle(items)
        if [token(item) for item in items] == before:
            items[0], items[1] = items[1], items[0]
    # This explicit order change starts new allocations at usage number one.
    # Per-track playback positions and already reserved jobs stay untouched.
    if key == 'music':
        state['next_music_id'] = token(items[0]) if items else ''
    elif key.startswith('pool:'):
        pool = next(p for p in state['resource_pools'] if p['id'] == key[5:])
        if pool['kind'] == 'music':
            pool['next_id'] = token(items[0]) if items else ''
    return True


def restart_layers(layers, key):
    """Discard only the affected binding cursor before rebinding the new list."""
    import copy
    result = copy.deepcopy(layers)
    for layer in result:
        if layer.get('kind')=='caption' and layer.get('caption_cursor_source')=='pool':
            cursor_key='pool:'+layer['caption_cursor_pool_id'] if layer.get('caption_cursor_pool_id') else 'image_pool'
            if cursor_key==key:layer.update(caption_cursor_items=[],caption_cursor_cursor=0)
        if layer.get('kind') == 'watermark':
            layer['corners'] = dict(zip(layer['corners'], restart_layers(list(layer['corners'].values()), key)))
        elif layer.get('source') == 'pool':
            pool_id = layer.get('pool_id', '')
            bound_key = 'pool:'+pool_id if pool_id else ('image_pool' if layer['kind'] in {'image', 'caption'} else 'copy_pool')
            if bound_key == key:
                layer['sequence_items'], layer['sequence_cursor'] = [], 0
    return result

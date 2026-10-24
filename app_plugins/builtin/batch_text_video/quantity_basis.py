"""A pool can determine the batch length independently of background presence."""
from .custom_pools import get_pool
from .store import file_identity


def choices(state):
    result = [('', '沿用上方搭配方式')]
    for key, label in (('copy_pool', '默认文案池'), ('backgrounds', '默认背景池'),
                       ('image_pool', '默认图片池'), ('music', '默认音乐池')):
        result.append(('builtin:'+key, f'{label}（{len(state.get(key, []))} 项）'))
    result.extend(('pool:'+pool['id'], f"{pool['name']}（{len(pool['items'])} 项）")
                  for pool in state.get('resource_pools', []))
    return result


def entries(state, identifier, copies, videos):
    if not identifier: return None
    if identifier == 'builtin:copy_pool': rows = copies
    elif identifier == 'builtin:backgrounds': rows = [dict(id=file_identity(path)) for path in videos]
    elif identifier in {'builtin:image_pool', 'builtin:music'}: rows = state.get(identifier[8:], [])
    elif identifier.startswith('pool:'): rows = get_pool(state, identifier[5:])['items']
    else: raise ValueError('数量基准池已失效，请重新选择。')
    if not rows: raise ValueError('数量基准池为空，请添加素材或选择其他基准。')
    return [(identifier, row['id']) for row in rows]

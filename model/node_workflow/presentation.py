"""Visual metadata and opt-in layout. Neither changes execution/wire order."""
import math
import re
from uuid import uuid4


def validate_annotations(graph):
    items = graph.get('annotations', [])
    if not isinstance(items, list) or len(items) > 128:
        raise ValueError('便签／分组数量无效（最多128个）。')
    nodes, ids, owned = {n['id'] for n in graph.get('nodes', [])}, set(), set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not 1 <= len(item['id']) <= 100
                or item['id'] in ids or item['id'] in nodes or item.get('kind') not in {'note', 'group'}
                or not isinstance(item.get('title'), str) or len(item['title']) > 120
                or not isinstance(item.get('text', ''), str) or len(item.get('text', '')) > 8000
                or not re.fullmatch(r'#[0-9a-fA-F]{6}', str(item.get('color', '')))
                or type(item.get('collapsed', False)) is not bool or type(item.get('move_members', False)) is not bool):
            raise ValueError('便签／分组数据无效。')
        ids.add(item['id'])
        for key in ('position', 'size'):
            values = item.get(key)
            if (not isinstance(values, list) or len(values) != 2 or any(type(v) not in (int, float)
                    or not math.isfinite(v) or abs(v) > 100000 for v in values)
                    or key == 'size' and any(v < 80 for v in values)):
                raise ValueError('便签／分组位置或尺寸无效。')
        members = item.get('members', [])
        if not isinstance(members, list) or any(value not in nodes for value in members) or len(set(members)) != len(members):
            raise ValueError('注释分组引用了不存在的节点。')
        if item['kind'] == 'note' and members or owned.intersection(members):
            raise ValueError('同一个节点不能同时属于多个注释分组，避免整组移动重复。')
        owned.update(members)


def annotation(kind, position, *, members=(), size=(300, 200)):
    return dict(id=uuid4().hex, kind=kind, title='注释分组' if kind == 'group' else '便签', text='',
                color='#638eb1' if kind == 'group' else '#be9b53', position=list(position), size=list(size),
                members=list(members), collapsed=False, move_members=bool(members))


def arrange(graph, sizes, selected=None, orientation='horizontal'):
    """Layer by dependencies; change only requested positions, deterministic order."""
    nodes = {node['id']: node for node in graph['nodes']}
    selected = set(nodes) if selected is None else set(selected).intersection(nodes)
    if not selected:
        return False
    incoming = {key: set() for key in selected}
    for a, _, b, _ in graph['connections']:
        if a in selected and b in selected:
            incoming[b].add(a)
    levels, pending = {}, set(selected)
    while pending:
        ready = [key for key in nodes if key in pending and incoming[key] <= levels.keys()]
        if not ready:
            raise ValueError('节点连接存在环路，无法自动整理；原位置未修改。')
        for key in ready:
            levels[key] = max((levels[p] + 1 for p in incoming[key]), default=0)
            pending.remove(key)
    origin = [min(nodes[key].get('position', [0, 0])[axis] for key in selected) for axis in (0, 1)]
    primary, secondary = (0, 1) if orientation == 'horizontal' else (1, 0)
    offset, positions = 0, {}
    for level in range(max(levels.values()) + 1):
        group = [key for key in nodes if levels.get(key) == level]
        cursor = 0
        for key in group:
            point = list(origin); point[primary] += offset; point[secondary] += cursor
            positions[key] = point
            cursor += sizes[key][secondary] + 80
        offset += max(sizes[key][primary] for key in group) + 120
    for key, point in positions.items():
        nodes[key]['position'] = point
    return True

"""Presentation-only loop membership. Never controls execution or node order."""
from dataclasses import dataclass
import re

from .loops import BEGIN, paired_blocks

PALETTE = ('#ce9bed', '#78cbb0', '#efb477', '#7fb9ed', '#e998b5', '#c4d17e')


@dataclass(frozen=True)
class LoopRegion:
    begin: str
    end: str
    members: frozenset
    depth: int
    color: str
    title: str


def loop_regions(graph):
    nodes = {node['id']: node for node in graph['nodes']}
    forward, backward = {}, {}
    for source, _output, target, _input in graph['connections']:
        forward.setdefault(source, set()).add(target)
        backward.setdefault(target, set()).add(source)

    def reachable(start, links):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(links.get(node, ()))
        return seen

    pairs, _ends, errors = paired_blocks(graph)
    invalid = {node_id for node_id, _error in errors}
    blocks = []
    for end, begin in pairs.items():
        if end in invalid:
            continue
        members = reachable(begin, forward) & reachable(end, backward)
        blocks.append((begin, end, frozenset(members)))
    result = []
    for index, (begin, end, members) in enumerate(blocks):
        depth = sum(begin in other and end in other and members < other
                    for _first, _last, other in blocks)
        appearance = nodes[begin].get('appearance', {})
        color = appearance.get('loop_color') if isinstance(appearance, dict) else None
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            color = PALETTE[index % len(PALETTE)]
        mode = nodes[begin]['params'].get('mode', 'count')
        caption = {'count': 'For · 按次数', 'foreach': 'For Each · 逐项遍历',
                   'while': '条件循环 · 执行后判断'}.get(mode, '循环方式无效')
        if mode == 'count':
            external_count = any(edge[2:] == [end, 'count'] for edge in graph['connections'])
            caption += ' · 次数来自输入' if external_count else f" · {nodes[end]['params'].get('count', 10)} 次"
        result.append(LoopRegion(begin, end, members, depth, color,
                                 f'循环 {index + 1} · {caption}'))
    return sorted(result, key=lambda region: region.depth)

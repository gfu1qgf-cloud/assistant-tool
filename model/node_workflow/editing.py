"""Atomic graph edits shared by mouse gestures, commands and tests."""
import copy


def connection_candidate(graph, registry, source, target, *, replacing=()):
    """Return new edges or an explanation without changing the user's graph."""
    from .typing import resolve_port_types
    nodes = {n['id']: n for n in graph['nodes']}
    if source[0] == target[0]:
        raise ValueError('不能把节点连接到自身。')
    try:
        a, b = nodes[source[0]], nodes[target[0]]
        output = next(p for p in registry.definitions[a['kind']].output_ports(a['params']) if p.key == source[1])
        input_ = next(p for p in registry.definitions[b['kind']].input_ports(b['params']) if p.key == target[1])
    except (KeyError, StopIteration):
        raise ValueError('连接端口不存在或插件不可用。') from None
    edge = [source[0], output.key, target[0], input_.key]
    removed = lambda e: e in replacing or not input_.multiple and e[2:] == edge[2:]
    edges = [e for e in graph['connections'] if not removed(e)]
    if edge not in edges:
        previous = next((i for i, e in enumerate(graph['connections']) if removed(e) and e[2:] == edge[2:]), None)
        if previous is None:
            edges.append(edge)
        else:
            edges.insert(sum(not removed(e) for e in graph['connections'][:previous]), edge)
    _, conflicts = resolve_port_types({**graph, 'connections': edges}, registry)
    if conflicts.get((target[0], False, input_.key)):
        raise ValueError('类型不匹配／不兼容：' + ' / '.join(conflicts[(target[0], False, input_.key)]))
    adjacency = {}
    for e in edges:
        adjacency.setdefault(e[0], []).append(e[2])
    seen = set()
    def reaches(value):
        if value == source[0]: return True
        if value in seen: return False
        seen.add(value)
        return any(reaches(n) for n in adjacency.get(value, ()))
    if reaches(target[0]):
        raise ValueError('连接会形成非法环路。')
    return edges


def insertion_options(graph, registry, node_id, edge):
    if edge not in graph['connections'] or node_id in (edge[0], edge[2]): return []
    if any(node_id in (e[0], e[2]) for e in graph['connections']): return []
    node = next(n for n in graph['nodes'] if n['id'] == node_id)
    definition = registry.definitions[node['kind']]
    options = []
    for inp in definition.input_ports(node['params']):
        for out in definition.output_ports(node['params']):
            trial = copy.deepcopy(graph)
            try:
                trial['connections'] = connection_candidate(trial, registry, edge[:2], [node_id, inp.key], replacing=(edge,))
                trial['connections'] = connection_candidate(trial, registry, [node_id, out.key], edge[2:])
            except ValueError:
                continue
            options.append((inp.key, out.key))
    return options


def insert_node_on_edge(graph, registry, node_id, edge, ports):
    if ports not in insertion_options(graph, registry, node_id, edge):
        raise ValueError('此节点没有可用的插入端口组合。')
    position = graph['connections'].index(edge)
    new = connection_candidate(graph, registry, edge[:2], [node_id, ports[0]], replacing=(edge,))
    new = connection_candidate({**graph, 'connections': new}, registry, [node_id, ports[1]], edge[2:])
    added = [[edge[0], edge[1], node_id, ports[0]], [node_id, ports[1], edge[2], edge[3]]]
    # The downstream wire retains its exact multi-input ordering.
    remaining = [e for e in graph['connections'] if e != edge]
    remaining[position:position] = added
    graph['connections'] = remaining


def gather_reroute(graph, node_id, edge):
    """Collect equivalent fan-out wires, never silently replace another source."""
    from .reroute import transparent_kind
    nodes = {n['id']: n for n in graph['nodes']}
    if edge not in graph['connections'] or node_id in (edge[0], edge[2]): return False
    def origin(source, out, seen=None):
        seen = set() if seen is None else seen
        if source in seen: raise ValueError('接点来源包含环路。')
        seen.add(source)
        wires = [e for e in graph['connections'] if e[2:] == [source, 'value']]
        if transparent_kind(nodes[source]['kind']) and out == 'value' and len(wires) == 1:
            return origin(wires[0][0], wires[0][1], seen)
        return source, out
    upstream = [e for e in graph['connections'] if e[2] == node_id]
    if len(upstream) != 1: raise ValueError('先给接点连接一个数据来源。')
    if origin(*upstream[0][:2]) != origin(*edge[:2]):
        raise ValueError('来源不同，不能等价收束；请显式重接或使用合并节点。')
    replacement = [node_id, 'value', edge[2], edge[3]]
    if replacement in graph['connections']: return False
    adjacency = {}
    for old in graph['connections']:
        value = replacement if old == edge else old
        adjacency.setdefault(value[0], set()).add(value[2])
    seen = set()
    def reaches(value):
        if value == node_id: return True
        if value in seen: return False
        seen.add(value)
        return any(reaches(child) for child in adjacency.get(value, ()))
    if reaches(edge[2]):
        raise ValueError('收线会形成环路，原连线未修改。')
    graph['connections'][graph['connections'].index(edge)] = replacement
    return True

"""Data-only node fragments. Inspired by NodeGraphQt's clipboard serialization.

No upstream code is copied: preserve our typed ports and provider contracts.
Never carry workflow identity, runtime objects, external wires or output choice.
"""
import copy
import json
from uuid import uuid4

from .graph import GraphError, MAX_NODES, check_document, empty_graph

MIME_TYPE = 'application/x-assistant-tool-nodes+json'
FORMAT = 'assistant-tool/nodes'
MAX_BYTES = 1024 * 1024


def capture_nodes(graph, selected):
    check_document(graph)
    ids = set(selected)
    for item in graph.get('annotations', []):
        if item['id'] in ids:
            ids.update(item.get('members', []))
    # A selected Begin/End represents its loop block. Include its paired end,
    # body and nested regions, so a copied block cannot feed back into originals.
    from .loop_regions import loop_regions
    regions = loop_regions(graph)
    while True:
        expanded = set(ids)
        for region in regions:
            if {region.begin, region.end} & ids:
                expanded.update(region.members)
        if expanded == ids:
            break
        ids = expanded
    nodes = [copy.deepcopy(node) for node in graph['nodes'] if node['id'] in ids]
    annotations = [copy.deepcopy(item) for item in graph.get('annotations', []) if item['id'] in ids
                   or item.get('members') and set(item['members']) <= ids]
    ids.update(item['id'] for item in annotations)
    if not nodes and not annotations:
        raise GraphError('请先选择要复制的节点（可框选多个）。')
    edges = [list(edge) for edge in graph['connections'] if edge[0] in ids and edge[2] in ids]
    value = {'format': FORMAT, 'version': 1, 'nodes': nodes, 'connections': edges}
    if annotations:
        value['annotations'] = annotations
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(data) > MAX_BYTES:
        raise GraphError('所选节点数据超过 1MB，请分批复制。')
    return data, ids


def read_fragment(data, registry):
    if not isinstance(data, (str, bytes)) or len(data.encode('utf-8') if isinstance(data, str) else data) > MAX_BYTES:
        raise GraphError('剪贴板节点数据超过 1MB。')
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('format') != FORMAT or value.get('version') != 1:
            raise ValueError('format')
        json.dumps(value, allow_nan=False)  # Reject NaN, infinities and excessive nesting.
        fragment = dict(empty_graph(), nodes=value['nodes'], connections=value['connections'], annotations=value.get('annotations', []))
        check_document(fragment)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as error:
        raise GraphError('剪贴板不是有效的本程序节点数据；未修改工作流。') from error
    if not fragment['nodes'] and not fragment.get('annotations'):
        raise GraphError('剪贴板没有节点。')
    definitions = {}
    inputs = {}
    for node in fragment['nodes']:
        definition = registry.definitions.get(node['kind'])
        if definition is None or node.get('version') != definition.version:
            raise GraphError('节点提供者不可用或接口版本不兼容：' + node['kind'])
        definitions[node['id']] = definition
        inputs[node['id']] = definition.input_ports(node['params'])
    targets, seen = set(), set()
    for source, out, target, inp in fragment['connections']:
        source_node = next(n for n in fragment['nodes'] if n['id'] == source)
        output = next((p for p in definitions[source].output_ports(source_node['params']) if p.key == out), None)
        input_ = next((p for p in inputs[target] if p.key == inp), None)
        edge = (source, out, target, inp)
        if (output is None or input_ is None or edge in seen
                or not input_.multiple and (target, inp) in targets):
            raise GraphError('剪贴板包含无效端口或重复连线；未修改工作流。')
        targets.add((target, inp)); seen.add(edge)
    return fragment


def paste_nodes(graph, data, registry, position):
    """Return a new graph and pasted IDs, or fail without mutating anything."""
    fragment = read_fragment(data, registry)
    if len(graph['nodes']) + len(fragment['nodes']) > MAX_NODES:
        raise GraphError(f'粘贴后超过 {MAX_NODES} 个节点，请分批处理。')
    if len(graph['connections']) + len(fragment['connections']) > 512:
        raise GraphError('粘贴后超过 512 条连线。')
    result = copy.deepcopy(graph)
    from .code_node import clear_code_approvals
    clear_code_approvals(fragment)
    objects = fragment['nodes'] + fragment.get('annotations', [])
    mapping = {node['id']: uuid4().hex for node in objects}
    left = min(node.get('position', [0, 0])[0] for node in objects)
    top = min(node.get('position', [0, 0])[1] for node in objects)
    for node in fragment['nodes']:
        node['id'] = mapping[node['id']]
        x, y = node.get('position', [0, 0])
        node['position'] = [x - left + position[0], y - top + position[1]]
        # A copied import should capture fresh stamps when the job is prepared.
        for key in ('video_stamp', 'subtitles_stamp'):
            node['params'].pop(key, None)
        result['nodes'].append(node)
    for item in fragment.get('annotations', []):
        item['id'] = mapping[item['id']]
        item['position'] = [item['position'][0] - left + position[0], item['position'][1] - top + position[1]]
        item['members'] = [mapping[key] for key in item.get('members', [])]
        result.setdefault('annotations', []).append(item)
    result['connections'].extend([[mapping[a], out, mapping[b], inp]
                                 for a, out, b, inp in fragment['connections']])
    # Preserve chosen terminal when duplicating, but adopt the first pasted sink
    # when the new canvas has no output yet.
    if not result.get('output_node'):
        from .workflow_types import output_kinds
        kinds = output_kinds(result, registry)
        outputs = [node['id'] for node in fragment['nodes'] if node['kind'] in kinds]
        if len(outputs) == 1:
            result['output_node'] = outputs[0]
    check_document(result)
    return result, set(mapping.values())

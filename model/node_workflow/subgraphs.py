"""By-value subgraphs compiled into the existing DAG, not a second executor."""
import copy
import hashlib
from uuid import uuid4
from .contracts import NodeDefinition, NodeParameter, NodePort

KIND = 'core.subgraph'
INPUT = 'core.subgraph_input'
OUTPUT = 'core.subgraph_output'
MAX_DEPTH = 8


def interface_ports(params, direction):
    specs = params.get(direction, [])
    if not isinstance(specs, list) or len(specs) > 64:
        raise ValueError('子图每侧最多64个端口。')
    ports = []
    for spec in specs:
        if (not isinstance(spec, dict) or not isinstance(spec.get('id'), str) or not spec['id']
                or not isinstance(spec.get('title'), str) or not spec['title'] or spec.get('type') in {None, '', 'any'}
                or type(spec.get('required', True)) is not bool):
            raise ValueError('子图接口必须使用明确的端口ID、标题与数据类型。')
        ports.append(NodePort(spec['id'], spec['title'], spec['type'], required=spec.get('required', True), capabilities=tuple(spec.get('capabilities', ()))))
    return tuple(ports)


def definition_ports(params, output):
    return interface_ports(params, 'outputs' if output else 'inputs')


def register_subgraphs(registry):
    from .graph import empty_graph
    registry.register('core', NodeDefinition(KIND, '子图', category='组织与子图',
        parameters=(NodeParameter('name', '子图名称', default='子图'), NodeParameter('graph', '内部图', 'object', empty_graph()),
            NodeParameter('inputs', '对外输入', 'object', []), NodeParameter('outputs', '对外输出', 'object', []),
            NodeParameter('promoted', '公开参数', 'object', []), NodeParameter('values', '参数值', 'object', {})),
        input_factory=lambda p: definition_ports(p, False), output_factory=lambda p: definition_ports(p, True),
        build=lambda _i, _p, _c: (_ for _ in ()).throw(ValueError('子图须先由公共图编译器展开。')),
        description='将选中节点封装；显式输入输出，内部仍使用同一执行器。双击进入，返回或展开不会改变业务依赖。'
            '子图按值保存，不引用外部文件，禁止递归／非法环路。'))
    registry.register('core', NodeDefinition(INPUT, '子图输入', category='组织与子图',
        parameters=(NodeParameter('type', '端口类型', default='text'), NodeParameter('required', '必填', 'object', True),
                    NodeParameter('capabilities', '资源能力', 'object', [])),
        input_factory=lambda p: (NodePort('value', '外部输入', p.get('type', 'text'), required=p.get('required', True), capabilities=tuple(p.get('capabilities', ()))),),
        output_factory=lambda p: (NodePort('value', '传入值', p.get('type', 'text'), capabilities=tuple(p.get('capabilities', ()))),),
        build=lambda i, _p, _c: {'value': i.get('value')}, description='由子图外部接口连接，不需要在内部手工复制任务数据。'))
    registry.register('core', NodeDefinition(OUTPUT, '子图输出', category='组织与子图',
        parameters=(NodeParameter('ports', '输出端定义', 'object', []),),
        input_factory=lambda p: interface_ports({'inputs': p.get('ports', [])}, 'inputs'),
        output_factory=lambda p: interface_ports({'outputs': p.get('ports', [])}, 'outputs'),
        build=lambda i, _p, _c: dict(i), description='显式声明要交给外部的结果；不是文件导出节点。'))


def has_subgraphs(graph):
    return any(node['kind'] == KIND for node in graph.get('nodes', []))


def flatten(graph, registry, root_id=None, *, depth=0):
    """Return a plain DAG, resolved preview target and editor-facing error map."""
    from .graph import check_document, MAX_NODES
    if depth > MAX_DEPTH:
        raise ValueError('子图嵌套最多8层；不支持递归引用。')
    check_document(graph)
    result = copy.deepcopy(graph)
    aliases = {node['id']: node['id'] for node in result['nodes']}
    resolved = root_id
    for wrapper in list(result['nodes']):
        if wrapper['kind'] != KIND:
            continue
        params, wrapper_id = wrapper['params'], wrapper['id']
        inner = copy.deepcopy(params['graph'])
        inner['execution_mode'] = graph.get('execution_mode', 'detail')
        if inner.get('workflow_type'):
            raise ValueError('子图内部不能另选插件专用工作流类型。')
        for exposure in params.get('promoted', []):
            target = next((n for n in inner['nodes'] if n['id'] == exposure['node']), None)
            if target is None or exposure['parameter'] not in target['params']:
                raise ValueError('公开参数引用了不存在的内部参数。')
            if exposure['id'] in params.get('values', {}):
                target['params'][exposure['parameter']] = copy.deepcopy(params['values'][exposure['id']])
        preview_inside = resolved[len(wrapper_id)+1:] if resolved and resolved.startswith(wrapper_id + '/') else None
        flat, nested_root, nested_aliases = flatten(inner, registry, preview_inside, depth=depth+1)
        prefix = wrapper_id + '/'
        ids = {n['id']: prefix + n['id'] if len(prefix + n['id']) <= 100
               else hashlib.sha256((prefix + n['id']).encode('utf-8')).hexdigest() for n in flat['nodes']}
        endpoint = flat.get('output_node')
        if endpoint not in ids:
            raise ValueError('子图缺少明确的内部输出节点。')
        end_node = next(n for n in flat['nodes'] if n['id'] == endpoint)
        if end_node['kind'] != OUTPUT:
            raise ValueError('子图末端必须为“子图输出”，不能在内部导出成品。')
        input_specs = params.get('inputs', [])
        output_specs = params.get('outputs', [])
        interface_ports(params, 'inputs'); interface_ports(params, 'outputs')
        entry_nodes = {node['id']: node for node in inner['nodes']}
        for spec in input_specs:
            entry = entry_nodes.get(spec.get('node'))
            if entry is None or entry['kind'] != INPUT or entry['params'].get('type') != spec['type']:
                raise ValueError('子图输入接口与内部输入节点不一致。')
        if {p.key: p.data_type for p in registry.definitions[OUTPUT].output_ports(end_node['params'])} != {s['id']: s['type'] for s in output_specs}:
            raise ValueError('子图输出接口与内部输出节点不一致。')
        rebuilt = []
        by_input = {spec['id']: spec for spec in input_specs}
        by_output = {spec['id']: spec for spec in output_specs}
        for a, out, b, inp in result['connections']:
            if a == wrapper_id:
                if out not in by_output: raise ValueError('子图输出端不存在。')
                a = ids[endpoint]
            if b == wrapper_id:
                if inp not in by_input: raise ValueError('子图输入端不存在。')
                b, inp = ids[by_input[inp]['node']], 'value'
            rebuilt.append([a, out, b, inp])
        result['connections'] = rebuilt + [[ids[a], out, ids[b], inp] for a, out, b, inp in flat['connections']]
        result['nodes'].remove(wrapper)
        for node in flat['nodes']:
            old_id = node['id']; node['id'] = ids[old_id]
            aliases[node['id']] = wrapper_id
            node['editor_owner'] = wrapper_id
            node['subgraph_path'] = [str(params.get('name', '子图'))] + node.get('subgraph_path', [])
            result['nodes'].append(node)
        aliases.pop(wrapper_id, None)
        for item in result.get('annotations', []):
            item['members'] = [key for key in item.get('members', []) if key != wrapper_id]
        if result.get('output_node') == wrapper_id:
            result['output_node'] = ids[endpoint]
        if resolved == wrapper_id:
            resolved = ids[endpoint]
        elif preview_inside is not None:
            resolved = ids[nested_root or preview_inside]
        if len(result['nodes']) > MAX_NODES or len(result['connections']) > 512:
            raise ValueError('子图展开后的节点／连线超过128／512上限。')
    return result, resolved, aliases


def find_node(graph, node_id):
    first, _, rest = node_id.partition('/')
    node = next((n for n in graph['nodes'] if n['id'] == first), None)
    if rest:
        return find_node(node['params']['graph'], rest) if node and node['kind'] == KIND else None
    return node


def collapse(graph, registry, selected, name):
    from .graph import new_node, empty_graph
    from .typing import resolve_port_types
    from .workflow_types import output_kinds
    from .loops import paired_blocks, loop_dependencies
    selected = set(selected)
    nodes = [n for n in graph['nodes'] if n['id'] in selected]
    if not nodes or any(n['kind'] in output_kinds(graph, registry) or n['kind'] in {INPUT, OUTPUT}
            or n['kind'] in {t.entry_kind for t in registry.workflow_types.values()} for n in nodes):
        raise ValueError('请选择内部处理节点；专用入口、子图接口和成品导出不能封装。')
    from .loop_regions import loop_regions
    for region in loop_regions(graph):
        if set(region.members).intersection(selected) and not set(region.members) <= selected:
            raise ValueError('循环必须连同Begin、End及完整内部节点一起封装。')
    types, conflicts = resolve_port_types(graph, registry)
    if any(key[0] in selected for key in conflicts):
        raise ValueError('所选节点有类型冲突，请先修正。')
    point = [min(n['position'][axis] for n in nodes) for axis in (0, 1)]
    inner = empty_graph(); inner['execution_mode'] = graph.get('execution_mode', 'detail')
    inner['nodes'] = copy.deepcopy(nodes)
    moved_annotations = {item['id'] for item in graph.get('annotations', []) if item.get('members') and set(item['members']) <= selected}
    inner['annotations'] = [copy.deepcopy(item) for item in graph.get('annotations', []) if item['id'] in moved_annotations]
    for item in inner['annotations']:
        item['position'] = [item['position'][axis] - point[axis] + 300 for axis in (0, 1)]
    for node in inner['nodes']:
        node['position'] = [node['position'][axis] - point[axis] + 300 for axis in (0, 1)]
    wrapper = new_node(registry.definitions[KIND], point); wrapper['params']['name'] = name
    inputs, outputs, output_map, outer_edges = [], [], {}, []
    for edge in graph['connections']:
        a, out, b, inp = edge
        if a in selected and b in selected:
            inner['connections'].append(list(edge)); continue
        if b in selected:
            dtype = types.get((b, False, inp))
            if not dtype: raise ValueError('子图输入类型尚未确定。')
            port = next(p for p in registry.definitions[next(n['kind'] for n in nodes if n['id'] == b)].input_ports(next(n['params'] for n in nodes if n['id'] == b)) if p.key == inp)
            entry = new_node(registry.definitions[INPUT], (0, len(inputs)*140)); entry['params'].update(type=dtype, required=port.required, capabilities=list(port.capabilities))
            inner['nodes'].append(entry)
            key = 'in_' + uuid4().hex[:12]
            inputs.append(dict(id=key, title=port.title, type=dtype, required=port.required, node=entry['id'], capabilities=list(port.capabilities)))
            inner['connections'].append([entry['id'], 'value', b, inp])
            outer_edges.append([a, out, wrapper['id'], key]); continue
        if a in selected:
            if (a, out) not in output_map:
                dtype = types.get((a, True, out))
                if not dtype: raise ValueError('子图输出类型尚未确定。')
                source = next(n for n in nodes if n['id'] == a)
                port = next(p for p in registry.definitions[source['kind']].output_ports(source['params']) if p.key == out)
                key = 'out_' + uuid4().hex[:12]
                output_map[(a, out)] = key
                outputs.append(dict(id=key, title=port.title, type=dtype, required=True, source=a, port=out))
            outer_edges.append([wrapper['id'], output_map[(a, out)], b, inp]); continue
        outer_edges.append(list(edge))
    if not outputs:
        raise ValueError('所选节点没有连向外部的结果；请先接到下游或预览，再封装。')
    sink = new_node(registry.definitions[OUTPUT], (max(n['position'][0] for n in inner['nodes'])+350, 0))
    sink['params']['ports'] = [{k: s[k] for k in ('id', 'title', 'type', 'required')} for s in outputs]
    inner['nodes'].append(sink); inner['output_node'] = sink['id']
    inner['connections'].extend([[s['source'], s['port'], sink['id'], s['id']] for s in outputs])
    wrapper['params'].update(graph=inner, inputs=inputs, outputs=outputs)
    candidate = copy.deepcopy(graph)
    candidate['nodes'] = [n for n in candidate['nodes'] if n['id'] not in selected] + [wrapper]
    candidate['connections'] = outer_edges
    if 'annotations' in candidate:
        candidate['annotations'] = [item for item in candidate['annotations'] if item['id'] not in moved_annotations]
    for item in candidate.get('annotations', []):
        item['members'] = [key for key in item.get('members', []) if key not in selected]
    flatten(candidate, registry)
    graph.clear(); graph.update(candidate)
    return wrapper


def expand(graph, registry, node_id):
    wrapper = next(n for n in graph['nodes'] if n['id'] == node_id and n['kind'] == KIND)
    candidate = copy.deepcopy(graph)
    # Expand only one level, keep nested wrappers available for further editing.
    params = wrapper['params']; inner = copy.deepcopy(params['graph'])
    for exposure in params.get('promoted', []):
        target = next(n for n in inner['nodes'] if n['id'] == exposure['node'])
        if exposure['id'] in params.get('values', {}):
            target['params'][exposure['parameter']] = copy.deepcopy(params['values'][exposure['id']])
    mapping = {n['id']: uuid4().hex for n in inner['nodes']}
    by_input = {s['id']: s for s in params['inputs']}
    edges = []
    for a, out, b, inp in candidate['connections']:
        if a == node_id: a = mapping[inner['output_node']]
        if b == node_id: b, inp = mapping[by_input[inp]['node']], 'value'
        edges.append([a, out, b, inp])
    edges.extend([[mapping[a], out, mapping[b], inp] for a, out, b, inp in inner['connections']])
    candidate['nodes'] = [n for n in candidate['nodes'] if n['id'] != node_id]
    for node in inner['nodes']:
        node['id'] = mapping[node['id']]
        node['position'] = [node['position'][axis] + wrapper['position'][axis] for axis in (0, 1)]
        candidate['nodes'].append(node)
    candidate['connections'] = edges
    for item in inner.get('annotations', []):
        item['id'] = uuid4().hex
        item['members'] = [mapping[key] for key in item.get('members', [])]
        item['position'] = [item['position'][axis] + wrapper['position'][axis] for axis in (0, 1)]
        candidate.setdefault('annotations', []).append(item)
    for item in candidate.get('annotations', []):
        item['members'] = [key for key in item.get('members', []) if key != node_id]
    graph.clear(); graph.update(candidate)
    return set(mapping.values())



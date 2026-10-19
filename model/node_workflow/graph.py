"""Typed DAG validation and execution of trusted, registered plan builders."""

from model.AppDataStorage import data_io, data_file, data_directory
import copy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from .contracts import (NodeDefinition, NodeParameter, NodePort, ResourceRef,
                        FilterOperation, RenderPlan, TaskSnapshot, VideoTimeline, SubtitleTrack, DirectoryRef, FileList, ResourcePool)
from .contracts import validate_ports
from .operations import bind_operations
from .scope import execution_mode

SCHEMA_VERSION = 1
MAX_NODES = 128

# Stable field-level ports. The task snapshot stays private to the entry node.
TASK_FIELDS = (
    ("task_path", "任务路径", "directory"), ("task_audio_text", "任务文案", "text"),
    ("task_id", "任务编号", "text"), ("task_name", "任务名称", "text"),
    ("language", "任务语言", "text"), ("task_type", "任务类型", "text"),
    ("task_audio_type", "语音类型", "text"), ("task_audio_voice_id", "语音 ID", "text"),
    ("admin", "管理员", "text"), ("creator", "制作人", "text"),
    ("task_date", "任务日期", "text"), ("task_reference_link", "参考链接", "text"),
    ("review_required", "需要审核", "boolean"), ("subcategory_text", "子类别", "text"),
    ("submission_task_type", "交付类型", "text"), ("daily_stat_sheet", "统计分页", "text"),
    ("daily_stat_category", "统计类别", "text"),
    ("task_id_number", "任务编号 · 数值", "number"),
)

OUTPUT_KINDS = {"core.render", "core.task_render", "core.export", "core.py_output"}


class GraphError(ValueError):
    pass


class NodeRegistry:
    def __init__(self):
        self.definitions = {}
        self.owners = {}
        self.workflow_types = {}
        self.workflow_type_owners = {}

    def register_workflow_type(self, owner, definition):
        from .workflow_types import register_type
        register_type(self, owner, definition)

    def register(self, owner, definition):
        if not isinstance(definition, NodeDefinition):
            raise TypeError("节点接口只接受 NodeDefinition。")
        if (not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", definition.kind)
                or not definition.kind.startswith(owner + ".")
                or type(definition.version) is not int or definition.version < 1
                or not callable(definition.build) or definition.execution not in {"plan", "compute"}
                or definition.prepare is not None and not callable(definition.prepare)
                or definition.inspector_factory is not None and not callable(definition.inspector_factory)
                or definition.compile_operation is not None and not callable(definition.compile_operation)
                or definition.compile_source is not None and not callable(definition.compile_source)
                or definition.validate_parameters is not None and not callable(definition.validate_parameters)
                or definition.input_factory is not None and not callable(definition.input_factory)
                or definition.output_factory is not None and not callable(definition.output_factory)
                or definition.select_input is not None and not callable(definition.select_input)
                or not isinstance(definition.category, str) or len(definition.category) > 120
                or definition.category and any(not part.strip() for part in definition.category.split("/"))
                or not isinstance(definition.keywords, tuple)
                or len(definition.keywords) > 50
                or any(not isinstance(word, str) or len(word) > 100 for word in definition.keywords)
                or not isinstance(definition.execution_scope, str)
                or definition.execution_scope not in {"any", "detail", "line"}):
            raise ValueError("节点必须使用插件命名空间、版本号和可调用的构建接口。")
        if definition.kind in self.definitions:
            raise ValueError("节点类型重复：" + definition.kind)
        if definition.terminal_result is not None:
            result = definition.terminal_result
            if (not isinstance(result, NodePort) or definition.outputs or definition.output_factory is not None
                    or result.data_type == "any" or not result.required or result.multiple):
                raise ValueError("终点节点不能有可连线输出；内部结果必须使用固定类型、必填、单值约定。")
        defaults = {p.key: copy.deepcopy(p.default) for p in definition.parameters}
        for ports in (definition.inputs, definition.input_ports(defaults), definition.result_ports(defaults)):
            validate_ports(ports)
        self.definitions[definition.kind] = definition
        self.owners[definition.kind] = owner

    def register_function(self, owner, function):
        from .function_library import function_definition
        self.register(owner, function_definition(function))

    def subset(self, enabled):
        result = NodeRegistry()
        for kind, definition in self.definitions.items():
            if enabled(self.owners[kind]):
                result.register(self.owners[kind], definition)
        for kind, definition in self.workflow_types.items():
            if enabled(self.workflow_type_owners[kind]):
                result.register_workflow_type(self.workflow_type_owners[kind], definition)
        return result


def new_node(definition, position=(0, 0)):
    return {"id": uuid4().hex, "kind": definition.kind, "version": definition.version,
            "params": {p.key: copy.deepcopy(p.default) for p in definition.parameters},
            "position": list(position)}


def empty_graph():
    return {"schema": SCHEMA_VERSION, "execution_mode": "detail", "nodes": [], "connections": [], "output_node": ""}


def check_document(graph):
    if not isinstance(graph, dict) or graph.get("schema") != SCHEMA_VERSION:
        raise GraphError("工作流版本不支持；未修改原文件。")
    if (not isinstance(graph.get('workflow_type', ''), str)
            or len(graph.get('workflow_type', '')) > 120):
        raise GraphError('插件工作流类型格式无效。')
    try:
        execution_mode(graph)
    except ValueError as error:
        raise GraphError(str(error)) from error
    nodes, edges = graph.get("nodes"), graph.get("connections")
    if (not isinstance(nodes, list) or len(nodes) > MAX_NODES
            or not isinstance(edges, list) or len(edges) > 512):
        raise GraphError("工作流节点或连接数量无效。")
    if not isinstance(graph.get("output_node", ""), str):
        raise GraphError("工作流输出节点 ID 无效。")
    view = graph.get("view", {})
    if (not isinstance(view, dict) or not isinstance(view.get("connection_style", "smooth"), str)
            or view.get("connection_style", "smooth") not in {"smooth", "straight", "elbow", "rounded"}):
        raise GraphError("工作流连线样式无效。")
    if view.get('orientation', 'horizontal') not in {'horizontal', 'vertical'}:
        raise GraphError('节点布局方向无效。')
    ids = set()
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                or not 0 < len(node["id"]) <= 100 or node["id"] in ids
                or not isinstance(node.get("kind"), str)
                or not isinstance(node.get("params"), dict)):
            raise GraphError("工作流节点格式无效或 ID 重复。")
        position = node.get("position", [0, 0])
        if (not isinstance(position, list) or len(position) != 2
                or any(type(p) not in (int, float) or not math.isfinite(p)
                       or abs(p) > 100000 for p in position)):
            raise GraphError("节点位置无效。")
        ids.add(node["id"])
    for edge in edges:
        if (not isinstance(edge, list) or len(edge) != 4
                or any(not isinstance(value, str) for value in edge)
                or edge[0] not in ids or edge[2] not in ids):
            raise GraphError("连接引用了不存在的节点。")
    from .presentation import validate_annotations
    validate_annotations(graph)


def validate(graph, registry, *, root_id=None):
    """Only the selected output's ancestors need to be complete."""
    check_document(graph)
    from .subgraphs import has_subgraphs, flatten
    if has_subgraphs(graph):
        try:
            flat, resolved, aliases = flatten(graph, registry, root_id)
            errors, order, incoming = validate(flat, registry, root_id=resolved)
            return [(aliases.get(key, key), message) for key, message in errors], order, incoming
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            return [('', '子图：' + str(error))], [], {}
    nodes = {n["id"]: n for n in graph["nodes"]}
    incoming = {}
    errors = []
    from .workflow_types import selected_type, output_kinds
    try:
        workflow = selected_type(graph, registry)
        valid_outputs = output_kinds(graph, registry)
    except ValueError as error:
        errors.append(('', str(error)))
        workflow, valid_outputs = None, set()
    from .typing import resolve_port_types
    effective_types, type_conflicts = resolve_port_types(graph, registry)
    from .reroute import transparent_kind
    def original_source(source, out, seen=None):
        seen = set() if seen is None else seen
        if source in seen:
            return source, out
        seen.add(source)
        if transparent_kind(nodes[source]["kind"]) and out == "value":
            upstream = [e for e in graph["connections"] if e[2:] == [source, "value"]]
            if len(upstream) == 1:
                return original_source(upstream[0][0], upstream[0][1], seen)
        return source, out
    capability_cache = {}
    def known_capabilities(source, out):
        key = (source, out)
        if key in capability_cache:
            return capability_cache[key]
        # Also breaks hand-edited cycles; DFS below reports those explicitly.
        capability_cache[key] = None
        definition = registry.definitions.get(nodes[source]["kind"])
        if definition is None:
            return None
        capabilities = None
        if transparent_kind(definition.kind) and out == "value":
            upstream = [e for e in graph["connections"] if e[2:] == [source, "value"]]
            capabilities = known_capabilities(upstream[0][0], upstream[0][1]) if len(upstream) == 1 else None
        elif (definition.kind in {"core.switch", "core.choose"} or definition.kind.startswith("core.switch_")) and out == "value":
            keys = {p.key for p in definition.input_ports(nodes[source]['params']) if p.data_type != 'boolean'}
            upstream = [e for e in graph["connections"] if e[2] == source and e[3] in keys]
            branches = [known_capabilities(e[0], e[1]) for e in upstream]
            known = [caps for caps in branches if caps is not None]
            capabilities = set.intersection(*known) if known else None
        elif definition.kind == "core.import":
            capabilities = {"seek", "replay"}
        elif definition.kind == "core.transport":
            capabilities = {"replay"} if nodes[source]["params"].get("mode") == "stream" else {"seek", "replay"}
        else:
            port = next((p for p in definition.output_ports(nodes[source]['params']) if p.key == out), None)
            capabilities = set(port.capabilities) if port and port.capabilities else None
        capability_cache[key] = capabilities
        return capabilities
    for edge in graph["connections"]:
        source, out, target, inp = edge
        first = registry.definitions.get(nodes[source]["kind"])
        second = registry.definitions.get(nodes[target]["kind"])
        output = next((p for p in first.output_ports(nodes[source]['params']) if p.key == out), None) if first else None
        input_ = next((p for p in second.input_ports(nodes[target]['params']) if p.key == inp), None) if second else None
        if output is None or input_ is None:
            errors.append((target, "端口不存在、插件已禁用或数据类型不匹配。"))
            continue
        conflict = type_conflicts.get((target, False, inp))
        if conflict:
            errors.append((target, "数据类型不匹配／同类型组不一致：" + " / ".join(conflict) +
                           "。同一多态节点的数据输入、输出必须是同一种类型。"))
        capabilities = known_capabilities(source, out)
        required_caps = set(input_.capabilities)
        if second.kind == "core.preview" and effective_types.get((target, False, inp)) == "video":
            required_caps.add("seek")
        if capabilities is not None and required_caps - capabilities:
            errors.append((target, "输入端“" + input_.title + "”需要可定位资源，不能直接连接顺序流；请先使用内存缓存。"))
        key = (target, inp)
        connections = incoming.setdefault(key, [])
        if edge in connections or connections and not input_.multiple:
            errors.append((target, "输入端“" + input_.title + "”不允许重复连接。"))
        connections.append(edge)
    output_id = graph.get("output_node", "") if root_id is None else root_id
    if output_id not in nodes or root_id is None and nodes[output_id]["kind"] not in valid_outputs:
        errors.append(('', '请选择此工作流类型的输出节点。' if graph.get('workflow_type') else
                       '请选择一个导出成品节点作为输出；旧版输出结算器也可继续使用。'))
    if workflow and root_id is None and sum(n['kind'] == workflow.entry_kind for n in nodes.values()) != 1:
        errors.append(('', '插件工作流必须且只能有一个专用输入节点。'))
    visiting, visited, order = set(), set(), []
    def visit(node_id):
        if node_id in visiting:
            errors.append((node_id, "发现循环连接，节点链无法执行。"))
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        node = nodes[node_id]
        definition = registry.definitions.get(node["kind"])
        if definition is None:
            errors.append((node_id, "节点提供者不可用：" + node["kind"]))
        elif node.get("version") != definition.version:
            errors.append((node_id, "节点接口版本不兼容，需重新添加该节点。"))
        else:
            mode = execution_mode(graph)
            for value in registry.workflow_types.values():
                if node['kind'] in {value.entry_kind, value.output_kind} and (not workflow or workflow.type_id != value.type_id):
                    errors.append((node_id, '此节点只能用于“' + value.title + '”类型工作流。'))
            if definition.execution_scope not in {"any", mode}:
                errors.append((node_id, f"节点“{definition.title}”仅适用于 {definition.execution_scope.title()} 模式；当前为 {mode.title()}。"))
            if node["kind"] == "core.export" and node["params"].get("destination") == "task" and mode != "line":
                errors.append((node_id, "导出到当前任务目录需要 Line 模式；Detail 模式请使用指定路径。"))
            if node["kind"] == "core.export" and node["params"].get("destination") == "file" and not str(node["params"].get("output") or "").strip() and not incoming.get((node_id, 'output')):
                errors.append((node_id, "请填写导出文件路径。"))
            if node['kind'] == 'core.render' and not str(node['params'].get('output') or '').strip() and not incoming.get((node_id, 'output')):
                errors.append((node_id, '请填写或连接导出文件路径。'))
            for port in definition.input_ports(node['params']):
                connections = incoming.get((node_id, port.key), [])
                if port.required and not connections:
                    errors.append((node_id, "必须输入端“" + port.title + "”未连接。"))
                for edge in connections:
                    source_id, source_key = original_source(edge[0], edge[1])
                    source = nodes[source_id]
                    if (port.required and source["kind"] == "core.import"
                            and source_key == "subtitles" and not source["params"].get("subtitles")):
                        errors.append((node_id, "必须输入端“" + port.title + "”的导入节点未指定字幕。"))
                    visit(edge[0])
            for is_output, ports in ((False, definition.input_ports(node['params'])), (True, definition.output_ports(node['params']))):
                for port in ports:
                    key = (node_id, is_output, port.key)
                    if port.data_type == "any" and not effective_types.get(key) and key not in type_conflicts:
                        # A metadata-only Begin does not need an arbitrary seed.
                        if node["kind"] == "core.loop_begin" and not any(
                                edge[0] == node_id and edge[1] == "value" for edge in graph["connections"]):
                            continue
                        errors.append((node_id, "多态端口“" + port.title + "”尚未确定数据类型，请接入具体数据来源。"))
            for param in definition.parameters:
                value = node["params"].get(param.key, param.default)
                if param.required and param.kind in {"video_file", "subtitle_file", "output_file", "directory_file"} and not str(value).strip():
                    errors.append((node_id, "请填写“" + param.title + "”。"))
                if param.kind == "integer" and (type(value) is not int or not param.minimum <= value <= param.maximum):
                    errors.append((node_id, "参数“" + param.title + "”超出范围。"))
                if param.kind == "number" and (type(value) not in (int, float) or not math.isfinite(value)
                                               or not param.minimum <= value <= param.maximum):
                    errors.append((node_id, "参数“" + param.title + "”超出范围或不是有限数值。"))
                if param.kind == "choice" and value not in [choice[0] for choice in param.choices]:
                    errors.append((node_id, "参数“" + param.title + "”选项无效。"))
            if definition.validate_parameters is not None:
                try:
                    definition.validate_parameters(copy.deepcopy(node["params"]))
                except Exception as error:
                    errors.append((node_id, "参数设置无效：" + str(error)))
        visiting.remove(node_id)
        visited.add(node_id)
        order.append(node_id)
    if output_id in nodes:
        visit(output_id)
        from .loops import BEGIN, paired_blocks, validate_blocks
        if root_id is not None:
            # Internal previews cook the paired End first, then inspect its last
            # actual pass. Include the full block in preparation and validation.
            _pairs, ends, _block_errors = paired_blocks(graph)
            while True:
                additional = [ends[key] for key in list(order)
                              if nodes[key]["kind"] == BEGIN and key in ends and ends[key] not in visited]
                if not additional:
                    break
                for end in additional:
                    visit(end)
        errors.extend(validate_blocks(graph, incoming, order, output_id, preview=root_id is not None))
    return errors, order, incoming


def snapshot(graph, registry, *, root_id=None):
    from .subgraphs import flatten, has_subgraphs
    if has_subgraphs(graph):
        graph, root_id, _ = flatten(graph, registry, root_id)
    result = copy.deepcopy(graph)
    errors, order, _ = validate(result, registry, root_id=root_id)
    if errors:
        raise GraphError("\n".join(message for _, message in errors))
    for node in result["nodes"]:
        if node["id"] in order and node["kind"] == "core.import":
            for key, kind in (("video", "video"), ("subtitles", "subtitles")):
                if node["params"].get(key):
                    ref = ResourceRef.capture(node["params"][key], kind)
                    node["params"][key + "_stamp"] = [ref.size, ref.modified_ns]
    return result


def evaluate_graph(graph, registry, context, *, root_id=None):
    """Use the same typed builders inside and outside structured loop scopes."""
    from .subgraphs import flatten, has_subgraphs
    if has_subgraphs(graph):
        graph, root_id, _ = flatten(graph, registry, root_id)
    context.workflow_mode = execution_mode(graph)
    context.workflow_registry = registry
    errors, order, incoming = validate(graph, registry, root_id=root_id)
    if errors:
        raise GraphError("\n".join(message for _, message in errors))
    from .typing import resolve_port_types
    effective_types, _ = resolve_port_types(graph, registry)
    from .evaluation import GraphEvaluator
    evaluator = GraphEvaluator(graph, registry, context, incoming, effective_types)
    target = graph["output_node"] if root_id is None else root_id
    try:
        scope = evaluator.inspect_scope(target, evaluator.root) if root_id is not None else evaluator.root
    except ValueError as error:
        raise GraphError(str(error)) from error
    return evaluator.evaluate(target, scope)


def build_plan(graph, registry, context):
    plan = evaluate_graph(graph, registry, context).get("result")
    if not isinstance(plan, RenderPlan):
        raise GraphError("结算器未得到有效的渲染计划。")
    return plan


def _import(_inputs, params, _context):
    video = ResourceRef.capture(params["video"], "video", params.get("video_stamp"))
    if Path(video.path).suffix.lower() not in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"}:
        raise GraphError("请导入本地视频文件，不能使用网络地址。")
    subtitles = (ResourceRef.capture(params["subtitles"], "subtitles", params.get("subtitles_stamp"))
                 if params.get("subtitles") else None)
    return {"video": video, "subtitles": subtitles}


def _merge(inputs, _params, _context):
    return {"operations": tuple(op for group in inputs["operations"] for op in group)}


def _transport(inputs, params, context):
    if isinstance(inputs["video"], VideoTimeline):
        raise GraphError("剪辑计划尚未编码；请直接连接结算器，压缩传输节点只接受已生成的视频资源。")
    session = getattr(context, "media", None)
    if session is None:
        raise GraphError("压缩视频传输必须在媒体执行会话中运行。")
    from .video_stream import transport
    return {"video": transport(inputs["video"], session, params.get("mode", "memory"),
                              params.get("limit_mb", 128))}


def _render(inputs, params, context):
    video = inputs["video"]
    operations = bind_operations(video, inputs["operations"], context)
    if not operations:
        raise GraphError("操作集合为空。")
    target = inputs.get('output') if inputs.get('output') is not None else params['output']
    output = str(Path(target).expanduser().resolve())
    if Path(output).suffix.lower() != ".mp4":
        raise GraphError("第一版成品请选择 MP4。")
    task = getattr(context, "task", None)
    if task is not None and not Path(output).is_relative_to(Path(task.directory).resolve()):
        raise GraphError("任务工作流成品必须输出到该任务目录内。")
    if Path(output).exists() or output.casefold() == video.path.casefold():
        raise GraphError("成品路径已存在或与源视频相同，请换一个名称；不会覆盖文件。")
    return {"result": RenderPlan(video, operations, output)}


def _task(_inputs, _params, context):
    task = getattr(context, "task", None)
    if not isinstance(task, TaskSnapshot):
        raise GraphError("请先在主程序选择任务，并载入节点面板。")
    fields = task.fields
    values = {key: str(fields[key]) if fields.get(key) is not None else ""
              for key, _title, kind in TASK_FIELDS if kind == "text"}
    values["task_path"] = DirectoryRef(task.directory, task.key)
    values["language"] = task.language
    values["review_required"] = bool(fields.get("review_required"))
    from .basic import parse_number
    try:
        values["task_id_number"] = parse_number(values.get("task_id", ""))
    except ValueError:
        values["task_id_number"] = None  # Do not invent zero for a non-numeric identifier.
    return values


def _task_field(_inputs, params, context):
    if not isinstance(getattr(context, "task", None), TaskSnapshot):
        raise GraphError("请先载入主程序任务。")
    fields = context.task.fields
    key = str(params.get("field") or "task_audio_text")
    value = fields.get(key, fields.get("raw", {}).get(key))
    if value is None or not isinstance(value, (str, int, float, bool)):
        raise GraphError("任务没有可用的字段：" + key)
    return {"text": str(value)}


def _task_render(inputs, params, context):
    task = context.task
    video = inputs["video"]
    if getattr(video, "task_key", "") and video.task_key != task.key:
        raise GraphError("视频来自另一项任务，不能自动写入当前任务目录。")
    operations = bind_operations(video, inputs.get("operations"), context)
    suffix = str(params.get("suffix") or "字幕成品").strip()
    if not re.fullmatch(r"[^\\/:*?\"<>|\x00-\x1f]{1,60}", suffix) or suffix in {".", ".."}:
        raise GraphError("成品名称后缀包含无效字符。")
    identifier = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(task.fields.get("task_id") or "任务"))[:80]
    directory = Path(task.directory) / "节点工作流结果"
    base = directory / f"{identifier}_智能剪辑_{suffix}.mp4"
    output, count = base, 2
    while any(output.with_suffix(ext).exists() for ext in (".mp4", ".srt", ".workflow.json")):
        output = base.with_name(f"{base.stem}_{count:03}.mp4")
        count += 1
    return {"result": RenderPlan(video, tuple(operations), str(output))}


def register_core(registry):
    from .selection import register_selection
    register_selection(registry)
    from .loops import register_loops
    register_loops(registry)
    from .global_data import register_global_data
    register_global_data(registry)
    from .reroute import register_reroute
    register_reroute(registry)
    from .filesystem import register_filesystem
    register_filesystem(registry)
    from .video_probe_nodes import register_video_probe
    register_video_probe(registry)
    from .subtitle_nodes import register_subtitle_nodes
    register_subtitle_nodes(registry)
    from .flow_nodes import register_flow_nodes
    register_flow_nodes(registry)
    from .basic import register_basic
    register_basic(registry)
    from .value_nodes import register_value_nodes
    register_value_nodes(registry)
    from .code_node import register_code_node
    register_code_node(registry)
    from .subgraphs import register_subgraphs
    register_subgraphs(registry)
    from .visual_nodes import register_visual_nodes
    register_visual_nodes(registry)
    from .text_nodes import register_text_nodes
    register_text_nodes(registry)
    from .path_nodes import register_path_nodes
    register_path_nodes(registry)
    from .python_nodes import register_python_nodes
    register_python_nodes(registry)
    from .python_extended import register_extended_nodes
    register_extended_nodes(registry)
    from .video_stream import register_video_nodes
    register_video_nodes(registry)
    from .preview import register_preview
    register_preview(registry)
    registry.register("core", NodeDefinition("core.text", "文本",
        outputs=(NodePort("text", "文本", "text"),),
        parameters=(NodeParameter("text", "文本内容", "multiline"),),
        build=lambda _inputs, params, _context: {"text": str(params.get("text") or "")},
        category="Python 原生/数据与转换", description="独立文本输入，可替换任务文案或提供其他文本；不读取任务数据。"))
    registry.register("core", NodeDefinition("core.task", "任务入口 · 主程序所选行",
        outputs=tuple(NodePort(key, title, kind) for key, title, kind in TASK_FIELDS), build=_task, version=2, execution_scope="line",
        description="每个任务字段有独立输出端；任务路径接 listdir，文案接文本输入。完整快照仅由入口内部读取，不传给业务节点。"))
    registry.register("core", NodeDefinition("core.task_field", "任务自定义字段",
        outputs=(NodePort("text", "字段文字", "text"),), version=2, execution_scope="line",
        parameters=(NodeParameter("field", "字段名／原始列名", default="task_audio_text"),), build=_task_field,
        description="从本次绑定任务读取一个自定义列，输出单个文本值。常用字段直接从任务入口的独立端口连接。"))
    registry.register("core", NodeDefinition("core.task_render", "结算并导出 · 当前任务目录（兼容）",
        inputs=(NodePort("video", "视频", "video"), NodePort("operations", "操作集合", "operations", required=False)),
        terminal_result=NodePort("result", "内部导出计划", "render_plan"),
        parameters=(NodeParameter("suffix", "成品名称后缀", default="字幕成品"),), build=_task_render, execution_scope="line",
        description="自动输出到该任务的节点工作流结果目录；重跑自动编号，保留已有成品。只在这里最终编码。"))
    registry.register("core", NodeDefinition("core.import", "导入视频 / 字幕",
        outputs=(NodePort("video", "视频", "video"), NodePort("subtitles", "字幕", "subtitles")),
        parameters=(NodeParameter("video", "视频文件", "video_file"),
                    NodeParameter("subtitles", "字幕文件（可选）", "subtitle_file", required=False)), build=_import,
        description="只引用本地文件，不复制原视频；字幕节点使用原有 SRT，不加载模型。"))
    registry.register("core", NodeDefinition("core.transport", "压缩视频传输 · 试验",
        inputs=(NodePort("video", "视频", "video"),),
        outputs=(NodePort("video", "视频资源", "video"),),
        parameters=(NodeParameter("mode", "传输方式", "choice", "memory",
                        choices=(("memory", "内存压缩 MP4 · 可定位"), ("stream", "管道 MP4 · 顺序流"))),
                    NodeParameter("limit_mb", "本节点压缩缓存上限（MB）", "integer", 128, 16, 512)),
        build=_transport,
        description="内存和管道输出同一种资源引用。仅重新封装，不重新编码；缓存总上限为 512 MB，超限报错，不偷偷写中间视频。资源仅在本次运行中有效。"))
    registry.register("core", NodeDefinition("core.merge", "合并操作 · Merge",
        inputs=(NodePort("operations", "操作集合（按接线顺序）", "operations", multiple=True),),
        outputs=(NodePort("operations", "操作集合", "operations"),), build=_merge,
        description="可连接多个操作节点；连接顺序就是执行顺序，右键连接可调整。"))
    registry.register("core", NodeDefinition("core.render", "结算并导出 · 指定路径（兼容）",
        inputs=(NodePort("video", "视频", "video"), NodePort("operations", "操作集合", "operations"),
                NodePort('output', '输出路径 · 可覆盖参数', 'text', required=False)),
        terminal_result=NodePort("result", "内部导出计划", "render_plan"),
        parameters=(NodeParameter("output", "成品路径", "output_file", required=False),), build=_render,
        description="一次 FFmpeg 渲染；保持原视频尺寸、时长和音频。不会覆盖现有成品。"))


def example_graph(registry, transport=""):
    graph = empty_graph()
    kinds = ["core.import", "batch_text_video.caption", "core.merge", "core.render"]
    for i, kind in enumerate(kinds):
        if kind in registry.definitions:
            graph["nodes"].append(new_node(registry.definitions[kind], (i * 275, 40)))
    by_kind = {node["kind"]: node["id"] for node in graph["nodes"]}
    graph["output_node"] = by_kind.get("core.render", "")
    if "batch_text_video.caption" in by_kind:
        a, b, c, d = (by_kind[kind] for kind in kinds)
        graph["connections"] = [[a,"subtitles",b,"subtitles"],
                                [b,"operations",c,"operations"], [c,"operations",d,"operations"],
                                [a,"video",d,"video"]]
        graph["output_node"] = d
        if transport:
            if transport not in {"memory", "stream"}:
                raise GraphError("示例的媒体传输方式无效。")
            node = new_node(registry.definitions["core.transport"], (275, 40))
            node["params"]["mode"] = transport
            graph["nodes"].append(node)
            for existing in graph["nodes"]:
                if existing["id"] not in {a, node["id"]}:
                    existing["position"][0] += 275
            graph["connections"] = [
                [node["id"], edge[1], edge[2], edge[3]]
                if edge[0] == a and edge[1] == "video" else edge[:]
                for edge in graph["connections"]]
            graph["connections"].insert(0, [a, "video", node["id"], "video"])
    return graph


def task_example_graph(registry):
    kinds = ("core.task", "core.listdir", "smart_video_editor.edit", "batch_text_video.caption", "core.task_render")
    unavailable = [kind for kind in kinds if kind not in registry.definitions]
    if unavailable:
        raise GraphError("任务示例需要启用智能剪辑和批量文案视频插件：" + ", ".join(unavailable))
    graph = empty_graph()
    graph["execution_mode"] = "line"
    graph["nodes"] = [new_node(registry.definitions[kind], (index * 300, 40)) for index, kind in enumerate(kinds)]
    a, files, b, c, d = [node["id"] for node in graph["nodes"]]
    graph["connections"] = [[a, "task_path", files, "directory"], [files, "files", b, "files"],
                            [a, "task_audio_text", b, "script"], [a, "language", b, "language"],
                            [a, "task_id", b, "task_id"], [a, "task_name", b, "task_name"],
                            [b, "subtitles", c, "subtitles"], [c, "operations", d, "operations"],
                            [b, "video", d, "video"]]
    graph["output_node"] = d
    return graph


def export_example_graph(registry, task_mode=False):
    """Explicit settlement/export split, preserving legacy example files unchanged."""
    graph = copy.deepcopy(task_example_graph(registry) if task_mode else example_graph(registry))
    old = next(node for node in graph["nodes"] if node["id"] == graph["output_node"])
    settling = new_node(registry.definitions["core.settle"], tuple(old["position"]))
    exporting = new_node(registry.definitions["core.export"], (old["position"][0] + 300, old["position"][1]))
    exporting["params"]["destination"] = "task" if task_mode else "file"
    graph["connections"] = [
        [edge[0], edge[1], settling["id"], edge[3]] if edge[2] == old["id"] else edge[:]
        for edge in graph["connections"]]
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] != old["id"]] + [settling, exporting]
    graph["connections"].append([settling["id"], "plan", exporting["id"], "plan"])
    graph["output_node"] = exporting["id"]
    return graph


def migrate_task_ports(graph, registry):
    """Translate our previous bundled-task graph without editing its saved file."""
    result = copy.deepcopy(graph)
    if any(node["kind"] == "smart_video_editor.edit" and node.get("version") == 1
           for node in result["nodes"]) and "smart_video_editor.edit" not in registry.definitions:
        return result, False
    nodes = {node["id"]: node for node in result["nodes"]}
    task_ids = {key for key, node in nodes.items() if node["kind"] == "core.task" and node.get("version") == 1}
    changed = False
    for node in list(result["nodes"]):
        if node["kind"] == "smart_video_editor.edit" and node.get("version") == 1:
            links = [edge for edge in result["connections"] if edge[2:] == [node["id"], "task"]]
            if len(links) != 1 or links[0][0] not in task_ids:
                continue  # Unknown provider: report incompatible version, do not guess.
            source = links[0][0]
            result["connections"].remove(links[0])
            if not any(edge[2:] == [node["id"], "files"] for edge in result["connections"]):
                position = tuple(node["position"])
                listing = new_node(registry.definitions["core.listdir"], position)
                # Reserve a column only when a new discovery node is needed.
                for existing in result["nodes"]:
                    if existing["position"][0] >= position[0]:
                        existing["position"][0] += 300
                # Old discovery recursed; preserve it, but make the rule explicit now.
                listing["params"]["recursive"] = "yes"
                result["nodes"].append(listing)
                result["connections"] += [[source, "task_path", listing["id"], "directory"],
                    [listing["id"], "files", node["id"], "files"]]
            if not any(edge[2:] == [node["id"], "script"] for edge in result["connections"]):
                result["connections"].append([source, "task_audio_text", node["id"], "script"])
            for field in ("language", "task_id", "task_name"):
                if not any(edge[2:] == [node["id"], field] for edge in result["connections"]):
                    result["connections"].append([source, field, node["id"], field])
            node["version"] = registry.definitions[node["kind"]].version
            changed = True
        elif node["kind"] == "core.task_field" and node.get("version") == 1:
            result["connections"] = [edge for edge in result["connections"] if edge[2:] != [node["id"], "task"]]
            node["version"] = 2
            changed = True
    for key in task_ids:
        nodes[key]["version"] = 2
        changed = True
    check_document(result)
    return result, changed


def load_graph(path):
    source = Path(path)
    if source.stat().st_size > 1024 * 1024:
        raise GraphError("工作流文件超过 1MB。")
    graph = json.loads(data_file(source).read_text(encoding="utf-8-sig"))
    check_document(graph)
    return graph


def save_graph(path, graph):
    import os
    import tempfile
    check_document(graph)
    destination = Path(path)
    data_directory(destination.parent).ensure(parents=True, exist_ok=True)
    fd, temporary = data_io.mkstemp(prefix=".workflow-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(graph, stream, ensure_ascii=False, indent=2, allow_nan=False)
        data_io.replace(temporary, destination)
    finally:
        data_file(Path(temporary)).unlink(missing_ok=True)

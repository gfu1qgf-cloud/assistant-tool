"""One evaluator for ordinary DAGs and nested, scoped Begin/End blocks."""
import copy
import math
import time

from .contracts import (ResourceRef, FilterOperation, RenderPlan, TaskSnapshot,
                        VideoTimeline, SubtitleTrack, DirectoryRef, FileList, ResourcePool, ColorValue)
from .loops import (BEGIN, END, LoopBlock, LoopResults, paired_blocks,
                    loop_dependencies, freeze_result, result_bytes, MAX_COLLECT_BYTES)
from .python_values import PythonValue, validate_data
from .video_stream import VideoStream

MAX_NODE_BUILDS = 100000
MAX_LOOP_DEPTH = 16


class Scope:
    def __init__(self, parent=None, begin=None, values=None):
        self.parent = parent
        self.begin = begin
        self.values = values
        self.cache = {}
        self.last_passes = {}
        self.depth = 0 if parent is None else parent.depth + 1

    def chain(self):
        current = self
        while current is not None:
            yield current
            current = current.parent


class GraphEvaluator:
    def __init__(self, graph, registry, context, incoming, effective_types):
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        self.registry, self.context = registry, context
        self.incoming, self.types = incoming, effective_types
        self.pairs, self.ends, _errors = paired_blocks(graph)
        self.dependencies, _open = loop_dependencies(graph, incoming, self.pairs)
        self.root = Scope()
        self.builds = 0
        self.cooking = set()
        self.reported_nodes, self.last_report = set(), 0.0

    def owner(self, node_id, scope):
        deps = self.dependencies[node_id]
        active = {item.begin for item in scope.chain() if item.begin}
        if deps - active:
            raise ValueError("循环体只能在对应 Begin/End 的迭代范围内执行。")
        return next((item for item in scope.chain() if item.begin in deps), self.root)

    def inspect_scope(self, node_id, scope):
        """Preview an internal node using the paired block's actual last pass."""
        deps = self.dependencies[node_id]
        while True:
            active = {item.begin for item in scope.chain() if item.begin}
            missing = deps - active
            if not missing:
                return scope
            ready = [begin for begin in missing if self.dependencies[self.ends[begin]] <= active]
            if not ready:
                raise ValueError("循环块嵌套关系无效，无法预览。")
            end = self.ends[sorted(ready)[0]]
            self.evaluate(end, scope)
            scope = self.owner(end, scope).last_passes.get(end)
            if scope is None:
                raise ValueError("这个循环执行了 0 次，没有循环内部画面；请预览 End 输出。")

    def evaluate(self, node_id, scope=None):
        from .graph import GraphError
        scope = self.root if scope is None else scope
        owner = self.owner(node_id, scope)
        if node_id in owner.cache:
            return owner.cache[node_id]
        self.context.check_canceled()
        if self.nodes[node_id]["kind"] == BEGIN:
            if owner.begin != node_id or owner.values is None:
                raise GraphError("Begin 尚未进入对应的循环块。")
            return owner.values
        key = (id(owner), node_id)
        if key in self.cooking:
            raise GraphError("循环块交叉引用，不能递归进入尚未结束的 End。")
        self.cooking.add(key)
        try:
            if self.nodes[node_id]["kind"] == END:
                outputs = self.run_loop(node_id, owner)
                self.check_outputs(node_id, outputs)
            else:
                definition = self.registry.definitions[self.nodes[node_id]['kind']]
                ports = {port.key: port for port in definition.input_ports(self.nodes[node_id]['params'])}
                def get_input(key):
                    if key not in ports:
                        raise GraphError('条件选择请求了未声明的输入端。')
                    return self.input_value(node_id, ports[key], owner)
                if definition.select_input is not None:
                    selected = definition.select_input(copy.deepcopy(self.nodes[node_id]['params']), get_input)
                    inputs = {selected: get_input(selected)}
                else:
                    inputs = {key: get_input(key) for key in ports}
                outputs = self.build(node_id, inputs)
            owner.cache[node_id] = outputs
            return outputs
        except Exception as error:
            self.context.check_canceled()
            if isinstance(error, GraphError):
                raise
            definition = self.registry.definitions[self.nodes[node_id]["kind"]]
            self.context.failed_node = {"id": node_id, "title": definition.title}
            raise GraphError(f"{definition.title}：{error}") from error
        finally:
            self.cooking.remove(key)

    def input_value(self, node_id, port, scope):
        from .graph import GraphError
        definition = self.registry.definitions[self.nodes[node_id]["kind"]]
        upstream = [self.evaluate(edge[0], scope)[edge[1]]
                    for edge in self.incoming.get((node_id, port.key), [])]
        if port.required and (not upstream or any(value is None for value in upstream)):
            raise GraphError(definition.title + "：必须输入端“" + port.title + "”的上游没有提供资源。")
        for value in upstream:
            required = set(port.capabilities)
            if definition.kind == "core.preview" and self.types.get((node_id, False, port.key)) == "video":
                required.add("seek")
            if value is not None and required - set(getattr(value, "capabilities", ())):
                raise GraphError(definition.title + "：输入端“" + port.title +
                                 "”需要可定位/可重读资源；请先接入内存缓存，不能直接连接顺序流。")
        upstream = [copy.deepcopy(value) if type(value) in (list, dict) else value for value in upstream]
        return tuple(upstream) if port.multiple else upstream[0] if upstream else None

    def build(self, node_id, inputs, *, bound=None):
        from .graph import GraphError
        self.context.check_canceled()
        self.builds += 1
        if self.builds > MAX_NODE_BUILDS:
            raise GraphError("本次工作流超过 100000 次节点执行的安全上限；未静默截断。")
        node = self.nodes[node_id]
        definition = self.registry.definitions[node["kind"]]
        title = ' / '.join(node.get('subgraph_path', []) + [definition.title])
        self.context.node_prepared = getattr(self.context, "prepared", {}).get(node_id, {})
        self.context.node_id = node_id
        now = time.monotonic()
        if node_id not in self.reported_nodes or not self.dependencies[node_id] or now - self.last_report >= .15:
            self.context.report(("执行节点：" if definition.execution == "compute" else "准备节点：") + definition.title, None)
            self.reported_nodes.add(node_id)
            self.last_report = now
        try:
            outputs = bound if bound is not None else definition.build(inputs, copy.deepcopy(node["params"]), self.context)
            self.check_outputs(node_id, outputs)
            return {port.key: outputs.get(port.key) for port in definition.result_ports(node['params'])}
        except Exception as error:
            self.context.check_canceled()
            self.context.failed_node = {"id": node.get('editor_owner', node_id), "title": title}
            if getattr(error, "workflow_skipped", False):
                self.context.skipped_reason = str(error)
            raise GraphError(f"{title}：{error}") from error

    def check_outputs(self, node_id, outputs):
        from .graph import GraphError
        definition = self.registry.definitions[self.nodes[node_id]["kind"]]
        if not isinstance(outputs, dict):
            raise GraphError("节点必须返回输出端字典。")
        for port in definition.result_ports(self.nodes[node_id]['params']):
            value = outputs.get(port.key)
            dtype = self.types.get((node_id, True, port.key)) or port.data_type
            if value is None:
                if port is definition.terminal_result:
                    raise GraphError("终点节点没有返回有效的内部结果。")
                continue
            if dtype in {"list", "mapping"}:
                validate_data(value)
            if dtype == "data" and isinstance(value, PythonValue):
                value.value
            valid = ((dtype in {"video", "subtitles", "audio"} and isinstance(value, ResourceRef) and value.data_type == dtype)
                     or dtype == "video" and isinstance(value, (VideoTimeline, VideoStream))
                     or dtype == "subtitles" and isinstance(value, SubtitleTrack)
                     or dtype == "task" and isinstance(value, TaskSnapshot)
                     or dtype == "directory" and isinstance(value, DirectoryRef)
                     or dtype == "files" and isinstance(value, FileList)
                     or dtype == "resource_pool" and isinstance(value, ResourcePool)
                     or dtype == "boolean" and type(value) is bool
                     or dtype == "number" and type(value) in (int, float) and math.isfinite(value)
                     or dtype == "integer" and type(value) is int
                     or dtype == "float" and type(value) is float and math.isfinite(value)
                     or dtype == "color" and isinstance(value, ColorValue)
                     or dtype == "text" and isinstance(value, str)
                     or dtype == "list" and type(value) is list
                     or dtype == "mapping" and type(value) is dict
                     or dtype == "data" and isinstance(value, PythonValue)
                     or dtype == "operations" and isinstance(value, tuple) and all(isinstance(op, FilterOperation) for op in value)
                     or dtype == "render_plan" and isinstance(value, RenderPlan)
                     or dtype == "loop_block" and isinstance(value, LoopBlock)
                     or dtype == "loop_results" and isinstance(value, LoopResults))
            if not valid:
                label = "内部结果" if port is definition.terminal_result else "输出端"
                raise GraphError(label + "“" + port.title + "”数据类型不符合约定。")

    def run_loop(self, end_id, scope):
        if scope.depth >= MAX_LOOP_DEPTH:
            raise ValueError("循环嵌套超过 16 层。")
        begin_id = self.pairs[end_id]
        begin, end = self.nodes[begin_id], self.nodes[end_id]
        bparams, params = begin["params"], end["params"]
        mode = bparams.get("mode", "count")
        bdefinition = self.registry.definitions[BEGIN]
        edefinition = self.registry.definitions[END]
        initial = self.input_value(begin_id, bdefinition.inputs[0], scope)
        items = self.input_value(begin_id, bdefinition.inputs[1], scope)
        count = self.input_value(end_id, next(p for p in edefinition.inputs if p.key == "count"), scope)
        if count is None and self.incoming.get((end_id, "count")):
            raise ValueError("次数输入已连接，但上游没有提供数值；未改用默认次数。")
        count = params.get("count", 10) if count is None else count
        limit = params.get("limit", 1000)
        if type(count) not in (int, float) or not math.isfinite(count) or int(count) != count or not 0 <= count <= 10000:
            raise ValueError("执行次数必须是 0～10000 的整数。")
        if mode == "foreach" and type(items) is not list:
            raise ValueError("逐项遍历的列表输入没有提供有效列表。")
        iterations = len(items) if mode == "foreach" else limit if mode == "while" else int(count)
        if iterations > limit:
            raise ValueError(f"需要 {iterations} 次迭代，超过安全上限 {limit}；请调整 End 上限。")
        result_type = self.types.get((end_id, True, "last"))
        initial_type = self.types.get((begin_id, False, "initial"))
        last = initial if result_type == initial_type else None
        state, collected, byte_count, completed = initial, [], 0, 0
        stop_port = next(p for p in edefinition.inputs if p.key == "stop")
        result_port = next(p for p in edefinition.inputs if p.key == "result")
        gather = params.get("gather", "collect")
        stopped = False
        for iteration in range(iterations):
            self.context.check_canceled()
            if iteration == 0 or (iteration + 1) % 50 == 0:
                self.context.report(f"循环块：第 {iteration + 1} 次 / {iterations}" + ("（条件循环上限）" if mode == "while" else ""), None)
            current = state if bparams.get("feedback", "no") == "yes" else initial
            bound = {"block": LoopBlock(begin_id), "value": current,
                     "item": PythonValue.capture(items[iteration] if mode == "foreach" else None),
                     "iteration": iteration, "index": iteration + 1,
                     "number": bparams.get("start", 0) + iteration * bparams.get("step", 1)}
            values = self.build(begin_id, {}, bound=bound)
            child = Scope(scope, begin_id, values)
            child.cache[begin_id] = values
            last = self.input_value(end_id, result_port, child)
            stop = self.input_value(end_id, stop_port, child)
            if stop is None and self.incoming.get((end_id, "stop")):
                raise ValueError("停止条件已连接，但上游没有提供真／假值。")
            completed = iteration + 1
            state = copy.deepcopy(last) if type(last) in (list, dict) else last
            captured = freeze_result(last)
            if gather == "collect":
                byte_count += result_bytes(captured)
                if byte_count > MAX_COLLECT_BYTES:
                    raise ValueError("循环收集结果超过 8 MB；请改为“仅保留最终结果”或减小循环。未静默截断。")
                collected.append(captured)
            scope.last_passes[end_id] = child
            if stop is True:
                stopped = True
                break
        if mode == "while" and not stopped:
            raise ValueError(f"停止条件始终为假，已达到 {limit} 次安全上限；循环未正常完成。")
        if gather == "last" and completed:
            collected = [freeze_result(last)]
        return {"last": last, "results": LoopResults(result_type, tuple(collected)), "iterations": completed}

"""Structured Begin/End blocks. Feedback is managed by the evaluator, not wires."""
from dataclasses import dataclass, fields, is_dataclass

from .contracts import NodeDefinition, NodeParameter, NodePort
from .python_values import PythonValue, unpack, validate_data

BEGIN = "core.loop_begin"
END = "core.loop_end"
MAX_ITERATIONS = 10000
MAX_COLLECT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LoopBlock:
    begin_id: str


@dataclass(frozen=True)
class LoopResults:
    """Typed, immutable collection; media references are never copied or encoded."""
    data_type: str
    items: tuple

    def to_list(self):
        return validate_data([item.to_list() if isinstance(item, LoopResults) else unpack(item)
                              for item in self.items])


def freeze_result(value):
    return PythonValue.capture(value) if type(value) in (list, dict) else value


def result_bytes(value):
    """Conservative metadata budget, excluding session-owned video buffers."""
    seen, total = set(), 0
    def visit(item):
        nonlocal total
        if total > MAX_COLLECT_BYTES or id(item) in seen:
            return
        if type(item) is str:
            total += len(item.encode("utf-8")) + 64
        elif type(item) in (int, float, bool) or item is None:
            total += 32
        elif isinstance(item, PythonValue):
            total += len(item.data_json.encode("utf-8")) + 128
        elif isinstance(item, tuple) or is_dataclass(item):
            seen.add(id(item))
            total += 128
            children = item if isinstance(item, tuple) else (getattr(item, field.name) for field in fields(item))
            for child in children:
                visit(child)
        else:
            total += 256
    visit(value)
    return total


def paired_blocks(graph):
    """Resolve explicit block links, including transparent reroutes/previews."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    incoming = {}
    for edge in graph["connections"]:
        incoming.setdefault((edge[2], edge[3]), []).append(edge)

    def source(node_id, key, seen):
        if node_id in seen:
            return None
        seen.add(node_id)
        kind = nodes[node_id]["kind"]
        if kind == BEGIN and key == "block":
            return node_id
        if kind in {"core.reroute", "core.preview"} and key == "value":
            links = incoming.get((node_id, "value"), [])
            if len(links) == 1:
                return source(links[0][0], links[0][1], seen)
        return None

    by_end, by_begin, errors = {}, {}, []
    for node in graph["nodes"]:
        if node["kind"] != END:
            continue
        links = incoming.get((node["id"], "block"), [])
        begin = source(links[0][0], links[0][1], set()) if len(links) == 1 else None
        if begin is None:
            errors.append((node["id"], "End 的循环块输入必须连接一个 Begin 的“循环块”输出。"))
            continue
        if begin in by_begin:
            errors.append((node["id"], "一个 Begin 只能配对一个 End；请新建另一组循环块。"))
            errors.append((by_begin[begin], "这个 Begin 已连接多个 End。"))
        by_end[node["id"]] = begin
        by_begin[begin] = node["id"]
    return by_end, by_begin, errors


def loop_dependencies(graph, incoming, pairs):
    """End closes its own iteration scope, while retaining enclosing scopes."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    dependencies, open_ends, active = {}, {}, set()

    def visit(node_id):
        if node_id in dependencies:
            return dependencies[node_id]
        if node_id in active:
            return set()  # The ordinary DAG validator reports physical cycles.
        active.add(node_id)
        result = set()
        for (target, _key), links in incoming.items():
            if target == node_id:
                for edge in links:
                    result.update(visit(edge[0]))
        if nodes[node_id]["kind"] == BEGIN:
            result.add(node_id)
        elif nodes[node_id]["kind"] == END and node_id in pairs:
            open_ends[node_id] = set(result)
            result.discard(pairs[node_id])
        dependencies[node_id] = result
        active.remove(node_id)
        return result

    for node_id in nodes:
        visit(node_id)
    return dependencies, open_ends


def validate_blocks(graph, incoming, order, target, preview=False):
    nodes = {node["id"]: node for node in graph["nodes"]}
    pairs, ends, errors = paired_blocks(graph)
    reachable = set(order)
    errors = [error for error in errors if error[0] in reachable]
    dependencies, open_ends = loop_dependencies(graph, incoming, pairs)
    used_outputs = {(edge[0], edge[1]) for edge in graph["connections"] if edge[2] in reachable}
    for node_id in reachable:
        node = nodes[node_id]
        if node["kind"] == BEGIN:
            if node_id not in ends:
                errors.append((node_id, "Begin 没有配对的 End；循环体必须通过 End 输出。"))
            if node["params"].get("mode", "count") == "foreach" and not incoming.get((node_id, "items")):
                errors.append((node_id, "逐项遍历模式需要连接 Begin 的“列表”输入。"))
            if (node_id, "value") in used_outputs and not incoming.get((node_id, "initial")):
                errors.append((node_id, "使用 Begin 的“当前数据”输出时，必须连接“初始数据”。"))
        elif node["kind"] == END and node_id in pairs:
            begin = nodes[pairs[node_id]]
            for edge in incoming.get((node_id, "count"), []):
                if pairs[node_id] in dependencies.get(edge[0], set()):
                    errors.append((node_id, "次数输入必须来自循环块外，不能依赖本轮数据。"))
            if begin["params"].get("mode", "count") == "while" and not incoming.get((node_id, "stop")):
                errors.append((node_id, "条件循环必须连接 End 的“停止条件”：真时结束，假时继续。"))
            for other, other_begin in pairs.items():
                if (other != node_id and other in reachable
                        and other_begin in open_ends.get(node_id, set())
                        and pairs[node_id] in open_ends.get(other, set())):
                    errors.append((node_id, "循环块交叉引用；嵌套循环必须先经过内层 End，再连接外层 End。"))
    if not preview:
        for begin in dependencies.get(target, set()):
            errors.append((target, "循环体数据越过了 End；请从配对 End 的输出连接后续节点。"))
    return errors


def _managed(_inputs, _params, _context):
    raise RuntimeError("Begin/End 必须由工作流执行器成对执行。")


def _operations(inputs, _params, _context):
    from .contracts import FilterOperation
    values = inputs["results"]
    if values.data_type != "operations" or any(not isinstance(group, tuple) or
            any(not isinstance(item, FilterOperation) for item in group) for group in values.items):
        raise TypeError("只接受循环生成的操作集合，不能把视频或文本当成操作。")
    operations = tuple(item for group in values.items for item in group)
    if len(operations) > MAX_ITERATIONS:
        raise ValueError("合并后的操作超过 10000 个。")
    return {"operations": operations}


def register_loops(registry):
    category = "Python 原生/流程控制/Begin · End 循环块"
    registry.register("core", NodeDefinition(BEGIN, "循环开始 · Begin",
        inputs=(NodePort("initial", "初始数据（按需）", "any", required=False, type_group="state"),
                NodePort("items", "列表（逐项模式）", "list", required=False)),
        outputs=(NodePort("block", "循环块 → End", "loop_block"),
                 NodePort("value", "当前数据", "any", type_group="state"),
                 NodePort("item", "当前项 · Python 数据", "data"),
                 NodePort("iteration", "迭代索引 · 从 0 开始", "number"),
                 NodePort("index", "第几次 · 从 1 开始", "number"),
                 NodePort("number", "起始值 + 索引 × 步长", "number")),
        parameters=(NodeParameter("mode", "循环方式", "choice", "count", choices=(
                        ("count", "按次数 · For"), ("foreach", "逐项遍历 · For Each"),
                        ("while", "条件循环 · 执行后判断"))),
                    NodeParameter("feedback", "当前数据来源", "choice", "no", choices=(
                        ("no", "每轮使用初始数据"), ("yes", "上一轮结果反馈 · 第一轮用初始数据"))),
                    NodeParameter("start", "数值起始值", "number", 0, -1e12, 1e12),
                    NodeParameter("step", "数值步长", "number", 1, -1e12, 1e12)),
        build=_managed, category=category, keywords=("for", "foreach", "while", "Houdini", "循环", "反馈"),
        description="循环块接配对 End；迭代数据只能在块内使用。列表当前项是 Python 数据，可接 int/str/list 等转换节点。反馈不需要拉回环线；条件循环至少执行一轮。"))
    registry.register("core", NodeDefinition(END, "循环结束 · End",
        inputs=(NodePort("block", "配对 Begin 循环块", "loop_block"),
                NodePort("result", "本轮结果", "any", type_group="result"),
                NodePort("stop", "停止条件 · 真时结束", "boolean", required=False),
                NodePort("count", "次数（覆盖参数）", "number", required=False)),
        outputs=(NodePort("last", "最终／最后一轮结果", "any", type_group="result"),
                 NodePort("results", "收集的结果", "loop_results"),
                 NodePort("iterations", "实际执行次数", "number")),
        parameters=(NodeParameter("count", "执行次数（按次数模式）", "integer", 10, 0, MAX_ITERATIONS),
                    NodeParameter("limit", "安全次数上限", "integer", 1000, 1, MAX_ITERATIONS),
                    NodeParameter("gather", "结果保存方式", "choice", "collect", choices=(
                        ("collect", "收集每一轮 · 保持原顺序"), ("last", "仅保留最终结果 · 节省内存")))),
        build=_managed, category=category, keywords=("for", "foreach", "while", "循环", "收集", "反馈"),
        description="统一管理循环次数和停止条件。真时结束；条件循环在本轮完成后判断。超出上限报错，不静默截断；支持后台取消。循环只构建原有数据/视频计划，不生成中间视频。"))
    registry.register("core", NodeDefinition("core.loop_list", "循环结果 → Python 列表",
        inputs=(NodePort("results", "循环结果", "loop_results"),),
        outputs=(NodePort("items", "列表", "list"),), build=lambda i, _p, _c: {"items": i["results"].to_list()},
        category=category, description="把纯数据循环结果转换成普通列表；不将视频对象、文件句柄或操作伪装成 JSON。"))
    registry.register("core", NodeDefinition("core.loop_operations", "循环结果 → 合并视频操作",
        inputs=(NodePort("results", "循环结果", "loop_results"),),
        outputs=(NodePort("operations", "操作集合", "operations"),), build=_operations, category=category,
        description="按迭代顺序合并字幕、图片等原有插件操作，接结算器一次渲染；不重新实现插件业务。"))


def add_loop_pair(graph, registry, position=(0, 0), mode="count"):
    from .graph import new_node, MAX_NODES
    if len(graph["nodes"]) + 2 > MAX_NODES:
        raise ValueError("添加循环块需要两个空余节点位置。")
    begin = new_node(registry.definitions[BEGIN], position)
    end = new_node(registry.definitions[END], (position[0] + 650, position[1]))
    begin["params"]["mode"] = mode
    graph["nodes"].extend((begin, end))
    graph["connections"].append([begin["id"], "block", end["id"], "block"])
    return begin, end


def loop_example_graph(registry, mode="count"):
    from .graph import empty_graph, new_node
    graph = empty_graph()
    begin, end = add_loop_pair(graph, registry, (0, 0), mode)
    begin["params"]["start"] = 1
    end["params"]["count"] = 5
    def node(kind, x, y=0, **params):
        item = new_node(registry.definitions[kind], (x, y))
        item["params"].update(params)
        graph["nodes"].append(item)
        return item["id"]
    def wire(a, out, b, inp):
        graph["connections"].append([a, out, b, inp])
    b, e = begin["id"], end["id"]
    if mode == "foreach":
        source = node("core.py_json", -600, content='["图片一", "图片二", "图片三"]')
        items = node("core.py_list", -300)
        wire(source, "value", items, "value")
        wire(items, "items", b, "items")
        wire(b, "item", e, "result")
    elif mode == "while":
        begin["params"]["feedback"] = "yes"
        initial = node("core.number", -300, value=0)
        one = node("core.number", 0, 500, value=1)
        add = node("core.arithmetic", 300, operation="add")
        threshold = node("core.number", 300, 500, value=5)
        compare = node("core.compare", 650, 500, operation="ge")
        wire(initial, "number", b, "initial")
        wire(b, "value", add, "a")
        wire(one, "number", add, "b")
        wire(add, "number", e, "result")
        wire(add, "number", compare, "a")
        wire(threshold, "number", compare, "b")
        wire(compare, "boolean", e, "stop")
    else:
        square = node("core.arithmetic", 300, operation="multiply")
        wire(b, "number", square, "a")
        wire(b, "number", square, "b")
        wire(square, "number", e, "result")
    listing = node("core.loop_list", 1000)
    box = node("core.py_data", 1300)
    output = node("core.py_output", 1600)
    wire(e, "results", listing, "results")
    wire(listing, "items", box, "value")
    wire(box, "value", output, "value")
    graph["output_node"] = output
    return graph

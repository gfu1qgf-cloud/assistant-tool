"""Typed, zero-copy routing points; no model, media conversion or file writes."""
import math

from .contracts import NodeDefinition, NodePort


ROUTE_TYPES = (("text", "文本"), ("number", "数值"), ("boolean", "真／假"),
               ("directory", "目录"), ("files", "文件列表"), ("video", "视频"),
               ("subtitles", "字幕"), ("audio", "音频"), ("operations", "操作集合"),
               ("render_plan", "视频计划"), ("task", "任务快照（兼容）"))
ROUTE_KINDS = frozenset({"core.reroute"})


def register_reroute(registry):
    registry.register("core", NodeDefinition("core.reroute", "中间接点",
        inputs=(NodePort("value", "数据", "any", type_group="value"),),
        outputs=(NodePort("value", "数据", "any", type_group="value"),),
        build=lambda inputs, _params, _context: {"value": inputs["value"]},
        description="接线决定类型，输入输出必须一致。原样传递数据，不复制、不渲染；Alt 点击连线插入，拖动整理；删除自动恢复连接。"))


def transparent_kind(kind):
    # Only trusted core definitions have transparent data/capability semantics.
    return kind in ROUTE_KINDS or kind == "core.preview" or kind in {
        "core.preview_" + kind for kind, _ in ROUTE_TYPES if kind != "task"}


def insert_reroute(graph, registry, edge, position):
    from .graph import GraphError, MAX_NODES, new_node
    if edge not in graph["connections"]:
        raise GraphError("此连接已改变，请重新点击。")
    if len(graph["nodes"]) >= MAX_NODES or len(graph["connections"]) >= 512:
        raise GraphError("工作流已达到节点／连接数量上限，不能插入接点。")
    if len(position) != 2 or any(type(p) not in (int, float) or not math.isfinite(p)
                                or abs(p) > 100000 for p in position):
        raise GraphError("接点位置无效。")
    nodes = {node["id"]: node for node in graph["nodes"]}
    first = registry.definitions.get(nodes[edge[0]]["kind"])
    second = registry.definitions.get(nodes[edge[2]]["kind"])
    output = next((p for p in first.output_ports(nodes[edge[0]]['params']) if p.key == edge[1]), None) if first else None
    input_ = next((p for p in second.input_ports(nodes[edge[2]]['params']) if p.key == edge[3]), None) if second else None
    from .typing import resolve_port_types
    _types, conflicts = resolve_port_types(graph, registry)
    source_key = (edge[0], True, edge[1])
    if output is None or input_ is None or source_key in conflicts:
        raise GraphError("连接类型无效，不能插入接点。")
    definition = registry.definitions.get("core.reroute")
    if definition is None:
        raise GraphError("中间接点提供者不可用。")
    node = new_node(definition, [round(p, 2) for p in position])
    index = graph["connections"].index(edge)
    # Keep the downstream edge in the original merge-input ordering.
    graph["connections"][index:index + 1] = [
        [edge[0], edge[1], node["id"], "value"],
        [node["id"], "value", edge[2], edge[3]]]
    graph["nodes"].append(node)
    return node


def remove_reroute(graph, node_id):
    """Remove a dot and reconnect its consumers, retaining multi-input ordering."""
    from .graph import GraphError
    inputs = [e for e in graph["connections"] if e[2] == node_id]
    result = []
    for edge in graph["connections"]:
        if edge[2] == node_id:
            continue
        if edge[0] == node_id:
            if len(inputs) != 1:
                continue
            edge = [inputs[0][0], inputs[0][1], edge[2], edge[3]]
        if edge in result:
            # Silently deduplicating would change repeated operations on a Merge.
            raise GraphError("删除接点会产生重复连接；请先断开不需要的分支。")
        result.append(edge)
    graph["connections"] = result
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]

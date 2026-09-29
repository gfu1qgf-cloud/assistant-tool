"""Insert a pasted Fusion tool graph into each clip of a video track.

Selected exposed *image* inputs and one output are spliced into an existing comp.
The original MediaIn/MediaOut tools and their upstream chain are preserved.
"""

import builtins
import collections
import copy
import hashlib
import os
import tempfile


MARKER = "LZX.FusionBatch.Template"
ENDPOINT_TYPES = {"MediaIn", "MediaOut"}


def _ports(tool, direction, data_type="Image"):
    method = tool.GetInputList if direction == "input" else tool.GetOutputList
    id_key = "INPS_ID" if direction == "input" else "OUTS_ID"
    type_key = "INPS_DataType" if direction == "input" else "OUTS_DataType"
    return {
        str(attrs[id_key]): port
        for port in (method() or {}).values()
        for attrs in [port.GetAttrs() or {}]
        if attrs.get(type_key) == data_type and attrs.get(id_key)
    }


def _all_ports(tool, direction):
    method = tool.GetInputList if direction == "input" else tool.GetOutputList
    id_key = "INPS_ID" if direction == "input" else "OUTS_ID"
    return {
        str(attrs[id_key]): port
        for port in (method() or {}).values()
        for attrs in [port.GetAttrs() or {}]
        if attrs.get(id_key)
    }


def _reg_id(tool):
    return str((tool.GetAttrs() or {}).get("TOOLS_RegID") or "")


def _read_settings(api, text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请先粘贴 Fusion 节点文本。")
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("节点文本超过 2 MB，请缩小模板。")
    # bmd.readfile expects a path and uses ordered() for native .setting files.
    if not hasattr(builtins, "OrderedDict"):
        builtins.OrderedDict = collections.OrderedDict
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".setting",
                                     delete=False) as handle:
            handle.write(text)
            path = handle.name
        settings = api.readfile(path)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    tools = settings.get("Tools") if isinstance(settings, dict) else None
    if not isinstance(tools, dict) or not tools:
        raise ValueError("无法识别 Fusion 节点文本：请从 Fusion 节点面板复制节点。")
    nodes = collections.OrderedDict()
    for name, data in tools.items():
        if not isinstance(data, dict) or not isinstance(data.get("__ctor"), str):
            raise ValueError("节点 {} 的设置格式不受支持。".format(name))
        if data["__ctor"] not in ENDPOINT_TYPES:
            nodes[str(name)] = data
    if not nodes:
        raise ValueError("文本里只有 MediaIn/MediaOut，没有可添加的效果节点。")
    return nodes


def _node_without_connections(node):
    result = copy.deepcopy(node)
    for value in (result.get("Inputs") or {}).values():
        if isinstance(value, dict):
            value.pop("SourceOp", None)
            value.pop("Source", None)
    return result


def _references(nodes):
    edges = []
    for target, node in nodes.items():
        for input_id, data in (node.get("Inputs") or {}).items():
            if isinstance(data, dict) and data.get("SourceOp"):
                edges.append((str(data["SourceOp"]), str(data.get("Source") or "Output"),
                              target, str(input_id)))
    return edges


def _make_tools(comp, nodes):
    created = {}
    try:
        for name, node in nodes.items():
            tool = comp.AddTool(node["__ctor"])
            if not tool:
                raise RuntimeError("当前达芬奇缺少 Fusion 节点：{}（{}）".format(name, node["__ctor"]))
            created[name] = tool
            wrapper = {"Tools": collections.OrderedDict([(name, _node_without_connections(node))])}
            if tool.LoadSettings(wrapper) is not True:
                raise RuntimeError("无法加载节点设置：{}".format(name))
        return created
    except Exception:
        _delete_tools(created.values())
        raise


def _delete_tools(tools):
    for tool in reversed(list(tools)):
        try:
            tool.Delete()
        except Exception:
            pass


def _connect_internal(nodes, created):
    for source, output_id, target, input_id in _references(nodes):
        if source not in created:
            if source.startswith("MediaIn") or source.startswith("MediaOut"):
                continue
            raise RuntimeError("节点 {} 引用了模板外的节点 {}。".format(target, source))
        output = _all_ports(created[source], "output").get(output_id)
        input_port = _all_ports(created[target], "input").get(input_id)
        if not output or not input_port or input_port.ConnectTo(output) is not True:
            raise RuntimeError("模板内部连接失败：{}.{}/{}.{}".format(
                source, output_id, target, input_id))


def _graph(api, fusion, text):
    nodes = _read_settings(api, text)
    scratch = fusion.NewComp(True, True, True)
    if not scratch:
        raise RuntimeError("无法创建临时 Fusion 节点预览。")
    try:
        created = _make_tools(scratch, nodes)
        _connect_internal(nodes, created)
        refs = _references(nodes)
        occupied_inputs = {(target, input_id) for source, _, target, input_id in refs
                           if source in created}
        consumed_outputs = {(source, output_id) for source, output_id, target, _ in refs
                            if source in created and target in created}
        inputs = ["{}.{}".format(name, port) for name, tool in created.items()
                  for port in _ports(tool, "input") if (name, port) not in occupied_inputs]
        outputs = ["{}.{}".format(name, port) for name, tool in created.items()
                   for port in _ports(tool, "output") if (name, port) not in consumed_outputs]
        if not inputs or not outputs:
            raise ValueError("模板没有可连接的空闲图像输入端或输出端。")
        internal = ["{}.{} → {}.{}".format(source, out, target, inp)
                    for source, out, target, inp in refs
                    if source in created and target in created]
        allowed_exits = {entry: [output for output in outputs
                                 if _reachable(refs, entry.split(".", 1)[0],
                                               output.split(".", 1)[0])]
                         for entry in inputs}
        return nodes, {"nodes": ["{} ({})".format(name, data["__ctor"])
                                  for name, data in nodes.items()],
                       "inputs": inputs, "outputs": outputs,
                       "internal_links": internal, "allowed_exits": allowed_exits}
    finally:
        scratch.Close()


def _reachable(refs, start, end):
    pending, visited = [start], set()
    while pending:
        name = pending.pop()
        if name == end:
            return True
        if name not in visited:
            visited.add(name)
            pending.extend(target for source, _, target, _ in refs if source == name)
    return False


def _clip_key(item):
    return "{}:{}:{}".format(item.GetStart(), item.GetEnd(), item.GetName())


def _items(timeline, track):
    if track < 1 or track > int(timeline.GetTrackCount("video")):
        raise ValueError("视频轨 {} 不存在。".format(track))
    return sorted(timeline.GetItemListInTrack("video", track) or [],
                  key=lambda item: (item.GetStart(), item.GetEnd(), item.GetName()))


def _existing_chain(comp):
    tools = list((comp.GetToolList(False) or {}).values())
    outputs = [tool for tool in tools if _reg_id(tool) == "MediaOut"]
    if len(outputs) != 1:
        raise RuntimeError("需要恰好一个 MediaOut，当前有 {} 个。".format(len(outputs)))
    media_input = _ports(outputs[0], "input").get("Input")
    if not media_input:
        raise RuntimeError("MediaOut 缺少图像 Input。")
    upstream = media_input.GetConnectedOutput()
    if not upstream:
        raise RuntimeError("MediaOut 未连接原有画面。")
    return media_input, upstream


def _already_applied(comp, digest):
    return any(tool.GetData(MARKER) == digest
               for tool in (comp.GetToolList(False) or {}).values())


def preview(api, resolve, timeline, text, track):
    track = int(track)
    _nodes, graph = _graph(api, resolve.Fusion(), text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    clips = []
    for item in _items(timeline, track):
        try:
            count = int(item.GetFusionCompCount() or 0)
            status = "可应用（将创建 Fusion）" if count == 0 else "可应用"
            if count > 1:
                status = "跳过：有多个 Fusion 合成"
            elif count == 1:
                comp = item.GetFusionCompByIndex(1)
                if not comp:
                    raise RuntimeError("无法读取 Fusion 合成")
                _existing_chain(comp)
                if _already_applied(comp, digest):
                    status = "跳过：该模板已应用"
        except Exception as exc:
            status = "跳过：{}".format(exc)
        clips.append({"name": item.GetName(), "key": _clip_key(item), "status": status})
    return {**graph, "project": str(timeline.GetName() or ""), "track": track,
            "digest": digest,
            "clips": clips}


def _splice(comp, nodes, entries, exit_port, digest):
    media_input, upstream = _existing_chain(comp)
    created = _make_tools(comp, nodes)
    attached = False
    try:
        _connect_internal(nodes, created)
        out_name, out_id = exit_port.rsplit(".", 1)
        output_port = _ports(created[out_name], "output").get(out_id)
        if not output_port:
            raise RuntimeError("选择的模板端口不存在。")
        for entry in entries:
            in_name, in_id = entry.rsplit(".", 1)
            input_port = _ports(created[in_name], "input").get(in_id)
            if not input_port or input_port.ConnectTo(upstream) is not True:
                raise RuntimeError("模板输入端 {} 无法连接原画面。".format(entry))
        if media_input.ConnectTo(output_port) is not True:
            raise RuntimeError("模板输出端无法接回 MediaOut。")
        attached = True
        created[out_name].SetData(MARKER, digest)
    except Exception:
        if attached or media_input.GetConnectedOutput() != upstream:
            media_input.ConnectTo(upstream)
        _delete_tools(created.values())
        raise


def apply(api, resolve, timeline, payload):
    text = str(payload.get("text") or "")
    track = int(payload.get("track") or 0)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != payload.get("digest") or str(timeline.GetName() or "") != payload.get("timeline"):
        raise ValueError("模板或时间线已变化，请重新预览。")
    nodes, graph = _graph(api, resolve.Fusion(), text)
    entries = payload.get("entries") or []
    exit_port = payload.get("exit")
    if (not isinstance(entries, list) or not entries or
            any(entry not in graph["inputs"] for entry in entries) or
            exit_port not in graph["outputs"] or
            not any(exit_port in graph["allowed_exits"].get(entry, []) for entry in entries)):
        raise ValueError("请重新选择有效的输入／输出端口。")
    items = _items(timeline, track)
    if [_clip_key(item) for item in items] != payload.get("clip_keys"):
        raise ValueError("轨道片段已变化，请重新预览，避免应用到错误片段。")
    results = []
    for item in items:
        name = item.GetName()
        try:
            count = int(item.GetFusionCompCount() or 0)
            if count > 1:
                results.append({"name": name, "status": "跳过：存在多个 Fusion 合成"})
                print("[Fusion] {}：跳过多个 Fusion 合成".format(name), flush=True)
                continue
            comp = item.GetFusionCompByIndex(1) if count else item.AddFusionComp()
            if not comp:
                raise RuntimeError("无法创建或读取 Fusion 合成")
            if _already_applied(comp, digest):
                results.append({"name": name, "status": "已应用，跳过"})
                print("[Fusion] {}：已应用，跳过".format(name), flush=True)
                continue
            comp.StartUndo("批量添加 Fusion 节点")
            try:
                comp.Lock()
                try:
                    _splice(comp, nodes, entries, exit_port, digest)
                finally:
                    comp.Unlock()
            finally:
                comp.EndUndo(True)
            results.append({"name": name, "status": "成功"})
            print("[Fusion] {}：成功".format(name), flush=True)
        except Exception as exc:
            results.append({"name": name, "status": "失败：{}".format(exc)})
            print("[Fusion] {}：失败：{}".format(name, exc), flush=True)
    success = sum(row["status"] == "成功" for row in results)
    return {"message": "Fusion 节点已应用 {} / {} 个片段；请检查下方逐项结果。".format(
                success, len(results)), "results": results}

"""Read-only pool adapter using submission data and the existing pool binder."""
import copy
from dataclasses import asdict
import json
from pathlib import Path

from app_plugins.api import NodeDefinition, NodeParameter, NodePort, ResourcePool
from model.node_workflow.contracts import ResourceRef

BUILTIN_POOLS = (("builtin:copy", "文案池", "copy"), ("builtin:image", "图片池", "image"),
                 ("builtin:background", "背景池（视频／图片）", "video"), ("builtin:music", "音乐池", "music"))


def pool_records(data):
    result = []
    for identifier, name, kind in BUILTIN_POOLS:
        key = {"copy": "copy_pool", "image": "image_pool", "video": "backgrounds", "music": "music"}[kind]
        items = data.get(key, [])
        if kind == "video":
            items = [{"id": str(index), "path": path, "name": Path(path).name} for index, path in enumerate(items)]
        result.append({"id": identifier, "name": name, "kind": kind, "items": copy.deepcopy(items)})
    result.extend(copy.deepcopy(data.get("resource_pools", [])))
    return result


def prepare_pool(_task, params, services):
    from .project_runtime import project_runtime
    project = project_runtime(services)
    profile, data = project['name'], project['data']
    pool = next((p for p in pool_records(data) if p["id"] == params.get("pool_id")), None)
    if pool is None:
        raise ValueError("本次运行传入的项目没有这个资源池，请在当前项目中添加或重新选择资源池。")
    # Only the pool contents cross the boundary, never jobs, credentials or Store.
    refs = []
    if pool['kind'] in {'image', 'video', 'visual'}:
        from .matting_assets import snapshot_layers
        frozen = snapshot_layers([dict(kind='image', path='', sequence_items=pool['items'])])[0]
        pool['matting'] = frozen.get('_matting_snapshot', {})
        for record in pool['matting'].values():
            if record and not record.get('error'):
                from .matting_assets import record_files
                for path in record_files(record): refs.append(asdict(ResourceRef.capture(path, 'saved_matte')))
    if pool["kind"] != "copy":
        for item in pool["items"]:
            path = Path(item["path"])
            if path.is_file():
                refs.append(asdict(ResourceRef.capture(path, "pool_file")))
    return {"pool": {"name": profile + " / " + pool["name"], "kind": pool["kind"],
                     "data_json": json.dumps(pool, ensure_ascii=False, allow_nan=False), "files": refs}}


def build_pool(_inputs, _params, context):
    value = copy.deepcopy(context.node_prepared.get("pool"))
    if not isinstance(value, dict):
        raise ValueError("缺少资源池快照，请从主程序重新运行或预览。")
    value["files"] = tuple(ResourceRef(**ref) for ref in value.get("files", []))
    pool = ResourcePool(**value)
    return {"pool": pool, "name": pool.name, "count": len(pool.data["items"])}


def pool_layer(layer, pool, index):
    """Reuse the batch source binder and resolver; select by batch iteration."""
    from .pools import bind_pool_layers
    from .components import resolve_sources
    if not isinstance(pool, ResourcePool):
        raise TypeError("资源池输入必须连接“加载资源池”节点。")
    data = pool.data
    if layer["kind"] == "text" and pool.kind not in {"copy", "text"}:
        raise ValueError("添加文字只能使用文案池或文字资源池。")
    if layer["kind"] == "image" and pool.kind not in {"image", "video", "visual"}:
        raise ValueError("视觉元素只能使用图片、视频或混合视觉资源池。")
    layer = copy.deepcopy(layer)
    layer.update(source="pool", sequence_items=[], sequence_cursor=0,
                 pool_id="" if pool.kind == "copy" or data["id"] == "builtin:image" else "node_pool")
    state = {"copy_pool": data["items"] if pool.kind == "copy" else [],
             "image_pool": data["items"] if data["id"] == "builtin:image" else [],
             "resource_pools": [{**data, "id": "node_pool"}] if layer["pool_id"] else []}
    # Text-file pools are resolved by the same bounded UTF-8 reader as the UI.
    for ref in pool.files:
        if pool.kind == "text": ref.verify()
    bound = bind_pool_layers([layer], state)[0]
    if 'matting' in data: bound['_matting_snapshot'] = data['matting']
    items = bound["sequence_items"]
    if not items:
        raise ValueError(pool.name + "：没有可用内容，请在文案视频资源池中添加。")
    bound["sequence_cursor"] = index % len(items)
    selected = items[bound["sequence_cursor"]]
    ref = None
    if layer["kind"] == "image":
        target = str(Path(selected["path"]).resolve())
        ref = next((item for item in pool.files if item.path == target), None)
        if ref is None:
            raise FileNotFoundError("提交时资源池文件不存在：" + target)
        ref.verify()
    elif pool.kind == "text":
        selected_id = selected["id"]
        target = str(Path(next(item["path"] for item in data["items"] if item["id"] == selected_id)).resolve())
        ref = next(item for item in pool.files if item.path == target)
    resolved, _ = resolve_sources([bound])
    return resolved[0], ref


def pool_node():
    from .workflow_pool_ui import pool_inspector
    return NodeDefinition("batch_text_video.load_pool", "加载资源池",
        outputs=(NodePort("pool", "资源池", "resource_pool"), NodePort("name", "池名称", "text"),
                 NodePort("count", "素材数量", "number")),
        parameters=(NodeParameter("pool_id", "资源池", default="builtin:copy"),),
        build=build_pool, prepare=prepare_pool, inspector_factory=pool_inspector,
        description="项目和池内容由本次运行传入，节点只选择资源池；包括默认池和自建池。"
                    "另存项目自动跟随本次项目，不读取旧路径、不切换配置、不推进原池进度。")

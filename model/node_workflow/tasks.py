"""Host-neutral task snapshots and preparation of independent workflow jobs."""
import copy
from dataclasses import asdict
import json
from pathlib import Path

from .contracts import TaskSnapshot, ResourceRef
from .scope import check_execution_task
from .global_data import WorkflowGlobals


def _plain(value):
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    raise TypeError("任务字段包含无法提交到后台的对象：" + type(value).__name__)


def capture_task(target, table="", language=""):
    task = target["task"]
    fields = _plain(vars(task) if not isinstance(task, dict) else task)
    directory = Path(target["target_dir"]).resolve()
    table = str(Path(table).resolve()) if table else ""
    identity = str(directory).casefold()
    return TaskSnapshot(identity, str(target.get("label") or fields.get("task_id") or directory.name),
                        str(directory), table, fields.get("source_row"),
                        json.dumps(fields, ensure_ascii=False, allow_nan=False), str(language or ""))


class PreparationServices:
    """Freeze each provider's runtime input once per graph submission."""
    def __init__(self,services):
        self._services,self._contexts = services,{}

    def __getattr__(self,name):
        return getattr(self._services,name)

    def workflow_runtime_context(self,name):
        if name not in self._contexts:
            self._contexts[name] = _plain(self._services.workflow_runtime_context(name))
        return copy.deepcopy(self._contexts[name])


def prepare_job(graph, registry, task, services, *, root_id=None, global_data=None):
    from .graph import snapshot, validate
    from .subgraphs import flatten, has_subgraphs
    if callable(getattr(services,'workflow_runtime_context',None)):
        services = PreparationServices(services)
    if has_subgraphs(graph):
        graph, root_id, _ = flatten(graph, registry, root_id)
    frozen = snapshot(graph, registry, root_id=root_id)
    mode = check_execution_task(frozen, task)
    globals_value = WorkflowGlobals.create(mode) if global_data is None else global_data
    if not isinstance(globals_value, WorkflowGlobals) or globals_value.mode != mode:
        raise ValueError("全局数据与工作流的执行模式不一致。")
    _, order, _ = validate(frozen, registry, root_id=root_id)
    prepared, resources = {}, set()
    directories = {Path(task.directory).resolve()} if task is not None else set()
    directories.update(Path(node["params"]["path"]).expanduser().resolve() for node in frozen["nodes"]
                       if node["id"] in order and node["kind"] == "core.directory")
    for node in frozen["nodes"]:
        if node["id"] not in order:
            continue
        callback = registry.definitions[node["kind"]].prepare
        if callback:
            result = callback(task, copy.deepcopy(node["params"]), services)
            # Reject widgets, callbacks, live model objects, credentials brokers, etc.
            result = _plain(result)
            json.dumps(result, allow_nan=False)
            prepared[node["id"]] = result
            resources.update(result.get("resources", []))
            from model.BackgroundTasks import directory_resource
            for root in directories:
                for relative in result.get("relative_writes", []):
                    target = (root / relative).resolve()
                    if target == root or not target.is_relative_to(root):
                        raise ValueError("节点写入目录必须位于输入目录内。")
                    resources.add(directory_resource(target))
    return {"graph": frozen, "task": asdict(task) if task is not None else None,
            "global_data": asdict(globals_value), "prepared": prepared, 'resolved_root': root_id}, tuple(sorted(resources))


def verify_stamp(record, data_type="video"):
    return ResourceRef.capture(record["path"], data_type, [record["size"], record["modified_ns"]])

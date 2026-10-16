"""Explicit workflow scope. No inference from whichever nodes happen to exist."""
from uuid import uuid4
from .contracts import TaskSnapshot
from .global_data import restore_globals

EXECUTION_MODES = (("detail", "Detail · 全局（执行一次）"),
                   ("line", "Line · 每行任务（逐行执行）"))


def execution_mode(graph):
    mode = graph.get("execution_mode", "detail")
    if not isinstance(mode, str) or mode not in {"detail", "line"}:
        raise ValueError("工作流执行模式必须是 Detail（全局）或 Line（每行任务）。")
    return mode


def check_execution_task(graph, task):
    mode = execution_mode(graph)
    if mode == "line" and not isinstance(task, TaskSnapshot):
        raise ValueError("Line 模式需要一行任务数据，请先载入主程序所选任务。")
    if mode == "detail" and task is not None:
        raise ValueError("Detail 是全局执行，不接受当前任务行；逐行处理请选择 Line 模式。")
    return mode


def initialize_execution(payload, context):
    """Each submitted job gets its own scope and row, never a shared mutable row."""
    task = TaskSnapshot(**payload["task"]) if payload.get("task") else None
    mode = check_execution_task(payload["graph"], task)
    globals_value = restore_globals(payload.get("global_data"), mode, is_preview=bool(payload.get("preview_node")))
    context.workflow_mode, context.task = mode, task
    context.workflow_globals = globals_value
    # A host may reuse one background context for several rows/files. Runtime
    # service budgets belong to this execution, not the lifetime of that worker.
    context.workflow_execution_id = uuid4().hex

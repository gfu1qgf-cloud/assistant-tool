"""Immutable, batch-scoped loop values; never a process-wide mutable counter."""
from dataclasses import dataclass, replace
from datetime import datetime
import re
from uuid import uuid4

from .contracts import NodeDefinition, NodePort


GLOBAL_OUTPUTS = (
    ("iteration", "当前循环次数 · 从 1 开始", "number"),
    ("iteration_index", "当前循环索引 · 从 0 开始", "number"),
    ("iteration_count", "本次循环总数", "number"),
    ("is_first", "是否首轮", "boolean"),
    ("is_last", "是否末轮", "boolean"),
    ("mode", "执行模式 · detail / line", "text"),
    ("batch_id", "本次批次 ID", "text"),
    ("started_at", "批次开始时间 · 含时区", "text"),
    ("run_date", "批次日期 · YYYY-MM-DD", "text"),
    ("is_preview", "是否预览执行", "boolean"),
)


@dataclass(frozen=True)
class WorkflowGlobals:
    mode: str
    batch_id: str
    started_at: str
    iteration: int = 1
    iteration_count: int = 1
    is_preview: bool = False

    def __post_init__(self):
        if not isinstance(self.mode, str) or self.mode not in {"detail", "line"}:
            raise ValueError("全局数据的执行模式无效。")
        if (type(self.iteration) is not int or type(self.iteration_count) is not int
                or not 1 <= self.iteration <= self.iteration_count):
            raise ValueError("当前循环次数必须是 1 到循环总数之间的整数。")
        if self.mode == "detail" and (self.iteration != 1 or self.iteration_count != 1):
            raise ValueError("Detail 全局执行一次，循环次数和总数必须为 1。")
        if not isinstance(self.batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.batch_id):
            raise ValueError("全局数据的批次 ID 无效。")
        try:
            timestamp = datetime.fromisoformat(self.started_at)
        except (TypeError, ValueError):
            raise ValueError("全局数据的开始时间必须是带时区的 ISO 时间。") from None
        if timestamp.utcoffset() is None:
            raise ValueError("全局数据的开始时间必须包含时区。")
        if type(self.is_preview) is not bool:
            raise ValueError("全局数据的预览标志必须是真／假。")

    @classmethod
    def create(cls, mode, iteration_count=1, *, is_preview=False):
        return cls(mode, uuid4().hex, datetime.now().astimezone().isoformat(timespec="seconds"),
                   iteration_count=iteration_count, is_preview=is_preview)

    def at_iteration(self, iteration):
        """One independent snapshot per iteration; batch identity/time stay fixed."""
        return replace(self, iteration=iteration)

    def values(self):
        return {"iteration": self.iteration, "iteration_index": self.iteration - 1,
                "iteration_count": self.iteration_count, "is_first": self.iteration == 1,
                "is_last": self.iteration == self.iteration_count, "mode": self.mode,
                "batch_id": self.batch_id, "started_at": self.started_at,
                "run_date": datetime.fromisoformat(self.started_at).date().isoformat(),
                "is_preview": self.is_preview}


def restore_globals(data, mode, *, is_preview=False):
    if data is None:
        return WorkflowGlobals.create(mode, is_preview=is_preview)
    if not isinstance(data, dict):
        raise ValueError("全局数据必须是本次执行的数据快照。")
    value = WorkflowGlobals(**data)
    if value.mode != mode:
        raise ValueError("全局数据与工作流的执行模式不一致。")
    return replace(value, is_preview=is_preview)


def build_global_data(_inputs, _params, context):
    value = getattr(context, "workflow_globals", None)
    if not isinstance(value, WorkflowGlobals) or value.mode != context.workflow_mode:
        raise ValueError("缺少本次执行的全局数据，请通过工作流运行或预览入口执行。")
    return value.values()


def register_global_data(registry):
    registry.register("core", NodeDefinition("core.global_data", "全局数据",
        outputs=tuple(NodePort(key, title, kind) for key, title, kind in GLOBAL_OUTPUTS),
        build=build_global_data,
        description="当前循环次数是本次批次的第几次迭代，不是表格行号，从 1 开始。"
                    "Line 按载入顺序分配 1…N；Detail 为 1/1。预览只看首轮，不推进循环。"
                    "各节点读取同一份只读快照；并发完成顺序、取消或失败不会改动其他项的编号。"
                    "重新执行重新从 1 开始，批次 ID 和开始时间在同批次内一致。"))

"""Immutable upload batches. No widgets, live config or shared task objects."""

from model.AppDataStorage import data_directory, data_io

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskResultError
from model.TaskResultFileGuard import copy_if_ready_and_updated, file_signature
from model.ThreadOutput import capture_thread_output
from model.TaskDataSnapshot import snapshot_task_data


UPLOAD_RESOURCE = "task-delivery:drive-upload"


def upload_work_directory(base_dir):
    """Media snapshots live beside external tasks, never in private VC Temp."""
    root = Path(base_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('上传临时目录的任务根目录不存在：' + str(root))
    return root / '.整理上传临时' / uuid4().hex


def require_snapshot_space(work_dir, files):
    parent = Path(work_dir).resolve()
    while not parent.exists():
        parent = parent.parent
    needed = sum(Path(source).stat().st_size for source in files)
    # Retain room for metadata and the next compressed/processed file. This is
    # a preflight estimate, not a promise that arbitrary node output has a limit.
    reserve = 256 * 1024 * 1024
    if shutil.disk_usage(parent).free < needed + reserve:
        raise OSError(f'任务磁盘空间不足：上传快照约需 {needed/1024**3:.2f} GB，'
                      f'并需保留256 MB余量。临时位置：{work_dir}。未复制视频。')
    from model.MediaStorage import require_media_output
    require_media_output(work_dir)


def version_of(path):
    return list(file_signature(Path(path).stat()))


def merge_pending_batches(config, base_dir, task_dates, batches):
    from model.TaskResultOrganizer import (_PENDING_STATE_LOCK, load_pending_changed_file_batches,
        merge_changed_file_batches, save_pending_changed_file_batches)
    with _PENDING_STATE_LOCK:
        current = load_pending_changed_file_batches(config, base_dir, task_dates)
        return save_pending_changed_file_batches(config, base_dir, task_dates,
                                                 merge_changed_file_batches(current, batches))


def complete_pending_versions(plan, uploaded_records):
    from datetime import date
    from model.TaskResultOrganizer import (_PENDING_STATE_LOCK, file_identity,
        load_pending_changed_file_batches, save_pending_changed_file_batches)
    succeeded = {file_identity(row.get("upload_source_file") or row.get("local_file") or "")
                 for row in uploaded_records}
    versions = {file_identity(item["original"]): item["version"]
                for item in plan["files"].values()
                if file_identity(item["original"]) in succeeded}
    days = [date.fromisoformat(value) for value in plan["task_dates"]]
    with _PENDING_STATE_LOCK:
        current = load_pending_changed_file_batches(plan["config"], plan["base_dir"], days)
        remaining = []
        for root, files in current:
            keep = []
            for path in files:
                expected = versions.get(file_identity(path))
                if expected is None or version_of(path) != expected:
                    keep.append(path)
            if keep:
                remaining.append((root, keep))
        save_pending_changed_file_batches(plan["config"], plan["base_dir"], days, remaining)


def discard_review_skips(config, base_dir, task_dates, batches):
    from model.TaskResultOrganizer import file_identity
    files = {file_identity(path): {"original": str(path), "version": version_of(path)}
             for _root, paths in batches for path in paths}
    plan = {"config": config, "base_dir": str(base_dir), "files": files,
            "task_dates": [day.isoformat() for day in task_dates]}
    complete_pending_versions(plan, [{"local_file": item["original"]} for item in files.values()])


def task_snapshot(task):
    return snapshot_task_data(task)


def snapshot_upload_plan(routed_batches, task_by_file, decisions, config, summary,
                         base_dir, task_dates, work_dir, only_changed):
    from model.TaskResultOrganizer import file_identity, get_upload_batch, task_for_file

    work_dir = Path(work_dir)
    routed_batches = [(root, list(files), prefix) for root, files, prefix in routed_batches]
    require_snapshot_space(work_dir, [source for _root, files, _prefix in routed_batches for source in files])
    data_directory(work_dir).ensure(parents=True, exist_ok=True)
    batch_date, batch_slot = get_upload_batch(config)
    plan = {"config": dict(config), "summary": dict(summary), "batches": [],
            "files": {}, "base_dir": str(base_dir),
            "task_dates": [day.isoformat() for day in task_dates],
            "batch_date": batch_date.isoformat(), "batch_slot": batch_slot,
            "work_dir": str(work_dir), "only_changed": only_changed}
    try:
        for index, (root, files, prefix) in enumerate(routed_batches):
            copied_root = work_dir / str(index)
            copied_files = []
            for source in files:
                source = Path(source)
                relative = source.relative_to(root)
                if ".." in relative.parts:
                    raise ValueError("上传文件不能位于结果目录外")
                target = copied_root / relative
                before = version_of(source)
                copy_if_ready_and_updated(source, target)
                if before != version_of(source):
                    raise OSError(f"准备上传快照时文件已变化：{source.name}；请重新整理")
                copied_files.append(str(target))
                plan["files"][file_identity(target)] = {
                    "original": str(source), "root": str(root), "version": before,
                    "task": task_snapshot(task_for_file(task_by_file, source)),
                    "decision": decisions.get(file_identity(source)),
                }
            if copied_files:
                plan["batches"].append((str(copied_root), copied_files, str(prefix)))
        return plan
    except BaseException:
        data_io.rmtree(work_dir)
        raise


class _LogSignal:
    def __init__(self, context):
        self.context = context

    def emit(self, message):
        self.context.log(message)


def run_delivery_upload(plan, context, *, registry=None):
    from model.GoogleDriveHelper import observe_uploads
    from model.TaskResultOrganizer import _SignalWriter, execute_task_result_upload

    context.report("连接网盘，按提交顺序上传", 0)
    writer = _SignalWriter(_LogSignal(context))
    postprocessor = None
    context.postprocessed_paths = []
    if plan.get('postprocess'):
        from app_plugins.builtin.task_delivery_workflow import processor
        if registry is None:
            raise ValueError('后处理节点提供者不可用，未退回原压缩规则。')
        postprocessor = processor(plan, plan['postprocess'], registry, context)
    with capture_thread_output(writer), observe_uploads(context, len(plan["files"])):
        result = execute_task_result_upload(plan, context=context, postprocessor=postprocessor)
    result['postprocessed_files'] = list(context.postprocessed_paths)
    result['postprocessed_dirs'] = sorted({str(Path(path).parent) for path in context.postprocessed_paths})
    result["paths"] = list(dict.fromkeys(result.get("result_dirs", []) + result['postprocessed_dirs']))
    failures = result.get("upload_failed_files", [])
    if failures and not result.get("cancelled_upload"):
        raise BackgroundTaskResultError(
            f"成功 {result.get('uploaded_file_count', 0)} 个，未成功 {len(failures)} 个；"
            "未成功文件保留在待上传记录中，下次整理可重试。", result)
    if not context.is_canceled():
        context.report(result.get("message", "上传完成"), 100)
    return result


def run_delivery_postprocess(plan, context, *, registry):
    from app_plugins.builtin.task_delivery_workflow import processor
    from model.TaskResultOrganizer import is_video_file
    process = processor(plan, plan['postprocess'], registry, context, local=True)
    paths = []
    for root, files, prefix in plan['batches']:
        for source in files:
            context.check_canceled()
            if is_video_file(Path(source)):
                paths.append(str(process(Path(source), Path(root), prefix)))
    context.report(f'本地后处理完成：{len(paths)} 个视频；未上传、未计数', 100)
    return {'paths': paths, 'processed': len(paths), 'message': '后处理完成；上传关闭，未记录为已上传'}


def run_quick_upload(payload, context):
    from datetime import date
    from app_plugins.builtin.task_delivery_quick_upload import collect_upload_files, upload_quick_files
    from model.GoogleDriveHelper import observe_uploads
    from model.TaskResultOrganizer import _SignalWriter

    context.report("准备简易上传文件快照", 0)
    sources = collect_upload_files(payload["sources"])
    if not sources:
        raise ValueError('没有可上传的文件。')
    # Keep the workspace outside the source folder so an overlapping organizer
    # cannot mistake snapshot copies for new result files.
    work_dir = upload_work_directory(Path(sources[0][0]).parent.parent)
    require_snapshot_space(work_dir, [source for source, _relative in sources])
    data_directory(work_dir.parent).ensure()
    with data_io.TemporaryDirectory(prefix="quick-", dir=str(work_dir.parent)) as temporary:
        prepared = []
        for source, relative in sources:
            context.check_canceled()
            context.report(f"准备上传快照：{Path(source).name}", 0)
            target = Path(temporary) / relative
            copy_if_ready_and_updated(source, target)
            prepared.append((target, relative))
        writer = _SignalWriter(_LogSignal(context))
        with capture_thread_output(writer), observe_uploads(context, len(prepared)):
            result = upload_quick_files([], payload["name"], payload["parent_folder"],
                prepared_plan=prepared, batch=(date.fromisoformat(payload["batch_date"]), payload["batch_slot"]),
                cancelled=context.is_canceled)
    if result["failures"] and not context.is_canceled():
        raise BackgroundTaskResultError(f"简易上传失败 {len(result['failures'])} 个，请重新选择未成功文件上传。", result)
    return result

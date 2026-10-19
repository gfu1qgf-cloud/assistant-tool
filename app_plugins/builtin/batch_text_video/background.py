"""Final exports belong to the host; preview and editing remain interactive."""
import copy
from pathlib import Path
import traceback
import threading

from model.BackgroundTasks import BackgroundTaskResultError, TaskCancellationToken, directory_resource
from .components import resolve_sources
from .engine import Cancelled, RENDERERS
from .store import Store, ProfileStore
from .render_progress import bind


def batch_resources(store, state, ids):
    from .store import file_identity
    jobs = [job for job in state["jobs"] if job["id"] in ids]
    profile = f"batch-profile:{file_identity(store.path)}:{state.get('active_profile', 'default')}"
    return [profile] + [directory_resource(
        job.get("task_dir") or state["settings"]["output_dir"] or Path(job["path"]).parent) for job in jobs]


def validate_batch_outputs(state, ids):
    """Reject two queue rows targeting the same final file before encoding."""
    from .engine import safe_output
    from .store import file_identity
    from .task_naming import output_stem

    jobs = {job['id']: job for job in state['jobs']}
    seen_ids, outputs = set(), {}
    for job_id in ids:
        if job_id in seen_ids:
            raise ValueError('同一批次重复选择了任务，未开始导出。')
        seen_ids.add(job_id)
        job = jobs[job_id]
        folder = job.get('task_dir') or state['settings']['output_dir'] or Path(job['path']).parent
        name = output_stem(job, state['settings'].get('task_naming_rules', {}))
        output = safe_output(folder, name, [job['path']])
        identity = file_identity(output)
        if identity in outputs:
            raise ValueError(f'两条任务会写入同一成品 {output.name}（{outputs[identity]}、{job_id}）；请修改输出名称后重试。')
        outputs[identity] = job_id


def execute_batch(store, state, ids, cancel, progress, message, done, renderers=None, *, node_effects=None, elevenlabs_binding=None):
    state = copy.deepcopy(state)
    from .render_slots import configure
    from .store import file_identity
    from model.MediaEncoding import normalize_export_settings
    from .export_pipeline import pipeline_workers, execute_parallel_batch
    try:
        normalize_export_settings(state['settings'])
        configure(file_identity(store.path), state['settings']['export_workers'], replace=False)
        renderer = (renderers or RENDERERS)[state['settings']['scheme']]
        workers = pipeline_workers(state['settings'], node_effects)
        if workers == 1 or not callable(getattr(renderer, 'prepare_export', None)):
            validate_batch_outputs(state, ids)
    except Exception as error:
        message(traceback.format_exc())
        return {'completed': 0, 'failed': 0, 'cancelled': cancel.is_set(), 'paths': [], 'failures': [],
                'error': '导出配置校验失败，未启动编码：' + str(error)}, state
    if state['settings']['export_workers'] > 1 and workers == 1:
        message('工作流含AI请求／自定义Python，为避免故障重排时重复请求或副作用，本批按单路执行。')
    if workers > 1:
        preparer = getattr(renderer, 'prepare_export', None)
        if (not callable(preparer) or not callable(getattr(store, 'completion_state', None)) or
                node_effects is not None and not callable(getattr(node_effects, 'fork', None))):
            message('当前进程中的渲染实现尚未提供完整并发接口，本次保持单路；新功能在下次正常重启后统一生效。')
        else:
            return execute_parallel_batch(store, state, ids, cancel, progress, message, done, preparer,
                node_effects=node_effects, elevenlabs_binding=elevenlabs_binding)
    completed = failed = 0
    paths, failures = [], []
    error = ""
    try:
        renderer = (renderers or RENDERERS)[state["settings"]["scheme"]]
        for index, job_id in enumerate(ids):
            if cancel.is_set():
                break
            job = next(item for item in state["jobs"] if item["id"] == job_id)
            progress(job_id, 0)
            message(f"[{index+1}/{len(ids)}] {Path(job['path']).name}")
            try:
                if job.get("requires_title") and not str(job.get("title", "")).strip():
                    raise ValueError("表格文案没有标题，请补上第一行标题后再生成。")
                _, sequence_plan = resolve_sources(state["settings"]["layers"], job.get("title", ""), job.get("body", ""), job=job)
                kwargs = {'node_effects': node_effects} if node_effects is not None else {}
                from model.ElevenLabsService import alignment_session
                with alignment_session(elevenlabs_binding):
                    output, plan = renderer(job, state, store.directory, cancel,
                                           bind(progress, job_id), message, **kwargs)
            except Cancelled:
                break
            except Exception as exc:
                if cancel.is_set():
                    break
                if Path(job.get("output") or "__missing__").is_file():
                    message("请检查既有输出，未推进音乐位置。")
                job["status"], job["error"] = "需处理", str(exc)
                state = store.save(state, state["revision"])
                failed += 1
                failures.append(f"{Path(job['path']).name}：{exc}")
                done(job_id, "需处理")
                message(traceback.format_exc())
                continue
            if node_effects is not None:
                sequence_plan.extend(copy.deepcopy(getattr(node_effects, 'sequence_plan', [])))
            state = store.completed(state, job_id, output, plan, sequence_plan=sequence_plan)
            paths.append(str(output))
            progress(job_id, 100)
            completed += 1
            done(job_id, "已完成")
            message("已完成：" + output.name)
    except Exception:
        message(traceback.format_exc())
        error = "记录保存或批处理异常，已停止，请查看程序日志。"
    result = {"completed": completed, "failed": failed, "cancelled": cancel.is_set(),
              "paths": paths, "failures": failures}
    if error:
        result["error"] = error
    return result, state


def run_batch_video_job(payload, context, *, node_registry=None):
    store = ProfileStore(payload["store_dir"], payload['profile_id']) if payload.get('profile_id') else Store(payload["store_dir"])
    state, ids = payload["state"], payload["ids"]
    if store.load()["revision"] != state["revision"]:
        raise ValueError("等待期间生成配置或资源池已改变，请重新打开文案视频并提交；未覆盖记录。")
    positions = {job_id: index for index, job_id in enumerate(ids)}
    percentages = dict.fromkeys(ids, 0)
    progress_lock = threading.Lock()
    finished = set()
    stages = {}
    def item_stage(job_id, text):
        if not context.is_canceled():
            with progress_lock:
                if stages.get(job_id) != text and job_id not in finished:
                    stages[job_id] = text
                    callback = getattr(context, 'report_item_stage', None)
                    if callable(callback): callback(job_id, text)
    def progress(job_id, value):
        if not context.is_canceled():
            with progress_lock:
                percentages[job_id] = max(percentages[job_id], max(0, min(99, int(value))))
                if callable(getattr(context, 'report_item', None)):
                    context.report_item(job_id, percentages[job_id])
                context.report(f"文案视频 · 已结束 {len(finished)}/{len(ids)}",
                               int(sum(percentages.values()) / max(1, len(ids))))
    progress.stage = item_stage
    def done(job_id, _status):
        with progress_lock:
            finished.add(job_id)
            callback = getattr(context, 'report_item_stage', None)
            if callable(callback): callback(job_id, _status)
            percentages[job_id] = 100
            if callable(getattr(context, 'report_item', None)):
                context.report_item(job_id, 100)
            context.report(f"文案视频 · 已结束 {len(finished)}/{len(ids)}",
                           int(sum(percentages.values()) / max(1, len(ids))))
    token = TaskCancellationToken(context)
    effects = None
    if payload.get('node_effects'):
        from .node_effects import EffectRuntime
        if node_registry is None: raise ValueError('缺少已注册的节点实现，未执行节点效果。')
        effects = EffectRuntime(payload['node_effects'], node_registry, token, context.log, context.log)
    result, _ = execute_batch(store, state, ids, token, progress,
                              context.log, done, node_effects=effects,
                              elevenlabs_binding=payload.get('elevenlabs_binding'))
    if result.get("error") or result["failed"]:
        raise BackgroundTaskResultError(result.get("error") or
            f"完成 {result['completed']} 个，失败 {result['failed']} 个；请点击结果核对。", result)
    return result

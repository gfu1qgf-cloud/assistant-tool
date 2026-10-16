"""Bounded encode pipeline with ordered, success-only rotation commits.

Preparation stays on the original worker (Qt fonts/model access); only final
FFmpeg encode+verification runs concurrently. Speculative results never become
visible files until their predecessors have committed. If a reservation fails,
dependent in-flight results are canceled/discarded and prepared against the last
committed state, so neither music nor pool cursors skip failed work.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait
from contextlib import ExitStack
import copy
from pathlib import Path
import threading
import time
import traceback


class ExportToken:
    def __init__(self, parent):
        self.parent, self.local = parent, threading.Event()

    def is_set(self):
        return self.local.is_set() or bool(self.parent and self.parent.is_set())

    def set(self):
        self.local.set()


def pipeline_workers(settings, effects=None):
    workers = settings.get('export_workers', 1)
    if workers <= 1:
        return workers
    if effects is not None:
        # Failed reservation replay must not repeat paid AI calls or arbitrary code.
        kinds = {node['kind'] for graphs in effects.bundle['jobs'].values() for payload in graphs.values()
                 for node in payload['graph']['nodes']}
        if kinds & {'ai_model_client.request', 'core.python_code'}:
            return 1
    return workers


def execute_parallel_batch(store, state, ids, cancel, progress, message, done, preparer,
                           *, node_effects=None, elevenlabs_binding=None):
    from .engine import Cancelled
    state = copy.deepcopy(state)
    speculative = copy.deepcopy(state)
    remaining = deque(enumerate(ids))
    pending = deque()
    outputs = {}
    completed = failed = 0
    paths, failures = [], []
    error = ''
    workers = min(pipeline_workers(state['settings'], node_effects), max(1, len(ids)))
    started = time.monotonic()
    message(f'并发导出：最多{workers}路最终编码；素材分配／字体与节点准备按序，结果及轮换仅成功后按序保存。')

    def fail(job_id, exc):
        nonlocal state, failed
        job = next(item for item in state['jobs'] if item['id'] == job_id)
        job.update(status='需处理', error=str(exc))
        state = store.save(state, state['revision'])
        failed += 1
        failures.append(f"{Path(job['path']).name}：{exc}")
        progress(job_id, 100)
        done(job_id, '需处理')
        message(f"[{job_id}] 导出失败：{exc}\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")

    def drain(entries):
        for entry in entries:
            entry['token'].set()
            entry['future'].cancel()
        waiting = [entry['future'] for entry in entries]
        notified = time.monotonic()
        while waiting:
            _finished, running = wait(waiting, timeout=.1)
            waiting = list(running)
            if waiting and time.monotonic()-notified > 10:
                message('正在安全停止未提交的并发编码；不会强制终止Python线程。')
                notified = time.monotonic()
        for entry in entries:
            entry['scope'].close()

    try:
        if len(ids) != len(set(ids)):
            raise ValueError('同一批次包含重复任务编号，未开始并发导出。')
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='TextVideoEncode') as executor:
            try:
                while remaining or pending:
                    while remaining and len(pending) < workers and not cancel.is_set():
                        index, job_id = remaining.popleft()
                        job = copy.deepcopy(next(item for item in speculative['jobs'] if item['id'] == job_id))
                        token = ExportToken(cancel)
                        scope = ExitStack()
                        report = lambda text, current=job_id: message(f'[{current}] {text}')
                        progress(job_id, 0)
                        message(f"[{index+1}/{len(ids)}] 准备 {Path(job['path']).name}")
                        try:
                            if job.get('requires_title') and not str(job.get('title', '')).strip():
                                raise ValueError('表格文案没有标题，请补上第一行标题后再生成。')
                            effect = node_effects.fork(cancel=token, report=report) if node_effects is not None else None
                            kwargs = {'node_effects': effect} if effect is not None else {}
                            render_state = copy.deepcopy(speculative)
                            render_state['settings']['export_workers'] = workers
                            from model.ElevenLabsService import alignment_session
                            with alignment_session(elevenlabs_binding):
                                prepared = scope.enter_context(preparer(job, render_state, store.directory, token,
                                    lambda value, current=job_id: progress(current, value), report, **kwargs))
                            from .store import file_identity
                            identity = file_identity(prepared.output)
                            if identity in outputs and outputs[identity] != job_id:
                                raise ValueError('本批次多个任务指向同一成品文件，已阻止覆盖；请检查任务文件名。')
                            reservation = store.completion_state(speculative, job_id, prepared.output,
                                prepared.music, prepared.sequence_plan)
                            outputs[identity] = job_id
                            future = executor.submit(prepared.encode)
                            pending.append(dict(index=index, job_id=job_id, token=token, scope=scope,
                                                prepared=prepared, future=future))
                            speculative = reservation
                        except Cancelled:
                            scope.close()
                            if cancel.is_set():
                                break
                            raise
                        except Exception as exc:
                            scope.close()
                            if cancel.is_set():
                                break
                            fail(job_id, exc)
                            if not pending:
                                speculative = copy.deepcopy(state)
                    if cancel.is_set():
                        break
                    if not pending:
                        continue
                    entry = pending[0]
                    try:
                        entry['future'].result(timeout=.1)
                    except TimeoutError:
                        if not entry['future'].done():
                            continue
                        # A renderer's own TimeoutError is a real job failure.
                        exc = entry['future'].exception()
                    except Exception as caught:
                        exc = caught
                    else:
                        exc = None
                    pending.popleft()
                    prepared, job_id = entry['prepared'], entry['job_id']
                    try:
                        if exc is None:
                            # Refuse stale-record replacement before touching an existing result.
                            if store.load()['revision'] != state['revision']:
                                raise ValueError('导出期间项目已被其他窗口修改，未覆盖其记录或未提交的成品。')
                            try:
                                store.completion_state(state, job_id, prepared.output, prepared.music,
                                                       sequence_plan=prepared.sequence_plan)
                                output = prepared.publish()
                            except Exception as caught:
                                exc = caught
                        if exc is not None:
                            if isinstance(exc, Cancelled) and cancel.is_set():
                                break
                            fail(job_id, exc)
                            if prepared.music or prepared.sequence_plan:
                                # Resource-dependent lookahead used this reservation.
                                redo = [(item['index'], item['job_id']) for item in pending]
                                drain(pending)
                                pending.clear()
                                remaining.extendleft(reversed(redo))
                                speculative = copy.deepcopy(state)
                                if redo:
                                    message('失败项未消耗轮换；已撤销后续未提交临时编码，自动按原队列重新分配。')
                            elif not pending:
                                speculative = copy.deepcopy(state)
                            continue
                        state = store.completed(state, job_id, output, prepared.music,
                                                sequence_plan=prepared.sequence_plan)
                        paths.append(str(output))
                        completed += 1
                        progress(job_id, 100)
                        done(job_id, '已完成')
                        message('已完成：'+output.name)
                        if not pending:
                            speculative = copy.deepcopy(state)
                    finally:
                        entry['scope'].close()
            finally:
                drain(pending)
                pending.clear()
    except Cancelled:
        if not cancel.is_set():
            error = '编码意外取消，未提交的任务及轮换已保留。'
    except Exception:
        message(traceback.format_exc())
        error = '记录保存或并发调度异常，已安全停止；请查看日志及已生成成品。'
    result = {'completed': completed, 'failed': failed, 'cancelled': cancel.is_set(),
              'paths': paths, 'failures': failures}
    if error:
        result['error'] = error
    message(f'本批导出总耗时 {time.monotonic()-started:.2f}秒，完成{completed}个，失败{failed}个。')
    return result, state

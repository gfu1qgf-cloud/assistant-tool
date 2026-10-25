"""Bounded encode pipeline with fixed native assignments and independent saves.

Qt/font/model preparation uses one stable thread; music uses bounded read-ahead
workers. The coordinator can publish finished encodes during model waits.
Native sources/music reserve once before encoding, then publish in completion
order. Failed tasks retain their assignment. Node-owned rotations keep the
legacy ordered reservation/replay contract until they provide frozen inputs.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError, wait
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
    paths_by_id = {}
    error = ''
    workers = min(pipeline_workers(state['settings'], node_effects), max(1, len(ids)))
    started = time.monotonic()
    message(f'本批导出：本项目最多{workers}路最终编码；原生任务编码前固定分配、完成即保存，失败重试沿用原分配；节点轮换保留原安全顺序。')

    def fail(job_id, exc, prepared=None):
        nonlocal state, failed
        job = next(item for item in state['jobs'] if item['id'] == job_id)
        job.update(status='需处理', error=str(exc))
        from .render_timing import save_item_timing
        save_item_timing(state, job_id, progress, '需处理', getattr(prepared, 'duration', None))
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
            preparation.cleanup(entry)

    try:
        if len(ids) != len(set(ids)):
            raise ValueError('同一批次包含重复任务编号，未开始并发导出。')
        from .export_preparation import PreparationWorker
        from .music_preparation import MusicPreparationSession
        with MusicPreparationSession(cancel, workers) as music, \
                ThreadPoolExecutor(max_workers=workers, thread_name_prefix='TextVideoEncode') as executor, \
                PreparationWorker(preparer, store.directory, progress, message, node_effects, elevenlabs_binding, music) as preparation:
            try:
                while remaining or pending or preparation.pending:
                    if remaining and preparation.pending is None and len(pending) < workers and not cancel.is_set():
                        index, job_id = remaining.popleft()
                        job = copy.deepcopy(next(item for item in speculative['jobs'] if item['id'] == job_id))
                        token = ExportToken(cancel)
                        progress(job_id, 0)
                        message(f"[{index+1}/{len(ids)}] 准备 {Path(job['path']).name}")
                        # Keep the GLOBAL policy even for a one-video project;
                        # local pipeline width is not the shared encode budget.
                        preparation.start(index, job, copy.deepcopy(speculative), token)
                    if preparation.ready():
                        preparing = preparation.pending
                        job_id, index, token = preparing['job_id'], preparing['index'], preparing['token']
                        entry = None
                        try:
                            entry = preparation.result()
                            prepared = entry['prepared']
                            from .store import file_identity
                            identity = file_identity(prepared.output)
                            if identity in outputs and outputs[identity] != job_id:
                                raise ValueError('本批次多个任务指向同一成品文件，已阻止覆盖；请检查任务文件名。')
                            from .render_assignment import reserve
                            state, reserved = reserve(store, state, job_id, prepared)
                            reservation = copy.deepcopy(state) if reserved else store.completion_state(speculative, job_id, prepared.output,
                                prepared.music, prepared.sequence_plan)
                            outputs[identity] = job_id
                            entry['future'] = executor.submit(prepared.encode)
                            pending.append(entry)
                            speculative = reservation
                        except Cancelled:
                            if entry is not None: preparation.cleanup(entry)
                            if cancel.is_set():
                                break
                            raise
                        except Exception as exc:
                            if entry is not None: preparation.cleanup(entry)
                            if cancel.is_set():
                                break
                            fail(job_id, exc, entry['prepared'] if entry else None)
                            if not pending:
                                speculative = copy.deepcopy(state)
                    if cancel.is_set():
                        break
                    if remaining and preparation.pending is None and len(pending) < workers:
                        continue  # Fill the next preparation slot before waiting for an encode.
                    if not pending:
                        preparation.wait_for()
                        continue
                    # Independent outputs can commit immediately. Resource-based
                    # reservations retain ordered commits and failure rollback.
                    entry = next((item for item in pending if item['future'].done()
                                  and not item['prepared'].music and not item['prepared'].sequence_plan), pending[0])
                    if not entry['future'].done():
                        preparation.wait_for(entry['future'])
                        continue
                    try:
                        entry['future'].result()
                    except TimeoutError:
                        if not entry['future'].done():
                            continue
                        # A renderer's own TimeoutError is a real job failure.
                        exc = entry['future'].exception()
                    except Exception as caught:
                        exc = caught
                    else:
                        exc = None
                    pending.remove(entry)
                    prepared, job_id = entry['prepared'], entry['job_id']
                    try:
                        if exc is None:
                            # Refuse stale-record replacement before touching an existing result.
                            revision = (store.current_revision() if callable(getattr(store, 'current_revision', None))
                                        else store.load()['revision'])
                            if revision != state['revision']:
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
                            fail(job_id, exc, prepared)
                            if prepared.music or prepared.sequence_plan:
                                # Resource-dependent lookahead used this reservation.
                                redo = [(item['index'], item['job_id']) for item in pending]
                                redo.extend(preparation.discard())
                                drain(pending)
                                pending.clear()
                                remaining.extendleft(reversed(redo))
                                speculative = copy.deepcopy(state)
                                if redo:
                                    message('失败项未消耗轮换；已撤销后续未提交临时编码，自动按原队列重新分配。')
                            elif not pending:
                                speculative = copy.deepcopy(state)
                            continue
                        from .render_timing import save_item_timing
                        save_item_timing(state, job_id, progress, '已完成', getattr(prepared, 'duration', None))
                        state = store.completed(state, job_id, output, prepared.music,
                                                sequence_plan=prepared.sequence_plan)
                        paths.append(str(output))
                        paths_by_id[job_id] = str(output)
                        completed += 1
                        progress(job_id, 100)
                        done(job_id, '已完成')
                        message('已完成：'+output.name)
                        if not pending:
                            speculative = copy.deepcopy(state)
                    finally:
                        preparation.cleanup(entry)
            finally:
                preparation.discard()
                drain(pending)
                pending.clear()
    except Cancelled:
        if not cancel.is_set():
            error = '编码意外取消，未提交的任务及轮换已保留。'
    except Exception as exc:
        message(traceback.format_exc())
        error = f'记录保存或并发调度异常，已安全停止：{type(exc).__name__}：{exc}；已完成{completed}个，未提交的轮换已保留。'
    result = {'completed': completed, 'failed': failed, 'cancelled': cancel.is_set(),
              'paths': [paths_by_id[job_id] for job_id in ids if job_id in paths_by_id], 'failures': failures}
    if error:
        result['error'] = error
    message(f'本批导出总耗时 {time.monotonic()-started:.2f}秒，完成{completed}个，失败{failed}个。')
    return result, state

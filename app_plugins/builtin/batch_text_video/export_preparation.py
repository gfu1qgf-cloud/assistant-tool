"""One stable preparation thread, including same-thread context cleanup.

Model waits must not block the coordinator from publishing finished encodes.
QImage/font preparation contexts enter and leave on this thread, not an encoder.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError, FIRST_COMPLETED, wait
from contextlib import ExitStack

from .render_progress import bind, stage


class PreparationWorker:
    def __init__(self, preparer, directory, progress, message, effects, binding, music=None):
        self.preparer, self.directory, self.progress = preparer, directory, progress
        self.message, self.effects, self.binding = message, effects, binding
        self.music = music
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='TextVideoPrepare')
        self.pending = None
        self.cleanups = []

    def __enter__(self):
        return self

    def start(self, index, job, state, token):
        if self.pending is not None:
            raise RuntimeError('上一条准备任务尚未回收。')
        entry = dict(index=index, job_id=job['id'], token=token)
        entry['future'] = self.executor.submit(self.prepare, job, state, token)
        self.pending = entry

    def prepare(self, job, state, token):
        scope = ExitStack()
        progress = bind(self.progress, job['id'])
        report = lambda text: self.message(f'[{job["id"]}] {text}')
        try:
            if job.get('requires_title') and not str(job.get('title', '')).strip():
                raise ValueError('表格文案没有标题，请补上第一行标题后再生成。')
            if token.is_set():
                from .engine import Cancelled
                raise Cancelled('已取消准备。')
            stage(progress, '准备素材')
            effect = self.effects.fork(cancel=token, report=report) if self.effects is not None else None
            kwargs = {'node_effects': effect} if effect is not None else {}
            from model.ElevenLabsService import alignment_session
            from .music_preparation import music_session
            with alignment_session(self.binding), music_session(self.music):
                prepared = scope.enter_context(self.preparer(job, state, self.directory, token, progress, report, **kwargs))
            return {'prepared': prepared, 'scope': scope}
        except BaseException:
            scope.close()
            raise

    def ready(self):
        return self.pending is not None and self.pending['future'].done()

    def result(self):
        entry, self.pending = self.pending, None
        entry.update(entry['future'].result())
        return entry

    def cleanup(self, entry):
        timing = getattr(self.progress, 'timing', None)
        if timing is not None and entry.get('job_id'):
            timing.pause(entry['job_id'])
        self.cleanups.append(self.executor.submit(entry['scope'].close))

    def wait_for(self, future=None):
        futures = ([future] if future is not None else [])
        if self.pending is not None:
            futures.append(self.pending['future'])
        if futures:
            wait(futures, timeout=.1, return_when=FIRST_COMPLETED)

    def discard(self):
        if self.pending is None:
            return []
        entry, self.pending = self.pending, None
        entry['token'].set()
        # Do not cancel the future: it must enter/leave its owned context itself.
        while True:
            try:
                value = entry['future'].result(timeout=.1)
                break
            except TimeoutError:
                if not entry['future'].done():
                    continue
                return [(entry['index'], entry['job_id'])]
            except Exception:
                return [(entry['index'], entry['job_id'])]
        self.cleanup({**entry, **value})
        return [(entry['index'], entry['job_id'])]

    def __exit__(self, *_args):
        try:
            self.discard()
            for future in self.cleanups:
                future.result()
        finally:
            self.executor.shutdown(wait=True)

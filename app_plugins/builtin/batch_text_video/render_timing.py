"""Monotonic worker measurements; overlapping jobs never add up to batch time."""
import copy
import math
import threading
import time

PHASES = {'prepare': '准备', 'wait_encode': '等编码名额', 'encode': '编码',
          'verify': '校验', 'wait_save': '等保存', 'save': '保存', 'replay': '等重排'}


def seconds(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0.


def clock_text(value):
    value = int(seconds(value))
    hours, rest = divmod(value, 3600)
    minutes, sec = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{sec:02d}'


def phase(text):
    if '编码名额' in text: return 'wait_encode'
    if text == '编码中' or text.startswith('编码等待') or 'CPU重试' in text: return 'encode'
    return {'校验成品': 'verify', '等待保存': 'wait_save', '保存成品': 'save', '等待重排': 'replay'}.get(text, 'prepare')


def item_details(value):
    if not value: return '暂无计时记录'
    parts = [f"处理耗时 {seconds(value.get('elapsed_seconds')):.2f}秒"]
    parts.extend(f'{label} {seconds(value.get("phases", {}).get(key)):.2f}秒' for key, label in PHASES.items()
                 if seconds(value.get('phases', {}).get(key)) > 0)
    return '；'.join(parts)


def summary(value):
    elapsed, completed = seconds(value.get('elapsed_seconds')), int(value.get('completed', 0))
    text = f'执行用时 {clock_text(elapsed)} · 已保存 {completed} 条'
    if completed and elapsed > 0:
        text += f' · {completed*60/elapsed:.2f}条/分 · 平均产出 {elapsed/completed:.2f}秒/条'
    duration = seconds(value.get('output_seconds'))
    if duration and elapsed:
        text += f' · 成品时长/用时 {duration/elapsed:.2f}×'
    return text


class BatchTiming:
    def __init__(self, ids, *, clock=time.monotonic):
        self.clock, self.started = clock, clock()
        self.items, self.ids = {}, list(ids)
        self.lock = threading.RLock()

    def _item(self, identifier, now):
        return self.items.setdefault(identifier, dict(start=now, at=now, phase='prepare', phases={}, status='', duration=0.))

    def stage(self, identifier, text):
        with self.lock:
            now = self.clock(); item = self._item(identifier, now)
            if item['status']: return
            name = phase(text)
            if name != item['phase']:
                self._accrue(item, now)
                item['phase'] = name

    def _accrue(self, item, now):
        name = item['phase']
        item['phases'][name] = item['phases'].get(name, 0.)+max(0., now-item['at'])
        item['at'] = now

    def snapshot(self, identifier, status='', duration=None):
        with self.lock:
            now = self.clock(); item = self._item(identifier, now)
            if duration is not None: item['duration'] = seconds(duration)
            phases = copy.copy(item['phases'])
            if not item['status']:
                phases[item['phase']] = phases.get(item['phase'], 0.)+max(0., now-item['at'])
            return dict(elapsed_seconds=max(0., item.get('end', now)-item['start']), phases=phases,
                        status=status or item['status'], duration_seconds=item['duration'])

    def done(self, identifier, status):
        with self.lock:
            now = self.clock(); item = self._item(identifier, now)
            if not item['status']:
                self._accrue(item, now); item.update(status=status, end=now)

    def pause(self, identifier):
        with self.lock:
            if identifier in self.items and not self.items[identifier]['status']:
                self.stage(identifier, '等待重排')

    def result(self, result):
        with self.lock:
            elapsed = max(0., self.clock()-self.started)
            values = {key: self.snapshot(key) for key in self.items}
            duration = sum(value['duration_seconds'] for value in values.values() if value['status'] == '已完成')
            return dict(elapsed_seconds=elapsed, completed=int(result.get('completed', 0)),
                        failed=int(result.get('failed', 0)), output_seconds=duration, items=values)

    def callbacks(self, progress, done):
        def update(identifier, value):
            with self.lock: self._item(identifier, self.clock())
            progress(identifier, value)
        def stage(identifier, text):
            self.stage(identifier, text)
            callback = getattr(progress, 'stage', None)
            if callable(callback): callback(identifier, text)
        def completed(identifier, status):
            self.done(identifier, status); done(identifier, status)
        update.stage, update.timing = stage, self
        return update, completed


def save_item_timing(state, identifier, progress, status, duration=None):
    timing = getattr(progress, 'timing', None)
    if timing is not None:
        job = next(item for item in state['jobs'] if item['id'] == identifier)
        job['render_timing'] = timing.snapshot(identifier, status, duration)

"""One memory-only timer for all projects; updates visible rows once a second."""
import time
from qt_compat import QtCore, QtWidgets
from .render_timing import clock_text, item_details, summary, seconds


class RenderTimingPanel(QtWidgets.QLabel):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner, self.runs = owner, {}
        self.setWordWrap(True)
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setToolTip('执行用时从后台任务开始运行计算，含准备、等待及保存；后台排队单独显示。\n'
            '平均产出=本批执行用时/实际保存数；并发单条耗时有重叠，不能相加当作本批耗时。\n'
            '单条耗时从开始准备到保存成品；悬停耗时列查看阶段明细。计时每秒刷新，不读素材或记录文件。')
        self.timer = QtCore.QTimer(self); self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick); self.timer.start()

    def begin(self, profile, ids, *, queued=True):
        self.runs[profile] = dict(submitted=time.monotonic(), started=None if queued else time.monotonic(),
            ended=None, ids=set(ids), items={}, result=None,
            task_id=getattr(self.owner._host_plans.get(profile), 'task_id', None))
        self.refresh()

    def _activate(self, profile, run):
        if run['started'] is not None: return
        handle = self.owner._host_plans.get(profile)
        record = handle.manager.get(handle.task_id) if handle is not None else {}
        if record.get('started_at'):
            run['started'] = time.monotonic()-max(0., time.time()-record['started_at'])

    def stage(self, profile, identifier, text):
        run = self.runs.get(profile)
        if run is None or run['ended'] is not None: return
        self._activate(profile, run)
        if run['started'] is None: run['started'] = time.monotonic()
        item = run['items'].setdefault(identifier, dict(start=time.monotonic(), end=None, status=''))
        if text in {'已完成', '需处理', '已取消'} and item['end'] is None:
            item.update(end=time.monotonic(), status=text)
            if text == '已完成': run['last_saved'] = item['end']

    def progress(self, profile, identifier, value):
        if value < 100: self.stage(profile, identifier, '')

    def finish(self, profile, result=None):
        run = self.runs.get(profile)
        if run is None: return
        if run['ended'] is None: run['ended'] = time.monotonic()
        if isinstance(result, dict) and isinstance(result.get('timing'), dict):
            run['result'] = result['timing']
        elif run['result'] is None:
            run['result'] = dict(elapsed_seconds=max(0., run['ended']-(run['started'] or run['ended'])),
                completed=sum(item['status'] == '已完成' for item in run['items'].values()), failed=0)
        self.refresh()

    def metrics(self, profile):
        run = self.runs.get(profile)
        if run:
            if run['result'] is not None: return run['result']
            if run['started'] is None: return None
            return dict(elapsed_seconds=max(0., time.monotonic()-run['started']),
                completed=sum(item['status'] == '已完成' for item in run['items'].values()))
        plans = self.owner.render_queue_panel.plans.values() if hasattr(self.owner, 'render_queue_panel') else ()
        return next((plan['render_timing'] for plan in reversed(list(plans))
                     if plan['profile_id'] == profile and plan.get('render_timing')), None)

    def item_value(self, profile, job):
        run = self.runs.get(profile)
        if run and job['id'] in run['ids']:
            if run['result'] is not None and job['id'] in run['result'].get('items', {}):
                return run['result']['items'][job['id']]
            item = run['items'].get(job['id'])
            if item:
                return dict(elapsed_seconds=max(0., (item['end'] or run['ended'] or time.monotonic())-item['start']))
            return None
        return job.get('render_timing')

    def refresh_row(self, row):
        table = self.owner.jobs
        if row < 0 or row >= len(self.owner.state['jobs']) or table.columnCount() < 4: return
        job = self.owner.state['jobs'][row]
        value = self.item_value(self.owner.state['active_profile'], job)
        item = table.item(row, 3)
        if item is None:
            item = QtWidgets.QTableWidgetItem(); table.setItem(row, 3, item)
        item.setText(clock_text(value['elapsed_seconds']) if value else '—')
        item.setToolTip(item_details(value))

    def refresh(self):
        profile = self.owner.state['active_profile']; run = self.runs.get(profile)
        if run and run['ended'] is None: self._activate(profile, run)
        value = self.metrics(profile)
        if run and run['started'] is None and run['ended'] is None:
            self.setText('后台排队 '+clock_text(time.monotonic()-run['submitted'])+' · 尚未开始执行')
        elif value:
            text = ('本批 · ' if run and run['ended'] is None else '上次执行 · ')+summary(value)
            if run and run['ended'] is None:
                text += '\n'+('距上次保存 '+clock_text(time.monotonic()-run['last_saved']) if run.get('last_saved') else '尚未保存首条成品')
            self.setText(text)
        else:
            self.setText('计时：开始渲染后显示执行用时、实际产出速度和单条耗时。')
        table = self.owner.jobs
        first, last = table.rowAt(0), table.rowAt(max(0, table.viewport().height()-1))
        if first >= 0:
            for row in range(first, (last if last >= 0 else min(table.rowCount()-1, first+40))+1): self.refresh_row(row)
        for row in range(self.owner.render_queue_panel.table.rowCount()) if hasattr(self.owner, 'render_queue_panel') else ():
            panel = self.owner.render_queue_panel
            plan = panel.plans.get(panel.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole), {})
            run = self.runs.get(plan.get('profile_id'))
            metrics = (self.metrics(plan['profile_id']) if run and run.get('task_id')
                       and run['task_id'] == plan.get('task_id') else plan.get('render_timing'))
            item = panel.table.item(row, 3)
            if item:
                item.setText(clock_text(metrics['elapsed_seconds']) if metrics else '—')
                item.setToolTip(summary(metrics) if metrics else '尚无计时记录')

    def tick(self):
        if self.isVisible(): self.refresh()

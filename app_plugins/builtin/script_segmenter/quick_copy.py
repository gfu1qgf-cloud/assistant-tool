"""Headless task-menu segmentation; clipboard writes stay on the GUI thread."""
import traceback

from qt_compat import QtCore
from model.ClipboardHelper import set_internal_clipboard_text
from .engine import CONFIG_KEY, format_result, normalize_settings, run_segmentation
from .duration_warning import CopyDurationGuard, duration_check


def run_quick_copy(payload, run):
    results = run_segmentation(payload, run)['entries']
    errors = [f"{item['label']}：{result['error']}" for item,result in zip(payload['entries'],results)
              if 'error' in result]
    if errors:
        raise ValueError('未复制，剪贴板保持原内容。\n'+'\n'.join(errors[:5]))
    blocks, warnings = [], 0
    for item,result in zip(payload['entries'],results):
        run.check_canceled()
        text = format_result(result['result'])
        warnings += result['result']['warning_count']
        blocks.append(f"【任务：{item['label']}】\n{text}" if len(results)>1 else text)
    return {'text':'\n\n'.join(blocks),'count':len(results),'warnings':warnings,
            'duration_checks':[duration_check(result['result'], label=item['label'])
                               for item,result in zip(payload['entries'],results)]}


class QuickCopyController(QtCore.QObject):
    def __init__(self, context):
        super().__init__(context.parent_widget)
        self.context, self.handle, self.active = context, None, True
        self.copy_guard = CopyDurationGuard(self, context.parent_widget)

    def copy_tasks(self, rows):
        if self.handle is not None or self.copy_guard.pending is not None:
            self.context.log('复制分段文案：正在处理上一批，请稍等；未重复提交。')
            return
        try:
            targets = self.context.task_targets(rows)
            if not targets:
                self.context.log('复制分段文案：请先选择任务；剪贴板未改变。')
                return
            entries = [{'key':str(index),'label':str(target['label']),
                        'text':str(getattr(target['task'],'task_audio_text','') or '')}
                       for index,target in enumerate(targets)]
            settings = normalize_settings(self.context.load_config().get(CONFIG_KEY,{}))
            self.handle = self.context.submit_background_task(
                title=f'复制分段文案 · {len(entries)}篇',runner=run_quick_copy,
                payload={'entries':entries,'settings':settings},kind='cpu',retryable=False)
            self.active = True
            self.handle.completed.connect(self.completed)
            self.handle.failed.connect(self.failed)
            self.handle.finished.connect(self.finished)
        except Exception:
            self.context.log('复制分段文案未完成，剪贴板未改变：\n'+traceback.format_exc())

    @QtCore.pyqtSlot(object)
    def completed(self, output):
        if not self.active:
            return
        try:
            def approved():
                if self.active:
                    set_internal_clipboard_text(output['text'])
                    warning = f"；{output['warnings']}段超时需人工核对" if output['warnings'] else ''
                    self.context.log(f"已复制 {output['count']}个任务的分段文案（含段号和建议秒数）{warning}。")
            self.copy_guard.request(output.get('duration_checks', []), approved)
        except Exception:
            self.context.log('复制分段文案失败：\n'+traceback.format_exc())

    @QtCore.pyqtSlot(str)
    def failed(self, message):
        if self.active:
            self.context.log('复制分段文案失败：'+message)

    @QtCore.pyqtSlot()
    def finished(self):
        self.handle = None

    def stop(self):
        self.active = False
        self.copy_guard.cancel()
        if self.handle is not None:
            self.handle.requestInterruption()

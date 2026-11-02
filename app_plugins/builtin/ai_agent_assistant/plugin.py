from qt_compat import QtCore, QtWidgets
from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand, PluginMainWidget
from .store import Store
from .tasks import snapshot, spreadsheet_id
from .sheets import run_export
from model.GoogleAccounts import GoogleAccountManager, DISABLED


class AIAgentAssistantPlugin(QtCore.QObject):
    plugin_id = 'ai_agent_assistant'
    display_name = 'AI Agent 助手'
    version = '0.1.0'
    required_api_version = 1

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.handle = None
        self._store = None

    @property
    def store(self):
        if self._store is None: self._store = Store(self.context.storage.path)
        return self._store

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand('open', 'AI Agent 助手…', lambda _rows: self.open_ui(), frozenset({MAIN_MENU}), order=32))
        context.register_command(PluginCommand('oral', '添加到 AI Agent 口播任务…', self.open_tasks,
            frozenset({TASK_CONTEXT_MENU}), order=35, enabled=lambda rows: bool(rows)))
        context.register_main_widget(PluginMainWidget('quick', self.quick, order=32, title=self.display_name))

    def quick(self, parent=None):
        widget = QtWidgets.QWidget(parent); layout = QtWidgets.QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 0)
        button = QtWidgets.QPushButton('口播任务 · 导入与填表', widget)
        button.clicked.connect(self.open_ui); layout.addWidget(button)
        return widget

    def open_ui(self, *_args):
        try: return self.open_dialog()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self.context.parent_widget, 'AI Agent 助手暂不可用', str(error))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import AssistantDialog
            self.dialog = AssistantDialog(self, self.context.parent_widget)
        self.dialog.refresh_auth()
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
        return self.dialog

    def open_tasks(self, rows):
        try:
            self.open_dialog(); self.import_rows(rows)
        except Exception as error:
            if self.dialog: self.dialog.status.setText(str(error))
            else: QtWidgets.QMessageBox.warning(self.context.parent_widget, 'AI Agent 任务导入失败', str(error))

    def import_rows(self, rows, *, new_version=False):
        return self.import_targets(self.context.task_targets(tuple(rows)), new_version=new_version)

    def import_targets(self, targets, *, new_version=False):
        if self.handle is not None: raise ValueError('请先完成或取消当前提交。')
        if len(targets) > 1000: raise ValueError('每次最多导入1000条，请在主界面分批选择。')
        payloads, skipped, errors = [], 0, []
        for target in targets:
            task = target.get('task')
            if task is None or not str(getattr(task, 'task_audio_text', '') or '').strip(): skipped += 1; continue
            try:
                value = snapshot(target, self.context.task_language(task))
                if self.dialog and self.dialog.chinese_source.isChecked() and not value['chinese_confirmed']:
                    value['chinese_confirmed'] = True
                    value['chinese_source'] = '用户确认：完整任务名称即中文台词'
                payloads.append(value)
            except ValueError as error: errors.append(str(error))
        if not payloads: raise ValueError('没有可导入的口播任务。' + ('；'.join(errors[:3]) if errors else '请先加载并选择包含口播台词的任务。'))
        result = self.store.stage(payloads, new_version=new_version)
        if self.dialog:
            self.dialog.reload()
            self.dialog.status.setText(f'已导入 {len(result["imported"])} 条，已存在 {len(result["unchanged"])} 条，未覆盖 {len(result["blocked"])} 条，跳过无台词 {skipped} 条。' + '；'.join(errors[:3]))
        return result

    def submit(self, target, ids, operation):
        if self.handle is not None: raise ValueError('当前操作尚未结束。')
        jobs = self.store.jobs(ids)
        if not jobs: raise ValueError('请选择任务。')
        self.store.save_settings(target)
        account = GoogleAccountManager().snapshot()['bindings'].get('sheets')
        if not account or account == DISABLED:
            raise ValueError('请先点“Google账号”登录并分配表格账号。本地任务已保留，也可先复制表格。')
        self.handle = self.context.submit_background_task(title='AI Agent · 口播' + ('提交' if operation == 'submit' else '核对'),
            runner=run_export, payload={**target, 'ids': list(ids), 'root': str(self.context.storage.path), 'operation': operation, 'account_id': account},
            kind='network', execution='parallel', resources=['agent-sheet:' + spreadsheet_id(target['url'])], retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed); self.handle.finished.connect(self.finished)
        self.handle.progressChanged.connect(self.progress)
        if self.dialog: self.dialog.busy(True); self.dialog.status.setText('已交由主程序后台执行，可关闭此窗口。')

    def completed(self, result):
        message = result['message']
        self.context.log(message)
        if self.dialog: self.dialog.reload(); self.dialog.status.setText(message)

    def failed(self, message):
        if self.dialog: self.dialog.reload(); self.dialog.status.setText(message)

    def progress(self, message, _percent):
        if self.dialog: self.dialog.status.setText(message)

    def finished(self):
        if self.sender() is not self.handle: return
        self.handle = None
        if self.dialog: self.dialog.reload()

    def can_close(self): return (self.handle is None, 'AI Agent 填表操作仍在结束中，请先完成或取消。')
    def stop(self):
        if self.handle is not None: return False
        if self.dialog: self.dialog.close()
        return True

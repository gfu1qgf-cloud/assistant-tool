"""Documentation-only plugin. Never installed by the production application."""
from app_plugins.api import (
    MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand, PluginMainWidget,
    PluginSettingsPage, PluginTabPage, NodePort, node_function,
)
from qt_compat import QtCore, QtWidgets

CONFIG_KEY = 'developer_example'


@node_function(kind='developer_example.uppercase', title='示例：转大写',
               inputs=(NodePort('text', '文字', 'text'),),
               outputs=(NodePort('result', '结果', 'text'),), category='开发示例')
def uppercase(text):
    """A pure node: no UI, network, files or hidden task inputs."""
    return text.upper()


def count_text(payload, run):
    """Host runner signature is (payload, TaskRunContext)."""
    run.check_canceled()
    run.report('统计文字', 10)
    result = {'characters': len(payload['text'])}
    run.check_canceled()
    run.report('完成', 100)
    return result


class ExampleSettings:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        form = QtWidgets.QFormLayout(self.widget)
        self.prefix = QtWidgets.QLineEdit(self.widget)
        form.addRow('结果前缀', self.prefix)
        hint = QtWidgets.QLabel('这是隔离开发示例，不包含账号、任务或密钥。', self.widget)
        hint.setWordWrap(True)
        form.addRow(hint)

    def load_config(self, config):
        self.prefix.setText(str(config.get(CONFIG_KEY, {}).get('prefix', '统计结果')))

    def validate(self):
        if not self.prefix.text().strip():
            raise ValueError('请填写结果前缀。')

    def update_config(self, config):
        config[CONFIG_KEY] = {'prefix': self.prefix.text().strip()}


class ExampleDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle('开发示例 · 独立窗口')
        self.resize(560, 300)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('输入内容在提交时固定；后台任务不读取此输入框。'))
        self.editor = QtWidgets.QPlainTextEdit(self)
        self.editor.setPlainText('Hello plugin / 你好，插件')
        layout.addWidget(self.editor)
        self.run_button = QtWidgets.QPushButton('提交文字统计', self)
        self.run_button.clicked.connect(self.submit)
        layout.addWidget(self.run_button)
        self.status = QtWidgets.QLabel('尚未提交', self)
        layout.addWidget(self.status)

    def submit(self):
        self.plugin.submit(self.editor.toPlainText())

    def closeEvent(self, event):
        # Close hides the window; host-owned jobs keep running.
        self.hide()
        event.ignore()


class ExamplePlugin(QtCore.QObject):
    plugin_id = 'developer_example'
    display_name = '开发示例'
    version = '1.0'
    required_api_version = 1

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.handle = None
        self.prefix = '统计结果'
        self.last_result = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            'open', '开发示例…', lambda rows: self.open_dialog(), frozenset({MAIN_MENU})))
        context.register_command(PluginCommand(
            'selected', '示例：查看所选任务数量', self.selected,
            frozenset({TASK_CONTEXT_MENU}), enabled=lambda rows: bool(rows)))
        context.register_main_widget(PluginMainWidget('quick', self.make_quick, title='开发示例'))
        context.register_tab_page(PluginTabPage('page', '开发示例分页', self.make_tab))
        context.register_settings_page(PluginSettingsPage('settings', '开发示例', ExampleSettings))
        context.register_function(uppercase)

    def make_quick(self, parent):
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QHBoxLayout(widget)
        layout.addWidget(QtWidgets.QLabel('插件提供内容，宿主管理卡片位置。'))
        button = QtWidgets.QPushButton('打开示例', widget)
        button.clicked.connect(lambda checked=False: self.open_dialog())
        layout.addWidget(button)
        return widget

    def make_tab(self, parent):
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(QtWidgets.QLabel('开发示例分页：内容与生命周期由插件负责。'))
        layout.addStretch()
        return widget

    def open_dialog(self):
        if self.dialog is None:
            self.dialog = ExampleDialog(self, self.context.parent_widget)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def selected(self, rows):
        self.context.log(f'所选任务数量：{len(rows)}')

    def submit(self, text):
        if self.handle is not None and self.handle.isRunning():
            return self.handle
        try:
            handle = self.context.submit_background_task(
                title='开发示例：文字统计', runner=count_text,
                payload={'text': str(text)}, kind='cpu', retryable=True)
        except (RuntimeError, ValueError) as error:
            # A slot must not leak an ordinary admission/configuration error
            # into Qt's event loop (e.g. the host is already shutting down).
            self.on_failed(str(error))
            self.context.log('示例任务未提交：' + str(error))
            return None
        self.handle = handle
        handle.completed.connect(self.on_completed)
        handle.failed.connect(self.on_failed)
        handle.finished.connect(self.on_finished)
        if self.dialog is not None:
            self.dialog.run_button.setEnabled(False)
            self.dialog.status.setText('已交给宿主后台任务')
        return handle

    @QtCore.pyqtSlot(object)
    def on_completed(self, result):
        self.last_result = dict(result)
        if self.dialog is not None:
            self.dialog.status.setText(f'{self.prefix}：{result["characters"]} 字符')

    @QtCore.pyqtSlot(str)
    def on_failed(self, message):
        if self.dialog is not None:
            self.dialog.status.setText('失败：' + message)

    @QtCore.pyqtSlot()
    def on_finished(self):
        if self.sender() is not self.handle:
            return
        self.handle = None
        if self.dialog is not None:
            self.dialog.run_button.setEnabled(True)

    def start(self):
        self.apply_settings(self.context.load_config())

    def apply_settings(self, config):
        self.prefix = str(config.get(CONFIG_KEY, {}).get('prefix', '统计结果'))
        return True

    def can_close(self):
        busy = self.handle is not None and self.handle.isRunning()
        return not busy, '示例后台任务仍在运行。' if busy else ''

    def stop(self):
        if not self.can_close()[0]:
            return False
        if self.dialog is not None:
            self.dialog.hide()
        return True

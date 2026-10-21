"""Browser behavior uses the main program's normal save/cancel transaction."""
from qt_compat import QtWidgets
from .browser_options import CONFIG_KEY, normalize, snapshot


class BrowserOptionsSettings(QtWidgets.QWidget):
    def __init__(self, parent=None, *, preferences=None):
        super().__init__(parent)
        self.widget = self
        self.preferences = preferences or {}
        layout = QtWidgets.QVBoxLayout(self)
        self.focus_before_action = QtWidgets.QCheckBox('网页操作前将浏览器切到前台', self)
        self.focus_before_action.setObjectName('flow_focus_before_action')
        layout.addWidget(self.focus_before_action)
        hint = QtWidgets.QLabel('参数设置、图片上传、生成提交、积分检测和网页下载恢复前，切到该账号的 Flow 窗口。'
            '生成等待、心跳和后台状态查询不切换窗口。多账号生成仍可并行。\n'
            '开启后由主程序调用 Windows 激活对应窗口，并核实实际前台窗口；不会永久置顶。'
            'Windows 拒绝时会在“浏览器与时长分档”的等待原因中显示。关闭后沿用后台操作方式。', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        layout.addWidget(QtWidgets.QLabel('点击程序设置窗口的“确定”保存；“取消”放弃本次修改。', self))
        self.focus_before_action.setChecked(snapshot(self.preferences)['focus_before_action'])

    def load_config(self, config):
        options = normalize(config[CONFIG_KEY]) if CONFIG_KEY in config else snapshot(self.preferences)
        self.focus_before_action.setChecked(options['focus_before_action'])

    def update_config(self, config):
        config[CONFIG_KEY] = {'focus_before_action': self.focus_before_action.isChecked()}
        return config

    def validate(self):
        normalize({'focus_before_action': self.focus_before_action.isChecked()})

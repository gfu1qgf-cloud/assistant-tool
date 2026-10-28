"""One nonblocking consent box; timeout, escape and window close all skip."""
import math
import time

from qt_compat import QtCore, QtWidgets


class HighCreditConfirmation(QtWidgets.QMessageBox):
    shown = QtCore.pyqtSignal(object)
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = dict(request)
        self.expires_at = None
        self.timed_out = False
        self.setWindowTitle('是否使用千分账号？')
        self.setIcon(QtWidgets.QMessageBox.Icon.Question)
        self.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        self.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        self.setEscapeButton(QtWidgets.QMessageBox.StandardButton.No)
        self.button(QtWidgets.QMessageBox.StandardButton.Yes).setText('是，记住并允许使用')
        self.button(QtWidgets.QMessageBox.StandardButton.No).setText('否，跳过此账号')
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_countdown)
        self.finished.connect(self.timer.stop)
        self.update_countdown()

    def paintEvent(self, event):
        super().paintEvent(event)
        # A queued show can precede its first paint by a busy GUI event batch.
        # Give the user the whole interval after the question is first drawn.
        if self.expires_at is None:
            shown_at = time.time()
            self.expires_at = shown_at + self.request.get('seconds', 5)
            self.shown.emit({'action': 'high_credit_prompt_shown', 'nonce': self.request['nonce'], 'shown_at': shown_at})
            self.update_countdown()
            self.timer.start()

    def update_countdown(self):
        remaining = max(0, self.expires_at - time.time()) if self.expires_at is not None else self.request.get('seconds', 5)
        points = self.request.get('credits')
        balance = f'{points:g}' if type(points) in (int, float) else '待检测'
        self.setText(f'{self.request["profile"]}\n{self.request["account"]}\n当前余额：{balance}\n'
            '此账号最近确认余额至少 100 积分，按千分账号处理；低于 100 分将自动降级。\n'
            '小额账号已用完或暂不可用，是否使用此账号？\n'
            '选择“是”会保存使用权限，普通账号仍优先；可右键修改。\n'
            f'{math.ceil(remaining)} 秒内不选择“是”将自动跳过。')
        if remaining <= 0:
            self.timed_out = True
            self.done(int(QtWidgets.QMessageBox.StandardButton.No))

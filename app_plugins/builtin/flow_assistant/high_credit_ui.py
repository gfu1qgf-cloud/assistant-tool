"""One nonblocking consent box; timeout, escape and window close all skip."""
import math
import time

from qt_compat import QtCore, QtWidgets


class HighCreditConfirmation(QtWidgets.QMessageBox):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle('是否使用千分账号？')
        self.setIcon(QtWidgets.QMessageBox.Icon.Question)
        self.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        self.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        self.setEscapeButton(QtWidgets.QMessageBox.StandardButton.No)
        self.button(QtWidgets.QMessageBox.StandardButton.Yes).setText('是，使用此账号')
        self.button(QtWidgets.QMessageBox.StandardButton.No).setText('否，跳过此账号')
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_countdown)
        self.finished.connect(self.timer.stop)
        self.update_countdown()
        self.timer.start()

    def update_countdown(self):
        remaining = max(0, self.request['expires_at'] - time.time())
        points = self.request.get('credits')
        balance = f'{points:g}' if type(points) in (int, float) else '待检测'
        self.setText(f'{self.request["profile"]}\n{self.request["account"]}\n当前余额：{balance}\n'
            '此账号曾检测到至少 100 积分，已标记为千分账号。\n'
            '小额账号已用完或暂不可用，是否使用此账号？\n'
            f'{math.ceil(remaining)} 秒内不选择“是”将自动跳过。')
        if remaining <= 0:
            self.done(int(QtWidgets.QMessageBox.StandardButton.No))

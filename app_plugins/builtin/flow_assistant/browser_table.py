"""Browser columns and display sorting, independent of account scheduling."""
from enum import IntEnum
import re

from qt_compat import QtWidgets


class Column(IntEnum):
    PROFILE = 0
    NAME = 1
    ACCOUNT = 2
    CREDITS = 3
    LANE = 4
    CONNECTION = 5
    PROJECT = 6
    STATE = 7
    REASON = 8


HEADERS = ('Chrome 配置', '配置名称', 'Flow 登录账号', '可用积分／本地估算',
           '时长分档', '扩展连接', '项目页面', '接单状态', '等待原因／下一步')


def natural_key(text):
    return tuple((1, int(part)) if part.isdecimal() else (0, part)
                 for part in re.split(r'(\d+)', str(text).casefold()))


class BrowserItem(QtWidgets.QTableWidgetItem):
    def __init__(self):
        super().__init__()
        self.sort_key = ()

    def __lt__(self, other):
        if isinstance(other, BrowserItem):
            return self.sort_key < other.sort_key
        return super().__lt__(other)

    def update_value(self, text, identifier, number=None, numeric=False):
        primary = ((1, 0) if number is None else (0, number)) if numeric else natural_key(text)
        self.sort_key = (primary, natural_key(identifier))
        if self.text() != text:
            self.setText(text)
        self.setToolTip(text)

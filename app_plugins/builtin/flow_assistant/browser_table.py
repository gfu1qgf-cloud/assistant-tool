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


def row_color(client, preferences, status, jobs=()):
    from .credit_cooldown import credit_hold
    from .credit_policy import balance_ready
    from .high_credit import is_high
    failed = preferences.get('browser_launch_failures',{}).get(client.get('profile'))
    affected = [row for row in jobs if client.get('id') and row.get('browser') == client['id'] or
                client.get('profile') and row.get('remote',{}).get('profile') == client['profile']]
    recovering = any(row.get('status') in {'uncertain','download_failed'} or
        row.get('status') == 'uploading' and row.get('remote',{}).get('transfer_retries',{}).get('upload',{}).get('last_error')
        for row in affected)
    if (failed or recovering or (client.get('reuse_recovery') or {}).get('state') == 'stopped' or status['state'] in {'需处理','账号已隔离'} or
            (client.get('credit_check') or {}).get('state') == 'failed' or
            (client.get('connection_recovery') or {}).get('state') in {'failed','stopped'}): return '#ffd6d6'
    if credit_hold(client,preferences) or balance_ready(client,preferences=preferences) and client['credits'] < 10: return '#fff0a6'
    if is_high(client,preferences): return '#d9eaff'
    if status['state'] in {'执行中','可接单','网页队列收尾中'}: return '#d8f0d8'
    return ''


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

"""Total spoken-duration checks; never add per-video padding to narration."""
import hashlib
import json
import math

from qt_compat import QtCore, QtWidgets


def duration_text(seconds):
    minutes, seconds = divmod(math.ceil(seconds), 60)
    return f'{minutes}分{seconds:02d}秒' if minutes else f'{seconds}秒'


def duration_check(result, indices=None, label=''):
    selected = set(indices) if indices is not None else None
    segments = result.get('segments', [])
    all_selected = selected is None or all(item['index'] in selected for item in segments)
    seconds = (result['estimated_seconds'] if all_selected else
               sum(item['estimated_seconds'] for item in segments if item['index'] in selected))
    limit = result.get('settings', {}).get('total_limit_seconds', 180)
    signature = hashlib.sha256(json.dumps(
        [result.get('source', ''), result.get('settings', {}),
         None if all_selected else sorted(selected), seconds, limit, label],
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return {'seconds': seconds, 'limit': limit, 'signature': signature, 'label': label}


def warning_text(check):
    seconds, limit = check['seconds'], check['limit']
    prefix = (check.get('label', '') + '：') if check.get('label') else ''
    return (f'{prefix}预计朗读{duration_text(seconds)}，超过{duration_text(limit)}上限'
            f'{duration_text(seconds-limit)}。建议精简约{math.ceil((seconds-limit)/seconds*100)}%的内容。')


class CopyDurationGuard(QtCore.QObject):
    """Asynchronous confirmation: no nested modal loop or worker waiting."""
    def __init__(self, owner, parent_widget=None):
        super().__init__(owner)
        self.parent_widget = parent_widget
        self.pending = None
        self.confirmed = set()

    def request(self, checks, approved):
        if self.pending is not None:
            self.pending.raise_()
            self.pending.activateWindow()
            return False
        exceeded = [item for item in checks if item['seconds'] > item['limit']
                    and item['signature'] not in self.confirmed]
        if not exceeded:
            approved()
            return True
        box = QtWidgets.QMessageBox(self.parent_widget)
        box.setWindowTitle('文案超过时长上限')
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText('\n'.join(warning_text(item) for item in exceeded[:8])
                    + (f'\n另有{len(exceeded)-8}篇超时。' if len(exceeded) > 8 else '')
                    + '\n实际配音时长可能有偏差。是否仍然复制？')
        cancel = box.addButton('返回修改／取消复制', QtWidgets.QMessageBox.ButtonRole.RejectRole)
        proceed = box.addButton('仍然复制', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        self.pending = box

        def finished(_result):
            active = self.pending is box
            self.pending = None
            if active and box.clickedButton() is proceed:
                if len(self.confirmed) >= 128:
                    self.confirmed.clear()
                self.confirmed.update(item['signature'] for item in exceeded)
                approved()
            box.deleteLater()

        box.finished.connect(finished)
        box.open()
        return False

    def cancel(self):
        box, self.pending = self.pending, None
        if box is not None:
            box.reject()

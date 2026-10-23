"""Bounded per-material parameter/brush undo, with shortcuts owned by this window."""
from collections import OrderedDict
import copy
import time

from qt_compat import QtCore, QtWidgets
from .matting_assets import identity
from .undo import EditHistory


class MattingUndoKeys(QtCore.QObject):
    def eventFilter(self, watched, event):
        if event.type() not in {QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.ShortcutOverride}: return False
        owner = self.parent()
        if not isinstance(watched, QtWidgets.QWidget) or watched.window() is not owner: return False
        modifiers = event.modifiers()
        if not modifiers & QtCore.Qt.KeyboardModifier.ControlModifier or modifiers & QtCore.Qt.KeyboardModifier.AltModifier: return False
        if event.key() not in {QtCore.Qt.Key.Key_Z, QtCore.Qt.Key.Key_Y}: return False
        event.accept()
        if event.type() == QtCore.QEvent.Type.KeyPress:
            redo = event.key() == QtCore.Qt.Key.Key_Y or bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
            (owner.redo_edit if redo else owner.undo_edit)()
        return True


class MattingHistory:
    def init_matting_history(self):
        self._histories, self._history_merge = OrderedDict(), None
        self.history = EditHistory(limit=60, byte_limit=2*1024*1024)
        self.undo_keys = MattingUndoKeys(self)
        for widget in [self, *self.findChildren(QtWidgets.QWidget)]: widget.installEventFilter(self.undo_keys)

    def select_history(self):
        key = identity(self.path)
        self.history = self._histories.pop(key, None) or EditHistory(limit=60, byte_limit=2*1024*1024)
        self._histories[key] = self.history
        while len(self._histories) > 8: self._histories.popitem(last=False)
        self._history_merge = None

    def record_values(self, before, after, merge=None):
        if before == after or not self.path: return
        now = time.monotonic()
        previous = self._history_merge
        if (merge and previous and previous[0] == merge and now-previous[1] < .5
                and self.history.past and not self.history.future and self.history.past[-1][1] == before):
            before = self.history.past.pop()[0]
        self.history.record(before, after)
        self._history_merge = (merge, now) if merge else None
        self._drafts[identity(self.path)] = copy.deepcopy(after)

    def undo_edit(self, _checked=False): return self._move_history(False)
    def redo_edit(self, _checked=False): return self._move_history(True)

    def _move_history(self, redo):
        if not self.path or self.parameters_locked(): return False
        self._history_merge = None
        if not self._parameters_valid:
            self.set_values(self.values)
            self.queue_preview(self.values)
            return True
        focus = self.focusWidget()
        while focus is not None and focus is not self:
            if isinstance(focus, QtWidgets.QAbstractSpinBox):
                focus.interpretText(); break
            focus = focus.parentWidget()
        if not self._parameters_valid:
            self.set_values(self.values); self.queue_preview(self.values); return True
        moved = (self.history.redo if redo else self.history.undo)(self.restore_values)
        self._history_merge = None
        self.refresh_enabled()
        return moved

    def restore_values(self, values):
        before = self.values
        self.canvas.trace = None
        self.set_values(values)
        self._drafts[identity(self.path)] = copy.deepcopy(self.values)
        self.queue_preview(before)
        return True

"""Qt command history inspired by NodeGraphQt's old/new state command pattern.

Independent implementation with supported Qt6 QUndoStack/QUndoCommand. Plain
compressed graph snapshots never retain QGraphicsItems, workers or callbacks.
"""
import json
import time
import zlib
from qt_compat import QtCore, QtGui


def snapshot(graph):
    data = json.dumps(graph, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return zlib.compress(data, 1)


class GraphChangeCommand(QtGui.QUndoCommand):
    def __init__(self, history, before, after, title, merge_key=None):
        super().__init__(title)
        self.history, self.before, self.after = history, before, after
        self.merge_key = merge_key
        self.last_change = time.monotonic()
        self.first_redo = True

    def id(self):
        return 1 if self.merge_key is not None else -1

    def mergeWith(self, other):
        if (not isinstance(other, GraphChangeCommand) or other.history is not self.history
                or self.merge_key != other.merge_key or other.last_change - self.last_change > .75):
            return False
        self.after, self.last_change = other.after, other.last_change
        if self.before == self.after:
            self.setObsolete(True)
        return True

    def undo(self):
        self.history.apply(self.before)

    def redo(self):
        if self.first_redo:
            self.first_redo = False
        else:
            self.history.apply(self.after)


class GraphHistory(QtCore.QObject):
    def __init__(self, graph, restore, parent=None):
        super().__init__(parent)
        self.stack = QtGui.QUndoStack(self)
        self.stack.setUndoLimit(100)
        self.restore, self.replaying = restore, False
        self.baseline = snapshot(graph)

    def reset(self, graph):
        self.stack.clear()
        self.baseline = snapshot(graph)

    def record(self, graph, title="编辑节点图", merge_key=None):
        if self.replaying:
            return
        after = snapshot(graph)
        if after == self.baseline:
            return
        before, self.baseline = self.baseline, after
        self.stack.push(GraphChangeCommand(self, before, after, title, merge_key))

    def apply(self, value):
        self.replaying = True
        try:
            self.baseline = value
            self.restore(json.loads(zlib.decompress(value)))
        finally:
            self.replaying = False

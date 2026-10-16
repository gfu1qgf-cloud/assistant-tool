"""Layer-local file import and explicit, ID-based list reordering.

The catalog can be reparented away from the property editor. Its drop behavior
must therefore belong to the catalog/list, not to the editor's old parent.
"""
from pathlib import Path
import uuid

from qt_compat import QtCore, QtGui, QtWidgets


def visual_file_paths(mime):
    from .visual_media import VISUAL_SUFFIXES
    urls = mime.urls() if mime.hasUrls() else []
    if not urls or not all(url.isLocalFile() and
            Path(url.toLocalFile()).suffix.casefold() in VISUAL_SUFFIXES for url in urls):
        return []
    return [url.toLocalFile() for url in urls]


def accept_copy(event):
    # Never ask Explorer to move/delete the original media after importing it.
    event.setDropAction(QtCore.Qt.DropAction.CopyAction)
    event.accept()


class VisualDropArea(QtWidgets.QWidget):
    def __init__(self, can_import, import_files, parent=None):
        super().__init__(parent)
        self.can_import, self.import_files = can_import, import_files
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if self.isEnabled() and self.can_import() and visual_file_paths(event.mimeData()):
            accept_copy(event)
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        paths = visual_file_paths(event.mimeData())
        if paths and self.isEnabled() and self.can_import() and self.import_files(paths):
            accept_copy(event)
        else:
            event.ignore()


class LayerList(QtWidgets.QListWidget):
    """One list owns one drag token; drops can reorder but never copy its rows."""
    MIME = 'application/x-lzx-batch-video-layer'

    def __init__(self, can_import, import_files, parent=None):
        super().__init__(parent)
        self.can_import, self.import_files = can_import, import_files
        self._token = uuid.uuid4().hex
        self._drop_row = None
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(False)
        self.setAutoScroll(True)
        self.setAutoScrollMargin(24)

    def mimeData(self, items):
        mime = super().mimeData(items)
        if len(items) == 1:
            identifier = items[0].data(QtCore.Qt.ItemDataRole.UserRole)
            mime.setData(self.MIME, (self._token+'\n'+str(identifier)).encode('utf-8'))
        return mime

    def _source_row(self, mime):
        if not mime.hasFormat(self.MIME):
            return -1
        raw = bytes(mime.data(self.MIME))
        if len(raw) > 4096:
            return -1
        try:
            token, identifier = raw.decode('utf-8').split('\n', 1)
        except (UnicodeError, ValueError):
            return -1
        if token == self._token:
            for row in range(self.count()):
                if str(self.item(row).data(QtCore.Qt.ItemDataRole.UserRole)) == identifier:
                    return row
        return -1

    def startDrag(self, _supported_actions):
        items = self.selectedItems()
        if not self.isEnabled() or len(items) != 1:
            return
        drag = QtGui.QDrag(self)
        drag.setMimeData(self.mimeData(items))
        rect = self.visualItemRect(items[0])
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(QtCore.QPoint(min(40, rect.width()//2), rect.height()//2))
        try:
            # We already move the original row in dropEvent. The default view
            # startDrag can otherwise perform a second source-row removal.
            drag.exec(QtCore.Qt.DropAction.MoveAction)
        finally:
            self._clear_indicator()
            drag.deleteLater()

    def _insertion_row(self, position):
        item = self.itemAt(position)
        if item is None:
            return 0 if position.y() < 0 else self.count()
        rect = self.visualItemRect(item)
        return self.row(item) + (position.y() >= rect.center().y())

    def move_row(self, source, target):
        if not self.isEnabled() or not (0 <= source < self.count() and 0 <= target < self.count()):
            return False
        if source == target:
            return True
        root = QtCore.QModelIndex()
        return self.model().moveRows(root, source, 1, root, target+1 if source < target else target)

    def dragEnterEvent(self, event):
        if self.isEnabled() and self._source_row(event.mimeData()) >= 0:
            super().dragEnterEvent(event)
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
        elif self.isEnabled() and self.can_import() and visual_file_paths(event.mimeData()):
            accept_copy(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.isEnabled() and self._source_row(event.mimeData()) >= 0:
            super().dragMoveEvent(event)  # Retain Qt's edge auto-scroll.
            self._drop_row = self._insertion_row(event.position().toPoint())
            self.viewport().update()
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
        else:
            self._clear_indicator()
            self.dragEnterEvent(event)

    def dropEvent(self, event):
        source = self._source_row(event.mimeData())
        self._clear_indicator()
        if self.isEnabled() and source >= 0:
            slot = self._insertion_row(event.position().toPoint())
            target = slot-1 if source < slot else slot
            if self.move_row(source, target):
                event.setDropAction(QtCore.Qt.DropAction.MoveAction)
                event.accept()
                return
        elif self.isEnabled() and self.can_import():
            paths = visual_file_paths(event.mimeData())
            if paths and self.import_files(paths):
                accept_copy(event)
                return
        event.ignore()

    def _clear_indicator(self):
        self._drop_row = None
        self.viewport().update()

    def dragLeaveEvent(self, event):
        self._clear_indicator()
        super().dragLeaveEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_row is None:
            return
        if self._drop_row < self.count():
            y = self.visualItemRect(self.item(self._drop_row)).top()
        else:
            y = self.visualItemRect(self.item(self.count()-1)).bottom()+1 if self.count() else 1
        y = max(1, min(self.viewport().height()-2, y))
        painter = QtGui.QPainter(self.viewport())
        painter.setPen(QtGui.QPen(self.palette().highlight().color(), 2))
        painter.drawLine(2, y, self.viewport().width()-3, y)

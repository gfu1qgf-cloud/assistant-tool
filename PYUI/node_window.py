"""Independent magnetic window. No docking, reparenting or host-layout edits."""
from qt_compat import QtCore, QtWidgets


class NodeWindow(QtWidgets.QMainWindow):
    attachmentChanged = QtCore.pyqtSignal(bool)
    SNAP_DISTANCE = 26
    GAP = 8

    def __init__(self, main_window):
        super().__init__(main_window, QtCore.Qt.WindowType.Window)
        self.main_window = main_window
        self.setObjectName('node_workflow_window')
        self.setWindowTitle('节点工作流 · 独立窗口')
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.attached = False
        self.anchor = 'right'
        self.offset = 0
        self._moving = False
        self._hidden_for_owner = False
        self.follow_timer = QtCore.QTimer(self)
        self.follow_timer.setSingleShot(True)
        self.follow_timer.timeout.connect(self.follow_owner)
        self.snap_timer = QtCore.QTimer(self)
        self.snap_timer.setSingleShot(True)
        self.snap_timer.timeout.connect(self.snap_after_drag)
        main_window.installEventFilter(self)

    def set_attached(self, attached, side=None, offset=None):
        self.snap_timer.stop()
        if attached:
            main, frame = self.main_window.frameGeometry(), self.frameGeometry()
            if side is None:
                # Prefer the nearest edge; keep the resulting window on screen.
                gaps = {'right': abs(frame.left() - main.right() - self.GAP),
                        'left': abs(main.left() - frame.right() - self.GAP),
                        'bottom': abs(frame.top() - main.bottom() - self.GAP),
                        'top': abs(main.top() - frame.bottom() - self.GAP)}
                side = min(gaps, key=gaps.get)
            self.anchor = side
            self.offset = (frame.top() - main.top() if side in {'right', 'left'} else
                           frame.left() - main.left()) if offset is None else offset
        self.attached = bool(attached)
        self.setWindowTitle('节点工作流 · ' + ('已吸附主窗口' if self.attached else '独立窗口'))
        self.attachmentChanged.emit(self.attached)
        if self.attached:
            self.follow_owner()

    def target_position(self, main, frame):
        if self.anchor == 'right':
            return QtCore.QPoint(main.right() + 1 + self.GAP, main.top() + self.offset)
        if self.anchor == 'left':
            return QtCore.QPoint(main.left() - frame.width() - self.GAP, main.top() + self.offset)
        if self.anchor == 'bottom':
            return QtCore.QPoint(main.left() + self.offset, main.bottom() + 1 + self.GAP)
        return QtCore.QPoint(main.left() + self.offset, main.top() - frame.height() - self.GAP)

    def move_frame(self, target, *, clamp=True):
        frame = self.frameGeometry()
        if clamp:
            screen = QtWidgets.QApplication.screenAt(target) or self.main_window.screen()
            available = screen.availableGeometry()
            target = QtCore.QPoint(
                max(available.left(), min(target.x(), available.right() + 1 - frame.width())),
                max(available.top(), min(target.y(), available.bottom() + 1 - frame.height())))
        self._moving = True
        try:
            self.move(self.pos() + target - frame.topLeft())
        finally:
            self._moving = False

    def follow_owner(self):
        if self.attached and self.isVisible() and not self.main_window.isMinimized():
            self.move_frame(self.target_position(self.main_window.frameGeometry(), self.frameGeometry()))

    def snap_candidate(self):
        main, frame = self.main_window.frameGeometry(), self.frameGeometry()
        overlap_y = min(main.bottom(), frame.bottom()) - max(main.top(), frame.top())
        overlap_x = min(main.right(), frame.right()) - max(main.left(), frame.left())
        candidates = []
        if overlap_y >= 40:
            candidates.extend((('right', abs(frame.left() - main.right() - 1 - self.GAP), frame.top() - main.top()),
                               ('left', abs(main.left() - frame.right() - 1 - self.GAP), frame.top() - main.top())))
        if overlap_x >= 40:
            candidates.extend((('bottom', abs(frame.top() - main.bottom() - 1 - self.GAP), frame.left() - main.left()),
                               ('top', abs(main.top() - frame.bottom() - 1 - self.GAP), frame.left() - main.left())))
        candidate = min(candidates, key=lambda item: item[1], default=None)
        return candidate if candidate and candidate[1] <= self.SNAP_DISTANCE else None

    def snap_after_drag(self):
        if QtWidgets.QApplication.mouseButtons() & QtCore.Qt.MouseButton.LeftButton:
            self.snap_timer.start(120)
            return
        candidate = self.snap_candidate() if self.isVisible() and not self.isMaximized() else None
        if candidate:
            self.set_attached(True, candidate[0], candidate[2])

    def moveEvent(self, event):
        super().moveEvent(event)
        if not hasattr(self, '_moving') or self._moving or not self.isVisible():
            return
        # Manual movement detaches immediately, then snaps only after release.
        if self.attached:
            self.set_attached(False)
        self.snap_timer.start(160)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'follow_timer') and self.attached:
            self.follow_timer.start(0)

    def eventFilter(self, watched, event):
        if watched is self.main_window:
            if event.type() in (QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize):
                if self.attached:
                    self.follow_timer.start(0)
            elif event.type() == QtCore.QEvent.Type.WindowStateChange:
                if self.main_window.isMinimized():
                    self._hidden_for_owner = self.isVisible()
                    self.hide()
                elif self._hidden_for_owner:
                    self._hidden_for_owner = False
                    self.show()
                    self.follow_timer.start(0)
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        # Hiding UI never cancels queued work or destroys the unsaved graph.
        self._hidden_for_owner = False
        self.snap_timer.stop()
        self.hide()
        event.ignore()

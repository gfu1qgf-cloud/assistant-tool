"""Keep wheel gestures for navigation, never for accidental numeric edits."""
from qt_compat import QtCore, QtGui, QtWidgets

_PREFIX = 'app_plugins.builtin.batch_text_video'
_SCOPE = '_batch_text_video_numeric_wheel_scope'


def _protected(widget):
    while widget is not None:
        module = type(widget).__module__
        if widget.property(_SCOPE) or module == _PREFIX or module.startswith(_PREFIX+'.'):
            return True
        widget = widget.parentWidget()
    return False


class NumericWheelGuard(QtCore.QObject):
    def eventFilter(self, watched, event):
        if event.type() != QtCore.QEvent.Type.Wheel or not isinstance(watched, QtWidgets.QWidget):
            return False
        spin = watched
        while spin is not None and not isinstance(spin, QtWidgets.QAbstractSpinBox):
            # Qt may deliver the wheel to the spin's editable child first.
            if spin is not watched and not isinstance(spin, QtWidgets.QLineEdit):
                return False
            spin = spin.parentWidget()
        if spin is None or not _protected(spin): return False
        event.accept()
        parent = spin.parentWidget()
        while parent is not None:
            if isinstance(parent, QtWidgets.QAbstractScrollArea) and parent.isEnabled():
                viewport = parent.viewport()
                position = QtCore.QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
                forwarded = QtGui.QWheelEvent(position, event.globalPosition(), event.pixelDelta(),
                    event.angleDelta(), event.buttons(), event.modifiers(), event.phase(),
                    event.inverted())
                forwarded.ignore()
                before = (parent.horizontalScrollBar().value(), parent.verticalScrollBar().value())
                QtWidgets.QApplication.sendEvent(viewport, forwarded)
                after = (parent.horizontalScrollBar().value(), parent.verticalScrollBar().value())
                if before == after and event.angleDelta().isNull() and not event.pixelDelta().isNull():
                    # Windows styles may ignore a pixel-only touchpad event.
                    parent.horizontalScrollBar().setValue(before[0]-event.pixelDelta().x())
                    parent.verticalScrollBar().setValue(before[1]-event.pixelDelta().y())
                    after = (parent.horizontalScrollBar().value(), parent.verticalScrollBar().value())
                if before != after: break
            parent = parent.parentWidget()
        return True


def protect_numeric_wheels(scope=None):
    app = QtWidgets.QApplication.instance()
    if app is None: return None
    if scope is not None: scope.setProperty(_SCOPE, True)
    guard = getattr(app, '_batch_text_video_numeric_wheel_guard', None)
    if guard is None:
        guard = NumericWheelGuard(app)
        app._batch_text_video_numeric_wheel_guard = guard
        app.installEventFilter(guard)
    return guard

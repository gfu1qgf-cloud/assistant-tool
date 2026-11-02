"""Host-owned result publication; a reference job must outlive its window."""
from functools import partial
from pathlib import Path
import weakref

from qt_compat import QtCore, QtGui


def open_generated_sheets(paths):
    """Called by the host's View Result action on the GUI thread."""
    try:
        paths = tuple(Path(path) for path in paths)
        if not paths or not all(path.is_file() for path in paths): return False
        target = paths[0] if len(paths) == 1 else paths[0].parent
        return bool(QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target))))
    except (OSError, ValueError):
        return False


def _visible(reference):
    try:
        view = reference()
        return view is not None and view.isVisible()
    except RuntimeError:  # Native Qt view may already have been destroyed.
        return False


class ReferenceJobResult(QtCore.QObject):
    def __init__(self, handle, view):
        super().__init__(handle)
        self.handle, self.view = handle, weakref.ref(view)
        handle.completed.connect(self.completed)

    @QtCore.pyqtSlot(object)
    def completed(self, result):
        if self.handle.manager.get(self.handle.task_id)['status'] != 'succeeded': return
        paths = result.get('paths') if isinstance(result, dict) else None
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths): return
        # Only plain paths enter this callback. It never closes over a QWidget.
        self.handle.set_result_handler(partial(open_generated_sheets, tuple(paths)), pending=not _visible(self.view))


def bind_reference_result(handle, view):
    if not hasattr(handle, '_facebook_reference_result'):
        handle._facebook_reference_result = ReferenceJobResult(handle, view)
    return handle._facebook_reference_result

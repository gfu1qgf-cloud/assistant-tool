"""Visible rows only: two decoders, bounded memory, disposable external PNGs.

Models carry path strings. Painting never stats/opens files or constructs a
file-backed QIcon; scrolling replaces the wanted set instead of queuing a pool.
"""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import threading
import time
import uuid
import weakref

from qt_compat import QtCore, QtGui, QtWidgets
from .preview_cache import cache_root

ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 217
MEMORY_LIMIT = 8*1024**2
DISK_LIMIT = 128*1024**2
_prune_lock = threading.Lock()
_last_prune = {}


def descriptor(source='', legacy=''):
    return (str(source or ''), str(legacy or '')) if source or legacy else None


def _prune(root, keep):
    with _prune_lock:
        now = time.monotonic()
        if now-_last_prune.get(str(root), -60) < 60: return
        _last_prune[str(root)] = now
        files = []
        for path in root.glob('*.png'):
            if len(path.stem) != 64 or any(c not in '0123456789abcdef' for c in path.stem) or path.is_symlink(): continue
            stat = path.stat(); files.append((stat.st_mtime_ns, path, stat.st_size))
        total = sum(row[2] for row in files)
        for _, path, size in sorted(files):
            if total <= DISK_LIMIT: break
            if path == keep: continue
            path.unlink(missing_ok=True); total -= size


def decode(payload, cancel):
    """All media/file work runs on the bounded worker pool, never in a Qt paint."""
    if cancel.is_set(): return QtGui.QImage()
    from model.AppDataStorage import application_storage
    storage = application_storage()
    source, legacy = (storage.resolve(p) if p else None for p in payload)
    source = source or legacy
    storage.check(source)
    stamp = source.stat()
    if not source.is_file() or stamp.st_size > 200*1024**2: return QtGui.QImage()
    key = hashlib.sha256(('thumbnail-v2\0'+str(source)+'\0'+str(stamp.st_size)+'\0'+str(stamp.st_mtime_ns)).encode()).hexdigest()
    root = cache_root('thumbnails'); target = root/(key+'.png')
    if not target.is_symlink() and target.is_file() and target.stat().st_size <= 1024**2:
        image = QtGui.QImage(str(target))
        if not image.isNull() and max(image.width(), image.height()) <= 128: return image
    if cancel.is_set(): return QtGui.QImage()
    from .visual_media import is_animated
    if is_animated(source):
        # Existing video imports have no poster file. Decode just one small frame.
        import subprocess
        from .engine import resolve_tools
        ffmpeg, _ = resolve_tools()
        process = subprocess.Popen([ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-i', str(source),
            '-vf', 'scale=128:128:force_original_aspect_ratio=decrease', '-frames:v', '1',
            '-threads', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic()+8
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    process.kill(); process.communicate(); return QtGui.QImage()
                try:
                    output, _ = process.communicate(timeout=.1); break
                except subprocess.TimeoutExpired: pass
            image = QtGui.QImage.fromData(output) if process.returncode == 0 else QtGui.QImage()
        finally:
            if process.poll() is None: process.kill(); process.communicate()
    else:
        # Old private thumbnails are copied lazily; the original remains untouched.
        image = QtGui.QImage()
        if legacy and legacy != source and legacy.is_file():
            storage.check(legacy)
            legacy_stamp = legacy.stat()
            if legacy_stamp.st_size <= 1024**2 and legacy_stamp.st_mtime_ns >= stamp.st_mtime_ns:
                image = QtGui.QImage(str(legacy))
        if image.isNull():
            from .layers import read_image
            image = read_image(source, QtCore.QSize(128, 128))
    if image.isNull() or cancel.is_set(): return QtGui.QImage()
    if max(image.width(), image.height()) > 128:
        image = image.scaled(128, 128, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                             QtCore.Qt.TransformationMode.SmoothTransformation)
    root.mkdir(parents=True, exist_ok=True)
    pending = root/(key+'.'+uuid.uuid4().hex+'.part')
    try:
        if not image.save(str(pending), 'PNG'): return image
        if not cancel.is_set():
            os.replace(pending, target); _prune(root, target)
    except OSError:
        pass  # A full/unavailable disposable cache must not hide a decoded image.
    finally:
        pending.unlink(missing_ok=True)
    return image


class _Decode(QtCore.QRunnable):
    def __init__(self, service, payload, cancel):
        super().__init__(); self.service, self.payload, self.cancel = service, payload, cancel

    def run(self):
        try: image = decode(self.payload, self.cancel)
        except Exception: image = QtGui.QImage()  # An unavailable thumbnail never blocks editing/export.
        self.service.result.emit(self.payload, image, self.cancel)


class ThumbnailService(QtCore.QObject):
    changed = QtCore.pyqtSignal(object)
    result = QtCore.pyqtSignal(object, object, object)

    def __init__(self, parent=None, *, limit=MEMORY_LIMIT):
        super().__init__(parent)
        self.limit, self.bytes = limit, 0
        self.stopped = False
        self.items, self.wanted, self.running, self.owners = OrderedDict(), {}, {}, {}
        self.tried = set()
        self.failures = {}
        self.pool = QtCore.QThreadPool(self); self.pool.setMaxThreadCount(2)
        self.result.connect(self._ready, QtCore.Qt.ConnectionType.QueuedConnection)
        if parent is not None: parent.aboutToQuit.connect(self.stop)

    def stop(self):
        self.stopped = True
        self.wanted.clear()
        for cancel in self.running.values(): cancel.set()

    def icon(self, payload):
        key = tuple(payload) if payload else None
        if key in self.failures and time.monotonic() >= self.failures[key]:
            self.failures.pop(key, None)
            old = self.items.pop(key, None)
            if old: self.bytes -= old[1]
            self.tried.discard(key)
        found = self.items.get(key)
        if found is None: return QtGui.QIcon()
        self.items.move_to_end(tuple(payload)); return found[0]

    def want(self, owner, payloads):
        if payloads: self.wanted[owner] = tuple(dict.fromkeys(payloads))
        else: self.wanted.pop(owner, None)
        desired = {key for rows in self.wanted.values() for key in rows}
        self.tried.intersection_update(desired)
        for key, cancel in self.running.items():
            if key not in desired: cancel.set()
        self._pump()

    def _pump(self):
        if self.stopped: return
        from PyQt6 import sip
        for owner, reference in list(self.owners.items()):
            view = reference()
            if view is None or sip.isdeleted(view):
                self.owners.pop(owner, None); self.wanted.pop(owner, None)
        for rows in self.wanted.values():
            for key in rows:
                if len(self.running) >= 2: return
                if key in self.items or key in self.running or key in self.tried: continue
                self.tried.add(key)
                cancel = threading.Event(); self.running[key] = cancel
                self.pool.start(_Decode(self, key, cancel))

    @QtCore.pyqtSlot(object, object, object)
    def _ready(self, key, image, cancel):
        if self.running.get(key) is not cancel: return
        self.running.pop(key)
        if cancel.is_set(): self.tried.discard(key)
        if not cancel.is_set():
            size = max(64, image.sizeInBytes())
            icon = QtGui.QIcon(QtGui.QPixmap.fromImage(image)) if not image.isNull() else QtGui.QIcon()
            self.items[key] = (icon, size); self.bytes += size
            if image.isNull(): self.failures[key] = time.monotonic()+3
            while (self.bytes > self.limit or len(self.items) > 256) and self.items:
                old_key, (_, old) = self.items.popitem(last=False); self.bytes -= old
                self.failures.pop(old_key, None)
            self.changed.emit(key)
        self._pump()


def service():
    app = QtWidgets.QApplication.instance()
    if not hasattr(app, '_batch_thumbnail_service'):
        app._batch_thumbnail_service = ThumbnailService(app)
    return app._batch_thumbnail_service


class _Delegate(QtWidgets.QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        payload = index.data(ROLE)
        if payload:
            option.icon = service().icon(payload)
            option.features |= QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasDecoration


class VisibleThumbnails(QtCore.QObject):
    def __init__(self, view, *, combo=False):
        super().__init__(view)
        self.view, self.combo, self.owner = view, combo, uuid.uuid4().hex
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(75)
        self.timer.timeout.connect(self.refresh)
        self.service = service(); self.service.changed.connect(self._ready)
        self.service.owners[self.owner] = weakref.ref(view)
        view.installEventFilter(self)
        if combo:
            view.currentIndexChanged.connect(self.schedule)
        else:
            view.viewport().installEventFilter(self)
            view.verticalScrollBar().valueChanged.connect(self.schedule)
            view.horizontalScrollBar().valueChanged.connect(self.schedule)
            view.setItemDelegate(_Delegate(view))
        view.model().dataChanged.connect(self.schedule)
        view.model().modelReset.connect(self.schedule)
        view.model().rowsInserted.connect(self.schedule)
        view.model().rowsRemoved.connect(self.schedule)

    def schedule(self, *_):
        # Trailing debounce also prevents decoding every row during fast scrolling.
        self.service.want(self.owner, ())
        self.timer.start()

    def eventFilter(self, watched, event):
        if event.type() in {QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Paint}:
            if not self.timer.isActive(): self.timer.start()
        elif event.type() == QtCore.QEvent.Type.Hide:
            self.timer.stop(); self.service.want(self.owner, ())
        return False

    def refresh(self):
        view, indexes = self.view, []
        if view.isVisible():
            if self.combo:
                indexes = [view.model().index(view.currentIndex(), view.modelColumn(), view.rootModelIndex())]
            else:
                viewport = view.viewport()
                first = view.indexAt(QtCore.QPoint(3, 1))
                last = view.indexAt(QtCore.QPoint(3, max(1, viewport.height()-1)))
                if first.isValid():
                    end = last.row() if last.isValid() else min(view.model().rowCount()-1, first.row()+viewport.height()//12+2)
                    for row in range(first.row(), end+1):
                        index = view.model().index(row, 0, view.rootIndex())
                        if view.visualRect(index).intersects(viewport.rect()): indexes.append(index)
        payloads = [tuple(index.data(ROLE)) for index in indexes if index.isValid() and index.data(ROLE)]
        self.service.want(self.owner, payloads)

    def _ready(self, key):
        if key in self.service.wanted.get(self.owner, ()):
            (self.view if self.combo else self.view.viewport()).update()


def attach(view):
    view._thumbnails = VisibleThumbnails(view)
    return view._thumbnails


from .component_ui import Combo


class ThumbnailCombo(Combo):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumbnails = VisibleThumbnails(self, combo=True)
        self._thumbnail_view = QtWidgets.QListView(self)
        self._thumbnail_view.setUniformItemSizes(True)
        self.setView(self._thumbnail_view)
        attach(self._thumbnail_view)

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self); option = QtWidgets.QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentIcon = service().icon(self.currentData(ROLE))
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_ComboBoxLabel, option)

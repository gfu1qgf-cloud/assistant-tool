"""Explicit manual network smoke test; never runs in CI unit tests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from types import SimpleNamespace

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.waste_reminder.plugin import WasteReminderPlugin


app = QtWidgets.QApplication([])
QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
app.setFont(QtGui.QFont('Microsoft YaHei', 9))
parent = QtWidgets.QWidget()
root = Path("qa/waste-smoke").resolve()
plugin = WasteReminderPlugin(root)
plugin.register(SimpleNamespace(parent_widget=parent, load_config=lambda: {}, save_config=lambda: True,
    log=lambda message, **_: print(message, flush=True), notify=lambda title, message, **_: print(title, message, flush=True),
    register_command=lambda _: None, register_settings_page=lambda _: None))
plugin._prepare()
dialog = plugin.open_dialog()
plugin.check_official(manual=True)
loop = QtCore.QEventLoop()
plugin.worker.finished.connect(loop.quit)
QtCore.QTimer.singleShot(90000, loop.quit)
loop.exec()
if plugin.is_updating():
    plugin.worker.requestInterruption()
    print("Network test timed out", flush=True)
    plugin.worker.wait(25000)
else:
    dialog.refresh()
    dialog.resize(1000, 780)
    dialog.show()
    app.processEvents()
    root.mkdir(parents=True, exist_ok=True)
    dialog.grab().save(str(root/"dashboard.png"))
    print("Notices:", len(plugin.notices["items"]), "Status:", plugin.status, flush=True)
plugin.stop()

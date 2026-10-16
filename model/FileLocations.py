"""Read-only source-file location opening, shared by Qt views."""
import os
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets


def open_file_location(path, parent=None):
    """Reveal an existing file in Explorer; never build a shell command."""
    title = "打开文件位置"
    if not path:
        QtWidgets.QMessageBox.information(parent, title, "请先选择一个视频片段。")
        return False
    try:
        target = Path(path)
        if not target.is_file():
            QtWidgets.QMessageBox.warning(
                parent, title, f"原文件已移动或不存在：\n{target}\n\n请刷新当前任务并重新选择片段。"
            )
            return False
        target = target.resolve()
        if os.name == "nt":
            started, _pid = QtCore.QProcess.startDetached(
                "explorer.exe", ["/select,", str(target)]
            )
        else:
            started = QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(target.parent))
            )
        if not started:
            QtWidgets.QMessageBox.warning(parent, title, f"无法打开文件管理器：\n{target.parent}")
        return bool(started)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        QtWidgets.QMessageBox.warning(parent, title, f"打开文件位置失败：\n{path}\n\n{error}")
        return False

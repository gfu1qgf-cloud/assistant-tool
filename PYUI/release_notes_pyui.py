"""Local release history viewer; does not call external services."""

from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from app_paths import APP_ROOT


class ReleaseNotesDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, changelog_path=None):
        super().__init__(parent)
        self.setWindowTitle("更新日志")
        self.resize(860, 700)
        self.setMinimumSize(580, 400)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        layout = QtWidgets.QVBoxLayout(self)
        search_row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit(self)
        self.search.setPlaceholderText("查找版本或功能，例如 v1.0.14、智能剪辑")
        search_row.addWidget(self.search, 1)
        next_button = QtWidgets.QPushButton("查找下一处", self)
        search_row.addWidget(next_button)
        layout.addLayout(search_row)
        self.browser = QtWidgets.QTextBrowser(self)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        layout.addWidget(self.browser, 1)
        path = Path(changelog_path) if changelog_path is not None else APP_ROOT / "CHANGELOG.md"
        try:
            self.browser.setMarkdown(path.read_text(encoding="utf-8-sig"))
        except OSError:
            self.browser.setPlainText(
                f"没有找到或无法读取更新日志：\n{path}\n\n请使用完整发布包，保留其中的 CHANGELOG.md。"
            )
        self.search.returnPressed.connect(self.find_next)
        next_button.clicked.connect(self.find_next)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def find_next(self):
        query = self.search.text().strip()
        if query and not self.browser.find(query):
            cursor = self.browser.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
            self.browser.setTextCursor(cursor)
            self.browser.find(query)

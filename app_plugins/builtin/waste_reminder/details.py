import html
import json
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets


def category_data():
    return json.loads((Path(__file__).parent/"data/category_details.json").read_text(encoding="utf-8"))


class CategoryDetailsDialog(QtWidgets.QDialog):
    def __init__(self, plugin, calendar, key, parent=None):
        super().__init__(parent)
        self.plugin, self.calendar = plugin, calendar
        self.data = category_data()
        self.setWindowTitle("垃圾怎么分类？ · 中文详细说明")
        self.resize(760, 680)
        layout = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.category = QtWidgets.QComboBox()
        for name in calendar.types:
            self.category.addItem(calendar.label(name).split(" ", 1)[-1], name)
        self.category.setCurrentIndex(self.category.findData(key))
        self.category.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.category, 1)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("在当前说明里找物品，例如：猫砂、纸盒")
        self.search.textChanged.connect(self.find)
        row.addWidget(self.search, 1)
        layout.addLayout(row)
        self.text = QtWidgets.QTextBrowser()
        self.text.setOpenExternalLinks(False)
        self.text.anchorClicked.connect(self.open_source)
        layout.addWidget(self.text, 1)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.refresh()

    def refresh(self, *_):
        key = self.category.currentData()
        entry = self.data["categories"].get(key) if self.calendar.commune.casefold() == self.data["commune"].casefold() else None
        if not entry:
            self.text.setPlainText("此 Comune/类别尚无经过核实的物品说明，请查看当地官方原件。")
            return
        parts = [f"<h2>{html.escape(self.calendar.label(key).split(' ', 1)[-1])}</h2>",
                 f"<p><b>怎么投放：</b>{html.escape(entry['method'])}</p>"]
        for title, field in (("可以投（官方类别的具体例子）", "yes"),
                             ("不要投入这一类", "no"), ("容易弄错", "confusing")):
            parts.append(f"<h3>{title}</h3><ul>"+"".join(f"<li>{html.escape(x)}</li>" for x in entry[field])+"</ul>")
        parts.append(f"<hr><p>资料核对：{self.data['verified_on']}，适用于 Valle Lomellina。说明不是穷举；官方新公告优先。")
        if self.calendar.year != self.data["year"]:
            parts.append("<b>当前日历年份与说明年份不同，请特别核对新年度规则。</b>")
        parts.append("有疑问先拨 TeknoService：<b>800 681 650</b>，不猜测当地罚款金额。</p>")
        for index, source in enumerate(self.data["sources"]):
            parts.append(f"<p><a href='detail:{index}'>{html.escape(source['title'])}</a></p>")
        if self.plugin.notices.get("items"):
            latest = self.plugin.notices["items"][0]
            title = latest.get("title_zh") or latest.get("zh_subject") or latest.get("title", "")
            parts.append(f"<p><b>最新官方公告：</b>{html.escape(latest['date'])} · {html.escape(title)} "
                         "<a href='detail:news'>查看全文与原文</a></p>")
        self.text.setHtml("".join(parts))
        self.find(self.search.text())

    def find(self, text):
        cursor = self.text.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.text.setTextCursor(cursor)
        if text.strip():
            self.text.find(text.strip())

    def open_source(self, url):
        if url.path() == "news":
            dashboard = self.plugin.open_dialog()
            dashboard.tabs.setCurrentIndex(2)
            dashboard.raise_()
            return
        try:
            source = self.data["sources"][int(url.path())]
            path = (self.plugin.folder/source["file"]).resolve()
            if path.is_relative_to(self.plugin.folder.resolve()) and path.is_file():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(source["url"]))
        except (ValueError, IndexError):
            pass


def bind_category_menu(widget, plugin, calendar, key):
    widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
    widget.setToolTip("右键查看分类详细说明、可投/不可投物品与常见误区")
    def popup(position):
        menu = QtWidgets.QMenu(widget)
        action = menu.addAction("查看分类说明…")
        if menu.exec(widget.mapToGlobal(position)) is action:
            plugin.show_category_details(calendar, key)
    widget.customContextMenuRequested.connect(popup)

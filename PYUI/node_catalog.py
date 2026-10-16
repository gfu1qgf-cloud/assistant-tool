"""Searchable catalogue driven by registry metadata, not plugin imports."""
from qt_compat import QtCore, QtWidgets
from model.node_workflow.graph import OUTPUT_KINDS
from model.node_workflow.preview import is_preview
from model.node_workflow.reroute import ROUTE_KINDS


def node_category(definition):
    kind = definition.kind
    if definition.category:
        return definition.category
    return ("预览" if is_preview(kind) else
        "路径处理" if kind.startswith("core.path_") else
        "处理与导出" if kind in OUTPUT_KINDS | {"core.settle", "core.merge", "core.transport", "core.compress"} else
        "数值与逻辑" if kind in {"core.number", "core.boolean", "core.arithmetic", "core.compare", "core.boolean_math", "core.not", "core.switch"} else
        "文本与转换" if kind == "core.text" or kind.startswith("core.text_") or kind == "core.number_text" else
        "任务与文件" if kind.startswith("core.") else "插件处理/" + kind.split(".")[0])


def visible_nodes(registry):
    return [definition for kind, definition in registry.definitions.items()
            if kind not in ROUTE_KINDS | {'core.subgraph', 'core.subgraph_input', 'core.subgraph_output'}
            and not kind.startswith(("core.preview_", "core.switch_"))]


class NodePicker(QtWidgets.QDialog):
    nodeSelected = QtCore.pyqtSignal(str)

    def __init__(self, registry, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self.setWindowTitle("查找节点")
        self.resize(570, 430)
        layout = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索中文名称、Python 函数名或插件名…")
        layout.addWidget(self.search)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["节点", "分类"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 290)
        layout.addWidget(self.tree, 1)
        self.caption = QtWidgets.QLabel()
        layout.addWidget(self.caption)
        self.entries = []
        for definition in visible_nodes(registry):
            category = node_category(definition)
            item = QtWidgets.QTreeWidgetItem(self.tree, [definition.title, category.replace("/", " › ")])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, definition.kind)
            item.setToolTip(0, definition.description)
            terms = " ".join((definition.title, definition.kind, category, definition.description, *definition.keywords)).casefold()
            self.entries.append((item, terms))
        self.search.textChanged.connect(self.filter)
        self.search.returnPressed.connect(self.choose)
        self.tree.itemActivated.connect(lambda _item, _column: self.choose())
        self.filter("")

    def filter(self, text):
        tokens = text.casefold().split()
        shown = []
        for item, terms in self.entries:
            visible = all(token in terms for token in tokens)
            item.setHidden(not visible)
            if visible:
                shown.append(item)
        current = self.tree.currentItem()
        if current is None or current.isHidden():
            self.tree.setCurrentItem(shown[0] if shown else None)
        self.caption.setText(f"{len(shown)} 个节点 · Enter／双击添加 · Esc 关闭")

    def choose(self):
        current = self.tree.currentItem()
        if current is not None and not current.isHidden():
            self.nodeSelected.emit(current.data(0, QtCore.Qt.ItemDataRole.UserRole))
            self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        self.search.setFocus()

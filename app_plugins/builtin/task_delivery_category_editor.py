"""Small editor for the persistent quantity category catalog."""
from qt_compat import QtCore, QtWidgets
from model.DailyQuantityCategories import validate_categories


class CategoryEditorDialog(QtWidgets.QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        options, self.revision = store.load()
        self.setWindowTitle("管理统计类别")
        self.resize(760, 540)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "这是固定的本地列表，不会从网上更新。分页、类别名称需与统计表一致。\n"
            "修改或删除选项不会改动历史视频分类，也不会重命名线上表格。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["统计分页 / 类别"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        layout.addWidget(self.tree, 1)
        for sheet, labels in options.items():
            parent_item = self._item(sheet)
            self.tree.addTopLevelItem(parent_item)
            for label in labels:
                parent_item.addChild(self._item(label))
        self.tree.expandAll()
        actions = QtWidgets.QHBoxLayout()
        for title, callback in (("新增分页", self.add_sheet), ("新增类别", self.add_category),
                                ("修改名称", self.rename), ("删除", self.remove)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _item(value):
        item = QtWidgets.QTreeWidgetItem([" ".join(value.split())])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, value)
        item.setToolTip(0, value)
        return item

    def categories(self):
        result = {}
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            sheet = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if sheet in result:
                raise ValueError("统计分页名称重复：" + sheet)
            result[sheet] = [item.child(i).data(0, QtCore.Qt.ItemDataRole.UserRole)
                             for i in range(item.childCount())]
        return validate_categories(result)

    def _insert(self, value, parent=None):
        item = self._item(value)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        try:
            self.categories()
        except ValueError as exc:
            if parent is None:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            else:
                parent.removeChild(item)
            self.status.setText(str(exc))
            return False
        self.tree.setCurrentItem(item)
        if parent:
            parent.setExpanded(True)
        self.status.clear()
        return True

    def add_sheet(self):
        value, ok = QtWidgets.QInputDialog.getText(self, "新增分页", "统计表分页名称：")
        if ok:
            self._insert(value)

    def add_category(self):
        selected = self.tree.currentItem()
        if selected is None:
            self.status.setText("先选择一个统计分页。")
            return
        parent = selected.parent() or selected
        value, ok = QtWidgets.QInputDialog.getMultiLineText(self, "新增类别", "类别名称（与统计表一致）：")
        if ok:
            self._insert(value, parent)

    def rename(self):
        item = self.tree.currentItem()
        if item is None:
            return
        original = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        value, ok = QtWidgets.QInputDialog.getMultiLineText(self, "修改名称", "名称：", original)
        if not ok:
            return
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, value)
        try:
            self.categories()
        except ValueError as exc:
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, original)
            self.status.setText(str(exc))
            return
        item.setText(0, " ".join(value.split()))
        item.setToolTip(0, value)
        self.status.clear()

    def remove(self):
        item = self.tree.currentItem()
        if item is None:
            return
        if QtWidgets.QMessageBox.question(self, "删除选项", "仅从可选列表删除，历史记录不受影响。确定？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if item.parent() is not None:
            item.parent().removeChild(item)
        else:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))

    def save(self):
        try:
            self.store.save(self.categories(), self.revision)
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.accept()

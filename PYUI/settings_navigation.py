"""Host-owned navigation for core and automatically registered plugin pages.

The existing page widgets and QTabWidget API are retained. Only the crowded
tab bar is replaced, so validation/focus and configuration controllers keep
working without copying forms or maintaining a second settings model.
"""

from qt_compat import QtCore, QtWidgets


class SettingsNavigation(QtWidgets.QWidget):
    def __init__(self, pages, parent=None):
        super().__init__(parent)
        self.pages = pages
        self.items = {}
        self._first_show = True
        self.setObjectName("settings_navigation")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter)

        sidebar = QtWidgets.QWidget(self.splitter)
        sidebar.setMinimumWidth(170)
        sidebar.setMaximumWidth(340)
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 8, 0)
        sidebar_layout.setSpacing(8)
        self.search = QtWidgets.QLineEdit(sidebar)
        self.search.setPlaceholderText("搜索设置页…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("按设置页名称或插件名称筛选；不会搜索密钥内容")
        sidebar_layout.addWidget(self.search)
        self.tree = QtWidgets.QTreeWidget(sidebar)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setAnimated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet("QTreeView::item { padding:7px 4px; }")
        sidebar_layout.addWidget(self.tree, 1)
        self.empty_label = QtWidgets.QLabel("没有匹配的设置页", sidebar)
        self.empty_label.hide()
        sidebar_layout.addWidget(self.empty_label)

        content = QtWidgets.QWidget(self.splitter)
        content.setMinimumWidth(0)
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QtWidgets.QLabel(content)
        self.heading.setWordWrap(True)
        font = self.heading.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.heading.setFont(font)
        content_layout.addWidget(self.heading)
        self.pages.tabBar().hide()
        self.pages.setMinimumWidth(0)
        content_layout.addWidget(self.pages, 1)
        self.splitter.addWidget(sidebar)
        self.splitter.addWidget(content)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([230, 800])

        self.program_group = self._group("程序设置")
        self.plugin_group = self._group("插件设置")
        plugin_entries = {}
        for index in range(pages.count()):
            widget = pages.widget(index)
            plugin_id = widget.property("settings_plugin_id")
            if plugin_id:
                plugin_entries.setdefault(str(plugin_id), []).append((index, widget))
            else:
                self._page(self.program_group, index)
        for plugin_id, entries in plugin_entries.items():
            name = str(entries[0][1].property("settings_plugin_name") or plugin_id)
            if len(entries) == 1:
                self._page(self.plugin_group, entries[0][0])
            else:
                group = QtWidgets.QTreeWidgetItem(self.plugin_group, [name])
                group.setFlags(group.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
                for index, _widget in entries:
                    self._page(group, index)
                group.setExpanded(True)
        self.program_group.setExpanded(True)
        self.plugin_group.setExpanded(True)
        self.plugin_group.setHidden(not bool(plugin_entries))
        self.tree.currentItemChanged.connect(self._selected)
        self.pages.currentChanged.connect(self._page_changed)
        self.search.textChanged.connect(self._filter)
        self._page_changed(pages.currentIndex())

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            # Set sizes after geometry is available; constructing a splitter
            # before layout otherwise leaves a very narrow sidebar.
            QtCore.QTimer.singleShot(0, self._initial_layout)

    def _initial_layout(self):
        width = self.splitter.width()
        sidebar_width = min(240, max(170, width // 4))
        self.splitter.setSizes([sidebar_width, max(1, width - sidebar_width)])
        self.tree.verticalScrollBar().setValue(0)

    def _group(self, title):
        item = QtWidgets.QTreeWidgetItem(self.tree, [title])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def _page(self, parent, index):
        title = self.pages.tabText(index)
        widget = self.pages.widget(index)
        plugin_name = str(widget.property("settings_plugin_name") or "")
        item = QtWidgets.QTreeWidgetItem(parent, [title])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, index)
        item.setToolTip(0, " / ".join(filter(None, (plugin_name, title))))
        self.items[index] = item

    def _selected(self, item, _previous):
        if item is not None:
            index = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(index, int):
                self.pages.setCurrentIndex(index)

    def _page_changed(self, index):
        item = self.items.get(index)
        if item is None:
            return
        # Validation/focus requests must remain visible even after searching.
        if item.isHidden():
            self.search.clear()
        parts = [item.text(0)]
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parts.insert(0, parent.text(0))
            parent = parent.parent()
        self.heading.setText(" / ".join(parts))
        blocker = QtCore.QSignalBlocker(self.tree)
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        del blocker

    def _filter(self, text):
        query = text.strip().casefold()
        for index, item in self.items.items():
            item.setHidden(query not in item.toolTip(0).casefold() and query not in item.text(0).casefold())

        def update_group(group):
            visible = False
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.childCount():
                    update_group(child)
                visible = visible or not child.isHidden()
            group.setHidden(not visible)
            if query and visible:
                group.setExpanded(True)
        update_group(self.program_group)
        update_group(self.plugin_group)
        visible_items = [item for item in self.items.values() if not item.isHidden()]
        self.empty_label.setVisible(not visible_items)
        current = self.items.get(self.pages.currentIndex())
        if visible_items and current is not None and current.isHidden():
            self.tree.setCurrentItem(visible_items[0])

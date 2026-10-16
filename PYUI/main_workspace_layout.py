"""Host layout only: resizable workspace and unclipped plugin cards."""
from copy import deepcopy

from qt_compat import QtCore, QtWidgets


MAIN_SPLITTERS_CONFIG_KEY = "main_window_splitters"


class PluginPanelScrollArea(QtWidgets.QScrollArea):
    """Reserve scrollbar space instead of hiding an oversized widget's edge."""

    minimumWidthChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumHeight(160)

    def setWidget(self, widget):
        super().setWidget(widget)
        widget.installEventFilter(self)
        self._sync_content_width()

    def _sync_content_width(self):
        content = self.widget()
        if content is None:
            return
        # Keep the same minimum whether the vertical scrollbar is visible or
        # not. Qt's viewport then always has room for the full card contents.
        minimum = content.minimumSizeHint().expandedTo(content.minimumSize())
        scrollbar = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
        width = max(0, minimum.width()) + scrollbar + 2 * self.frameWidth()
        if self.minimumWidth() != width:
            self.setMinimumWidth(width)
            self.updateGeometry()
            self.minimumWidthChanged.emit()

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() in (
            QtCore.QEvent.Type.LayoutRequest, QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.StyleChange, QtCore.QEvent.Type.FontChange,
        ):
            self._sync_content_width()
        return super().eventFilter(watched, event)


class MainWorkspaceLayout:
    """Reuse existing widgets and signals; never rebuild plugins or tasks."""

    def __init__(self, window):
        self.window = window
        grid = window.gridLayout
        grid.removeItem(window.task_list_header_layout)
        for widget in (window.task_table_widget, window.file_explorer_tree_view,
                       window.tabWidget, window.label_3, window.log_text_edit):
            grid.removeWidget(widget)

        self.main = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, window)
        self.main.setObjectName("main_workspace_splitter")
        self.directory = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self.main)
        self.directory.setObjectName("task_directory_splitter")
        self.directory.addWidget(window.task_table_widget)
        self.directory.addWidget(window.file_explorer_tree_view)
        self.directory.setStretchFactor(0, 2)
        self.directory.setStretchFactor(1, 1)
        window.task_table_widget.setMinimumWidth(200)
        window.file_explorer_tree_view.setMinimumWidth(160)

        tasks = QtWidgets.QWidget(self.main)
        tasks.setObjectName("main_tasks_pane")
        layout = QtWidgets.QVBoxLayout(tasks)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(window.task_list_header_layout)
        layout.addWidget(self.directory, 1)
        self.main.addWidget(tasks)
        self.main.addWidget(window.tabWidget)
        self.main.setStretchFactor(0, 3)
        self.main.setStretchFactor(1, 2)
        self.main.setMinimumHeight(300)

        log_pane = QtWidgets.QWidget(window)
        log_pane.setObjectName("main_log_pane")
        log_layout = QtWidgets.QVBoxLayout(log_pane)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)
        log_layout.addWidget(window.label_3)
        log_layout.addWidget(window.log_text_edit, 1)
        window.log_text_edit.setMinimumHeight(60)
        self.vertical = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, window)
        self.vertical.setObjectName("main_log_splitter")
        self.vertical.addWidget(self.main)
        self.vertical.addWidget(log_pane)
        self.vertical.setStretchFactor(0, 4)
        self.vertical.setStretchFactor(1, 1)
        # Keep one permanent expanding item. Independent node windows never
        # reparent the workspace or modify this root layout.
        self.host = QtWidgets.QWidget(window)
        self.host.setObjectName("main_workspace_host")
        self.host.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                QtWidgets.QSizePolicy.Policy.Expanding)
        self.host_layout = QtWidgets.QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_layout.setSpacing(0)
        self.host_layout.addWidget(self.vertical, 1)
        grid.addWidget(self.host, 1, 0, 4, 3)
        grid.setRowStretch(1, 1)

        self.splitters = {"workspace": self.main, "log": self.vertical,
                          "directory": self.directory}
        self.saved = {}
        for name, splitter in self.splitters.items():
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(7)
            splitter.setStyleSheet(
                "QSplitter::handle { background: palette(button); border: 1px solid palette(mid); }"
                "QSplitter::handle:hover { background: palette(highlight); }"
            )
            splitter.handle(1).setToolTip("拖动调整区域比例")
            splitter.splitterMoved.connect(lambda _position, _index, key=name: self._remember(key))
        self.main.setSizes([650, 450])
        self.vertical.setSizes([740, 180])
        self.directory.setSizes([500, 220])
        window.main_splitter = self.main
        window.log_splitter = self.vertical
        window.task_directory_splitter = self.directory

    def bind_plugin_scroll(self, scroll):
        self.plugin_scroll = scroll
        scroll.minimumWidthChanged.connect(self._update_tools_width)
        self._update_tools_width()

    def _update_tools_width(self):
        # QTabWidget can shrink its current page below the page's minimum
        # size; therefore the splitter must constrain the tab container too.
        window = self.window
        margins = window.verticalLayout.contentsMargins()
        width = max(self.plugin_scroll.minimumWidth() + margins.left() + margins.right(),
                    window.common_tools.minimumSizeHint().width())
        frame = window.tabWidget.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_DefaultFrameWidth)
        window.tabWidget.setMinimumWidth(width + 2 * frame)

    def _remember(self, name):
        sizes = self.splitters[name].sizes()
        if all(size > 0 for size in sizes):
            self.saved[name] = sizes

    def load(self, config):
        saved = config.get(MAIN_SPLITTERS_CONFIG_KEY)
        self.saved = deepcopy(saved) if isinstance(saved, dict) else {}
        for name, splitter in self.splitters.items():
            sizes = self.saved.get(name)
            if self._valid_sizes(sizes):
                splitter.setSizes(sizes)

    @staticmethod
    def _valid_sizes(sizes):
        return (isinstance(sizes, list) and len(sizes) == 2
                and all(type(size) is int and 0 < size <= 100000 for size in sizes))

    def restore_directory(self):
        # Hidden splitter children have zero geometry. Apply the remembered
        # ratio after showing the directory, not while it is still hidden.
        sizes = self.saved.get("directory")
        self.directory.setSizes(sizes if self._valid_sizes(sizes) else [500, 220])

    def update_config(self, config):
        # A resize/layout request is not a user edit. Persist only explicitly
        # dragged ratios, so opening the app does not rewrite configuration.
        if self.saved:
            config[MAIN_SPLITTERS_CONFIG_KEY] = deepcopy(self.saved)

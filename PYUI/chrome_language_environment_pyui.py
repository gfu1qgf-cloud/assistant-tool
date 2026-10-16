"""Language configuration generator attached to the existing Chrome launcher."""
from qt_compat import QtCore, QtGui, QtWidgets, Qt
from model.ChromeLanguageEnvironment import ChromeLanguageEnvironmentStore, LANGUAGE_PRESETS


class ChromeLanguageEnvironmentPanel(QtWidgets.QWidget):
    def __init__(self, chrome_path_provider, parent=None, storage=None):
        super().__init__(parent)
        self.chrome_path_provider = chrome_path_provider
        self.store = ChromeLanguageEnvironmentStore(storage)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        title = QtWidgets.QLabel("独立的外语浏览环境", self)
        title.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(title)
        hint = QtWidgets.QLabel(
            "默认使用英文界面、英文网页偏好与英文拼写检查；不继承原浏览器的中文配置、"
            "扩展或登录状态。新环境中的登录状态会保留，之后可直接继续使用。", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list_widget = QtWidgets.QListWidget(self)
        self.list_widget.setMinimumHeight(140)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.context_menu)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.open_selected())
        layout.addWidget(self.list_widget, 1)
        self.details = QtWidgets.QLabel(self)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details)
        actions = QtWidgets.QHBoxLayout()
        self.create_button = QtWidgets.QPushButton("配置生成器…", self)
        self.open_button = QtWidgets.QPushButton("打开所选环境", self)
        self.more_button = QtWidgets.QToolButton(self)
        self.more_button.setText("更多")
        self.more_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.more_button)
        menu.addAction("检查语言（本地页面）", lambda: self.open_selected("check"))
        menu.addAction("打开 Chrome 语言设置", lambda: self.open_selected("languages"))
        menu.addSeparator()
        menu.addAction("打开配置目录", self.open_directory)
        menu.addAction("刷新列表", self.refresh)
        self.more_button.setMenu(menu)
        actions.addWidget(self.create_button)
        actions.addStretch(1)
        actions.addWidget(self.more_button)
        actions.addWidget(self.open_button)
        layout.addLayout(actions)
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = QtWidgets.QLabel(
            "此环境不启用 Chrome 配置同步，防止账号同步带回中文设置。网站账号自身的语言"
            "设置和组织策略仍可能优先生效；不改变系统键盘、地区、时区或 IP。"
            "生成器只初始化新环境，不会覆盖已使用环境的 Cookie 或浏览器设置。", self)
        note.setWordWrap(True)
        palette = note.palette()
        palette.setColor(QtGui.QPalette.ColorRole.WindowText,
                         palette.color(QtGui.QPalette.ColorRole.PlaceholderText))
        note.setPalette(palette)
        layout.addWidget(note)
        self.create_button.clicked.connect(self.show_generator)
        self.open_button.clicked.connect(lambda _checked=False: self.open_selected())
        self.list_widget.currentItemChanged.connect(self.update_details)
        self.refresh()

    def selected_id(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole)["id"] if item is not None else None

    def refresh(self, selected_id=None):
        selected_id = selected_id or self.selected_id()
        try:
            records = self.store.list_environments()
        except Exception as error:
            self.status.setText(f"读取失败：{error}")
            self.create_button.setEnabled(False)
            self.open_button.setEnabled(False)
            return
        self.create_button.setEnabled(True)
        self.list_widget.clear()
        for record in records:
            label = LANGUAGE_PRESETS[record["language"]][3]
            item = QtWidgets.QListWidgetItem(f"{record['name']}   ·   {label}")
            item.setData(Qt.UserRole, record)
            self.list_widget.addItem(item)
            if record["id"] == selected_id:
                self.list_widget.setCurrentItem(item)
        if self.list_widget.currentItem() is None and records:
            self.list_widget.setCurrentRow(0)
        self.status.setText("尚未创建环境。点击“配置生成器”即可创建。" if not records else "双击环境即可打开；右键可检查语言。")
        self.update_details()

    def update_details(self, *_args):
        item = self.list_widget.currentItem()
        self.open_button.setEnabled(item is not None)
        if item is None:
            self.details.setText("")
            return
        record = item.data(Qt.UserRole)
        preset = LANGUAGE_PRESETS[record["language"]]
        self.details.setText(f"浏览器语言：{preset[3]}\n网页首选语言：{', '.join(preset[1])}\n"
                             "与任务浏览器隔离，保留此环境自己的登录状态。")

    def show_generator(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("外语环境 · 配置生成器")
        dialog.resize(480, 250)
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        name = QtWidgets.QLineEdit("English signup", dialog)
        name.setMaxLength(80)
        language = QtWidgets.QComboBox(dialog)
        for key, preset in LANGUAGE_PRESETS.items():
            language.addItem(preset[3], key)
        form.addRow("环境名称：", name)
        form.addRow("浏览器 / 网页语言：", language)
        layout.addLayout(form)
        explanation = QtWidgets.QLabel(
            "配置由程序保存，无需手动选文件。只创建语言环境，不自动申请账号，"
            "也不复制其他浏览器的密码、Cookie 或中文设置。", dialog)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        error_label = QtWidgets.QLabel(dialog)
        error_label.setWordWrap(True)
        error_label.setStyleSheet("color:#b23a3a;")
        layout.addWidget(error_label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=dialog)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("生成配置")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        layout.addWidget(buttons)

        def create():
            try:
                record = self.store.create(name.text(), language.currentData())
            except Exception as error:
                error_label.setText(str(error))
                return
            self.refresh(record["id"])
            self.status.setText("配置已生成。点击“打开所选环境”开始使用。")
            dialog.accept()

        buttons.accepted.connect(create)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()

    def open_selected(self, destination="newtab"):
        environment_id = self.selected_id()
        if environment_id is None:
            QtWidgets.QMessageBox.information(self, "选择环境", "请先创建或选择一个外语环境。")
            return
        try:
            self.store.launch(self.chrome_path_provider(), environment_id, destination)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "Chrome 启动失败", str(error))
            self.status.setText("启动失败，配置与登录数据未改变。")
            return
        self.status.setText("已打开独立 Chrome 环境；关闭启动器不会关闭 Chrome。")

    def open_directory(self):
        environment_id = self.selected_id()
        if environment_id is None:
            return
        try:
            path = self.store.environment_directory(environment_id)
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))):
                raise RuntimeError("系统未能打开此目录。")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "打开目录失败", str(error))

    def context_menu(self, point):
        item = self.list_widget.itemAt(point)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)
        menu = QtWidgets.QMenu(self)
        menu.addAction("打开环境", lambda: self.open_selected())
        menu.addAction("检查语言（本地页面）", lambda: self.open_selected("check"))
        menu.addAction("Chrome 语言设置", lambda: self.open_selected("languages"))
        menu.addSeparator()
        menu.addAction("打开配置目录", self.open_directory)
        menu.exec(self.list_widget.mapToGlobal(point))

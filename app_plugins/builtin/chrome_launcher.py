import logging

from qt_compat import QtGui, QtWidgets

from app_plugins.api import (
    MAIN_MENU,
    PluginCommand,
    PluginMainWidget,
    PluginSettingsPage,
)
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    CHROME_NEXT_HOTKEY_ID,
    DEFAULT_CHROME_NEXT_HOTKEY,
    GlobalHotkeyManager,
    hotkey_setting_changed,
    normalize_hotkey_sequence,
)
from PYUI.chrome_runner_pyui import ChromeRunnerDialog


class ChromeLauncherQuickActions(QtWidgets.QWidget):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.open_button = QtWidgets.QPushButton("开启谷歌浏览器", self)
        self.open_button.setObjectName("open_chrome_btn")
        self.next_button = QtWidgets.QPushButton("启动下一个浏览器", self)
        self.next_button.setObjectName("launch_next_chrome_btn")
        layout.addWidget(self.open_button)
        layout.addWidget(self.next_button)
        self.open_button.clicked.connect(
            lambda _checked=False: plugin.open_launcher()
        )
        self.next_button.clicked.connect(
            lambda _checked=False: plugin.launch_next_profile()
        )
        if getattr(plugin, 'flow_controller', None) is not None:
            self.open_button.setText('浏览器配置／分组')
            self.flow_button = QtWidgets.QPushButton('Flow 视频生成', self)
            layout.addWidget(self.flow_button)
            self.flow_button.clicked.connect(lambda _checked=False: plugin.flow_controller.open_flow_queue())


class ChromeLauncherSettingsPage:
    """Settings owned by the Chrome launcher plugin.

    Profile groups, preset websites, and iterator cursors remain editable in
    ChromeRunnerDialog and deliberately retain their existing config keys.
    """

    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("Chrome 启动器", self.widget)
        title.setStyleSheet("font-weight:600;font-size:14px;")
        layout.addWidget(title)

        form = QtWidgets.QFormLayout()
        self.hotkey_edit = QtWidgets.QKeySequenceEdit(self.widget)
        form.addRow("启动下一个浏览器：", self.hotkey_edit)
        layout.addLayout(form)

        hint = QtWidgets.QLabel(
            "该快捷键在主程序运行时全局生效。Chrome Profile、预设网站、"
            "分组及各分组的迭代位置继续保存在原配置中，可从"
            "浏览器配置页维护。",
            self.widget,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        value = config.get(
            CHROME_NEXT_HOTKEY_CONFIG_KEY,
            DEFAULT_CHROME_NEXT_HOTKEY,
        )
        try:
            value = normalize_hotkey_sequence(value)
        except ValueError:
            value = DEFAULT_CHROME_NEXT_HOTKEY
        self.hotkey_edit.setKeySequence(QtGui.QKeySequence(value))

    def normalized_hotkey(self):
        return normalize_hotkey_sequence(
            self.hotkey_edit.keySequence().toString(
                QtGui.QKeySequence.PortableText
            )
        )

    def hotkey_fields(self):
        return (("启动下一个浏览器", self.hotkey_edit, self.widget),)

    def validate(self):
        self.normalized_hotkey()

    def update_config(self, config):
        # Keep the historical key so existing installations migrate with zero
        # data conversion and older releases can still read the shortcut.
        config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = self.normalized_hotkey()


class ChromeLauncherPlugin:
    plugin_id = "chrome_launcher"
    display_name = "Chrome 启动器"
    version = "1.2"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None
        self.hotkey_manager = None
        self.global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY
        self.quick_actions = None
        self.flow_controller = None
        self.profile_skip_factory = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand('bookmarks', '浏览器书签库…',
            lambda _rows:self.open_bookmarks(), frozenset({MAIN_MENU}),
            tooltip='从 Chrome、Edge、Firefox 导入去重；校验与加密备份后可清理源书签',order=31))
        context.register_command(
            PluginCommand(
                command_id="open",
                title="Chrome 启动器",
                callback=lambda _rows: self.open_launcher(),
                locations=frozenset({MAIN_MENU}),
                tooltip="管理任务浏览器、迭代分组，以及外语独立环境配置",
                order=30,
            )
        )
        context.register_settings_page(
            PluginSettingsPage(
                page_id="settings",
                title="Chrome 插件",
                factory=ChromeLauncherSettingsPage,
                order=210,
            )
        )
        context.register_main_widget(PluginMainWidget(
            widget_id="quick_actions",
            factory=self.create_quick_actions,
            order=20,
            title="Chrome 浏览器",
        ))

    def create_quick_actions(self, parent=None):
        self.quick_actions = ChromeLauncherQuickActions(self, parent)
        return self.quick_actions

    def start(self):
        config = self.context.load_config()
        try:
            self.global_hotkey = normalize_hotkey_sequence(
                config.get(
                    CHROME_NEXT_HOTKEY_CONFIG_KEY,
                    DEFAULT_CHROME_NEXT_HOTKEY,
                )
            )
        except ValueError:
            self.global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY

        config_path = getattr(
            self.context.parent_widget,
            "config_name",
            "config.json",
        )
        self.dialog = ChromeRunnerDialog(
            self.context.parent_widget,
            config_path=str(config_path),
            language_storage=self.context.storage,
            bookmark_context=self.context,
        )
        self.dialog.profile_skip_factory = self.profile_skip_factory
        self.hotkey_manager = GlobalHotkeyManager(
            self.context.parent_widget,
            hotkey_id=CHROME_NEXT_HOTKEY_ID,
        )
        self.hotkey_manager.activated.connect(self.launch_next_profile)
        self._register_hotkey(self.global_hotkey, show_error=False)
        self._update_host_button_tooltips()

    def _register_hotkey(self, shortcut, show_error=True):
        try:
            shortcut = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            self.context.log(f"全局快捷键无效：{error}", logging.ERROR)
            if show_error:
                QtWidgets.QMessageBox.warning(
                    self.context.parent_widget,
                    "全局快捷键无效",
                    f"启动下一个浏览器：{error}",
                )
            return False

        if self.hotkey_manager.register(shortcut):
            self.global_hotkey = shortcut
            self.context.log(f"全局快捷键已启用：{shortcut}")
            self._update_host_button_tooltips()
            return True

        message = (
            f"{shortcut} 无法注册，可能已被其他程序占用。"
            f" {self.hotkey_manager.last_error}"
        )
        self.context.log(f"全局快捷键未启用：{message}", logging.ERROR)
        if show_error:
            QtWidgets.QMessageBox.warning(
                self.context.parent_widget,
                "全局快捷键注册失败",
                message,
            )
        return False

    def _update_host_button_tooltips(self):
        open_button = (
            self.quick_actions.open_button if self.quick_actions is not None
            else getattr(self.context.parent_widget, "open_chrome_btn", None)
        )
        if open_button is not None:
            open_button.setToolTip(
                "打开统一浏览器配置、分组、外语环境及书签管理。" if self.flow_controller is not None
                else "打开 Chrome 启动器；配置由 Chrome 插件管理。"
            )
        next_button = (
            self.quick_actions.next_button if self.quick_actions is not None
            else getattr(self.context.parent_widget, "launch_next_chrome_btn", None)
        )
        if next_button is not None:
            next_button.setToolTip(
                "启动已保存迭代队列中的下一个 Chrome Profile。"
                + ("自动跳过 Flow 已确认积分不足且仍在冷却的账号。" if self.flow_controller is not None else "") +
                f"系统全局快捷键：{self.global_hotkey}"
            )

    def open_launcher(self):
        if self.dialog is None:
            raise RuntimeError("Chrome 启动器尚未初始化")
        if self.flow_controller is not None:
            return self.flow_controller.open_browser_manager()
        return self.dialog.exec()

    def open_bookmarks(self):
        if self.dialog is None: raise RuntimeError('浏览器插件尚未初始化。')
        self.dialog.environment_tabs.setCurrentWidget(self.dialog.bookmarks_panel)
        self.dialog.bookmarks_panel.refresh()
        return self.open_launcher()

    def launch_next_profile(self):
        if self.dialog is None:
            raise RuntimeError("Chrome 启动器尚未初始化")
        profile = self.dialog.launch_next_profile()
        if profile:
            self.context.log(
                f"已按顺序启动 Chrome：{profile['name']} "
                f"({profile['directory']})"
            )
        return profile

    def apply_settings(self, config):
        shortcut = config.get(CHROME_NEXT_HOTKEY_CONFIG_KEY, self.global_hotkey)
        if not hotkey_setting_changed(shortcut, self.global_hotkey):
            return True
        return self._register_hotkey(shortcut, show_error=False)

    def update_config(self, config):
        config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = self.global_hotkey

    def stop(self):
        if self.hotkey_manager is not None:
            self.hotkey_manager.close()
            self.hotkey_manager = None
        if self.dialog is not None:
            self.dialog.close()

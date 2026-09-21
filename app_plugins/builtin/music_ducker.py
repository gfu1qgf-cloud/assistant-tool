import logging

from qt_compat import QtCore, QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from model.MusicDucker import (
    DEFAULT_MUSIC_DUCKER_SETTINGS,
    MusicDuckerThread,
    normalize_music_ducker_settings,
)


MUSIC_DUCKER_CONFIG_KEY = "music_ducker_settings"


def settings_from_config(config):
    config = config if isinstance(config, dict) else {}
    return normalize_music_ducker_settings(config.get(MUSIC_DUCKER_CONFIG_KEY))


class MusicDuckerSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("音乐压制", self.widget)
        title.setStyleSheet("font-weight:600;font-size:14px;")
        layout.addWidget(title)
        form = QtWidgets.QFormLayout()

        self.trigger_apps_edit = QtWidgets.QLineEdit(self.widget)
        self.trigger_apps_edit.setToolTip("用逗号分隔；不写 .exe 时会自动补全。")
        self.music_apps_edit = QtWidgets.QPlainTextEdit(self.widget)
        self.music_apps_edit.setMaximumHeight(100)
        self.music_apps_edit.setToolTip("每行一个进程名，也可以用逗号分隔。")
        self.duck_to_spin = QtWidgets.QSpinBox(self.widget)
        self.duck_to_spin.setRange(0, 100)
        self.duck_to_spin.setSuffix(" %")
        self.threshold_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.threshold_spin.setRange(0, 1)
        self.threshold_spin.setDecimals(4)
        self.threshold_spin.setSingleStep(0.001)
        self.threshold_spin.setToolTip("数值越低越灵敏；太低可能把底噪也当成出声。")
        self.release_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.release_spin.setRange(0, 600)
        self.release_spin.setDecimals(1)
        self.release_spin.setSuffix(" 秒")
        self.fade_down_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.fade_down_spin.setRange(0, 600)
        self.fade_down_spin.setDecimals(1)
        self.fade_down_spin.setSuffix(" 秒")
        self.fade_up_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.fade_up_spin.setRange(0, 600)
        self.fade_up_spin.setDecimals(1)
        self.fade_up_spin.setSuffix(" 秒")
        self.interval_spin = QtWidgets.QSpinBox(self.widget)
        self.interval_spin.setRange(50, 5000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setSuffix(" ms")

        form.addRow("触发程序：", self.trigger_apps_edit)
        form.addRow("压低的程序：", self.music_apps_edit)
        form.addRow("压低后的音量：", self.duck_to_spin)
        form.addRow("触发峰值阈值：", self.threshold_spin)
        form.addRow("安静等待时间：", self.release_spin)
        form.addRow("压低渐变时间：", self.fade_down_spin)
        form.addRow("恢复渐变时间：", self.fade_up_spin)
        form.addRow("检测间隔：", self.interval_spin)
        layout.addLayout(form)
        hint = QtWidgets.QLabel(
            "检测到触发程序出声后，平滑压低指定音乐程序；停止插件时会恢复原音量。",
            self.widget,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        self.set_settings(settings_from_config(config))

    def set_settings(self, settings):
        settings = normalize_music_ducker_settings(settings)
        self.trigger_apps_edit.setText(", ".join(settings["trigger_apps"]))
        self.music_apps_edit.setPlainText("\n".join(settings["music_apps"]))
        self.duck_to_spin.setValue(settings["duck_to_percent"])
        self.threshold_spin.setValue(settings["peak_threshold"])
        self.release_spin.setValue(settings["release_seconds"])
        self.fade_down_spin.setValue(settings["fade_down_seconds"])
        self.fade_up_spin.setValue(settings["fade_up_seconds"])
        self.interval_spin.setValue(settings["check_interval_ms"])

    def settings(self):
        return normalize_music_ducker_settings({
            "trigger_apps": self.trigger_apps_edit.text(),
            "music_apps": self.music_apps_edit.toPlainText(),
            "duck_to_percent": self.duck_to_spin.value(),
            "peak_threshold": self.threshold_spin.value(),
            "release_seconds": self.release_spin.value(),
            "fade_down_seconds": self.fade_down_spin.value(),
            "fade_up_seconds": self.fade_up_spin.value(),
            "check_interval_ms": self.interval_spin.value(),
        })

    def update_config(self, config):
        config[MUSIC_DUCKER_CONFIG_KEY] = self.settings()


class MusicDuckerPlugin:
    plugin_id = "music_ducker"
    display_name = "音乐压制"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = dict(DEFAULT_MUSIC_DUCKER_SETTINGS)
        self.thread = None
        self._bound = False

    def register(self, context):
        self.context = context
        self.settings = settings_from_config(context.load_config())
        context.register_command(PluginCommand(
            command_id="toggle",
            title="启用音乐压制",
            callback=self._menu_toggled,
            locations=frozenset({MAIN_MENU}),
            tooltip="达芬奇出声时自动压低音乐，停止时恢复原音量",
            order=35,
            checkable=True,
            checked=lambda: self.is_running(),
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="音乐压制",
            factory=MusicDuckerSettingsPage,
            order=120,
        ))

    def start(self):
        self.settings = settings_from_config(self.context.load_config())
        self._bind_main_controls()
        self._sync_controls()

    def _bind_main_controls(self):
        if self._bound:
            return
        window = self.context.parent_widget
        checkbox = getattr(window, "music_ducker_checkbox", None)
        settings_button = getattr(window, "music_ducker_settings_btn", None)
        if checkbox is not None:
            checkbox.toggled.connect(self._checkbox_toggled)
        if settings_button is not None:
            settings_button.clicked.connect(self.open_parameter_dialog)
        self._bound = True

    def apply_settings(self, config):
        self.settings = settings_from_config(config)
        if self.is_running():
            self.context.log(
                "[音乐压制] 新参数已保存，将在下次启用音乐压制时生效。"
            )
        return True

    def update_config(self, config):
        config[MUSIC_DUCKER_CONFIG_KEY] = normalize_music_ducker_settings(
            self.settings
        )
        return config

    def is_running(self):
        return self.thread is not None and self.thread.isRunning()

    def _menu_toggled(self, _rows, checked):
        self.set_enabled(bool(checked))

    def _checkbox_toggled(self, checked):
        self.set_enabled(bool(checked))

    def set_enabled(self, enabled):
        if not enabled:
            if self.is_running():
                self.thread.stop()
                self._sync_controls(stopping=True)
            else:
                self._sync_controls()
            return
        if self.is_running():
            self._sync_controls()
            return

        thread = MusicDuckerThread(
            self.settings, self.context.parent_widget
        )
        thread.message.connect(self._on_message)
        thread.error.connect(self._on_error)
        thread.finished.connect(self._on_finished)
        self.thread = thread
        thread.start()
        self._sync_controls()

    def _sync_controls(self, stopping=False):
        running = self.is_running()
        window = self.context.parent_widget
        checkbox = getattr(window, "music_ducker_checkbox", None)
        settings_button = getattr(window, "music_ducker_settings_btn", None)
        if checkbox is not None:
            checkbox.blockSignals(True)
            checkbox.setChecked(running)
            checkbox.setEnabled(not stopping)
            checkbox.setText(
                "正在停止音乐压制..."
                if stopping
                else "音乐压制运行中" if running else "启用音乐压制"
            )
            checkbox.setStyleSheet(
                "QCheckBox { color: #1B5E20; font-weight: bold; }"
                if running and not stopping else ""
            )
            checkbox.blockSignals(False)
        if settings_button is not None:
            settings_button.setEnabled(not running and not stopping)
        self.context.update_command(
            "toggle",
            title="音乐压制运行中" if running else "启用音乐压制",
            checked=running,
        )

    def open_parameter_dialog(self, _checked=False):
        if self.is_running():
            QtWidgets.QMessageBox.information(
                self.context.parent_widget,
                "音乐压制",
                "请先停止音乐压制，再修改运行参数。",
            )
            return
        dialog = QtWidgets.QDialog(self.context.parent_widget)
        dialog.setWindowTitle("音乐压制参数")
        dialog.resize(520, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        page = MusicDuckerSettingsPage(dialog)
        page.set_settings(self.settings)
        layout.addWidget(page.widget)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            dialog,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        previous = dict(self.settings)
        self.settings = page.settings()
        if self.context.save_config():
            self.context.log("[音乐压制] 参数已保存。")
        else:
            self.settings = previous

    def _on_message(self, message):
        self.context.log(f"[音乐压制] {message}")

    def _on_error(self, message):
        self.context.log(f"[音乐压制错误] {message}", level=logging.ERROR)
        QtWidgets.QMessageBox.critical(
            self.context.parent_widget, "音乐压制启动失败", str(message)
        )

    def _on_finished(self):
        thread = self.thread
        self.thread = None
        self._sync_controls()
        if thread is not None:
            thread.deleteLater()

    def can_close(self):
        if not self.is_running():
            return True, ""
        thread = self.thread
        thread.stop()
        self._sync_controls(stopping=True)
        if thread.wait(5000):
            self.thread = None
            self._sync_controls()
            thread.deleteLater()
            return True, ""
        return False, "音乐压制正在恢复音乐音量，请稍后再关闭程序。"

    def stop(self):
        thread = self.thread
        if thread is not None and thread.isRunning():
            thread.stop()
            thread.wait(5000)
        self.thread = None
        self._sync_controls()

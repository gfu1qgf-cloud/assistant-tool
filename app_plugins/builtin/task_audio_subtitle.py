import copy
import logging
import random
import traceback
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from app_plugins.api import TASK_CONTEXT_MENU, PluginCommand, PluginSettingsPage
from model.ApiKeyHelper import (
    API_KEY_STATUSES_CONFIG_KEY,
    format_unix_time,
    normalize_api_keys,
    select_api_keys_for_attempt,
)
from model.AudioHelper import CreateAudio, CreateAudio3, CreateTTSAudio
from model.AudioSettings import audio_profile_voice_count, normalize_audio_settings
from model.SubtitleHelper import generate_srt_whisper_only
from PYUI.main_setting_pyui import AudioProfileDialog


TASK_AUDIO_SUBTITLE_CONFIG_KEY = "task_audio_subtitle"
TASK_MEDIA_SUBMENU = "音频与字幕"
MENU_VISIBILITY_KEYS = (
    ("show_audio_only", "生成任务音频【不带字幕】"),
    ("show_audio_with_subtitle", "生成任务音频【带字幕】"),
    ("show_audio_task_name", "生成任务音频【使用任务名】"),
    ("show_subtitle_default", "生成任务字幕"),
    ("show_subtitle_custom", "生成任务字幕【不使用默认配置】"),
)
DEFAULT_TASK_AUDIO_SUBTITLE_SETTINGS = {
    "show_audio_only": True,
    "show_audio_with_subtitle": True,
    "show_audio_task_name": True,
    "show_subtitle_default": True,
    "show_subtitle_custom": True,
    "subtitle_include_line_breaks": False,
    "subtitle_max_words_per_block": 0,
    "subtitle_block_gap_ms": -1,
    "overwrite_existing_subtitles": False,
}
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a")


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def settings_from_config(config):
    config = config if isinstance(config, dict) else {}
    source = config.get(TASK_AUDIO_SUBTITLE_CONFIG_KEY)
    source = source if isinstance(source, dict) else {}
    result = dict(DEFAULT_TASK_AUDIO_SUBTITLE_SETTINGS)
    for key, _title in MENU_VISIBILITY_KEYS:
        if key in source:
            result[key] = bool(source[key])
    result["subtitle_include_line_breaks"] = bool(source.get(
        "subtitle_include_line_breaks",
        config.get("subtitle_include_line_breaks", False),
    ))
    result["subtitle_max_words_per_block"] = _bounded_int(
        source.get(
            "subtitle_max_words_per_block",
            config.get("subtitle_max_words_per_block", 0),
        ),
        0,
        0,
        50,
    )
    result["subtitle_block_gap_ms"] = _bounded_int(
        source.get(
            "subtitle_block_gap_ms",
            config.get("subtitle_block_gap_ms", -1),
        ),
        -1,
        -1,
        5000,
    )
    result["overwrite_existing_subtitles"] = bool(source.get(
        "overwrite_existing_subtitles", False
    ))
    return result


def find_task_audio_file(target_dir, task_name=""):
    target_dir = Path(target_dir)
    stems = ["task_audio"]
    task_name = str(task_name or "").strip()
    if task_name and task_name.casefold() != "task_audio":
        stems.append(task_name)
    for stem in stems:
        for suffix in AUDIO_SUFFIXES:
            candidate = target_dir / (stem + suffix)
            if candidate.is_file():
                return candidate
    return None


class TaskAudioSubtitleSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        self.settings = dict(DEFAULT_TASK_AUDIO_SUBTITLE_SETTINGS)
        self.audio_settings = {}
        self._audio_settings_source_present = False
        self._audio_settings_modified = False
        layout = QtWidgets.QVBoxLayout(self.widget)
        tabs = QtWidgets.QTabWidget(self.widget)
        layout.addWidget(tabs)

        defaults_tab = QtWidgets.QWidget(tabs)
        defaults_layout = QtWidgets.QVBoxLayout(defaults_tab)
        menu_group = QtWidgets.QGroupBox("任务右键菜单", defaults_tab)
        menu_layout = QtWidgets.QVBoxLayout(menu_group)
        self.menu_checkboxes = {}
        for key, title in MENU_VISIBILITY_KEYS:
            checkbox = QtWidgets.QCheckBox(title, menu_group)
            self.menu_checkboxes[key] = checkbox
            menu_layout.addWidget(checkbox)
        defaults_layout.addWidget(menu_group)

        subtitle_group = QtWidgets.QGroupBox("默认字幕参数", defaults_tab)
        subtitle_form = QtWidgets.QFormLayout(subtitle_group)
        self.line_break_checkbox = QtWidgets.QCheckBox("保留任务文案换行", subtitle_group)
        self.max_words_spinbox = QtWidgets.QSpinBox(subtitle_group)
        self.max_words_spinbox.setRange(0, 50)
        self.max_words_spinbox.setSpecialValueText("按字符")
        self.max_words_spinbox.setSuffix(" 个")
        self.max_words_spinbox.setToolTip("设置 4 时会尽量形成每块 3–4 个单词。")
        self.gap_spinbox = QtWidgets.QSpinBox(subtitle_group)
        self.gap_spinbox.setRange(-1, 5000)
        self.gap_spinbox.setSpecialValueText("保留原间隔")
        self.gap_spinbox.setSuffix(" ms")
        self.gap_spinbox.setToolTip("设置 0 ms 时字幕块首尾相接。")
        self.overwrite_checkbox = QtWidgets.QCheckBox(
            "默认覆盖已经存在的 SRT", subtitle_group
        )
        subtitle_form.addRow("换行：", self.line_break_checkbox)
        subtitle_form.addRow("每块最多单词：", self.max_words_spinbox)
        subtitle_form.addRow("字幕块间隔：", self.gap_spinbox)
        subtitle_form.addRow("已有字幕：", self.overwrite_checkbox)
        defaults_layout.addWidget(subtitle_group)
        hint = QtWidgets.QLabel(
            "主界面的字幕控件仍可用于快速调整；“临时配置”只影响本次右键操作，"
            "不会修改这里的全局默认值。",
            defaults_tab,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        defaults_layout.addWidget(hint)
        defaults_layout.addStretch(1)
        tabs.addTab(defaults_tab, "默认与菜单")

        profiles_tab = QtWidgets.QWidget(tabs)
        profiles_layout = QtWidgets.QVBoxLayout(profiles_tab)
        self.profile_table = QtWidgets.QTableWidget(profiles_tab)
        self.profile_table.setColumnCount(7)
        self.profile_table.setHorizontalHeaderLabels(
            ["名称", "模型", "Voice 数量", "语速", "音调", "稳定性", "性别/标签"]
        )
        self.profile_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.profile_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.profile_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.profile_table.verticalHeader().setVisible(False)
        header = self.profile_table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        profiles_layout.addWidget(self.profile_table, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("新增", profiles_tab)
        self.edit_button = QtWidgets.QPushButton("查看/编辑", profiles_tab)
        self.copy_button = QtWidgets.QPushButton("复制", profiles_tab)
        self.remove_button = QtWidgets.QPushButton("删除", profiles_tab)
        for button in (self.add_button, self.edit_button, self.copy_button, self.remove_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        profiles_layout.addLayout(buttons)
        note = QtWidgets.QLabel(
            "配置名称必须与任务表格中的语音参数一致。双击一项可以编辑。",
            profiles_tab,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        profiles_layout.addWidget(note)
        tabs.addTab(profiles_tab, "AI 语音配置")

        self.add_button.clicked.connect(self.add_profile)
        self.edit_button.clicked.connect(self.edit_profile)
        self.copy_button.clicked.connect(self.copy_profile)
        self.remove_button.clicked.connect(self.remove_profile)
        self.profile_table.itemSelectionChanged.connect(self._update_buttons)
        self.profile_table.doubleClicked.connect(
            lambda index: self.edit_profile(index.row())
        )
        self._update_buttons()

    @staticmethod
    def _number_text(value):
        try:
            return "{:g}".format(float(value))
        except (TypeError, ValueError):
            return ""

    def _refresh_profiles(self, selected_name=""):
        self.profile_table.setRowCount(0)
        selected_row = -1
        for row, (name, profile) in enumerate(self.audio_settings.items()):
            self.profile_table.insertRow(row)
            values = (
                name,
                str(profile.get("model") or ""),
                str(audio_profile_voice_count(profile)),
                self._number_text(profile.get("speed")),
                str(profile.get("pitch") or ""),
                self._number_text(profile.get("stability")),
                str(profile.get("sex") or ""),
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column in (2, 3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.profile_table.setItem(row, column, item)
            if name == selected_name:
                selected_row = row
        if selected_row >= 0:
            self.profile_table.selectRow(selected_row)
        self._update_buttons()

    def _selected_profile(self):
        row = self.profile_table.currentRow()
        item = self.profile_table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else ""

    def _update_buttons(self):
        enabled = bool(self._selected_profile())
        for button in (self.edit_button, self.copy_button, self.remove_button):
            button.setEnabled(enabled)

    def _open_profile_dialog(self, original_name="", profile=None, suggested_name=""):
        dialog = AudioProfileDialog(
            existing_names=self.audio_settings,
            original_name=original_name,
            profile=profile,
            suggested_name=suggested_name,
            parent=self.widget,
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.result_name, dialog.result_profile

    def add_profile(self):
        result = self._open_profile_dialog(suggested_name="新语音配置")
        if result is not None:
            name, profile = result
            self.audio_settings[name] = profile
            self._audio_settings_modified = True
            self._refresh_profiles(name)

    def edit_profile(self, row=None):
        if isinstance(row, int) and row >= 0:
            self.profile_table.selectRow(row)
        original_name = self._selected_profile()
        if not original_name:
            return
        result = self._open_profile_dialog(
            original_name=original_name,
            profile=self.audio_settings[original_name],
        )
        if result is None:
            return
        name, profile = result
        updated = {}
        for existing_name, existing_profile in self.audio_settings.items():
            updated[name if existing_name == original_name else existing_name] = (
                profile if existing_name == original_name else existing_profile
            )
        self.audio_settings = updated
        self._audio_settings_modified = True
        self._refresh_profiles(name)

    def copy_profile(self):
        original_name = self._selected_profile()
        if not original_name:
            return
        suggested = original_name + " - 副本"
        suffix = 2
        folded = {name.casefold() for name in self.audio_settings}
        while suggested.casefold() in folded:
            suggested = f"{original_name} - 副本 {suffix}"
            suffix += 1
        result = self._open_profile_dialog(
            profile=copy.deepcopy(self.audio_settings[original_name]),
            suggested_name=suggested,
        )
        if result is not None:
            name, profile = result
            self.audio_settings[name] = profile
            self._audio_settings_modified = True
            self._refresh_profiles(name)

    def remove_profile(self):
        name = self._selected_profile()
        if not name:
            return
        answer = QtWidgets.QMessageBox.question(
            self.widget,
            "删除语音配置",
            f"确定删除“{name}”吗？\n引用这个名称的任务将无法生成音频。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.audio_settings.pop(name, None)
            self._audio_settings_modified = True
            self._refresh_profiles()

    def load_config(self, config):
        source = config if isinstance(config, dict) else {}
        self.settings = settings_from_config(config)
        self.audio_settings = normalize_audio_settings(
            source.get("audio_settings", {})
        )
        # A host may provide a filtered view containing only its own settings.
        # Missing is not the same as an explicitly empty profile collection:
        # do not let a missing field erase the full config when the dialog is
        # saved.  Explicit user edits still take precedence.
        self._audio_settings_source_present = "audio_settings" in source
        self._audio_settings_modified = False
        for key, checkbox in self.menu_checkboxes.items():
            checkbox.setChecked(bool(self.settings[key]))
        self.line_break_checkbox.setChecked(
            self.settings["subtitle_include_line_breaks"]
        )
        self.max_words_spinbox.setValue(
            self.settings["subtitle_max_words_per_block"]
        )
        self.gap_spinbox.setValue(self.settings["subtitle_block_gap_ms"])
        self.overwrite_checkbox.setChecked(
            self.settings["overwrite_existing_subtitles"]
        )
        self._refresh_profiles()

    def current_settings(self):
        settings = dict(DEFAULT_TASK_AUDIO_SUBTITLE_SETTINGS)
        for key, checkbox in self.menu_checkboxes.items():
            settings[key] = checkbox.isChecked()
        settings.update({
            "subtitle_include_line_breaks": self.line_break_checkbox.isChecked(),
            "subtitle_max_words_per_block": self.max_words_spinbox.value(),
            "subtitle_block_gap_ms": self.gap_spinbox.value(),
            "overwrite_existing_subtitles": self.overwrite_checkbox.isChecked(),
        })
        return settings

    def update_config(self, config):
        settings = self.current_settings()
        config[TASK_AUDIO_SUBTITLE_CONFIG_KEY] = settings
        if (
            self._audio_settings_source_present
            or self._audio_settings_modified
        ):
            config["audio_settings"] = copy.deepcopy(self.audio_settings)
        config["subtitle_include_line_breaks"] = settings[
            "subtitle_include_line_breaks"
        ]
        config["subtitle_max_words_per_block"] = settings[
            "subtitle_max_words_per_block"
        ]
        config["subtitle_block_gap_ms"] = settings["subtitle_block_gap_ms"]


class SubtitleOptionsDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        settings = dict(settings or {})
        self.setWindowTitle("本次字幕配置")
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.line_break_checkbox = QtWidgets.QCheckBox("保留任务文案换行", self)
        self.line_break_checkbox.setChecked(
            bool(settings.get("subtitle_include_line_breaks", False))
        )
        self.max_words_spinbox = QtWidgets.QSpinBox(self)
        self.max_words_spinbox.setRange(0, 50)
        self.max_words_spinbox.setSpecialValueText("按字符")
        self.max_words_spinbox.setSuffix(" 个")
        self.max_words_spinbox.setValue(int(settings.get("subtitle_max_words_per_block", 0)))
        self.gap_spinbox = QtWidgets.QSpinBox(self)
        self.gap_spinbox.setRange(-1, 5000)
        self.gap_spinbox.setSpecialValueText("保留原间隔")
        self.gap_spinbox.setSuffix(" ms")
        self.gap_spinbox.setValue(int(settings.get("subtitle_block_gap_ms", -1)))
        self.overwrite_checkbox = QtWidgets.QCheckBox("覆盖已经存在的 SRT", self)
        self.overwrite_checkbox.setChecked(
            bool(settings.get("overwrite_existing_subtitles", False))
        )
        form.addRow("换行：", self.line_break_checkbox)
        form.addRow("每块最多单词：", self.max_words_spinbox)
        form.addRow("字幕块间隔：", self.gap_spinbox)
        form.addRow("已有字幕：", self.overwrite_checkbox)
        layout.addLayout(form)
        note = QtWidgets.QLabel("这些参数只用于本次操作，不会修改全局默认配置。", self)
        note.setStyleSheet("color:#666;")
        layout.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return {
            "subtitle_include_line_breaks": self.line_break_checkbox.isChecked(),
            "subtitle_max_words_per_block": self.max_words_spinbox.value(),
            "subtitle_block_gap_ms": self.gap_spinbox.value(),
            "overwrite_existing_subtitles": self.overwrite_checkbox.isChecked(),
        }


class TaskAudioSubtitleWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, int)
    completed = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, plugin, operation, jobs, options, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.operation = operation
        self.jobs = list(jobs)
        self.options = dict(options or {})

    def run(self):
        try:
            result = self.plugin._execute_jobs(
                self.operation,
                self.jobs,
                self.options,
                lambda message, level=logging.INFO: self.progress.emit(
                    str(message), int(level)
                ),
                interrupted=self.isInterruptionRequested,
            )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(
                f"{type(error).__name__}: {error}", traceback.format_exc()
            )


class TaskAudioSubtitlePlugin:
    plugin_id = "task_audio_subtitle"
    display_name = "任务音频与字幕"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = dict(DEFAULT_TASK_AUDIO_SUBTITLE_SETTINGS)
        self.audio_settings = {}
        self._audio_settings_source_present = False
        self.worker = None
        self._key_index = 0
        self._bound = False

    def register(self, context):
        self.context = context
        self._load_config(context.load_config())
        commands = (
            ("audio_only", "生成任务音频【不带字幕】", "show_audio_only", self._audio_only),
            ("audio_with_subtitle", "生成任务音频【带字幕】", "show_audio_with_subtitle", self._audio_with_subtitle),
            ("audio_task_name", "生成任务音频【使用任务名】", "show_audio_task_name", self._audio_task_name),
            ("subtitle_default", "生成任务字幕", "show_subtitle_default", self._subtitle_default),
            ("subtitle_custom", "生成任务字幕【不使用默认配置】", "show_subtitle_custom", self._subtitle_custom),
        )
        for order, (command_id, title, visibility_key, callback) in enumerate(commands, 60):
            context.register_command(PluginCommand(
                command_id=command_id,
                title=title,
                callback=callback,
                locations=frozenset({TASK_CONTEXT_MENU}),
                tooltip="只处理当前选中的任务",
                order=order,
                enabled=lambda rows, plugin=self: bool(rows) and not plugin.is_running(),
                submenu=TASK_MEDIA_SUBMENU,
                visible=lambda _rows, key=visibility_key, plugin=self: bool(
                    plugin.settings.get(key, True)
                ),
            ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="音频与字幕插件",
            factory=TaskAudioSubtitleSettingsPage,
            order=105,
        ))

    @property
    def parent(self):
        return self.context.parent_widget

    def _load_config(self, config):
        config = config if isinstance(config, dict) else {}
        self.settings = settings_from_config(config)
        if "audio_settings" in config:
            self.audio_settings = normalize_audio_settings(
                config.get("audio_settings", {})
            )
            self._audio_settings_source_present = True

    def start(self):
        self._load_config(self.context.load_config())
        self._bind_main_controls()
        self._sync_main_controls()

    def _bind_main_controls(self):
        if self._bound:
            return
        window = self.parent
        bindings = (
            ("subtitle_line_break_checkbox", "toggled"),
            ("subtitle_max_words_spinbox", "valueChanged"),
            ("subtitle_gap_ms_spinbox", "valueChanged"),
        )
        for name, signal_name in bindings:
            widget = getattr(window, name, None)
            if widget is not None:
                getattr(widget, signal_name).connect(self._capture_main_controls)
        self._bound = True

    def _capture_main_controls(self, *_args):
        window = self.parent
        line_break = getattr(window, "subtitle_line_break_checkbox", None)
        max_words = getattr(window, "subtitle_max_words_spinbox", None)
        gap = getattr(window, "subtitle_gap_ms_spinbox", None)
        if line_break is not None:
            self.settings["subtitle_include_line_breaks"] = line_break.isChecked()
        if max_words is not None:
            self.settings["subtitle_max_words_per_block"] = max_words.value()
        if gap is not None:
            self.settings["subtitle_block_gap_ms"] = gap.value()

    def _sync_main_controls(self):
        window = self.parent
        widgets = (
            (getattr(window, "subtitle_line_break_checkbox", None), "setChecked", self.settings["subtitle_include_line_breaks"]),
            (getattr(window, "subtitle_max_words_spinbox", None), "setValue", self.settings["subtitle_max_words_per_block"]),
            (getattr(window, "subtitle_gap_ms_spinbox", None), "setValue", self.settings["subtitle_block_gap_ms"]),
        )
        for widget, method, value in widgets:
            if widget is None:
                continue
            widget.blockSignals(True)
            getattr(widget, method)(value)
            widget.blockSignals(False)
        self._sync_busy_controls()

    def apply_settings(self, config):
        self._load_config(config)
        self._sync_main_controls()
        return True

    def update_config(self, config):
        self._capture_main_controls()
        config[TASK_AUDIO_SUBTITLE_CONFIG_KEY] = dict(self.settings)
        if self._audio_settings_source_present or self.audio_settings:
            config["audio_settings"] = copy.deepcopy(self.audio_settings)
        config["subtitle_include_line_breaks"] = self.settings[
            "subtitle_include_line_breaks"
        ]
        config["subtitle_max_words_per_block"] = self.settings[
            "subtitle_max_words_per_block"
        ]
        config["subtitle_block_gap_ms"] = self.settings["subtitle_block_gap_ms"]
        return config

    def is_running(self):
        return self.worker is not None

    def can_close(self):
        if self.is_running():
            return False, "任务音频或字幕仍在生成，请等待完成后再退出。"
        return True, ""

    def stop(self):
        if self.is_running():
            self.worker.requestInterruption()
            self.worker.wait(5000)

    def current_subtitle_settings(self):
        self._capture_main_controls()
        return {
            "subtitle_include_line_breaks": self.settings["subtitle_include_line_breaks"],
            "subtitle_max_words_per_block": self.settings["subtitle_max_words_per_block"],
            "subtitle_block_gap_ms": self.settings["subtitle_block_gap_ms"],
            "overwrite_existing_subtitles": self.settings[
                "overwrite_existing_subtitles"
            ],
        }

    def _audio_only(self, rows):
        self.start_audio(rows, with_subtitles=False, use_task_name=False)

    def _audio_with_subtitle(self, rows):
        self.start_audio(rows, with_subtitles=True, use_task_name=False)

    def _audio_task_name(self, rows):
        self.start_audio(rows, with_subtitles=False, use_task_name=True)

    def _subtitle_default(self, rows):
        self.start_subtitles(rows, self.current_subtitle_settings())

    def _subtitle_custom(self, rows):
        dialog = SubtitleOptionsDialog(self.current_subtitle_settings(), self.parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.start_subtitles(rows, dialog.values())

    def generate_all_audio(self, use_task_name=False, with_subtitles=True):
        rows = range(len(getattr(self.parent, "task_list", []) or []))
        self.start_audio(rows, with_subtitles, use_task_name)

    def generate_all_subtitles(self):
        rows = range(len(getattr(self.parent, "task_list", []) or []))
        self.start_subtitles(rows, self.current_subtitle_settings())

    def _target_jobs(self, rows):
        targets = self.context.task_targets(tuple(rows or ()))
        jobs = []
        for target in targets:
            task = target.get("task")
            if task is None:
                continue
            language = self.context.task_language(task)
            jobs.append({
                "label": target.get("label") or str(getattr(task, "task_id", "")),
                "target_dir": str(target.get("target_dir") or ""),
                "task": task,
                "language": language if language and language != "unknown" else "sk",
            })
        return jobs

    def start_audio(self, rows, with_subtitles=False, use_task_name=False):
        options = self.current_subtitle_settings()
        options.update({
            "with_subtitles": bool(with_subtitles),
            "use_task_name": bool(use_task_name),
        })
        self._start_operation("audio", rows, options)

    def start_subtitles(self, rows, options):
        self._start_operation("subtitle", rows, options)

    def _start_operation(self, operation, rows, options):
        if self.is_running():
            QtWidgets.QMessageBox.information(
                self.parent, "音频与字幕", "已有生成任务正在运行，请等待完成。"
            )
            return
        jobs = self._target_jobs(rows)
        if not jobs:
            return
        worker = TaskAudioSubtitleWorker(
            self, operation, jobs, options, parent=self.parent
        )
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self.worker = worker
        self._sync_busy_controls()
        title = "任务音频" if operation == "audio" else "任务字幕"
        self.context.log(f"[{title}] 开始处理 {len(jobs)} 个任务。")
        worker.start()

    def _sync_busy_controls(self):
        busy = self.is_running()
        for name, normal_text in (("gen_audio_btn", "生成音频"), ("gen_vtt_btn", "生成字幕")):
            button = getattr(self.parent, name, None)
            if button is not None:
                button.setEnabled(not busy)
                button.setText("正在处理…" if busy else normal_text)

    def _on_progress(self, message, level):
        self.context.log(message, level=level)

    def _on_completed(self, result):
        summary = str(result.get("summary") or "处理完成。")
        self.context.log(summary, level=logging.ERROR if result.get("failed") else logging.INFO)
        self.context.notify(
            "音频与字幕处理完成",
            summary,
            critical=bool(result.get("failed")),
        )
        if result.get("failed"):
            QtWidgets.QMessageBox.warning(
                self.parent,
                "部分任务处理失败",
                summary + "\n\n详细信息已写入程序日志。",
            )

    def _on_failed(self, message, details):
        self.context.log(f"[音频与字幕错误] {message}\n{details}", level=logging.ERROR)
        QtWidgets.QMessageBox.critical(
            self.parent,
            "音频与字幕处理失败",
            f"{message}\n\n完整错误已写入程序日志。",
        )

    def _on_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_busy_controls()

    def _select_api_keys(self, progress):
        config = self.context.load_config()
        api_keys = config.get("elevenlabs_api_keys")
        if api_keys is None:
            api_keys = config.get("elevenlabs_api_key", "")
        api_keys = normalize_api_keys(api_keys)
        if not api_keys:
            return []
        selected, next_index, selection = select_api_keys_for_attempt(
            api_keys,
            config.get(API_KEY_STATUSES_CONFIG_KEY, {}),
            self._key_index,
        )
        self._key_index = next_index
        if selected:
            return selected
        mode = selection.get("mode")
        if mode == "waiting_exhausted":
            key = selection.get("next_key", "")
            progress(
                "所有 ElevenLabs Key 的额度都已用完；距离刷新最近的 Key "
                f"(...{key[-4:]}) 将在 {format_unix_time(selection.get('next_retry_unix'))} 后测试。",
                logging.ERROR,
            )
        elif mode == "waiting_cooldown":
            progress(
                "所有 ElevenLabs Key 都在冷却中，最早将在 "
                f"{format_unix_time(selection.get('next_retry_unix'))} 后重试。",
                logging.ERROR,
            )
        else:
            progress("没有可用的 ElevenLabs API Key。", logging.ERROR)
        return []

    def _create_audio(self, task, result_file, progress):
        audio_type = str(getattr(task, "task_audio_type", "") or "").strip()
        profile = self.audio_settings.get(audio_type)
        if profile is None:
            raise ValueError(f"语音参数“{audio_type or '空'}”没有对应配置")
        text = str(getattr(task, "task_audio_text", "") or "").strip()
        if not text:
            raise ValueError("任务语音文案为空")
        model = profile.get("model")
        if model == "edge":
            speed = float(profile.get("speed", 1.0))
            CreateTTSAudio(
                text,
                profile["sex"],
                str(result_file),
                rate="{:+d}%".format(round((speed - 1.0) * 100)),
                pitch=profile.get("pitch", "+0Hz"),
                progress_callback=progress,
            )
            return result_file.exists()
        if model == "elevenlabs":
            voices = profile.get("voices") or []
            voice_ids = profile.get("voice_ids") or []
            speed = float(profile.get("speed", 1.0))
            if voices:
                voice = random.choice(voices)
                voice_id = voice.get("id")
                speed = float(voice.get("speed", speed))
            elif voice_ids:
                voice_id = random.choice(voice_ids)
            else:
                raise ValueError("ElevenLabs 语音配置中没有 Voice ID")
            created = CreateAudio(
                text,
                str(result_file),
                voice_id=voice_id,
                speed=speed,
                api_keys=self._select_api_keys(progress),
                api_key_status_config=getattr(self.parent, "config_name", "config.json"),
                progress_callback=progress,
            )
            return created is not False and result_file.exists()
        if model == "elevenlabs3":
            voice_ids = profile.get("voice_ids") or []
            if not voice_ids:
                raise ValueError("ElevenLabs v3 语音配置中没有 Voice ID")
            created = CreateAudio3(
                text,
                str(result_file),
                voice_id=random.choice(voice_ids),
                stability=float(profile.get("stability", 0.35)),
                api_keys=self._select_api_keys(progress),
                api_key_status_config=getattr(self.parent, "config_name", "config.json"),
                progress_callback=progress,
            )
            return created is not False and result_file.exists()
        raise ValueError(f"不支持的音频模型：{model or '空'}")

    def _create_subtitle(self, audio_file, task, language, options):
        subtitle_file = Path(audio_file).with_suffix(".srt")
        overwrite = bool(options.get("overwrite_existing_subtitles", False))
        if subtitle_file.exists() and not overwrite:
            return "existing", subtitle_file
        gap_ms = _bounded_int(
            options.get("subtitle_block_gap_ms", -1), -1, -1, 5000
        )
        generate_srt_whisper_only(
            str(audio_file),
            str(getattr(task, "task_audio_text", "") or ""),
            str(subtitle_file),
            language=str(language or "sk"),
            include_line_breaks=bool(
                options.get("subtitle_include_line_breaks", False)
            ),
            max_words_per_block=_bounded_int(
                options.get("subtitle_max_words_per_block", 0), 0, 0, 50
            ),
            block_gap_ms=None if gap_ms < 0 else gap_ms,
            model=self.context.whisper_model(),
        )
        return "generated", subtitle_file

    def _execute_jobs(self, operation, jobs, options, progress, interrupted=lambda: False):
        stats = {
            "audio_generated": 0,
            "audio_existing": 0,
            "subtitle_generated": 0,
            "subtitle_existing": 0,
            "missing_audio": 0,
            "failed": 0,
            "cancelled": 0,
        }
        failures = []
        total = len(jobs)
        for index, job in enumerate(jobs, 1):
            if interrupted():
                stats["cancelled"] = total - index + 1
                break
            task = job["task"]
            label = str(job.get("label") or index)
            target_dir = Path(job["target_dir"])
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                progress(f"[{index}/{total}] {label}")
                if operation == "audio":
                    use_task_name = bool(options.get("use_task_name", False))
                    stem = (
                        str(getattr(task, "task_name", "") or "").strip()
                        if use_task_name else "task_audio"
                    ) or "task_audio"
                    audio_file = target_dir / (stem + ".mp3")
                    if audio_file.exists():
                        stats["audio_existing"] += 1
                        progress(f"已有音频，跳过生成：{audio_file.name}")
                    elif self._create_audio(task, audio_file, progress):
                        stats["audio_generated"] += 1
                        progress(f"音频已生成：{audio_file.name}")
                    else:
                        raise RuntimeError("音频接口没有返回有效文件")
                    if options.get("with_subtitles"):
                        state, subtitle_file = self._create_subtitle(
                            audio_file, task, job.get("language"), options
                        )
                        stats[f"subtitle_{state}"] += 1
                        progress(f"字幕{'已生成' if state == 'generated' else '已存在'}：{subtitle_file.name}")
                else:
                    audio_file = find_task_audio_file(
                        target_dir, getattr(task, "task_name", "")
                    )
                    if audio_file is None:
                        stats["missing_audio"] += 1
                        progress(f"{label}：没有找到 task_audio 或任务名音频，已跳过", logging.ERROR)
                        continue
                    state, subtitle_file = self._create_subtitle(
                        audio_file, task, job.get("language"), options
                    )
                    stats[f"subtitle_{state}"] += 1
                    progress(f"字幕{'已生成' if state == 'generated' else '已存在'}：{subtitle_file.name}")
            except Exception as error:
                stats["failed"] += 1
                failures.append(f"{label}：{type(error).__name__}: {error}")
                progress(failures[-1], logging.ERROR)
        summary = (
            "音频与字幕处理完成："
            f"音频新生成 {stats['audio_generated']}、已有 {stats['audio_existing']}；"
            f"字幕新生成 {stats['subtitle_generated']}、已有 {stats['subtitle_existing']}；"
            f"缺少音频 {stats['missing_audio']}、失败 {stats['failed']}"
        )
        if stats["cancelled"]:
            summary += f"、取消 {stats['cancelled']}"
        return {"summary": summary, "failures": failures, **stats}

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets

from PYUI.main_setting_pyui import _write_config_with_rolling_backups

from app_plugins.builtin.task_audio_subtitle import (
    TASK_AUDIO_SUBTITLE_CONFIG_KEY,
    TaskAudioSubtitlePlugin,
    TaskAudioSubtitleSettingsPage,
    find_task_audio_file,
    settings_from_config,
)
from app_plugins.host import PluginHost


class _MainWindow(QtWidgets.QWidget):
    def __init__(self, config=None):
        super().__init__()
        self.config = dict(config or {})
        self.logs = []

    def load_config(self):
        return dict(self.config)

    def saveCurrentConfig(self):
        return True

    def appendLog(self, message, end="", level=None):
        self.logs.append(str(message))

    def showDesktopNotification(self, title, message, critical=False):
        pass

    def selectedTaskRows(self):
        return []

    def taskTargetsForRows(self, rows, require_loaded=True):
        return []


class TaskAudioSubtitlePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_legacy_subtitle_values_are_used_as_plugin_defaults(self):
        settings = settings_from_config({
            "subtitle_include_line_breaks": True,
            "subtitle_max_words_per_block": 4,
            "subtitle_block_gap_ms": 0,
        })
        self.assertTrue(settings["subtitle_include_line_breaks"])
        self.assertEqual(settings["subtitle_max_words_per_block"], 4)
        self.assertEqual(settings["subtitle_block_gap_ms"], 0)

    def test_settings_page_round_trips_menu_visibility_and_audio_profiles(self):
        page = TaskAudioSubtitleSettingsPage()
        try:
            page.load_config({
                TASK_AUDIO_SUBTITLE_CONFIG_KEY: {
                    "show_audio_only": False,
                    "subtitle_max_words_per_block": 3,
                },
                "audio_settings": {
                    "demo": {
                        "model": "edge",
                        "sex": "女声",
                        "speed": 1.1,
                        "pitch": "+0Hz",
                    }
                },
            })
            saved = {}
            page.update_config(saved)
            self.assertFalse(saved[TASK_AUDIO_SUBTITLE_CONFIG_KEY]["show_audio_only"])
            self.assertEqual(
                saved[TASK_AUDIO_SUBTITLE_CONFIG_KEY]["subtitle_max_words_per_block"],
                3,
            )
            self.assertIn("demo", saved["audio_settings"])
            self.assertEqual(page.profile_table.rowCount(), 1)
        finally:
            page.widget.deleteLater()

    def test_filtered_host_config_does_not_erase_audio_profiles(self):
        page = TaskAudioSubtitleSettingsPage()
        try:
            page.load_config({
                TASK_AUDIO_SUBTITLE_CONFIG_KEY: {
                    "show_audio_only": False,
                },
            })
            saved = {"keep": "unrelated"}
            page.update_config(saved)
            self.assertNotIn("audio_settings", saved)
            self.assertEqual(saved["keep"], "unrelated")
        finally:
            page.widget.deleteLater()

    def test_config_writer_keeps_recovery_backup_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            original = {
                "audio_settings": {"voice": {"model": "edge"}},
                "marker": "before",
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            updated = dict(original, marker="after")

            _write_config_with_rolling_backups(path, updated)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                updated,
            )
            backups = list(path.parent.glob("config.json.bak-settings-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                json.loads(backups[0].read_text(encoding="utf-8")),
                original,
            )
            self.assertFalse(path.with_name("config.json.settings.tmp").exists())

    def test_hidden_context_commands_are_absent_and_visible_ones_share_submenu(self):
        main = _MainWindow({
            TASK_AUDIO_SUBTITLE_CONFIG_KEY: {
                "show_audio_only": False,
                "show_audio_with_subtitle": True,
                "show_audio_task_name": False,
                "show_subtitle_default": True,
                "show_subtitle_custom": False,
            }
        })
        try:
            host = PluginHost(main)
            host.install(TaskAudioSubtitlePlugin())
            menu = QtWidgets.QMenu(main)
            actions = host.populate_task_context_menu(menu, [1])
            self.assertEqual(
                [action.text() for action in actions],
                ["生成任务音频【带字幕】", "生成任务字幕"],
            )
            self.assertEqual(len(menu.actions()), 1)
            self.assertEqual(menu.actions()[0].menu().title(), "音频与字幕")
        finally:
            main.deleteLater()

    def test_audio_lookup_supports_task_name_outputs_after_default_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            named = root / "My Task.mp3"
            named.write_bytes(b"named")
            self.assertEqual(find_task_audio_file(root, "My Task"), named)
            default = root / "task_audio.wav"
            default.write_bytes(b"default")
            self.assertEqual(find_task_audio_file(root, "My Task"), default)

    def test_non_slovak_audio_warning_can_cancel_or_mute_only_this_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = QtWidgets.QWidget()
            plugin = TaskAudioSubtitlePlugin()
            plugin.context = SimpleNamespace(parent_widget=parent)
            task = SimpleNamespace(task_name="English task", task_audio_text="Hello world")
            jobs = [{
                "label": "7 | English task", "target_dir": temporary,
                "task": task, "language": "en", "detected_language": "en",
            }]
            seen = []

            def answer(choice, mute=False):
                def respond():
                    dialog = QtWidgets.QApplication.activeModalWidget()
                    seen.append(dialog.text())
                    if mute:
                        dialog.checkBox().setChecked(True)
                    dialog.button(choice).click()
                QtCore.QTimer.singleShot(0, respond)
                return plugin._confirm_audio_language(jobs, {"use_task_name": False})

            try:
                self.assertFalse(answer(QtWidgets.QMessageBox.StandardButton.No, mute=True))
                self.assertFalse(plugin._suppress_language_warning_this_run)
                self.assertTrue(answer(QtWidgets.QMessageBox.StandardButton.Yes, mute=True))
                self.assertTrue(plugin._suppress_language_warning_this_run)
                self.assertTrue(plugin._confirm_audio_language(jobs, {}))
                self.assertEqual(len(seen), 2)
                self.assertIn("斯洛伐克语", seen[0])
                self.assertFalse(TaskAudioSubtitlePlugin()._suppress_language_warning_this_run)
            finally:
                parent.deleteLater()

    def test_audio_language_warning_skips_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = QtWidgets.QWidget()
            plugin = TaskAudioSubtitlePlugin()
            plugin.context = SimpleNamespace(parent_widget=parent)
            Path(temporary, "task_audio.mp3").write_bytes(b"already generated")
            jobs = [{
                "label": "English", "target_dir": temporary,
                "task": SimpleNamespace(task_audio_text="Hello world", task_name="English"),
                "language": "en", "detected_language": "en",
            }]
            try:
                self.assertTrue(plugin._confirm_audio_language(jobs, {}))
                with patch.object(plugin, "_target_jobs", return_value=jobs), \
                     patch.object(plugin, "_confirm_audio_language", return_value=False), \
                     patch("app_plugins.builtin.task_audio_subtitle.TaskAudioSubtitleWorker") as worker:
                    plugin.context.log = lambda *_args: None
                    plugin.start_audio([0])
                    worker.assert_not_called()
            finally:
                parent.deleteLater()

    def test_audio_operation_uses_requested_filename_and_optional_subtitle(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = TaskAudioSubtitlePlugin()
            created_audio = []
            created_subtitles = []

            def create_audio(_task, path, _progress):
                path.write_bytes(b"audio")
                created_audio.append(path)
                return True

            def create_subtitle(path, _task, _language, _options):
                subtitle = Path(path).with_suffix(".srt")
                subtitle.write_text("demo", encoding="utf-8")
                created_subtitles.append(subtitle)
                return "generated", subtitle

            plugin._create_audio = create_audio
            plugin._create_subtitle = create_subtitle
            task = SimpleNamespace(
                task_id="1",
                task_name="Friendly name",
                task_audio_type="demo",
                task_audio_text="hello",
            )
            jobs = [{
                "label": "1 | Friendly name",
                "target_dir": temporary,
                "task": task,
                "language": "en",
            }]
            result = plugin._execute_jobs(
                "audio",
                jobs,
                {"use_task_name": True, "with_subtitles": True},
                lambda *_args: None,
            )

            self.assertEqual(created_audio[0].name, "Friendly name.mp3")
            self.assertEqual(created_subtitles[0].name, "Friendly name.srt")
            self.assertEqual(result["audio_generated"], 1)
            self.assertEqual(result["subtitle_generated"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["failures"], [])


if __name__ == "__main__":
    unittest.main()

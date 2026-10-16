import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets, Qt
from model.AppDataStorage import ApplicationStorage
from model.ChromeLanguageEnvironment import ChromeLanguageEnvironmentStore, LANGUAGE_PRESETS
from PYUI.chrome_language_environment_pyui import ChromeLanguageEnvironmentPanel
from PYUI.chrome_runner_pyui import ChromeRunnerDialog


class ChromeLanguageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = ApplicationStorage(self.root).for_plugin("chrome_launcher")
        self.store = ChromeLanguageEnvironmentStore(self.storage)

    def test_listing_does_not_create_files(self):
        self.assertEqual(self.store.list_environments(), [])
        self.assertFalse(self.storage.path.exists())

    def test_generator_seeds_only_new_isolated_foreign_preferences(self):
        for key, preset in LANGUAGE_PRESETS.items():
            with self.subTest(language=key):
                record = self.store.create(preset[3], key)
                directory = self.store.environment_directory(record["id"])
                state = json.loads((directory / "User Data/Local State").read_text(encoding="utf-8"))
                prefs = json.loads((directory / "User Data/Default/Preferences").read_text(encoding="utf-8"))
                self.assertEqual(state["intl"]["app_locale"], preset[0])
                self.assertEqual(prefs["intl"]["selected_languages"], ",".join(preset[1]))
                self.assertNotIn("accept_languages", prefs["intl"])
                self.assertFalse(any(value.startswith("zh") for value in preset[1]))
                self.assertEqual(prefs["spellcheck"]["dictionaries"], [preset[2]])
                self.assertFalse(prefs["spellcheck"]["use_spelling_service"])
                self.assertNotIn("safebrowsing", prefs)
                self.assertNotIn("proxy", prefs)
                self.assertTrue((directory / "language-check.html").is_file())
        self.assertEqual(len(self.store.list_environments()), len(LANGUAGE_PRESETS))

    def test_name_is_metadata_not_a_user_data_path(self):
        record = self.store.create("../My English browser")
        directory = self.store.environment_directory(record["id"])
        self.assertEqual(directory.parent, self.store.root.path)
        self.assertEqual(directory.name, record["id"])
        self.assertEqual(len(directory.name), 32)

    def test_duplicate_invalid_name_or_chinese_locale_cannot_overwrite(self):
        record = self.store.create("English signup")
        directory = self.store.environment_directory(record["id"])
        original = (directory / "User Data/Default/Preferences").read_bytes()
        for name, language in ((" ENGLISH SIGNUP ", "en-US"), ("", "en-US"),
                               ("x\ny", "en-US"), ("x" * 81, "en-US"), ("x", "zh-CN")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create(name, language)
        self.assertEqual(len(self.store.list_environments()), 1)
        self.assertEqual((directory / "User Data/Default/Preferences").read_bytes(), original)

    def test_failure_does_not_commit_a_partial_environment(self):
        from model.AppDataStorage import ManagedFile
        original = ManagedFile.write_json
        def fail_preferences(file, value, **options):
            if file.path.name == "Preferences":
                raise OSError("synthetic disk error")
            return original(file, value, **options)
        with patch.object(ManagedFile, "write_json", fail_preferences):
            with self.assertRaisesRegex(OSError, "synthetic"):
                self.store.create("English")
        self.assertEqual(self.store.list_environments(), [])
        self.assertEqual(list(self.store.root.path.iterdir()), [])

    def test_reopening_preserves_browser_preferences_and_credentials_byte_for_byte(self):
        record = self.store.create("English")
        directory = self.store.environment_directory(record["id"])
        profile = directory / "User Data/Default"
        (profile / "Cookies").write_bytes(b"synthetic cookies")
        (profile / "Login Data").write_bytes(b"synthetic passwords")
        prefs = profile / "Preferences"
        prefs.write_text('{"intl":{"selected_languages":"en-US,en"},"future":"keep"}', encoding="utf-8")
        before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        restored = ChromeLanguageEnvironmentStore(self.storage)
        self.assertEqual(restored.list_environments(), [record])
        with patch("model.ChromeLanguageEnvironment.subprocess.Popen") as popen:
            for destination in ("newtab", "check", "languages"):
                restored.launch("chrome.exe", record["id"], destination)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(before, {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()})

    def test_launch_is_native_and_separate_without_unsafe_flags(self):
        record = self.store.create("English UK", "en-GB")
        command = self.store.launch_command("chrome.exe", record["id"])
        self.assertIn("--lang=en-GB", command)
        self.assertIn("--disable-sync", command)
        self.assertEqual(command[-1], "chrome://newtab/")
        self.assertTrue(any(value.startswith("--user-data-dir=") and "language_environments" in value for value in command))
        for flag in ("--no-sandbox", "--disable-web-security", "--ignore-certificate-errors",
                     "--proxy-server", "--user-agent", "--remote-debugging-port"):
            self.assertFalse(any(value.startswith(flag) for value in command))
        with patch("model.ChromeLanguageEnvironment.subprocess.Popen") as popen:
            self.store.launch("chrome.exe", record["id"])
        popen.assert_called_once_with(command)

    def test_invalid_ids_and_destinations_are_rejected(self):
        record = self.store.create("English")
        for value in ("../Default", "C:/Users", "a" * 31, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.launch_command("chrome.exe", value)
        with self.assertRaises(ValueError):
            self.store.launch_command("chrome.exe", record["id"], "--no-sandbox")

    def test_missing_browser_files_do_not_create_an_empty_replacement(self):
        record = self.store.create("English")
        directory = self.store.environment_directory(record["id"])
        prefs = directory / "User Data/Default/Preferences"
        prefs.unlink()
        with self.assertRaisesRegex(ValueError, "配置文件缺失"):
            self.store.launch_command("chrome.exe", record["id"])
        self.assertFalse(prefs.exists())

    def test_corrupt_metadata_is_reported_and_never_reset(self):
        record = self.store.create("English")
        metadata = self.store.environment_directory(record["id"]) / "environment.json"
        metadata.write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未覆盖原数据"):
            self.store.list_environments()
        self.assertEqual(metadata.read_text(encoding="utf-8"), "broken")

    def test_launch_errors_and_offline_mode_leave_data_unchanged(self):
        record = self.store.create("English")
        with patch("model.ChromeLanguageEnvironment.subprocess.Popen", side_effect=OSError("start failed")):
            with self.assertRaisesRegex(OSError, "start failed"):
                self.store.launch("chrome.exe", record["id"])
        with patch("model.ChromeLanguageEnvironment.require_online", side_effect=PermissionError("offline")), \
             patch("model.ChromeLanguageEnvironment.subprocess.Popen") as popen:
            with self.assertRaisesRegex(PermissionError, "offline"):
                self.store.launch("chrome.exe", record["id"])
            popen.assert_not_called()
        self.assertEqual(self.store.list_environments(), [record])

    def test_runtime_browser_data_is_git_ignored(self):
        ignore = Path(__file__).resolve().parents[1] / ".gitignore"
        self.assertIn("/PluginData/", ignore.read_text(encoding="utf-8").splitlines())


class ChromeLanguageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = ApplicationStorage(self.temporary.name).for_plugin("chrome_launcher")
        self.panel = ChromeLanguageEnvironmentPanel(lambda: "chrome.exe", storage=self.storage)
        self.addCleanup(self.temporary.cleanup)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_create_select_and_open_actions_use_selected_environment(self):
        first = self.panel.store.create("English US")
        second = self.panel.store.create("English UK", "en-GB")
        self.panel.refresh(second["id"])
        self.assertEqual(self.panel.selected_id(), second["id"])
        self.assertIn("en-GB, en", self.panel.details.text())
        with patch.object(self.panel.store, "launch") as launch:
            self.panel.open_button.click()
            launch.assert_called_once_with("chrome.exe", second["id"], "newtab")
            launch.reset_mock()
            self.panel.more_button.menu().actions()[0].trigger()
            launch.assert_called_once_with("chrome.exe", second["id"], "check")
        self.assertNotEqual(first["id"], second["id"])

    def test_generator_is_a_working_ui_not_a_dead_button(self):
        observations = []
        def fill():
            dialog = self.panel.findChild(QtWidgets.QDialog)
            try:
                dialog.findChild(QtWidgets.QLineEdit).setText("Generated English")
                dialog.findChild(QtWidgets.QComboBox).setCurrentIndex(1)
                buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
                buttons.button(QtWidgets.QDialogButtonBox.Ok).click()
                observations.append(dialog.result())
            finally:
                dialog.reject()
        QtCore.QTimer.singleShot(0, fill)
        self.panel.create_button.click()
        self.assertEqual(observations, [QtWidgets.QDialog.Accepted])
        record = self.panel.store.list_environments()[0]
        self.assertEqual(record["name"], "Generated English")
        self.assertEqual(record["language"], "en-GB")
        self.assertEqual(self.panel.selected_id(), record["id"])

    def test_native_launch_failure_is_a_user_dialog_not_an_uncaught_qt_exception(self):
        record = self.panel.store.create("English")
        self.panel.refresh(record["id"])
        with patch.object(self.panel.store, "launch", side_effect=OSError("failure")), \
             patch.object(QtWidgets.QMessageBox, "warning") as warning:
            self.panel.open_button.click()
            warning.assert_called_once()
            self.assertIn("未改变", self.panel.status.text())

    def test_corrupt_configuration_disables_generator_instead_of_resetting(self):
        record = self.panel.store.create("English")
        metadata = self.panel.store.environment_directory(record["id"]) / "environment.json"
        metadata.write_text("broken", encoding="utf-8")
        self.panel.refresh()
        self.assertIn("未覆盖原数据", self.panel.status.text())
        self.assertFalse(self.panel.create_button.isEnabled())
        self.assertFalse(self.panel.open_button.isEnabled())

    def test_generator_validation_stays_open_and_keeps_existing_environment(self):
        record = self.panel.store.create("English signup")
        observed = []
        def duplicate():
            dialog = self.panel.findChild(QtWidgets.QDialog)
            try:
                buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
                buttons.button(QtWidgets.QDialogButtonBox.Ok).click()
                observed.append(any("同名" in label.text() for label in dialog.findChildren(QtWidgets.QLabel)))
            finally:
                dialog.reject()
        QtCore.QTimer.singleShot(0, duplicate)
        self.panel.create_button.click()
        self.assertEqual(observed, [True])
        self.assertEqual(self.panel.store.list_environments(), [record])

    def test_folder_action_and_restore_use_host_owned_location(self):
        record = self.panel.store.create("English")
        self.panel.refresh(record["id"])
        with patch("PYUI.chrome_language_environment_pyui.QtGui.QDesktopServices.openUrl", return_value=True) as open_url:
            self.panel.open_directory()
        self.assertEqual(open_url.call_args.args[0].toLocalFile(),
                         str(self.panel.store.environment_directory(record["id"])).replace("\\", "/"))
        second = ChromeLanguageEnvironmentPanel(lambda: "chrome.exe", storage=self.storage)
        try:
            self.assertEqual(second.selected_id(), record["id"])
        finally:
            second.deleteLater()

    def test_existing_launcher_layout_and_config_are_preserved(self):
        config = Path(self.temporary.name) / "config.json"
        original = '{"keep":"original","chrome_preset_websites":[]}'
        config.write_text(original, encoding="utf-8")
        with patch.object(ChromeRunnerDialog, "load_profiles", lambda _self: None):
            dialog = ChromeRunnerDialog(config_path=str(config), language_storage=self.storage)
        try:
            self.assertEqual([dialog.environment_tabs.tabText(index) for index in range(2)],
                             ["任务浏览器", "外语独立环境"])
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertIs(dialog.chrome_list_widget.window(), dialog)
            self.assertIs(dialog.run_btn.window(), dialog)
            ordinary = dialog.environment_tabs.widget(0)
            self.assertTrue(ordinary.isAncestorOf(dialog.chrome_list_widget))
            self.assertTrue(ordinary.isAncestorOf(dialog.add_web_btn))
            self.assertTrue(ordinary.isAncestorOf(dialog.run_btn))
            dialog.show()
            dialog.environment_tabs.setCurrentIndex(1)
            self.app.processEvents()
            self.assertFalse(dialog.chrome_list_widget.isVisible())
            self.assertFalse(dialog.add_web_btn.isVisible())
            self.assertFalse(dialog.run_btn.isVisible())
            self.assertTrue(dialog.language_environment_panel.isVisible())
            record = dialog.language_environment_panel.store.create("English")
            self.assertEqual(dialog.profiles, [])
            self.assertEqual(dialog.iterator_profile_directories, [])
            self.assertIsNotNone(record["id"])
            self.assertEqual(config.read_text(encoding="utf-8"), original)
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

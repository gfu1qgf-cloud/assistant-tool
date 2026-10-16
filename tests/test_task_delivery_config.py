import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.task_delivery_config import (
    TaskDeliveryConfigDialog, delivery_config_updates, missing_delivery_fields,
)
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
from model.TaskResultOrganizer import load_effective_config


def complete_config():
    return {
        "drive_parent_folder_id": "test-folder",
        "video_filename_creator_marker": "ABC",
        "task_submission_creator_marker": "ABC",
        "task_submission_creator": "Test creator",
        "review_sheet_submitter": "Test reviewer",
        "task_submission_sheet_url": "https://docs.google.com/spreadsheets/d/test",
        "review_sheet_url": "https://docs.google.com/spreadsheets/d/review",
    }


class RequirementTests(unittest.TestCase):
    def test_complete_configuration_needs_nothing(self):
        self.assertEqual(missing_delivery_fields(complete_config()), {})

    def test_local_only_does_not_require_upload_fields(self):
        for value in (False, "false", "0"):
            self.assertEqual(missing_delivery_fields({"run_upload": value}), {})

    def test_share_config_prompts_for_all_missing_identities(self):
        config = complete_config()
        for key in ("video_filename_creator_marker", "task_submission_creator_marker",
                    "task_submission_creator", "review_sheet_submitter"):
            config[key] = ""
        self.assertEqual(set(missing_delivery_fields(config)), {
            "creator_marker", "task_submission_creator", "review_sheet_submitter",
        })

    def test_missing_optional_sheets_are_not_forced(self):
        self.assertEqual(missing_delivery_fields({
            "drive_parent_folder_id": "folder", "video_filename_creator_marker": "ABC",
        }), {})

    def test_disabled_sheets_do_not_require_names(self):
        config = complete_config()
        config.update(task_submission_sheet_enabled=False, review_sheet_enabled=False,
                      task_submission_creator="", review_sheet_submitter="")
        self.assertEqual(missing_delivery_fields(config), {})

    def test_full_folder_upload_without_sheets_needs_no_marker(self):
        self.assertEqual(missing_delivery_fields({
            "drive_parent_folder_id": "folder", "upload_only_changed_files": False,
        }), {})

    def test_legacy_marker_fallback_does_not_prompt(self):
        config = complete_config()
        del config["task_submission_creator_marker"]
        self.assertEqual(missing_delivery_fields(config), {})

    def test_explicit_blank_submission_marker_can_reuse_existing_marker(self):
        config = complete_config()
        config["task_submission_creator_marker"] = ""
        self.assertEqual(missing_delivery_fields(config), {"creator_marker": "ABC"})
        self.assertEqual(delivery_config_updates(config, {"creator_marker": "ABC"}), {
            "task_submission_creator_marker": "ABC",
        })

    def test_missing_video_marker_does_not_overwrite_other_marker(self):
        config = complete_config()
        config["video_filename_creator_marker"] = ""
        self.assertEqual(delivery_config_updates(config, {"creator_marker": "NEW"}), {
            "video_filename_creator_marker": "NEW",
        })

    def test_values_are_trimmed_and_only_missing_fields_are_patched(self):
        config = {"drive_parent_folder_id": "folder", "private": ["keep"]}
        self.assertEqual(delivery_config_updates(config, {
            "creator_marker": " NEW ", "drive_parent_folder_id": "bad-override",
            "private": [],
        }), {"video_filename_creator_marker": "NEW", "task_submission_creator_marker": "NEW"})
        self.assertEqual(config["private"], ["keep"])

    def test_reject_blank_control_chars_and_invalid_marker(self):
        for value in (" ", "A\nB", "A/B"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delivery_config_updates({"drive_parent_folder_id": "folder"}, {"creator_marker": value})

    def test_drive_folder_input_is_validated_without_network(self):
        config = {"upload_only_changed_files": False}
        for value in ("folder_id", "https://drive.google.com/drive/folders/folder_id?usp=sharing"):
            self.assertEqual(delivery_config_updates(config, {"drive_parent_folder_id": value}), {
                "drive_parent_folder_id": value,
            })
        for value in ("https://example.com/drive/folders/folder_id",
                      "https://drive.google.com/file/d/file_id/view", "not a link"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delivery_config_updates(config, {"drive_parent_folder_id": value})


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_dialog_accepts_only_valid_complete_input(self):
        dialog = TaskDeliveryConfigDialog({})
        try:
            dialog.accept()
            self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
            self.assertFalse(dialog.error_label.isHidden())
            dialog.editors["drive_parent_folder_id"].setText("folder")
            dialog.editors["creator_marker"].setText("ABC")
            dialog.accept()
            self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.config_updates(), {
                "drive_parent_folder_id": "folder", "video_filename_creator_marker": "ABC",
                "task_submission_creator_marker": "ABC",
            })
        finally:
            dialog.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_dialog_has_no_default_personal_identity(self):
        config = complete_config()
        for key in ("video_filename_creator_marker", "task_submission_creator_marker",
                    "task_submission_creator", "review_sheet_submitter"):
            config[key] = ""
        dialog = TaskDeliveryConfigDialog(config)
        try:
            self.assertTrue(all(not editor.text() for editor in dialog.editors.values()))
            dialog.reject()
            self.assertEqual(dialog.config_updates(), {})
        finally:
            dialog.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = QtWidgets.QWidget()
        self.window.task_path_edit = QtWidgets.QLineEdit(self.temp.name, self.window)
        self.window.dateEdit = QtWidgets.QDateEdit(QtCore.QDate(2026, 10, 6), self.window)
        self.config = complete_config()
        self.log = Mock()
        self.context = SimpleNamespace(parent_widget=self.window, log=self.log,
            load_config=lambda: deepcopy(self.config), gemini_keys=object(), patch_config=Mock())
        from model.BackgroundTasks import BackgroundTaskManager
        self.context.background_tasks = BackgroundTaskManager(self.window)

        def save(updates, *, expected):
            self.assertEqual(expected, self.config)
            self.config.update(updates)
            return True

        self.context.patch_config.side_effect = save
        self.controller = TaskDeliveryController(SimpleNamespace(quick_actions=None), self.context)
        self.dialog = Mock()
        self.dialog.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
        self.dialog.config_updates.return_value = {"video_filename_creator_marker": "ABC"}
        self.factory = patch("app_plugins.builtin.task_delivery_controller.TaskResultOrganizerThread")
        self.thread_factory = self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.context.background_tasks.shutdown()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def make_missing(self):
        self.config["video_filename_creator_marker"] = ""

    def test_complete_config_starts_without_prompt(self):
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog") as dialog:
            result = self.controller.organize_results()
        dialog.assert_not_called()
        self.context.patch_config.assert_not_called()
        self.assertIs(result, self.thread_factory.return_value)
        result.start.assert_called_once()

    def test_save_happens_before_worker_is_created(self):
        self.make_missing()

        def shown():
            self.thread_factory.assert_not_called()
            self.assertTrue(self.controller._organize_starting)
            return QtWidgets.QDialog.DialogCode.Accepted

        self.dialog.exec.side_effect = shown
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog", return_value=self.dialog):
            self.controller.organize_results()
        self.context.patch_config.assert_called_once()
        self.assertEqual(self.thread_factory.call_args.args[2]["video_filename_creator_marker"], "ABC")
        self.dialog.deleteLater.assert_called_once()
        self.assertFalse(self.controller._organize_starting)

    def test_cancel_does_not_modify_config_or_start_worker(self):
        self.make_missing()
        original = deepcopy(self.config)
        self.dialog.exec.return_value = QtWidgets.QDialog.DialogCode.Rejected
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog", return_value=self.dialog):
            self.assertIsNone(self.controller.organize_results())
        self.context.patch_config.assert_not_called()
        self.thread_factory.assert_not_called()
        self.assertEqual(self.config, original)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertFalse(self.controller._organize_starting)

    def test_failed_save_does_not_start_worker(self):
        self.make_missing()
        self.context.patch_config.side_effect = None
        self.context.patch_config.return_value = False
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog", return_value=self.dialog):
            self.assertIsNone(self.controller.organize_results())
        self.thread_factory.assert_not_called()
        self.assertEqual(self.config["video_filename_creator_marker"], "")

    def test_hotkey_during_prompt_cannot_start_second_worker(self):
        self.make_missing()

        def shown():
            self.assertIsNone(self.controller.organize_results())
            self.thread_factory.assert_not_called()
            return QtWidgets.QDialog.DialogCode.Accepted

        self.dialog.exec.side_effect = shown
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog", return_value=self.dialog) as dialog:
            self.controller.organize_results()
        dialog.assert_called_once()
        self.thread_factory.assert_called_once()

    def test_local_only_does_not_prompt_for_missing_upload_settings(self):
        self.config = {"run_upload": False}
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog") as dialog:
            self.controller.organize_results()
        dialog.assert_not_called()
        self.assertFalse(self.thread_factory.call_args.args[2]["run_upload"])

    def test_read_error_is_reported_without_creating_worker(self):
        self.context.load_config = Mock(side_effect=ValueError("synthetic config read error"))
        with patch("app_plugins.builtin.task_delivery_controller.QMessageBox.warning") as message:
            self.assertIsNone(self.controller.organize_results())
        message.assert_called_once()
        self.thread_factory.assert_not_called()
        self.assertFalse(self.controller._organize_starting)

    def test_worker_thread_cannot_open_qt_dialog(self):
        with patch("app_plugins.builtin.task_delivery_controller.TaskDeliveryConfigDialog") as dialog:
            worker = threading.Thread(target=self.controller.organize_results)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        dialog.assert_not_called()
        self.thread_factory.assert_not_called()


class PersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        from model.GeminiKeyManager import GeminiKeyManager
        from model.ConfigHotReload import ConfigHotReload
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.original = {"video_filename_creator_marker": "", "gemini_api_keys": ["synthetic-key"],
                         "elevenlabs_api_keys": ["synthetic-voice-key"], "records": [1, 2]}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        manager = GeminiKeyManager(self.original)
        self.fake = SimpleNamespace(config_name=str(self.path), gemini_keys=manager, appendLog=Mock())
        from PYUI.main_pyui import MainDialog
        self.fake._save_current_config_locked = lambda *args: MainDialog._save_current_config_locked(self.fake, *args)
        self.fake.load_config = lambda: json.loads(self.path.read_text(encoding="utf-8"))
        self.fake.dump = lambda: manager.update_config(self.fake.load_config())
        self.monitor = ConfigHotReload(self.path, self.fake.dump, Mock(), Mock())
        self.monitor.timer.stop()
        self.fake.config_reload = self.monitor

    def tearDown(self):
        self.monitor.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def save(self):
        from PYUI.main_pyui import MainDialog
        return MainDialog.saveCurrentConfig(self.fake, show_errors=False,
            config_updates={"video_filename_creator_marker": "ABC"}, expected_config=self.original)

    def test_save_keeps_credentials_records_and_no_model_restart(self):
        self.assertTrue(self.save())
        expected = deepcopy(self.original)
        expected["video_filename_creator_marker"] = "ABC"
        actual = self.fake.load_config()
        for key, value in expected.items():
            self.assertEqual(actual[key], value)
        self.monitor.apply.assert_not_called()

    def test_same_field_external_change_is_not_overwritten(self):
        external = dict(self.original, video_filename_creator_marker="External")
        self.path.write_text(json.dumps(external), encoding="utf-8")
        self.assertFalse(self.save())
        self.assertEqual(self.fake.load_config(), external)

    def test_other_external_fields_survive(self):
        external = dict(self.original, extra_setting="External")
        self.path.write_text(json.dumps(external), encoding="utf-8")
        self.assertTrue(self.save())
        self.assertEqual(self.fake.load_config()["extra_setting"], "External")

    def test_disk_failure_keeps_original_config(self):
        with patch("PYUI.main_pyui.os.replace", side_effect=OSError("synthetic save failure")):
            self.assertFalse(self.save())
        self.assertEqual(self.fake.load_config(), self.original)
        self.assertFalse(Path(str(self.path) + ".main.tmp").exists())

    def test_missing_snapshot_cannot_patch(self):
        from PYUI.main_pyui import MainDialog
        self.assertFalse(MainDialog.saveCurrentConfig(self.fake, show_errors=False,
                         config_updates={"video_filename_creator_marker": "ABC"}))
        self.assertEqual(self.fake.load_config(), self.original)


if __name__ == "__main__":
    unittest.main()

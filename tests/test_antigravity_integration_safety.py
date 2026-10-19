"""No real accounts, processes, credentials or network are used by these tests."""
import ast
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import ApplicationStorage
from model.QtThreadLifecycle import thread_is_active
from app_plugins.builtin.antigravity_account_switcher import launcher, operations
from app_plugins.builtin.antigravity_account_switcher.dialog import AntigravitySwitcherDialog
from app_plugins.builtin.antigravity_account_switcher.plugin import AntigravityAccountSwitcherPlugin
from app_plugins.builtin.antigravity_account_switcher.quota import inspect_profile_credentials
from app_plugins.builtin.antigravity_account_switcher.store import (
    AUTH_KEYS_TO_SYNC, AccountSwitcherError, AntigravityProfileStore, default_store_root,
)
from app_plugins.builtin.antigravity_account_switcher.clients import AntigravityClient
from test_antigravity_account_switcher_plugin import create_mock_vscdb


def rows(path):
    with closing(sqlite3.connect(path)) as con:
        return dict(con.execute("SELECT key,value FROM ItemTable").fetchall())


def set_account(path, token="new-token", email="b@example.test"):
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "state.vscdb"
        create_mock_vscdb(source, token, email)
        values = rows(source)
    with closing(sqlite3.connect(path)) as con, con:
        con.executemany("DELETE FROM ItemTable WHERE key=?", [(key,) for key in AUTH_KEYS_TO_SYNC])
        con.executemany("INSERT INTO ItemTable VALUES (?,?)", list(values.items()))


class AccountSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.live = self.root / "live/state.vscdb"
        create_mock_vscdb(self.live, "a-token", "a@example.test")
        with closing(sqlite3.connect(self.live)) as con, con:
            con.execute("INSERT INTO ItemTable VALUES ('workspace.history','preserve me')")
        self.store = AntigravityProfileStore(self.root / "accounts", self.live, client_running=lambda: False)
        self.first = self.store.adopt_current("账号 A")

    def tearDown(self):
        self.tmp.cleanup()

    def second(self):
        second = self.store.add_profile("账号 B")
        create_mock_vscdb(self.store.profile_db_path(second), "b-token", "b@example.test")
        return second

    def test_only_auth_records_are_archived_and_paths_are_portable(self):
        self.assertEqual(set(rows(self.store.profile_db_path(self.first))), set(AUTH_KEYS_TO_SYNC))
        index = json.loads(self.store.index_path.read_text(encoding="utf8"))
        self.assertEqual(index["profiles"][0]["credential_dir"], "credentials/" + self.first.profile_id)
        self.assertLess(self.store.profile_db_path(self.first).stat().st_size, 100_000)

    def test_switch_keeps_workspace_and_removes_old_optional_auth_keys(self):
        second = self.second()
        path = self.store.profile_db_path(second)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("DELETE FROM ItemTable WHERE key=?", (AUTH_KEYS_TO_SYNC[-1],))
        self.store.switch_to(second.profile_id)
        current = rows(self.live)
        self.assertEqual(current["workspace.history"], "preserve me")
        self.assertNotIn(AUTH_KEYS_TO_SYNC[-1], current)
        self.assertEqual(rows(path)[AUTH_KEYS_TO_SYNC[0]], current[AUTH_KEYS_TO_SYNC[0]])

    def test_unbound_login_clears_only_auth_and_cannot_be_marked_ready_early(self):
        second = self.store.add_profile("新账号")
        self.store.switch_to(second.profile_id, allow_unbound=True)
        self.assertEqual(rows(self.live), {"workspace.history": "preserve me"})
        self.assertEqual(self.store.binding_profile_id, second.profile_id)
        self.assertFalse(self.store.has_auth(second))
        self.assertFalse(self.store.capture_current())
        self.assertTrue(self.store.has_auth(self.first))

    def test_binding_is_saved_by_refresh_and_survives_reopen(self):
        second = self.store.add_profile("新账号")
        self.store.switch_to(second.profile_id, allow_unbound=True)
        set_account(self.live)
        self.assertTrue(self.store.capture_current())
        reopened = AntigravityProfileStore(self.store.root, self.live, client_running=lambda: False)
        self.assertIsNone(reopened.binding_profile_id)
        self.assertTrue(reopened.has_auth(reopened.profile(second.profile_id)))
        self.assertEqual(rows(self.live)[AUTH_KEYS_TO_SYNC[0]], rows(reopened.profile_db_path(second))[AUTH_KEYS_TO_SYNC[0]])

    def test_duplicate_login_cannot_overwrite_new_profile(self):
        second = self.store.add_profile("新账号")
        self.store.switch_to(second.profile_id, allow_unbound=True)
        set_account(self.live, "rotated-token", "a@example.test")
        with self.assertRaisesRegex(AccountSwitcherError, "仍是已保存账号"):
            self.store.capture_current()
        self.assertEqual(self.store.binding_profile_id, second.profile_id)
        self.assertFalse(self.store.profile_db_path(second).exists())

    def test_unknown_binding_identity_is_not_guessed(self):
        second = self.store.add_profile("新账号")
        self.store.switch_to(second.profile_id, allow_unbound=True)
        with closing(sqlite3.connect(self.live)) as con, con:
            con.execute("INSERT INTO ItemTable VALUES (?,?)", (AUTH_KEYS_TO_SYNC[0], "new-opaque-token-value"))
        with self.assertRaisesRegex(AccountSwitcherError, "格式无法确认"):
            self.store.capture_current()

    def test_manual_account_change_does_not_overwrite_saved_profile(self):
        original = rows(self.store.profile_db_path(self.first))
        second = self.second()
        set_account(self.live, "c-token", "c@example.test")
        with self.assertRaisesRegex(AccountSwitcherError, "账号与当前档案不一致"):
            self.store.capture_current()
        with self.assertRaisesRegex(AccountSwitcherError, "账号与当前档案不一致"):
            self.store.switch_to(second.profile_id)
        self.assertEqual(rows(self.store.profile_db_path(self.first)), original)

    def test_failed_source_snapshot_prevents_switch(self):
        second = self.second()
        original = rows(self.live)
        with patch.object(self.store, "_sync_auth_keys", side_effect=OSError("snapshot unavailable")):
            with self.assertRaises(OSError):
                self.store.switch_to(second.profile_id)
        self.assertEqual(rows(self.live), original)
        self.assertFalse(self.store.needs_recovery)
        self.assertEqual(self.store.active_profile_id, self.first.profile_id)

    def test_failed_final_index_commit_restores_original_auth_and_index(self):
        second = self.second()
        original = rows(self.live)
        save = self.store.save
        calls = []
        def fail_once():
            calls.append(True)
            if len(calls) == 2:
                raise OSError("index commit failure")
            save()
        with patch.object(self.store, "save", side_effect=fail_once):
            with self.assertRaisesRegex(AccountSwitcherError, "已恢复原账号"):
                self.store.switch_to(second.profile_id)
        self.assertEqual(rows(self.live), original)
        self.assertFalse(self.store.needs_recovery)
        self.assertEqual(self.store.active_profile_id, self.first.profile_id)

    def test_failed_rollback_is_not_claimed_successful_and_open_does_not_rewrite_live(self):
        second = self.second()
        write = self.store._write_auth
        def fail_live(path, records):
            if path == self.live:
                raise OSError("cannot write live DB")
            write(path, records)
        with patch.object(self.store, "_write_auth", side_effect=fail_live):
            with self.assertRaisesRegex(AccountSwitcherError, "自动恢复也未完成"):
                self.store.switch_to(second.profile_id)
        self.assertTrue(self.store.needs_recovery)
        before = self.live.read_bytes()
        reopened = AntigravityProfileStore(self.store.root, self.live, client_running=lambda: True)
        self.assertTrue(reopened.needs_recovery)
        self.assertEqual(self.live.read_bytes(), before)
        with self.assertRaisesRegex(AccountSwitcherError, "退出 Antigravity"):
            reopened._recover_interrupted_switch()
        self.assertTrue(reopened.rollback_db_path.exists())
        reopened._client_running = lambda: False
        reopened._recover_interrupted_switch()
        self.assertFalse(reopened.needs_recovery)
        self.assertEqual(rows(self.live)["workspace.history"], "preserve me")

    def test_running_client_rejected_before_any_credential_write(self):
        second = self.second()
        original = self.live.read_bytes()
        self.store._client_running = lambda: True
        with self.assertRaisesRegex(AccountSwitcherError, "退出 Antigravity"):
            self.store.switch_to(second.profile_id)
        self.assertEqual(self.live.read_bytes(), original)
        self.assertFalse(self.store.rollback_db_path.exists())

    def test_archive_keeps_credentials_and_rolls_back_failed_index_save(self):
        second = self.second()
        original = rows(self.store.profile_db_path(second))
        with patch.object(self.store, "save", side_effect=OSError("index failed")):
            with self.assertRaises(OSError):
                self.store.archive_profile(second.profile_id)
        self.assertEqual(rows(self.store.profile_db_path(second)), original)
        destination = self.store.archive_profile(second.profile_id)
        self.assertEqual(rows(destination / "state.vscdb"), original)
        self.assertIsNone(self.store.profile(second.profile_id))

    def test_credential_path_escape_rejected(self):
        self.store._data["profiles"][0]["credential_dir"] = "../../other"
        with self.assertRaises(AccountSwitcherError):
            self.store.profiles()

    def test_profile_root_comes_from_host_namespace(self):
        storage = ApplicationStorage(self.root / "encrypted")
        with patch("app_plugins.builtin.antigravity_account_switcher.store.application_storage", return_value=storage):
            self.assertEqual(default_store_root(), storage.root / "PluginData/antigravity_account_switcher/accounts")
        self.assertFalse((self.root / "encrypted").exists())

    def test_invalid_index_version_is_a_controlled_error_not_reset(self):
        value = json.loads(self.store.index_path.read_text(encoding="utf8"))
        value["version"] = "invalid"
        self.store.index_path.write_text(json.dumps(value), encoding="utf8")
        before = self.store.index_path.read_bytes()
        with self.assertRaises(AccountSwitcherError):
            AntigravityProfileStore(self.store.root, self.live, client_running=lambda: False)
        self.assertEqual(self.store.index_path.read_bytes(), before)

    def test_no_live_database_or_auth_is_created_for_missing_client(self):
        missing = self.root / "missing-client/state.vscdb"
        store = AntigravityProfileStore(self.root / "empty", missing, client_running=lambda: False)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(AccountSwitcherError):
            store.adopt_current("test")
        self.assertIsNone(store.active_profile)

    def test_status_does_not_claim_live_quota_or_token_validity(self):
        status = inspect_profile_credentials(self.first.profile_id, self.store.profile_db_path(self.first).parent)
        self.assertTrue(status.success)
        self.assertIn("未联网验证", status.detail_text)
        self.assertIn("尚未接入", status.detail_text)
        self.assertNotIn("有效（", status.detail_text)

    def test_guard_failure_does_not_fall_back_to_plaintext(self):
        class Guard:
            def check(self, *args, **kwargs):
                raise OSError("test encrypted volume unavailable")
        storage = ApplicationStorage(self.root / "encrypted", guard=Guard())
        from model import AppDataStorage
        with patch.object(AppDataStorage, "_APPLICATION_STORAGE", storage):
            with self.assertRaises(OSError):
                AntigravityProfileStore(live_db_path=self.live, client_running=lambda: False)
        self.assertFalse((storage.root / "PluginData").exists())


class HostAndLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.live = self.root / "live/state.vscdb"
        create_mock_vscdb(self.live, "a-token", "a@example.test")
        self.store = AntigravityProfileStore(self.root / "accounts", self.live, client_running=lambda: False)

    def tearDown(self):
        self.app.processEvents()
        self.tmp.cleanup()

    def drain(self, condition, seconds=3):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(condition())

    def test_main_source_installs_plugin_without_touching_other_plugins(self):
        import app_paths
        source_root = Path(os.environ.get("LZX_TEST_SOURCE_ROOT", app_paths.APP_ROOT))
        tree = ast.parse((source_root / "PYUI/main_pyui.py").read_text(encoding="utf8"))
        installs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "install" and n.args and isinstance(n.args[0], ast.Call)
                    and isinstance(n.args[0].func, ast.Name)]
        names = [n.args[0].func.id for n in installs]
        self.assertEqual(names.count("AntigravityAccountSwitcherPlugin"), 1)
        self.assertIn("CodexAccountSwitcherPlugin", names)
        self.assertIn("ChromeLauncherPlugin", names)

    def test_actual_host_menu_registers_lazily_and_respects_plugin_disable(self):
        from app_plugins.host import PluginHost
        class Main(QtWidgets.QWidget):
            def load_config(self): return {}
            def appendLog(self, *args, **kwargs): pass
        main = Main()
        host = PluginHost(main)
        plugin = host.install(AntigravityAccountSwitcherPlugin())
        menu_bar = QtWidgets.QMenuBar(main)
        host.attach_main_menu(menu_bar)
        self.assertIn("Antigravity 多账号切换…", [a.text() for a in host.menu.actions()])
        self.assertIsNone(plugin.store)
        self.assertIsNone(plugin.dialog)
        self.assertIn("antigravity_account_switcher", [e["plugin_id"] for e in host.plugin_entries()])
        main.deleteLater()

    def test_plugin_open_uses_context_storage_not_home_and_reuses_window(self):
        storage = ApplicationStorage(self.root / "private")
        context = SimpleNamespace(storage=storage.for_plugin("antigravity_account_switcher"),
                                  parent_widget=None, register_command=Mock(), log=Mock())
        plugin = AntigravityAccountSwitcherPlugin()
        plugin.register(context)
        client = AntigravityClient('Antigravity IDE', self.root / 'test.exe', self.root / 'client')
        with patch('app_plugins.builtin.antigravity_account_switcher.plugin.discover_clients', return_value=[client]):
            dialog = plugin.open_manager()
        self.assertEqual(plugin.store.root, storage.root / ("PluginData/antigravity_account_switcher/accounts/clients/" + client.identity))
        self.drain(lambda: dialog._quota_worker is None)
        with patch('app_plugins.builtin.antigravity_account_switcher.plugin.discover_clients', return_value=[client]):
            self.assertIs(plugin.open_manager(), dialog)
        self.drain(lambda: dialog._quota_worker is None)
        self.assertTrue(plugin.can_close()[0])
        plugin.stop()

    def test_worker_close_failure_never_switches_or_launches(self):
        store = Mock()
        worker = operations.AccountOperationWorker(store, "bind", "target")
        with patch.object(operations, "is_antigravity_running", return_value=True), \
             patch.object(operations, "close_antigravity", return_value=False), \
             patch.object(operations, "launch_antigravity") as launch:
            worker.run()
        store.switch_to.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(worker.changed)
        self.assertIn("未正常退出", worker.error)

    def test_launch_failure_reports_environment_already_switched(self):
        store = Mock()
        worker = operations.AccountOperationWorker(store, "switch", "target")
        with patch.object(operations, "is_antigravity_running", return_value=False), \
             patch.object(operations, "launch_antigravity", side_effect=OSError("missing")):
            worker.run()
        store.switch_to.assert_called_once_with("target", allow_unbound=False)
        self.assertTrue(worker.changed)
        self.assertIn("已经切换", worker.error)

    def test_gui_does_not_freeze_and_cannot_destroy_running_switch_worker(self):
        dialog = AntigravitySwitcherDialog(self.store)
        self.drain(lambda: dialog._quota_worker is None)
        entered, release = threading.Event(), threading.Event()
        def slow_operation(*args, **kwargs):
            entered.set()
            release.wait(3)
        with patch.object(operations, "is_antigravity_running", return_value=False), \
             patch.object(operations, "launch_antigravity"), \
             patch.object(self.store, "switch_to", side_effect=slow_operation), \
             patch.object(QtWidgets.QMessageBox, "warning"):
            dialog._start_operation("switch", "target")
            self.drain(entered.is_set)
            self.assertFalse(dialog.shutdown())
            ticks = []
            QtCore.QTimer.singleShot(0, lambda: ticks.append(True))
            self.app.processEvents()
            self.assertTrue(ticks)
            self.assertFalse(dialog.add_button.isEnabled())
            release.set()
            self.drain(lambda: dialog._operation_worker is None)
        self.assertTrue(dialog.shutdown())
        dialog.deleteLater()

    def test_local_inspection_threads_finish_before_delete_and_have_no_fake_quota(self):
        self.store.adopt_current("测试账号")
        dialog = AntigravitySwitcherDialog(self.store)
        self.drain(lambda: dialog._quota_worker is None)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertIn("本地", dialog.quota_button.text())
        self.assertIn("a@example.test", dialog.table.item(0, 1).text())
        self.assertTrue(dialog.shutdown())
        image = os.environ.get("LZX_A_PLUGIN_SCREENSHOT")
        if image:
            dialog.show()
            self.app.processEvents()
            self.assertTrue(dialog.grab().save(image))
        dialog.deleteLater()

    def test_process_probe_fails_closed_and_close_is_not_forced(self):
        with patch.object(launcher.psutil, "process_iter", side_effect=OSError("denied")):
            with self.assertRaises(launcher.LaunchError):
                launcher.is_antigravity_running()
        with patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(launcher.close_antigravity())
        script = run.call_args.args[0][-1]
        self.assertIn("CloseMainWindow", script)
        self.assertNotIn("-Force", script)
        self.assertNotIn("Stop-Process", script)

    def test_storage_failure_from_button_is_caught_and_clicked_boolean_is_ignored(self):
        dialog = AntigravitySwitcherDialog(self.store)
        self.drain(lambda: dialog._quota_worker is None)
        with patch.object(self.store, "next_ready_profile", side_effect=OSError("密盘不可用")), \
             patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.switch_next(False)
            self.assertIn("密盘不可用", warning.call_args.args[-1])
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

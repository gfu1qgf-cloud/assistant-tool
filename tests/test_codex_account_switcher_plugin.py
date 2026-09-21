import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app_plugins.api import MAIN_MENU
from app_plugins.builtin.codex_account_switcher.plugin import (
    CodexAccountSwitcherPlugin,
)
from app_plugins.builtin.codex_account_switcher.quota import (
    fetch_profile_quota,
    format_quota_strings,
)
from app_plugins.builtin.codex_account_switcher import launcher
from app_plugins.builtin.codex_account_switcher.store import (
    AccountProfileStore,
    AccountSwitcherError,
)


def write_auth(path, token="token"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tokens": {"access_token": token}}), encoding="utf-8"
    )


class AccountProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.live_home = root / "live" / ".codex"
        self.live_home.mkdir(parents=True)
        write_auth(self.live_home / "auth.json", "first-token")
        self.store = AccountProfileStore(root / "profiles", self.live_home)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_switch_only_replaces_live_auth_and_preserves_other_live_files(self):
        (self.live_home / "sessions").mkdir()
        history = self.live_home / "sessions" / "history.json"
        history.write_text("keep", encoding="utf-8")
        first = self.store.adopt_current("个人")
        second = self.store.add_empty_profile("工作")
        write_auth(self.store.profile_auth_path(second), "second-token")

        switched = self.store.switch_to(second.profile_id)

        self.assertEqual(switched.profile_id, second.profile_id)
        self.assertEqual(self.store.active_profile_id, second.profile_id)
        self.assertEqual((self.live_home / "auth.json").read_text(encoding="utf-8"), self.store.profile_auth_path(second).read_text(encoding="utf-8"))
        self.assertEqual(self.store.profile_auth_path(first).read_text(encoding="utf-8"), json.dumps({"tokens": {"access_token": "first-token"}}))
        self.assertEqual(history.read_text(encoding="utf-8"), "keep")

    def test_interrupted_switch_recovers_previous_auth_at_next_start(self):
        first = self.store.adopt_current("个人")
        second = self.store.add_empty_profile("工作")
        write_auth(self.store.profile_auth_path(second), "second-token")
        self.store._atomic_copy_auth(
            self.live_home / "auth.json", self.store.rollback_auth_path
        )
        self.store._data["transaction"] = {
            "from": first.profile_id,
            "to": second.profile_id,
            "had_live_auth": True,
        }
        self.store._save()
        write_auth(self.live_home / "auth.json", "partial-switch")

        recovered = AccountProfileStore(self.store.root, self.live_home)

        self.assertEqual(recovered.active_profile_id, first.profile_id)
        self.assertIn("first-token", (self.live_home / "auth.json").read_text(encoding="utf-8"))
        self.assertIsNone(recovered._data["transaction"])

    def test_unbound_profile_removes_only_live_auth_cache(self):
        first = self.store.adopt_current("个人")
        second = self.store.add_empty_profile("工作")
        session_path = self.live_home / "sessions" / "local-history.jsonl"
        session_path.parent.mkdir()
        session_path.write_text("keep", encoding="utf-8")

        self.store.switch_to(second.profile_id, allow_unbound=True)

        self.assertFalse((self.live_home / "auth.json").exists())
        self.assertIn("first-token", self.store.profile_auth_path(first).read_text(encoding="utf-8"))
        self.assertEqual(session_path.read_text(encoding="utf-8"), "keep")

    def test_failed_target_replace_rolls_back_live_auth(self):
        first = self.store.adopt_current("个人")
        second = self.store.add_empty_profile("工作")
        target_auth = self.store.profile_auth_path(second)
        write_auth(target_auth, "second-token")
        original_copy = self.store._atomic_copy_auth

        def fail_target_replace(source, destination):
            if source == target_auth and destination == self.store.live_auth_path:
                raise OSError("simulated target replacement failure")
            return original_copy(source, destination)

        with patch.object(self.store, "_atomic_copy_auth", side_effect=fail_target_replace):
            with self.assertRaises(AccountSwitcherError):
                self.store.switch_to(second.profile_id)

        self.assertEqual(self.store.active_profile_id, first.profile_id)
        self.assertIn("first-token", (self.live_home / "auth.json").read_text(encoding="utf-8"))
        self.assertIsNone(self.store._data["transaction"])

    def test_v2_profiles_are_migrated_without_moving_legacy_sessions(self):
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        live_home = root / "live" / ".codex"
        old_first = root / "switcher" / "profiles" / "first" / "codex_home"
        old_second = root / "switcher" / "profiles" / "second" / "codex_home"
        live_home.mkdir(parents=True)
        old_first.parent.mkdir(parents=True)
        old_second.mkdir(parents=True)
        write_auth(live_home / "auth.json", "first-token")
        write_auth(old_second / "auth.json", "second-token")
        legacy_session = old_second / "sessions" / "history.jsonl"
        legacy_session.parent.mkdir()
        legacy_session.write_text("legacy history", encoding="utf-8")
        store_root = root / "switcher"
        index = {
            "version": 2,
            "active_profile_id": "first",
            "profiles": [
                {"profile_id": "first", "name": "个人", "codex_home": str(old_first), "created_at": "2026-01-01T00:00:00+00:00", "last_used_at": None},
                {"profile_id": "second", "name": "工作", "codex_home": str(old_second), "created_at": "2026-01-01T00:00:00+00:00", "last_used_at": None},
            ],
            "transaction": None,
        }
        (store_root / "desktop-profiles.json").write_text(json.dumps(index), encoding="utf-8")

        migrated = AccountProfileStore(store_root, live_home)

        self.assertTrue(migrated.migration_performed)
        self.assertTrue(migrated.migration_backup_path.exists())
        self.assertIn("first-token", migrated.profile_auth_path(migrated.get_profile("first")).read_text(encoding="utf-8"))
        self.assertIn("second-token", migrated.profile_auth_path(migrated.get_profile("second")).read_text(encoding="utf-8"))
        self.assertEqual(legacy_session.read_text(encoding="utf-8"), "legacy history")

    def test_first_profile_backs_up_config_before_enabling_file_auth(self):
        config_path = self.live_home / "config.toml"
        config_path.write_text('model = "demo"\n', encoding="utf-8")

        self.store.adopt_current("个人")

        backup = self.live_home / "config.before-account-switcher.toml"
        self.assertEqual(backup.read_text(encoding="utf-8"), 'model = "demo"\n')
        self.assertIn(
            'cli_auth_credentials_store = "file"',
            config_path.read_text(encoding="utf-8"),
        )


class QuotaFormattingTests(unittest.TestCase):
    def test_rate_limits_include_both_windows_and_reset(self):
        status, detail, _used_a, remaining_a, _used_b, remaining_b, limited, reset = format_quota_strings(
            "plus",
            "person@example.test",
            {
                "primary_window": {"used_percent": 40, "reset_after_seconds": 900, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 20, "reset_after_seconds": 3600, "limit_window_seconds": 604800},
            },
        )
        self.assertIn("5h:60%", status)
        self.assertIn("7d:80%", status)
        self.assertIn("person@example.test", detail)
        self.assertEqual((remaining_a, remaining_b, limited, reset), (60, 80, False, 900))

    def test_network_failure_returns_a_displayable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            write_auth(auth_path)
            with patch(
                "app_plugins.builtin.codex_account_switcher.quota.request_chatgpt_usage",
                return_value=(-1, "请求超时"),
            ):
                quota = fetch_profile_quota("demo", auth_path)
        self.assertFalse(quota.success)
        self.assertEqual(quota.status_text, "查询超时")
        self.assertIn("超时", quota.detail_text)

    def test_cancel_during_refresh_does_not_write_new_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(json.dumps({
                "tokens": {
                    "access_token": "expired",
                    "refresh_token": "refresh",
                }
            }), encoding="utf-8")
            cancelled = {"value": False}
            callback = Mock()

            def refresh(_token, _timeout):
                cancelled["value"] = True
                return {"access_token": "new-token"}

            with patch(
                "app_plugins.builtin.codex_account_switcher.quota.request_chatgpt_usage",
                return_value=(401, "expired"),
            ), patch(
                "app_plugins.builtin.codex_account_switcher.quota.refresh_chatgpt_token",
                side_effect=refresh,
            ):
                quota = fetch_profile_quota(
                    "demo",
                    auth_path,
                    on_token_refreshed=callback,
                    is_cancelled=lambda: cancelled["value"],
                )

        self.assertEqual(quota.error_message, "cancelled")
        callback.assert_not_called()


class LauncherTests(unittest.TestCase):
    def test_codex_appx_fallback_aumid_is_accepted(self):
        result = SimpleNamespace(
            stdout="OpenAI.Codex_2p2nqsd0c76g0!App\n",
            returncode=0,
        )
        with patch.object(launcher.os, "name", "nt"), patch.object(
            launcher, "_run", return_value=result
        ) as run:
            value = launcher.detect_chatgpt_aumid()

        self.assertEqual(value, "OpenAI.Codex_2p2nqsd0c76g0!App")
        self.assertIn("OpenAI.Codex", run.call_args.args[0][-1])

    def test_launch_uses_resolved_codex_aumid(self):
        with patch.object(launcher.os, "name", "nt"), patch.object(
            launcher,
            "detect_chatgpt_aumid",
            return_value="OpenAI.Codex_demo!App",
        ), patch.object(launcher.subprocess, "Popen") as popen:
            launcher.launch_chatgpt_windows()

        popen.assert_called_once_with([
            "explorer.exe", "shell:AppsFolder\\OpenAI.Codex_demo!App"
        ])


class PluginRegistrationTests(unittest.TestCase):
    def test_plugin_registers_one_main_menu_command(self):
        class Context:
            def __init__(self):
                self.commands = []

            def register_command(self, command):
                self.commands.append(command)

        context = Context()
        CodexAccountSwitcherPlugin().register(context)

        self.assertEqual(len(context.commands), 1)
        self.assertEqual(context.commands[0].command_id, "open")
        self.assertEqual(context.commands[0].locations, frozenset({MAIN_MENU}))


if __name__ == "__main__":
    unittest.main()

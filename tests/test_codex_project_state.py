import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_plugins.builtin.codex_account_switcher.project_state import (
    ACCOUNT_STATE, ATOM_STATE, GLOBAL_STATE_FILE, auth_owner, share_local_projects,
)
from app_plugins.builtin.codex_account_switcher.store import AccountProfileStore, AccountSwitcherError


def auth(user, account="account"):
    payload = {"https://api.openai.com/auth": {"chatgpt_user_id": user, "chatgpt_account_id": account}}
    token = "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".sig"
    return {"tokens": {"id_token": token, "account_id": account}}


def project(pid="local-1"):
    return {"id": pid, "name": "项目", "rootPaths": ["C:\\work"], "updatedAt": 123}


def state():
    return {
        ACCOUNT_STATE: {"owner": auth_owner(auth("A")), "inactive": {
            auth_owner(auth("B")): {"atoms": {"private": "B"}, "globals": {"selected-project": None}},
        }},
        ATOM_STATE: {"private": "A"},
        "local-projects": {"local-1": project()},
        "selected-project": {"type": "local", "projectId": "local-1"},
        "project-order": ["local-1"],
        "thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "local-1"}},
        "thread-project-membership-host-ids": {"t1": "local"},
        "thread-writable-roots": {"t1": ["C:\\work", "C:\\extra"]},
        "sidebar-project-thread-orders": {"local-1": {"threadIds": ["t1"]}},
        "electron-saved-workspace-roots": ["C:\\work"],
        "active-workspace-roots": ["C:\\work"],
        "app-server-project-id-by-legacy-project-id-by-host": {"local:HOME": {"local-1": "server-1"}},
    }


def boot_select(source, owner):
    """Observed selectRendererOwner: startup selects null, then signed-in owner.

    This fixture uses our project keys as the account-scoped globals. It models
    the application's archive/restore behavior, rather than our merge algorithm.
    """
    result = deepcopy(source)
    globals_keys = set(source) - {ACCOUNT_STATE, ATOM_STATE, "unrelated"}
    for selected in (None, owner):
        scoped = result[ACCOUNT_STATE]
        if selected is not None and selected == scoped["owner"]:
            continue
        if scoped["owner"] is not None:
            scoped["inactive"][scoped["owner"]] = {
                "atoms": result.get(ATOM_STATE, {}),
                "globals": {k: result[k] for k in globals_keys if k in result},
            }
        snapshot = scoped["inactive"].pop(selected, {"atoms": {}, "globals": {}})
        for k in globals_keys:
            result.pop(k, None)
        result.update(snapshot["globals"])
        result[ATOM_STATE] = snapshot["atoms"]
        scoped["owner"] = selected
    return result


class ProjectStateTests(unittest.TestCase):
    def test_new_codex_boot_keeps_projects_and_local_threads(self):
        original = state()
        self.assertNotIn("local-projects", boot_select(original, auth_owner(auth("B"))))
        fixed = share_local_projects(original, auth("B"), "local:HOME")
        started = boot_select(fixed, auth_owner(auth("B")))
        for key in ("local-projects", "selected-project", "project-order", "thread-project-assignments",
                    "thread-writable-roots", "sidebar-project-thread-orders", "electron-saved-workspace-roots"):
            self.assertEqual(started[key], original[key])
        self.assertEqual(started[ATOM_STATE], {"private": "B"})
        self.assertEqual(fixed[ACCOUNT_STATE]["owner"], original[ACCOUNT_STATE]["owner"])
        self.assertEqual(original, state())

    def test_round_trip_keeps_new_projects_from_both_accounts(self):
        current = state()
        for user in ("B", "A", "B"):
            current = boot_select(share_local_projects(current, auth(user), "local:HOME"), auth_owner(auth(user)))
            self.assertIn("local-1", current["local-projects"])
            if user == "B":
                current["local-projects"]["local-2"] = project("local-2")
        self.assertIn("local-2", current["local-projects"])

    def test_empty_source_recovers_projects_from_inactive_account(self):
        current = boot_select(state(), auth_owner(auth("B")))
        fixed = share_local_projects(current, auth("C"), "local:HOME")
        started = boot_select(fixed, auth_owner(auth("C")))
        self.assertIn("local-1", started["local-projects"])

    def test_logged_out_startup_snapshot_is_recovered(self):
        current = state()
        original = {k: current.pop(k) for k in list(current) if k not in (ACCOUNT_STATE, ATOM_STATE)}
        current[ACCOUNT_STATE]["inactive"][auth_owner(auth("A"))] = {"atoms": {}, "globals": original}
        current[ACCOUNT_STATE]["owner"] = None
        self.assertIn("local-1", share_local_projects(current, auth("B"), "local:HOME")[ACCOUNT_STATE]["inactive"][auth_owner(auth("B"))]["globals"]["local-projects"])

    def test_cloud_remote_atoms_and_unknown_fields_do_not_cross(self):
        current = state()
        for pid, extra in (("g-p-cloud", {}), ("backed", {"chatGptBacking": {"accountId": "A"}}),
                           ("retired", {"retiredChatGptBacking": {"accountId": "A"}})):
            current["local-projects"][pid] = {**project(pid), **extra}
            current["project-order"].append(pid)
        current["remote-projects"] = {"remote": "private"}
        current["unknown-account-secret"] = "private"
        current["thread-project-assignments"]["remote-thread"] = {"projectKind": "local", "projectId": "local-1"}
        current["thread-project-membership-host-ids"]["remote-thread"] = "remote-ssh:x"
        fixed = share_local_projects(current, auth("B"), "local:HOME")
        target = fixed[ACCOUNT_STATE]["inactive"][auth_owner(auth("B"))]
        self.assertEqual(set(target["globals"]["local-projects"]), {"local-1"})
        self.assertEqual(target["globals"]["project-order"], ["local-1"])
        self.assertNotIn("remote-thread", target["globals"]["thread-project-assignments"])
        self.assertNotIn("remote-projects", target["globals"])
        self.assertNotIn("unknown-account-secret", target["globals"])
        self.assertEqual(target["atoms"], {"private": "B"})

    def test_destination_projects_and_cloud_associations_survive(self):
        current = state()
        target = current[ACCOUNT_STATE]["inactive"][auth_owner(auth("B"))]["globals"]
        target["local-projects"] = {"own": project("own"), "local-1": {**project(), "chatGptBacking": {"accountId": "B"}}}
        target["project-order"] = ["own"]
        target["remote-projects"] = {"private": "B"}
        fixed = share_local_projects(current, auth("B"), "local:HOME")
        updated = fixed[ACCOUNT_STATE]["inactive"][auth_owner(auth("B"))]["globals"]
        self.assertEqual(updated["local-projects"], target["local-projects"])
        self.assertEqual(updated["remote-projects"], target["remote-projects"])

    def test_server_ids_preserved_but_migration_flags_not_forged(self):
        current = state()
        current["app-server-projects-migration-by-host"] = {"local:HOME": {"projectsMigrated": True}}
        fixed = share_local_projects(current, auth("B"), "local:HOME")
        target = fixed[ACCOUNT_STATE]["inactive"][auth_owner(auth("B"))]["globals"]
        self.assertEqual(target["app-server-project-id-by-legacy-project-id-by-host"], current["app-server-project-id-by-legacy-project-id-by-host"])
        self.assertNotIn("app-server-projects-migration-by-host", target)

    def test_older_codex_and_same_account_need_no_rewrite(self):
        self.assertEqual(share_local_projects({"legacy": 1}, {}, "local:HOME"), {"legacy": 1})
        current = state()
        self.assertIs(share_local_projects(current, auth("A"), "local:HOME"), current)

    def test_unknown_account_schema_or_identity_is_rejected(self):
        current = state()
        current[ACCOUNT_STATE]["inactive"] = []
        with self.assertRaises(ValueError):
            share_local_projects(current, auth("B"), "local:HOME")
        with self.assertRaises(ValueError):
            share_local_projects(state(), {"tokens": {"id_token": "broken"}}, "local:HOME")

    def test_owner_hash_matches_javascript_json_stringify(self):
        expected = hashlib.sha256(b'["B","account"]').hexdigest()
        self.assertEqual(auth_owner(auth("B")), expected)


class SwitchTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.live = root / "live"
        self.live.mkdir()
        self.auth_path = self.live / "auth.json"
        self.auth_path.write_text(json.dumps(auth("A")), encoding="utf-8")
        self.store = AccountProfileStore(root / "profiles", self.live)
        self.first = self.store.adopt_current("A")
        self.second = self.store.add_empty_profile("B")
        self.store.profile_auth_path(self.second).write_text(json.dumps(auth("B")), encoding="utf-8")
        self.state_path = self.live / GLOBAL_STATE_FILE
        self.original = (json.dumps(state(), ensure_ascii=False, indent=2) + "\n").encode()
        self.state_path.write_bytes(self.original)
        self.original_auth = self.auth_path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_success_saves_backup_and_does_not_touch_databases(self):
        db = self.live / "state_5.sqlite"
        db.write_bytes(b"database unchanged")
        self.store.switch_to(self.second.profile_id)
        fixed = json.loads(self.state_path.read_bytes())
        self.assertIn("local-1", boot_select(fixed, auth_owner(auth("B")))["local-projects"])
        self.assertEqual((self.store.archive_root / "project-state-before-compat.json").read_bytes(), self.original)
        self.assertEqual(db.read_bytes(), b"database unchanged")
        self.assertFalse(self.store.rollback_state_path.exists())
        self.assertIsNone(self.store._data["transaction"])

    def test_invalid_state_aborts_before_auth_write(self):
        for content in (b"{broken", b"[]"):
            self.state_path.write_bytes(content)
            with self.assertRaises(AccountSwitcherError):
                self.store.switch_to(self.second.profile_id)
            self.assertEqual(self.auth_path.read_bytes(), self.original_auth)
            self.assertEqual(self.state_path.read_bytes(), content)

    def test_state_write_failure_restores_auth(self):
        real_write = self.store._atomic_write_state
        def fail(path, content):
            if path == self.state_path:
                raise OSError("state write failed")
            return real_write(path, content)
        with patch.object(self.store, "_atomic_write_state", side_effect=fail):
            with self.assertRaises(AccountSwitcherError):
                self.store.switch_to(self.second.profile_id)
        self.assertEqual(self.auth_path.read_bytes(), self.original_auth)
        self.assertEqual(self.state_path.read_bytes(), self.original)

    def test_index_commit_failure_restores_auth_and_exact_state_bytes(self):
        real_save = self.store.save
        def fail_commit():
            if self.store.active_profile_id == self.second.profile_id:
                raise OSError("commit failed")
            return real_save()
        with patch.object(self.store, "save", side_effect=fail_commit):
            with self.assertRaises(AccountSwitcherError):
                self.store.switch_to(self.second.profile_id)
        self.assertEqual(self.auth_path.read_bytes(), self.original_auth)
        self.assertEqual(self.state_path.read_bytes(), self.original)
        self.assertEqual(self.store.active_profile_id, self.first.profile_id)
        self.assertIsNone(self.store._data["transaction"])

    def test_interrupted_switch_recovers_both_files_at_next_start(self):
        real_save = self.store.save
        def crash_commit():
            if self.store.active_profile_id == self.second.profile_id:
                raise KeyboardInterrupt("simulated process loss")
            return real_save()
        with patch.object(self.store, "save", side_effect=crash_commit):
            with self.assertRaises(KeyboardInterrupt):
                self.store.switch_to(self.second.profile_id)
        recovered = AccountProfileStore(self.store.root, self.live)
        self.assertEqual(recovered.active_profile_id, self.first.profile_id)
        self.assertEqual(self.auth_path.read_bytes(), self.original_auth)
        self.assertEqual(self.state_path.read_bytes(), self.original)

    def test_concurrent_state_edit_is_preserved_and_backup_retained(self):
        real_copy = self.store._atomic_copy_auth
        concurrent = b'{"changed-by-other-process":true}'
        def concurrent_copy(source, destination):
            real_copy(source, destination)
            if source == self.store.profile_auth_path(self.second) and destination == self.auth_path:
                self.state_path.write_bytes(concurrent)
        with patch.object(self.store, "_atomic_copy_auth", side_effect=concurrent_copy):
            with self.assertRaises(AccountSwitcherError):
                self.store.switch_to(self.second.profile_id)
        self.assertEqual(self.auth_path.read_bytes(), self.original_auth)
        self.assertEqual(self.state_path.read_bytes(), concurrent)
        self.assertTrue(self.store.rollback_state_path.exists())
        self.assertIsNotNone(self.store._data["transaction"])

    def test_legacy_state_and_unbound_profile_remain_supported(self):
        self.state_path.write_bytes(b'{"legacy":true}')
        self.store.switch_to(self.second.profile_id)
        self.assertEqual(self.state_path.read_bytes(), b'{"legacy":true}')
        third = self.store.add_empty_profile("C")
        self.store.switch_to(third.profile_id, allow_unbound=True)
        self.assertFalse(self.auth_path.exists())


if __name__ == "__main__":
    unittest.main()

"""Regression for real topic wire shape, email authority and dual installations."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qt_compat import QtWidgets
from model.AppDataStorage import ApplicationStorage
from app_plugins.builtin.antigravity_account_switcher import clients, launcher
from app_plugins.builtin.antigravity_account_switcher.identity import IdentityFormatError, parse_user_status, topic_payload
from app_plugins.builtin.antigravity_account_switcher.quota import inspect_vscdb_auth
from app_plugins.builtin.antigravity_account_switcher.plugin import AntigravityAccountSwitcherPlugin
from app_plugins.builtin.antigravity_account_switcher.dialog import AntigravitySwitcherDialog
from test_antigravity_account_switcher_plugin import create_mock_vscdb
import test_antigravity_integration_safety as lifecycle_tests


def integer(value):
    output = bytearray()
    while value > 127:
        output.append(value & 127 | 128); value >>= 7
    return bytes(output + bytes([value]))


def field(tag, value):
    value = value.encode('utf8') if isinstance(value, str) else value
    return integer(tag << 3 | 2) + integer(len(value)) + value


def entry(key, data):
    return field(1, field(1, key) + field(2, field(1, base64.b64encode(data))))


def status_wire(data):
    return base64.b64encode(entry('userStatusSentinelKey', data)).decode('ascii')


class ProtobufIdentityTests(unittest.TestCase):
    def test_real_topic_shape_long_unicode_name_and_authoritative_email(self):
        # Unknown embedded email before and after the real field must be ignored.
        payload = field(33, field(1, 'support@other.test')) + field(3, 'Ján Novák ' * 12)
        payload += field(7, 'actual+test@example.test') + field(36, field(8, 'billing@other.test'))
        result = parse_user_status(status_wire(payload))
        self.assertEqual(result['email'], 'actual+test@example.test')
        self.assertTrue(result['name'].startswith('Ján Novák'))
        self.assertIsNone(result['plan'])

    def test_missing_authoritative_email_never_uses_embedded_address(self):
        with self.assertRaises(IdentityFormatError):
            parse_user_status(status_wire(field(33, field(1, 'wrong@example.test'))))

    def test_duplicate_email_fields_are_not_last_writer_wins(self):
        with self.assertRaises(IdentityFormatError):
            parse_user_status(status_wire(field(7, 'a@example.test') + field(7, 'b@example.test')))

    def test_wrong_sentinel_and_duplicate_sentinel_rejected(self):
        payload = field(7, 'a@example.test')
        for raw in (entry('otherSentinel', payload), entry('userStatusSentinelKey', payload) * 2):
            with self.subTest(raw=bool(raw)), self.assertRaises(IdentityFormatError):
                parse_user_status(base64.b64encode(raw).decode('ascii'))

    def test_unrelated_sentinel_does_not_override_current_identity(self):
        raw = entry('otherSentinel', field(7, 'wrong@example.test'))
        raw += entry('userStatusSentinelKey', field(7, 'correct@example.test'))
        self.assertEqual(parse_user_status(base64.b64encode(raw).decode('ascii'))['email'], 'correct@example.test')

    def test_unpadded_base64_supported_but_arbitrary_text_not(self):
        self.assertEqual(parse_user_status(status_wire(field(7, 'correct@example.test')).rstrip('='))['email'], 'correct@example.test')
        for value in ('garbage a@example.test', b'\xff', '%%%'):
            with self.subTest(value=bool(value)), self.assertRaises((IdentityFormatError, UnicodeError)):
                parse_user_status(value)

    def test_truncated_and_oversized_wire_rejected(self):
        for value in (base64.b64encode(b'\x0a\xff').decode(), 'A' * 2_000_004):
            with self.assertRaises(IdentityFormatError):
                parse_user_status(value)


class ClientSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local'), 'APPDATA': str(self.root / 'roaming')})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def installed(self, name):
        leaf = 'antigravity/Antigravity.exe' if name == 'Antigravity' else 'Antigravity IDE/Antigravity IDE.exe'
        exe = self.root / 'local/Programs' / leaf
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b'test executable, never launched')
        product = exe.parent / 'resources/app/product.json'
        product.parent.mkdir(parents=True)
        product.write_text(json.dumps({'nameShort': name, 'applicationName': 'antigravity' if name == 'Antigravity' else 'antigravity-ide'}), encoding='utf8')
        return clients.installed_client(exe)

    def test_data_directory_follows_product_not_old_hardcoded_name(self):
        client = self.installed('Antigravity IDE')
        create_mock_vscdb(client.database, 'new-account', 'new@example.test')
        old = self.root / 'roaming/Antigravity/User/globalStorage/state.vscdb'
        create_mock_vscdb(old, 'old-account', 'old@example.test')
        with patch.object(clients.psutil, 'process_iter', return_value=[]):
            chosen = clients.default_client()
        self.assertEqual(chosen.database, client.database)
        self.assertEqual(inspect_vscdb_auth(chosen.database)['email'], 'new@example.test')

    def test_orphan_executable_without_product_information_is_not_selected(self):
        old = self.root / 'local/Programs/antigravity/Antigravity.exe'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'orphan')
        current = self.installed('Antigravity IDE')
        with patch.object(clients.psutil, 'process_iter', return_value=[]):
            self.assertEqual(clients.default_client().identity, current.identity)

    def test_two_idle_installations_require_explicit_choice(self):
        self.installed('Antigravity'); self.installed('Antigravity IDE')
        with patch.object(clients.psutil, 'process_iter', return_value=[]):
            with self.assertRaises(clients.ClientSelectionError):
                clients.default_client()

    def test_running_executable_and_custom_user_data_are_bound(self):
        self.installed('Antigravity')
        current = self.installed('Antigravity IDE')
        custom = self.root / 'custom-data'
        proc = SimpleNamespace(info={'name': 'Antigravity IDE.exe'},
            exe=lambda: str(current.executable), cmdline=lambda: [str(current.executable), '--user-data-dir', str(custom)])
        with patch.object(clients.psutil, 'process_iter', return_value=[proc]):
            selected = clients.default_client()
        self.assertTrue(selected.running)
        self.assertEqual(selected.user_data, custom)

    def test_portable_user_data_supported(self):
        client = self.installed('Antigravity IDE')
        portable = client.executable.parent / 'data/user-data'
        portable.mkdir(parents=True)
        self.assertEqual(clients.installed_client(client.executable).user_data, portable)

    def test_launcher_uses_same_executable_and_explicit_data_directory(self):
        client = self.installed('Antigravity IDE')
        with patch.object(launcher.subprocess, 'Popen') as start:
            launcher.launch_antigravity(client=client)
        self.assertEqual(start.call_args.args[0], [str(client.executable), '--user-data-dir', str(client.user_data)])

    def test_other_installation_is_not_considered_selected_running_client(self):
        old = self.installed('Antigravity')
        selected = self.installed('Antigravity IDE')
        proc = SimpleNamespace(info={'name': 'Antigravity.exe'}, exe=lambda: str(old.executable))
        with patch.object(launcher.psutil, 'process_iter', return_value=[proc]):
            self.assertFalse(launcher.is_antigravity_running(selected))

    def test_other_data_environment_is_never_closed(self):
        client = self.installed('Antigravity IDE')
        proc = SimpleNamespace(info={'name': 'Antigravity IDE.exe'}, exe=lambda: str(client.executable),
            cmdline=lambda: [str(client.executable), '--user-data-dir', str(self.root / 'someone-else')], pid=123)
        with patch.object(launcher.psutil, 'process_iter', return_value=[proc]), \
             patch.object(launcher.subprocess, 'run') as close:
            self.assertFalse(launcher.close_antigravity(client))
        close.assert_not_called()


class AccountDisplaySourceTests(unittest.TestCase):
    # Reuse only the isolated harness, not its tests a second time.
    setUpClass = classmethod(lifecycle_tests.HostAndLifecycleTests.setUpClass.__func__)
    setUp = lifecycle_tests.HostAndLifecycleTests.setUp
    tearDown = lifecycle_tests.HostAndLifecycleTests.tearDown
    drain = lifecycle_tests.HostAndLifecycleTests.drain
    def test_table_uses_saved_identity_not_overwriting_it_with_live_account(self):
        saved = self.store.adopt_current('Account A')
        from test_antigravity_integration_safety import set_account
        set_account(self.live, email='b@example.test')
        dialog = AntigravitySwitcherDialog(self.store)
        self.drain(lambda: dialog._quota_worker is None)
        self.assertEqual(dialog.table.item(0, 1).text(), 'a@example.test')
        self.assertIn('b@example.test', dialog.active_label.text())
        self.assertNotIn('当前正在使用', dialog.table.item(0, 4).text())
        self.assertEqual(inspect_vscdb_auth(self.store.profile_db_path(saved))['email'], 'a@example.test')
        dialog.deleteLater()

    def test_legacy_unbound_archives_remain_untouched_and_not_adopted(self):
        storage = ApplicationStorage(self.root / 'private')
        area = storage.for_plugin('antigravity_account_switcher')
        legacy = area.directory('accounts').file('desktop-profiles.json')
        legacy.write_json({'profiles': [{'name': 'unverified legacy'}]})
        before = legacy.read_bytes()
        plugin = AntigravityAccountSwitcherPlugin()
        plugin.register(SimpleNamespace(storage=area, register_command=Mock(), parent_widget=None))
        client = clients.AntigravityClient('Antigravity IDE', self.root / 'test.exe', self.root / 'client')
        store = plugin._client_store(client)
        self.assertTrue(store.legacy_archives)
        self.assertEqual(store.profiles(), [])
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(store.live_db_path, client.database)


if __name__ == '__main__':
    unittest.main()

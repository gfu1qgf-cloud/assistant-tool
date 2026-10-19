import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app_plugins.api import MAIN_MENU
from app_plugins.builtin.antigravity_account_switcher.plugin import (
    AntigravityAccountSwitcherPlugin,
)
from app_plugins.builtin.antigravity_account_switcher.quota import (
    inspect_profile_credentials,
    inspect_vscdb_auth,
)
from app_plugins.builtin.antigravity_account_switcher.store import (
    AUTH_KEYS_TO_SYNC,
    AccountSwitcherError,
    AntigravityProfileStore,
)


def create_mock_vscdb(db_path: Path, token="mock_token_123", email="user@gmail.com"):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    # Match the installed client's actual protobuf topic envelope, not regex-shaped text.
    import base64
    def varint(value):
        result = bytearray()
        while value > 127:
            result.append((value & 127) | 128); value >>= 7
        result.append(value)
        return bytes(result)
    def field(number, value):
        value = value.encode('utf8') if isinstance(value, str) else value
        return varint(number << 3 | 2) + varint(len(value)) + value
    def topic(key, payload):
        entry = field(1, key) + field(2, field(1, base64.b64encode(payload)))
        return base64.b64encode(field(1, entry)).decode('ascii')
    cur.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("antigravityUnifiedStateSync.oauthToken", topic('oauthTokenInfoSentinelKey',
            field(1, 'mock-access-' + token) + field(2, 'Bearer') + field(3, 'mock-refresh-' + token))),
    )
    cur.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("antigravityUnifiedStateSync.userStatus", topic('userStatusSentinelKey', field(3, 'Test User') + field(7, email))),
    )
    cur.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("antigravityUnifiedStateSync.modelCredits", "availableCreditsSentinelKey"),
    )
    cur.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("antigravity.profileUrl", "https://avatar.test.com/photo.jpg"),
    )
    con.commit()
    con.close()


class AntigravityProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.live_db = self.root / "live" / "User" / "globalStorage" / "state.vscdb"
        create_mock_vscdb(self.live_db, token="token_a", email="account_a@gmail.com")
        self.store = AntigravityProfileStore(self.root / "profiles", self.live_db, client_running=lambda: False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_adopt_current_creates_initial_profile_and_copies_auth(self):
        first = self.store.adopt_current("主号")
        self.assertEqual(first.name, "主号")
        self.assertEqual(self.store.active_profile_id, first.profile_id)
        self.assertTrue(self.store.has_auth(first))

        # Check copied db in credential dir
        copied_db = self.store.profile_db_path(first)
        self.assertTrue(copied_db.is_file())
        info = inspect_vscdb_auth(copied_db)
        self.assertEqual(info["email"], "account_a@gmail.com")

    def test_switch_to_another_profile(self):
        first = self.store.adopt_current("主号")
        second = self.store.add_profile("备用号")
        # Give second account a distinct mock DB
        create_mock_vscdb(self.store.profile_db_path(second), token="token_b", email="account_b@gmail.com")

        switched = self.store.switch_to(second.profile_id)
        self.assertEqual(switched.profile_id, second.profile_id)
        self.assertEqual(self.store.active_profile_id, second.profile_id)

        # Verify live DB now contains second profile's auth
        con = sqlite3.connect(str(self.live_db))
        cur = con.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.oauthToken'")
        row = cur.fetchone()
        con.close()
        with sqlite3.connect(self.store.profile_db_path(second)) as expected:
            token_value = expected.execute('SELECT value FROM ItemTable WHERE key=?', (AUTH_KEYS_TO_SYNC[0],)).fetchone()[0]
        expected.close()
        self.assertEqual(token_value, row[0])

    def test_cannot_switch_to_unbound_profile_unless_allowed(self):
        self.store.adopt_current("主号")
        empty = self.store.add_profile("未登录新号")
        with self.assertRaises(AccountSwitcherError):
            self.store.switch_to(empty.profile_id, allow_unbound=False)

    def test_plugin_registration(self):
        plugin = AntigravityAccountSwitcherPlugin()
        context = SimpleNamespace(register_command=Mock())
        plugin.register(context)
        context.register_command.assert_called_once()
        cmd = context.register_command.call_args[0][0]
        self.assertEqual(cmd.command_id, "open")
        self.assertIn(MAIN_MENU, cmd.locations)


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


if __name__ == "__main__":
    unittest.main()

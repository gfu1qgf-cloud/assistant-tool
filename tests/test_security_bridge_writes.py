"""Read-only extension directories fail promptly, without tempfile retry loops."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_plugins.builtin.flow_assistant import bridge_install


class BridgeWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / 'browser_extensions/flow_bridge'
        self.active.mkdir(parents=True)
        self.before = b'{"version":"1.1.1","permissions":["tabs"]}'
        (self.active / 'manifest.json').write_bytes(self.before)

    def test_denied_build_directory_creation_is_attempted_once(self):
        original = bridge_install.data_io.mkdir
        attempts = []
        def denied(path, *args, **kwargs):
            if Path(path).name.startswith('flow-bridge-build-'):
                attempts.append(path)
                raise PermissionError('fixture: read-only directory')
            return original(path, *args, **kwargs)
        with patch.object(bridge_install.data_io, 'mkdir', denied), patch.object(bridge_install, 'builder') as builder:
            with self.assertRaises(PermissionError):
                bridge_install.prepare(self.root)
            builder.assert_not_called()
        self.assertEqual(len(attempts), 1)
        self.assertEqual((self.active / 'manifest.json').read_bytes(), self.before)
        self.assertEqual(list(self.active.parent.glob('flow-bridge-build-*')), [])

    def test_denied_atomic_creation_is_attempted_once_and_keeps_original(self):
        original = bridge_install.data_file
        attempts = []
        def denied(path):
            if path.name.startswith('.flow-update-'):
                from unittest.mock import Mock
                def fail(mode):
                    attempts.append((path, mode))
                    raise PermissionError('fixture: read-only directory')
                return Mock(open=fail)
            return original(path)
        with patch.object(bridge_install, 'data_file', denied):
            with self.assertRaises(PermissionError):
                bridge_install.atomic(self.active / 'manifest.json', b'new')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][1], 'xb')
        self.assertEqual((self.active / 'manifest.json').read_bytes(), self.before)
        self.assertEqual(list(self.active.glob('.flow-update-*')), [])

    def test_failed_replace_removes_our_temporary_file(self):
        with patch.object(bridge_install.data_io, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                bridge_install.atomic(self.active / 'manifest.json', b'new')
        self.assertEqual((self.active / 'manifest.json').read_bytes(), self.before)
        self.assertEqual(list(self.active.glob('.flow-update-*')), [])

    def test_name_collision_does_not_overwrite_or_remove_existing_file(self):
        from types import SimpleNamespace
        existing = self.active / ('.flow-update-' + 'a' * 32 + '.tmp')
        existing.write_bytes(b'do not touch')
        with patch.object(bridge_install.uuid, 'uuid4', return_value=SimpleNamespace(hex='a' * 32)):
            with self.assertRaises(FileExistsError):
                bridge_install.atomic(self.active / 'manifest.json', b'new')
        self.assertEqual(existing.read_bytes(), b'do not touch')
        self.assertEqual((self.active / 'manifest.json').read_bytes(), self.before)

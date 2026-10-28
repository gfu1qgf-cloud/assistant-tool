"""Deferred bridge activation is version-checked, permission-stable and reversible."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from app_plugins.builtin.flow_assistant import bridge_install
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class BridgeInstallTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);self.active=self.root/'browser_extensions/flow_bridge'
        self.active.mkdir(parents=True)
        self.original={'version':'1.1.45','permissions':['tabs']}
        (self.active/'manifest.json').write_text(json.dumps(self.original),encoding='utf-8')
        (self.active/'worker.js').write_text('old',encoding='utf-8')

    def build(self,root,*,destination):
        destination.mkdir()
        (destination/'manifest.json').write_text(json.dumps({**self.original,'version':EXTENSION_VERSION}),encoding='utf-8')
        (destination/'worker.js').write_text('new',encoding='utf-8')

    def test_current_version_is_left_alone_without_building(self):
        (self.active/'manifest.json').write_text(json.dumps({**self.original,'version':EXTENSION_VERSION}),encoding='utf-8')
        with patch.object(bridge_install,'builder') as build:self.assertFalse(bridge_install.prepare(self.root));build.assert_not_called()

    def test_matching_main_start_activates_exact_version_and_is_idempotent(self):
        with patch.object(bridge_install,'builder',return_value=SimpleNamespace(build=self.build)):
            self.assertTrue(bridge_install.prepare(self.root));self.assertFalse(bridge_install.prepare(self.root))
        self.assertEqual((self.active/'worker.js').read_text('utf-8'),'new')
        self.assertEqual(json.loads((self.active/'manifest.json').read_text('utf-8'))['version'],EXTENSION_VERSION)

    def test_upstream_description_update_is_display_only(self):
        def described(root,*,destination):
            self.build(root,destination=destination)
            manifest=json.loads((destination/'manifest.json').read_text('utf-8'))
            manifest['description']='基于已审查的 FlowCue 1.1.6'
            (destination/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with patch.object(bridge_install,'builder',return_value=SimpleNamespace(build=described)):
            self.assertTrue(bridge_install.prepare(self.root))
        self.assertEqual(json.loads((self.active/'manifest.json').read_text('utf-8'))['permissions'],['tabs'])

    def test_build_failure_keeps_the_active_extension_unchanged(self):
        with patch.object(bridge_install,'builder',side_effect=OSError('read denied')):
            with self.assertRaises(OSError):bridge_install.prepare(self.root)
        self.assertEqual((self.active/'worker.js').read_text('utf-8'),'old')
        self.assertEqual(json.loads((self.active/'manifest.json').read_text('utf-8')),self.original)

    def test_copy_failure_rolls_back_all_files_including_manifest(self):
        original=bridge_install.atomic
        def fail(path,data):
            if path.name=='manifest.json' and json.loads(data)['version']==EXTENSION_VERSION:raise OSError('write denied')
            original(path,data)
        with patch.object(bridge_install,'builder',return_value=SimpleNamespace(build=self.build)),patch.object(bridge_install,'atomic',side_effect=fail):
            with self.assertRaises(OSError):bridge_install.prepare(self.root)
        self.assertEqual((self.active/'worker.js').read_text('utf-8'),'old')
        self.assertEqual(json.loads((self.active/'manifest.json').read_text('utf-8')),self.original)

    def test_permission_change_or_future_version_is_not_silently_installed(self):
        def altered(root,*,destination):
            self.build(root,destination=destination)
            manifest=json.loads((destination/'manifest.json').read_text('utf-8'));manifest['permissions'].append('history')
            (destination/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with patch.object(bridge_install,'builder',return_value=SimpleNamespace(build=altered)):
            with self.assertRaises(ValueError):bridge_install.prepare(self.root)
        self.assertEqual((self.active/'worker.js').read_text('utf-8'),'old')
        (self.active/'manifest.json').write_text(json.dumps({**self.original,'version':'1.1.999'}),encoding='utf-8')
        with self.assertRaises(ValueError):bridge_install.prepare(self.root)

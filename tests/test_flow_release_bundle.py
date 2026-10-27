"""Exercise release resources in a synthetic frozen layout, without Chrome."""
import json
import os
from pathlib import Path
from runpy import run_path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from app_plugins.builtin.flow_assistant import bridge_install
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get('LZX_TEST_BASE_ROOT', ROOT))


class FlowFrozenLayoutTests(unittest.TestCase):
    def test_frozen_prepare_uses_extension_beside_executable_not_internal_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / 'browser_extensions/flow_bridge'
            active.mkdir(parents=True)
            (active / 'manifest.json').write_text(json.dumps({'version': EXTENSION_VERSION}), encoding='utf-8')
            internal = root / '_internal/app_plugins/builtin/flow_assistant/bridge_install.py'
            with patch.object(bridge_install, '__file__', str(internal)), \
                 patch('sys.frozen', True, create=True), \
                 patch('sys.executable', str(root / 'AssistantTool.exe')), \
                 patch.object(bridge_install, 'builder') as builder:
                self.assertFalse(bridge_install.prepare())
                builder.assert_not_called()

    def test_frozen_mismatch_does_not_require_or_run_source_build_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / 'browser_extensions/flow_bridge'
            active.mkdir(parents=True)
            manifest = {'version': '1.1.1'}
            (active / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with patch('sys.frozen', True, create=True), \
                 patch.object(bridge_install, 'builder') as builder:
                with self.assertRaisesRegex(ValueError, '完整.*包|版本.*一致'):
                    bridge_install.prepare(root)
                builder.assert_not_called()
            self.assertEqual(json.loads((active / 'manifest.json').read_text('utf-8')), manifest)


class FlowReleaseBundleTests(unittest.TestCase):
    def helpers(self):
        return run_path(str(ROOT / 'packaging/flow_bridge_resources.py'))

    def test_spec_stages_a_fresh_bridge_after_collecting_the_application(self):
        source = (ROOT / 'packaging/AssistantTool.spec').read_text('utf-8-sig')
        self.assertIn('flow_bridge_resources.py', source)
        self.assertIn("stage_flow_bridge(project_root, Path(DISTPATH) / 'AssistantTool')", source)
        self.assertLess(source.index('coll = COLLECT('), source.index('stage_flow_bridge(project_root'))

    def test_real_pinned_builder_stages_complete_bridge_without_touching_active_files(self):
        helpers = self.helpers()
        active = SOURCE_ROOT / 'browser_extensions/flow_bridge/manifest.json'
        before = active.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory) / 'AssistantTool'
            application.mkdir()
            result = helpers['stage_flow_bridge'](SOURCE_ROOT, application)
            self.assertEqual(result, application / 'browser_extensions/flow_bridge')
            actual = {p.relative_to(result).as_posix() for p in result.rglob('*') if p.is_file()}
            self.assertEqual(actual, set(helpers['BRIDGE_FILES']))
            self.assertEqual(json.loads((result / 'manifest.json').read_text('utf-8'))['version'], EXTENSION_VERSION)
            self.assertEqual(helpers['validate_bridge'](result, EXTENSION_VERSION), actual)
            self.assertFalse((application / 'tools').exists())
            self.assertFalse((application / 'config.json').exists())
            self.assertFalse((application / '本地分发').exists())
        self.assertEqual(active.read_bytes(), before)

    def synthetic(self, root, helpers):
        root.mkdir(parents=True)
        for name in helpers['BRIDGE_FILES']:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('synthetic', encoding='utf-8')
        (root / 'manifest.json').write_text(json.dumps({'manifest_version': 3,
            'version': EXTENSION_VERSION, 'background': {'service_worker': 'lzx-background.js'},
            'options_page': 'options.html', 'content_scripts': [{'js': ['lzx-content.js']}]}), encoding='utf-8')
        (root / 'SOURCE.json').write_text(json.dumps({'bridge_version': EXTENSION_VERSION, 'protocol': 1}), encoding='utf-8')

    def test_incomplete_mismatched_or_unsafe_bridge_is_not_publishable(self):
        helpers = self.helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'bridge'
            self.synthetic(root, helpers)
            validate = helpers['validate_bridge']
            validate(root, EXTENSION_VERSION)
            manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
            for change in ({'version': '1.1.0'}, {'options_page': '../outside.html'},
                           {'content_scripts': [{'js': ['missing.js']}]}):
                (root / 'manifest.json').write_text(json.dumps({**manifest, **change}), encoding='utf-8')
                with self.subTest(change=change), self.assertRaises(ValueError):
                    validate(root, EXTENSION_VERSION)
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (root / 'lzx-page.js').unlink()
            with self.assertRaisesRegex(ValueError, 'lzx-page'):
                validate(root, EXTENSION_VERSION)

    def test_release_guard_requires_bridge_files_at_the_same_install_location(self):
        helpers = self.helpers()
        guard = run_path(str(ROOT / 'packaging/check_release_artifact.py'))
        expected = {'browser_extensions/flow_bridge/' + name.casefold() for name in helpers['BRIDGE_FILES']}
        self.assertTrue(expected.issubset(guard['REQUIRED_FILES']))

    def test_missing_one_extension_script_rejects_an_otherwise_complete_archive(self):
        guard = run_path(str(ROOT / 'packaging/check_release_artifact.py'))
        missing = 'browser_extensions/flow_bridge/lzx-reference.js'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'release.zip'
            with ZipFile(path, 'w') as archive:
                for name in guard['REQUIRED_FILES'] - {missing}:
                    archive.writestr(name, 'synthetic')
            with self.assertRaisesRegex(ValueError, 'lzx-reference'):
                guard['validate_release_archive'](path)

    def test_extra_settings_or_empty_assets_are_rejected(self):
        helpers = self.helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'bridge'
            self.synthetic(root, helpers)
            extra = root / 'config.json'
            extra.write_text('{"synthetic": true}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '未声明'):
                helpers['validate_bridge'](root, EXTENSION_VERSION)
            extra.unlink()
            (root / 'lzx-content.js').write_bytes(b'')
            with self.assertRaisesRegex(ValueError, '为空'):
                helpers['validate_bridge'](root, EXTENSION_VERSION)

    def test_existing_packaged_extension_is_never_mixed_or_overwritten(self):
        helpers = self.helpers()
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory)
            target = application / 'browser_extensions/flow_bridge'
            target.mkdir(parents=True)
            marker = target / 'keep.txt'
            marker.write_bytes(b'not replaced')
            with self.assertRaisesRegex(ValueError, '已存在'):
                helpers['stage_flow_bridge'](SOURCE_ROOT, application)
            self.assertEqual(marker.read_bytes(), b'not replaced')
            self.assertEqual(list(target.iterdir()), [marker])


if __name__ == '__main__':
    unittest.main()

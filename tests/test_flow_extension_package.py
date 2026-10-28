"""Release archives must match fresh public sources, never active Chrome data."""
import json
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class FlowExtensionPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = run_path(str(ROOT / 'packaging/package_flow_extension.py'))

    def test_reproducible_install_and_complete_source_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / 'one', Path(temporary) / 'two'
            result = self.api['package'](ROOT, first, commit='synthetic-review')
            again = self.api['package'](ROOT, second, commit='synthetic-review')
            self.assertEqual(result, again)
            for path in first.iterdir():
                self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())
            with ZipFile(first / f"FlowBridge-{result['bridge_version']}.zip") as archive:
                self.assertIn('INSTALL.txt', archive.namelist())
                self.assertIn('GPL-3.0.txt', archive.namelist())
                self.assertEqual(len([n for n in archive.namelist() if n.startswith('FlowBridge/')]), 15)
            with ZipFile(first / f"FlowBridge-{result['bridge_version']}-source.zip") as archive:
                self.assertIn('tools/flow_security_patches.py', archive.namelist())
                self.assertIn('browser_extensions/flow_adapter/page_boundary.js', archive.namelist())
                self.assertNotIn('config.json', archive.namelist())
                self.assertNotIn('browser_extensions/flow_bridge/manifest.json', archive.namelist())
                expected = set(self.api['SOURCE_FILES']) | {'REBUILD.txt', 'REVIEW.txt'}
                self.assertEqual(set(archive.namelist()), expected)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'keep.txt').write_text('keep', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                self.api['package'](ROOT, root)
            self.assertEqual((root / 'keep.txt').read_text(), 'keep')

    def test_mixed_application_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = self.api['module']('package_test_resources', ROOT / 'packaging/flow_bridge_resources.py')
            app = root / 'app'
            app.mkdir()
            extension = helper.stage_flow_bridge(ROOT, app)
            (extension / 'options.html').write_text('tampered', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'differs'):
                self.api['package'](ROOT, root / 'out', application=app)
            self.assertFalse((root / 'out').exists())

    def test_application_and_standalone_match_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = self.api['module']('package_test_resources', ROOT / 'packaging/flow_bridge_resources.py')
            app = root / 'app'
            app.mkdir()
            helper.stage_flow_bridge(ROOT, app)
            result = self.api['package'](ROOT, root / 'out', application=app)
            on_disk = json.loads((root / 'out/FLOW_SHA256.json').read_text('utf-8'))
            self.assertEqual(result, on_disk)

    def test_public_source_rejects_symlinks_and_extra_files_are_not_included(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'public').write_text('safe', encoding='utf-8')
            self.assertEqual(self.api['public_file'](root, 'public'), b'safe')
            self.assertNotIn('config.json', self.api['SOURCE_FILES'])
            try:
                (root / 'linked').symlink_to(root / 'public')
            except OSError:
                self.skipTest('Windows symlink privilege unavailable')
            with self.assertRaisesRegex(ValueError, 'links'):
                self.api['public_file'](root, 'linked')

    def test_ci_uploads_extension_source_and_checksums(self):
        source = (ROOT / '.github/workflows/release.yml').read_text('utf-8-sig')
        self.assertIn('packaging/package_flow_extension.py', source)
        self.assertIn('dist_final/flow/FlowBridge-*.zip', source)
        self.assertIn('dist_final/flow/FLOW_SHA256.json', source)


if __name__ == '__main__':
    unittest.main()

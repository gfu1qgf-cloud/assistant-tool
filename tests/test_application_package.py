from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
module = runpy.run_path(str(ROOT/'packaging/package_application.py'))


class ApplicationPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root/'app'
        self.app.mkdir()
        (self.app/'AssistantTool.exe').write_bytes(b'executable fixture')
        (self.app/'BUILD_INFO.json').write_text('{}', 'utf-8')
        (self.app/'_internal').mkdir()
        (self.app/'_internal/中文.txt').write_bytes(b'content'*1000)
        self.output = self.root/'release.zip'

    def test_streamed_archive_keeps_relative_paths_and_bytes(self):
        self.assertEqual(module['package'](self.app, self.output), 3)
        with ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()),
                {'AssistantTool.exe', 'BUILD_INFO.json', '_internal/中文.txt'})
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read('_internal/中文.txt'), b'content'*1000)

    def test_existing_output_is_never_replaced(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            module['package'](self.app, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep')

    def test_output_inside_application_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'inside'):
            module['package'](self.app, self.app/'recursive.zip')
        self.assertFalse((self.app/'recursive.zip').exists())

    def test_failure_cleans_only_owned_partial_archive(self):
        with patch.object(ZipFile, 'write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                module['package'](self.app, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual((self.app/'AssistantTool.exe').read_bytes(), b'executable fixture')

    def test_missing_build_identity_is_rejected(self):
        (self.app/'BUILD_INFO.json').unlink()
        with self.assertRaisesRegex(ValueError, 'identity'):
            module['package'](self.app, self.output)
        self.assertFalse(self.output.exists())

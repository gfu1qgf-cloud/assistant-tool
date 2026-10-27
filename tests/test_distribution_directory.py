"""Stable local-only distribution directory must stay out of public archives."""
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
GUARD = run_path(str(ROOT / 'packaging/check_release_artifact.py'))


class LocalDistributionPrivacyTests(unittest.TestCase):
    def test_directory_is_explicitly_git_ignored(self):
        rules = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('/本地分发/', rules)

    def test_both_audiences_and_arbitrary_future_resources_rejected_from_release(self):
        for name in ('本地分发/内部版/当前配置/config.json',
                     '本地分发/外部版/外部基础配置.zip',
                     '_internal/本地分发/内部版/配套资源/example.txt',
                     '本地分发/历史归档/2026-10-27/config.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'release.zip'
                with ZipFile(path, 'w') as archive:
                    for required in GUARD['REQUIRED_FILES']:
                        archive.writestr(required, 'synthetic')
                    archive.writestr(name, 'synthetic')
                with self.assertRaises(ValueError):
                    GUARD['validate_release_archive'](path)

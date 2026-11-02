"""Synthetic ZIP privacy checks; never package a user's credential vault."""
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
GUARD = run_path(str(ROOT/'packaging/check_release_artifact.py'))


class GoogleAccountsReleaseTests(unittest.TestCase):
    def test_encrypted_vault_is_private_at_every_package_depth(self):
        for name in ('GoogleAccounts.json', '_internal/GoogleAccounts.json', 'nested/googleaccounts.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'synthetic.zip'
                with ZipFile(path, 'w') as archive:
                    for required in GUARD['REQUIRED_FILES']: archive.writestr(required, 'synthetic')
                    archive.writestr(name, 'synthetic ciphertext')
                with self.assertRaises(ValueError): GUARD['validate_release_archive'](path)

    def test_vault_is_git_ignored_and_volume_migration_catalog_includes_it(self):
        self.assertIn('/GoogleAccounts.json', (ROOT/'.gitignore').read_text('utf-8'))
        from model.PrivateDataCatalog import catalog
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'GoogleAccounts.json'
            path.write_text('synthetic ciphertext')
            self.assertIn('GoogleAccounts.json', [item.relative for item in catalog(directory)])

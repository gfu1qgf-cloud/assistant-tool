"""Synthetic archive entries only; do not read any private configurations."""
import ast
import os
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get('LZX_TEST_BASE_ROOT', ROOT))
GUARD = run_path(str(ROOT / 'packaging/check_release_artifact.py'))


class ReleasePrivacyInventoryTests(unittest.TestCase):
    def archive(self, extra):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'synthetic.zip'
        with ZipFile(path, 'w') as archive:
            for name in GUARD['REQUIRED_FILES']:
                archive.writestr(name, 'synthetic required file')
            archive.writestr(extra, 'synthetic private runtime content')
        return path

    def test_all_registered_private_directories_are_rejected(self):
        tree = ast.parse((SOURCE_ROOT / 'model/PrivateDataCatalog.py').read_text('utf-8-sig'))
        directories = next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == 'PRIVATE_DIRECTORIES' for target in node.targets))
        for directory in (*directories, 'AssistantToolData', 'CodexAccountProfiles'):
            for prefix in ('', '_internal/'):
                with self.subTest(directory=prefix + directory):
                    path = self.archive(prefix + directory + '/synthetic-state.json')
                    with self.assertRaises(ValueError):
                        GUARD['validate_release_archive'](path)

    def test_dependency_configuration_and_bundled_plugin_calendar_are_still_allowed(self):
        for name in ('_internal/dependency/config/schema.json',
                     '_internal/app_plugins/builtin/waste_reminder/data/schedule_2026.json'):
            with self.subTest(name=name):
                self.assertEqual(GUARD['validate_release_archive'](self.archive(name)),
                                 len(GUARD['REQUIRED_FILES']) + 1)

    def test_volume_migration_staging_and_mapped_legacy_data_are_private(self):
        for name in ('.AssistantToolData-migration-0123456789abcdef/config.json',
                     '_internal/.AssistantToolData-migration-0123456789abcdef/example.txt',
                     'ExternalData/synthetic/record.json',
                     '_internal/LegacyData/task_result_defaults.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                GUARD['validate_release_archive'](self.archive(name))


if __name__ == '__main__':
    unittest.main()

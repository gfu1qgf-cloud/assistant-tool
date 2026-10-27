"""Public release must not embed the separate private team-config package."""
from pathlib import Path
from runpy import run_path
import tempfile
import unittest
from zipfile import ZipFile

guard = run_path(str(Path(__file__).resolve().parents[1] / 'packaging/check_release_artifact.py'))


class PrivateConfigReleaseTests(unittest.TestCase):
    def archive(self, extra):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'public.zip'
        with ZipFile(path, 'w') as archive:
            for name in guard['REQUIRED_FILES']:
                archive.writestr(name, 'synthetic')
            archive.writestr(extra, 'synthetic')
        return path

    def test_team_zip_and_unpacked_or_dated_copies_rejected(self):
        for name in ('config/组内分享配置.zip', '组内分享配置-20261027.zip',
                     '_internal/config/组内分享配置/config.json',
                     '_internal/组内分享配置-20261027/使用说明.txt'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                guard['validate_release_archive'](self.archive(name))

    def test_volume_bindings_are_not_part_of_new_user_distribution(self):
        for name in ('storage_bootstrap.json', '_internal/storage_bootstrap.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                guard['validate_release_archive'](self.archive(name))

    def test_config_backups_are_not_a_way_to_bypass_private_config_check(self):
        for name in ('config.json.bak-review', '_internal/config2.json.bak',
                     'config/config.json.bak-20261027'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                guard['validate_release_archive'](self.archive(name))

    def test_third_party_licenses_and_template_resources_still_allowed(self):
        path = self.archive('_internal/third_party/LICENSE.txt')
        self.assertEqual(guard['validate_release_archive'](path), len(guard['REQUIRED_FILES']) + 1)


if __name__ == '__main__':
    unittest.main()

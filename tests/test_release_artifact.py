import tempfile
import unittest
from pathlib import Path
from runpy import run_path
from zipfile import ZipFile

validate_release_archive = run_path(
    str(Path(__file__).resolve().parents[1] / "packaging" / "check_release_artifact.py")
)["validate_release_archive"]


REQUIRED = (
    "AssistantTool.exe",
    "README.md",
    "config.example.json",
    "_internal/runtime/mpv/mpv.exe",
)


class ReleaseArtifactTests(unittest.TestCase):
    def make_archive(self, names):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        archive_path = Path(temporary.name) / "release.zip"
        with ZipFile(archive_path, "w") as archive:
            for name in names:
                archive.writestr(name, "test")
        return archive_path

    def test_clean_package_passes(self):
        archive = self.make_archive(REQUIRED)
        self.assertEqual(validate_release_archive(archive), len(REQUIRED))

    def test_private_runtime_config_fails(self):
        archive = self.make_archive((*REQUIRED, "config.json"))
        with self.assertRaisesRegex(ValueError, "config.json"):
            validate_release_archive(archive)

    def test_nested_credentials_fail(self):
        archive = self.make_archive((*REQUIRED, "_internal/GoogleDriveToken.json"))
        with self.assertRaisesRegex(ValueError, "GoogleDriveToken.json"):
            validate_release_archive(archive)

    def test_path_traversal_fails(self):
        archive = self.make_archive((*REQUIRED, "../private.txt"))
        with self.assertRaisesRegex(ValueError, "private.txt"):
            validate_release_archive(archive)

    def test_missing_executable_fails(self):
        archive = self.make_archive(REQUIRED[1:])
        with self.assertRaisesRegex(ValueError, "assistanttool.exe"):
            validate_release_archive(archive)

    def test_env_variants_and_runtime_cache_are_not_publishable(self):
        for name in ('.env.local', '_internal/.env.production',
                     'SmartMusicSearch/index.sqlite3', 'config/waste_reminder/state.json'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_release_archive(self.make_archive((*REQUIRED, name)))

    def test_empty_env_example_and_builtin_calendar_are_publishable(self):
        archive = self.make_archive((*REQUIRED, '.env.example',
            '_internal/app_plugins/builtin/waste_reminder/data/schedule_2026.json'))
        self.assertEqual(validate_release_archive(archive), len(REQUIRED) + 2)


if __name__ == "__main__":
    unittest.main()

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location(
    "fetch_ffmpeg", Path(__file__).resolve().parents[1]/"packaging"/"fetch_ffmpeg.py")
fetch_ffmpeg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_ffmpeg)


class FetchFFmpegTests(unittest.TestCase):
    def test_wrong_hash_never_installs_or_executes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)/"bad.zip"
            archive.write_bytes(b"not trusted")
            target = Path(directory)/"runtime"
            with self.assertRaisesRegex(ValueError, "校验失败"):
                fetch_ffmpeg.install_archive(archive, target)
            self.assertFalse(target.exists())

    def test_verified_extraction_only_uses_fixed_basenames(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)/"sample.zip"
            with zipfile.ZipFile(archive, "w") as file:
                for name in (*fetch_ffmpeg.RUNTIME_FILES, "LICENSE", "README.txt"):
                    file.writestr("ffmpeg-build/bin/" + name, "test")
                file.writestr("../../outside.txt", "ignored")
            target = Path(directory)/"runtime"
            with patch.object(fetch_ffmpeg, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()):
                fetch_ffmpeg.install_archive(archive, target)
            self.assertTrue((target/"ffmpeg.exe").is_file())
            self.assertTrue((target/"LICENSE").is_file())
            self.assertFalse((Path(directory)/"outside.txt").exists())
            self.assertEqual(len(list(target.iterdir())), 6)


if __name__ == "__main__":
    unittest.main()

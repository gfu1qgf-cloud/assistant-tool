import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from app_plugins.builtin.smart_music_search.audio import segment_starts
from app_plugins.builtin.smart_music_search.encoder import MusicEncoder
from app_plugins.builtin.smart_music_search.index import MusicIndex, discover_music
from app_plugins.builtin.smart_music_search.settings import normalize_settings
from app_plugins.builtin.smart_music_search.ui import SmartMusicSearchDialog


class _FakeEncoder:
    model_id = "fake-music-v1"

    def __init__(self):
        self.calls = 0

    def _load_music(self):
        return None

    def audio(self, samples):
        self.calls += 1
        return np.array([1.0, 0.0], dtype=np.float32)


class SmartMusicSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_sampling_is_bounded_and_reaches_ends(self):
        self.assertEqual(segment_starts(8), [0.0])
        starts = segment_starts(600)
        self.assertEqual(len(starts), 8)
        self.assertEqual(starts[0], 0)
        self.assertEqual(starts[-1], 590)

    def test_default_library_and_lazy_dialog(self):
        settings = normalize_settings()
        self.assertEqual(settings["library_root"], r"D:\2.配乐库")
        with tempfile.TemporaryDirectory() as directory:
            dialog = SmartMusicSearchDialog(settings, index=MusicIndex(directory),
                                            encoder=_FakeEncoder())
            self.assertEqual(dialog.index.count("fake-music-v1"), 0)
            self.assertFalse(dialog.is_busy())
            dialog.close()

    def test_incremental_index_and_duration_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "music"
            root.mkdir()
            first = root / "欢乐.mp3"
            second = root / "悲伤.flac"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            index = MusicIndex(Path(directory) / "index")
            encoder = _FakeEncoder()
            self.assertEqual(len(discover_music(root)), 2)
            with patch("app_plugins.builtin.smart_music_search.index.media_duration",
                       side_effect=lambda path, _probe: 120 if Path(path) == first else 40), patch(
                "app_plugins.builtin.smart_music_search.index.decode_segment",
                return_value=np.ones(48000, dtype=np.float32),
            ):
                outcome = index.sync(root, encoder, "ffmpeg", "ffprobe")
                self.assertEqual(outcome["updated"], 2)
                calls = encoder.calls
                self.assertEqual(index.sync(root, encoder, "ffmpeg", "ffprobe")["updated"], 0)
                self.assertEqual(encoder.calls, calls)
                results = index.search(encoder.model_id, [1, 0], seconds=60)
                self.assertEqual([r["path"] for r in results], [str(first)])
                self.assertEqual(len(index.search(encoder.model_id, [1, 0],
                                                  seconds=60, include_short=True)), 2)
                first.write_bytes(b"changed")
                self.assertEqual(index.sync(root, encoder, "ffmpeg", "ffprobe")["updated"], 1)
                second.unlink()
                index.sync(root, encoder, "ffmpeg", "ffprobe")
                self.assertEqual(index.count(encoder.model_id), 1)

    def test_missing_library_does_not_erase_index(self):
        with tempfile.TemporaryDirectory() as directory:
            index = MusicIndex(directory)
            with self.assertRaises(FileNotFoundError):
                index.sync(Path(directory) / "missing", _FakeEncoder(), "", "")

    def test_english_query_skips_translator(self):
        encoder = MusicEncoder()
        self.assertEqual(encoder.translate("hopeful piano"), "hopeful piano")


if __name__ == "__main__":
    unittest.main()

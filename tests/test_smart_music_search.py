import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from app_plugins.builtin.smart_music_search.audio import encode_reference, segment_starts
from app_plugins.builtin.smart_music_search.encoder import MODEL_ID, MusicEncoder
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
        vector = np.array([1.0, 0.02 * (self.calls % 3)], dtype=np.float32)
        return vector / np.linalg.norm(vector)


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

    def test_reference_audio_uses_selected_excerpt(self):
        class Encoder:
            def audio(self, samples):
                return np.asarray([float(samples[0]), 1.0], dtype=np.float32)

        with patch("app_plugins.builtin.smart_music_search.audio.media_duration",
                   return_value=30.0), patch(
            "app_plugins.builtin.smart_music_search.audio.decode_segment",
            return_value=np.asarray([0.25], dtype=np.float32),
        ) as decode:
            result = encode_reference("reference.mp4", 12.0, Encoder(),
                                      "ffmpeg", "ffprobe")
            decode.assert_called_once_with("reference.mp4", 12.0, "ffmpeg")
            np.testing.assert_array_equal(result, [0.25, 1.0])
            with self.assertRaisesRegex(ValueError, "超出文件长度"):
                encode_reference("reference.mp4", 30.0, Encoder(),
                                 "ffmpeg", "ffprobe")
        with patch("app_plugins.builtin.smart_music_search.audio.media_duration",
                   return_value=30.0), patch(
            "app_plugins.builtin.smart_music_search.audio.decode_segment",
            return_value=np.zeros(48000, dtype=np.float32),
        ):
            with self.assertRaisesRegex(ValueError, "几乎无声"):
                encode_reference("silent.mp4", 0, Encoder(), "ffmpeg", "ffprobe")

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

    def test_short_or_broken_files_do_not_abort_following_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "music"
            root.mkdir()
            for name in ("01.mp3", "02.mp3", "03.mp3", "04.mp3", "05.mp3"):
                (root / name).write_bytes(b"fixture")
            index = MusicIndex(Path(directory) / "index")
            encoder = _FakeEncoder()

            def duration(path, _probe):
                number = int(Path(path).stem)
                if number == 4:
                    raise ValueError("broken audio")
                return 5.0 if number <= 3 else 30.0

            with patch("app_plugins.builtin.smart_music_search.index.media_duration",
                       side_effect=duration), patch(
                "app_plugins.builtin.smart_music_search.index.decode_segment",
                return_value=np.ones(48000, dtype=np.float32),
            ):
                result = index.sync(root, encoder, "ffmpeg", "ffprobe")
                self.assertEqual(result["skipped"], 3)
                self.assertEqual(result["failed"], 1)
                self.assertEqual(index.count(encoder.model_id), 1)
                self.assertEqual(len(index.failures(encoder.model_id)), 1)
                self.assertEqual(index.sync(root, encoder, "ffmpeg", "ffprobe")["updated"], 0)

    def test_english_query_skips_translator(self):
        encoder = MusicEncoder()
        self.assertEqual(encoder.translate("hopeful piano"), "hopeful piano")
        self.assertIn("horror soundtrack", encoder.translate("恐怖"))
        self.assertFalse(encoder.needs_translation("恐怖"))
        self.assertTrue(encoder.needs_translation("庄严的配乐"))
        self.assertIn("larger_clap_general", MODEL_ID)

    def test_text_encoding_uses_full_model_direction(self):
        class Tensor:
            def __init__(self, values):
                self.values = np.asarray(values, dtype=np.float32)

            def detach(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return self.values

        class Processor:
            def __call__(self, *, text, **_kwargs):
                return {"texts": text}

        class Model:
            def get_text_features(self, *, texts):
                choices = {
                    "happy": [0, 1, 0],
                    "dark": [0, 0, 1],
                }
                return [Tensor(choices[item]) for item in texts]

        class Torch:
            inference_mode = staticmethod(nullcontext)

        encoder = MusicEncoder()
        encoder._load_music = lambda: (Torch, Processor(), Model())
        happy, _ = encoder.text("happy")
        dark, _ = encoder.text("dark")
        self.assertLess(float(np.dot(happy, dark)), 0.1)

    def test_audio_retrieval_distinguishes_nearly_parallel_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            index = MusicIndex(Path(directory) / "index")
            now = 123.0
            vectors = ((1.0, 0.03, 0.0), (1.0, 0.0, 0.03),
                       (1.0, -0.03, -0.03))
            with index._connect() as db:
                for number, vector in enumerate(vectors):
                    path = Path(directory) / f"{number}.mp3"
                    path.write_bytes(b"fixture")
                    db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                               (str(path), 7, 0, 30.0, "test", "", now))
                    db.execute("INSERT INTO segments VALUES (?,?,?)",
                               (str(path), 0.0,
                                np.asarray(vector, dtype=np.float16).tobytes()))
            first = index.search("test", [0, 1, 0], limit=3)
            second = index.search("test", [0, 0, 1], limit=3)
            self.assertEqual(Path(first[0]["path"]).name, "0.mp3")
            self.assertEqual(Path(second[0]["path"]).name, "1.mp3")
            self.assertEqual(len(index.search("test", [0, 1, 0], limit=None)), 3)

    def test_results_can_be_loaded_in_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = SmartMusicSearchDialog(
                {"library_root": directory, "result_limit": 10},
                index=MusicIndex(Path(directory) / "index"), encoder=_FakeEncoder(),
            )
            rows = [{"path": str(Path(directory) / f"{n}.mp3"),
                     "score": 1 - n / 100, "duration": 60.0,
                     "start": 0.0, "short": False} for n in range(23)]
            dialog._show_results(rows)
            self.assertEqual(dialog.table.rowCount(), 10)
            self.assertTrue(dialog.more_button.isEnabled())
            dialog.more_button.click()
            self.assertEqual(dialog.table.rowCount(), 20)
            dialog.more_button.click()
            self.assertEqual(dialog.table.rowCount(), 23)
            self.assertFalse(dialog.more_button.isEnabled())
            dialog.close()

    def test_filenames_do_not_override_audio_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            index = MusicIndex(Path(directory) / "index")
            with index._connect() as db:
                for name, vector in (("恐怖.mp3", [0, 1]), ("欢快.mp3", [1, 0])):
                    path = Path(directory) / name
                    path.write_bytes(b"fixture")
                    db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                               (str(path), 7, 0, 30.0, "test", "", 0.0))
                    db.execute("INSERT INTO segments VALUES (?,?,?)",
                               (str(path), 0.0,
                                np.asarray(vector, dtype=np.float16).tobytes()))
            results = index.search("test", [1, 0], limit=None)
            self.assertEqual(Path(results[0]["path"]).name, "欢快.mp3")


if __name__ == "__main__":
    unittest.main()

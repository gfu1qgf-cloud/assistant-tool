import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.smart_music_search.audio import encode_reference, segment_starts
from app_plugins.builtin.smart_music_search.encoder import (
    MODEL_ID, MODEL_SPECS, MusicEncoder, build_music_prompt,
)
from app_plugins.builtin.smart_music_search.feedback import MusicFeedback, search_key
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
        cls.app.setQuitOnLastWindowClosed(False)

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
                       side_effect=lambda path, _probe: 120 if Path(path).resolve() == first.resolve() else 40), patch(
                "app_plugins.builtin.smart_music_search.index.decode_segment",
                return_value=np.ones(48000, dtype=np.float32),
            ):
                outcome = index.sync(root, encoder, "ffmpeg", "ffprobe")
                self.assertEqual(outcome["updated"], 2)
                calls = encoder.calls
                self.assertEqual(index.sync(root, encoder, "ffmpeg", "ffprobe")["updated"], 0)
                self.assertEqual(encoder.calls, calls)
                results = index.search(encoder.model_id, [1, 0], seconds=60)
                self.assertEqual([r["path"] for r in results], [str(first.resolve())])
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

    def test_chinese_tags_combine_without_translating_the_presets(self):
        def unexpected_translation(_query):
            self.fail("selected Chinese tags must bypass the translation model")

        self.assertEqual(
            build_music_prompt("", ("懊悔自责",), ("钢琴",), unexpected_translation),
            "deeply sorrowful remorseful emotional pain piano music",
        )
        self.assertEqual(
            build_music_prompt("恐怖", ("恐怖",), (), unexpected_translation),
            "ominous eerie instrumental music",
        )
        self.assertEqual(
            MusicEncoder().translate("懊悔自责"),
            "deeply sorrowful remorseful instrumental music, heavy sadness and emotional pain",
        )

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

        encoder = MusicEncoder("general", "legacy")
        encoder._load_music = lambda: (Torch, Processor(), Model())
        happy, _ = encoder.text("happy")
        dark, _ = encoder.text("dark")
        self.assertLess(float(np.dot(happy, dark)), 0.1)

    def test_music_model_is_pinned_safe_and_legacy_identity_preserved(self):
        encoder = MusicEncoder()
        self.assertEqual(encoder.model_key, "general")
        self.assertEqual(len(encoder.spec["revision"]), 40)
        self.assertIn("larger_clap_general", encoder.model_id)
        self.assertEqual(MusicEncoder("general", "legacy").model_id, MODEL_ID)
        with patch.object(encoder, "_cached", return_value=True) as cached:
            self.assertTrue(encoder.music_is_cached())
        cached.assert_called_once_with(MODEL_SPECS["general"]["repository"], MODEL_SPECS["general"]["revision"])
        self.assertNotIn("music", MODEL_SPECS)
        self.assertNotEqual(encoder.translate("懊悔"), encoder.translate("懊悔自责"))

    def test_model_health_guard_rejects_collapsed_features(self):
        class Tensor:
            def __init__(self, values):
                self.values = np.asarray(values, dtype=np.float32)
            def detach(self):
                return self
            def cpu(self):
                return self
            def numpy(self):
                return self.values
        with self.assertRaisesRegex(RuntimeError, "几乎相同"):
            MusicEncoder._validate_text_features([Tensor([1, 0, 0]) for _ in range(3)])
        self.assertEqual(MusicEncoder._validate_text_features([
            Tensor([1, 0, 0]), Tensor([0, 1, 0]), Tensor([0, 0, 1])]), 0.)

    def test_finished_worker_keeps_options_locked_until_queued_cleanup(self):
        # This models a finished native thread whose queued UI cleanup has not
        # run yet: settings must remain deferred and no second task may start.
        with tempfile.TemporaryDirectory() as directory:
            dialog = SmartMusicSearchDialog({}, index=MusicIndex(directory), encoder=_FakeEncoder())
            class FinishedWorker:
                def isRunning(self):
                    return False
                def deleteLater(self):
                    pass
            dialog.worker = FinishedWorker()
            self.assertTrue(dialog.is_busy())
            dialog.update_settings({**dialog.settings, "coverage": "balanced"})
            self.assertEqual(dialog.settings["coverage"], "full")
            dialog._finished()
            self.assertEqual(dialog.settings["coverage"], "balanced")
            dialog.close()

    def test_model_and_coverage_indexes_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = MusicIndex.for_encoder(MusicEncoder("general", "legacy"), directory)
            full = MusicIndex.for_encoder(MusicEncoder(), directory)
            balanced = MusicIndex.for_encoder(MusicEncoder("general", "balanced"), directory)
            self.assertEqual(legacy.path.name, "index.sqlite3")
            self.assertEqual(len({legacy.path, full.path, balanced.path}), 3)
            song = Path(directory) / "fixture.mp3"
            song.write_bytes(b"audio")
            with legacy._connect() as db:
                db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                           (str(song), 5, 0, 60., MODEL_ID, "", 0.))
            before = legacy.path.read_bytes()
            full.count(MusicEncoder().model_id)
            self.assertEqual(legacy.path.read_bytes(), before)
            self.assertEqual(legacy.count(MODEL_ID), 1)

    def test_precise_sampling_covers_all_normal_songs_and_is_bounded(self):
        starts = MusicEncoder().segment_starts(600)
        self.assertGreater(len(starts), 8)
        self.assertEqual(starts[0], 0)
        self.assertEqual(starts[-1], 590)
        self.assertLessEqual(max(b-a for a, b in zip(starts, starts[1:])), 10.001)
        self.assertLess(len(segment_starts(600, "balanced")), len(starts))
        self.assertEqual(len(segment_starts(24 * 3600, "full")), 256)

    def test_sync_uses_encoder_coverage_and_resume_skips_completed_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "songs"
            root.mkdir()
            (root / "song.mp3").write_bytes(b"audio")
            encoder = _FakeEncoder()
            encoder.segment_starts = lambda duration: [0., 10., 20., 30.]
            index = MusicIndex(Path(directory) / "index")
            with patch("app_plugins.builtin.smart_music_search.index.media_duration", return_value=40.), patch(
                "app_plugins.builtin.smart_music_search.index.decode_segment", return_value=np.ones(48000)
            ):
                index.sync(root, encoder, "ffmpeg", "ffprobe")
                self.assertEqual(encoder.calls, 4)
                self.assertEqual(index.sync(root, encoder, "ffmpeg", "ffprobe")["updated"], 0)

    def test_stable_ranking_does_not_reward_only_one_isolated_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            index = MusicIndex(Path(directory) / "index")
            with index._connect() as db:
                for name, scores in (("spike.mp3", [.99, .1, .1, .1]),
                                     ("consistent.mp3", [.8, .8, .8, .8])):
                    path = Path(directory) / name
                    path.write_bytes(b"audio")
                    db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?)", (str(path), 5, 0, 40., "test", "", 0.))
                    for start, score in enumerate(scores):
                        vector = [score, np.sqrt(1-score*score)]
                        db.execute("INSERT INTO segments VALUES (?,?,?)", (str(path), start*10., np.asarray(vector, dtype=np.float16).tobytes()))
            self.assertEqual(Path(index.search("test", [1, 0])[0]["path"]).name, "spike.mp3")
            self.assertEqual(Path(index.search("test", [1, 0], stable=True)[0]["path"]).name, "consistent.mp3")
            with self.assertRaises(ValueError):
                index.search("test", [float("nan"), 0])

    def test_feedback_is_persistent_query_specific_and_not_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "first.mp3", Path(directory) / "second.mp3"]
            for path in paths:
                path.write_bytes(b"audio")
            rows = [{"path": str(path), "score": score, "duration": 60., "start": 0., "short": False}
                    for path, score in zip(paths, [.5, .48])]
            key = search_key("恐怖", ("紧张",), ())
            feedback = MusicFeedback(directory)
            feedback.set(key, paths[1], 1)
            ranked = MusicFeedback(directory).apply(rows, key)
            self.assertEqual(ranked[0]["path"], str(paths[1]))
            self.assertAlmostEqual(ranked[0]["audio_score"], .48)
            self.assertEqual(feedback.apply(ranked, key)[0]["score"], ranked[0]["score"])
            self.assertEqual(feedback.apply(rows, search_key("平静"))[0]["path"], str(paths[0]))
            self.assertEqual(feedback.apply(rows, key, False)[0]["path"], str(paths[0]))
            paths[1].write_bytes(b"changed music")
            self.assertEqual(feedback.apply(rows, key)[0]["path"], str(paths[0]))
            feedback.set(key, paths[1], -1)
            self.assertEqual(feedback.apply(rows, key)[1]["preference"], -1)
            feedback.set(key, paths[1], 0)
            self.assertTrue(all(row["preference"] == 0 for row in feedback.apply(rows, key)))

    def test_dialog_model_switch_is_lazy_and_settings_defer_during_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            encoder = MusicEncoder()
            dialog = SmartMusicSearchDialog({"library_root": directory},
                index=MusicIndex.for_encoder(encoder, directory))
            changed = []
            dialog.settingsChanged.connect(changed.append)
            with patch.object(MusicEncoder, "_load_music", side_effect=AssertionError("must stay lazy")):
                dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("unfused"))
                self.assertEqual(dialog.encoder.model_key, "unfused")
                self.assertEqual(changed[-1]["model_key"], "unfused")
            old_encoder = dialog.encoder
            class Worker:
                def isRunning(self):
                    return True
                def deleteLater(self):
                    pass
            dialog.worker = Worker()
            dialog.update_settings({**dialog.settings, "model_key": "general"})
            self.assertIs(dialog.encoder, old_encoder)
            dialog._finished()
            self.assertEqual(dialog.encoder.model_key, "general")
            dialog.close()

    def test_result_feedback_marks_are_visible_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / "song.mp3"
            song.write_bytes(b"audio")
            dialog = SmartMusicSearchDialog({}, index=MusicIndex(directory), encoder=_FakeEncoder())
            dialog._feedback_key = search_key("恐怖")
            row = {"path": str(song), "score": .4, "duration": 60., "start": 0., "short": False}
            dialog._show_results([row])
            dialog._mark_feedback(row, 1)
            self.assertIn("符合", dialog.table.item(0, 0).text())
            dialog._mark_feedback(row, 0)
            self.assertNotIn("符合", dialog.table.item(0, 0).text())
            dialog.close()

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

    def test_music_preview_slider_seeks_and_refreshes_listening_window(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = SmartMusicSearchDialog(
                {"library_root": directory},
                index=MusicIndex(Path(directory) / "index"), encoder=_FakeEncoder(),
            )

            class FakePlayer:
                def __init__(self):
                    self.positions = []
                    self.play_calls = 0

                def setPosition(self, position):
                    self.positions.append(position)

                def play(self):
                    self.play_calls += 1

                def stop(self):
                    pass

            player = FakePlayer()
            dialog._player = player
            dialog.duration.setValue(20)
            dialog._preview_duration_changed(120_000)
            dialog._seek_preview_to(50_000)
            self.assertEqual(player.positions, [50_000])
            self.assertEqual(dialog._preview_end_ms, 70_000)
            dialog._seek_pressed()
            dialog.seek_slider.setValue(40_000)
            dialog._preview_position_changed(52_000)
            self.assertEqual(dialog.seek_slider.value(), 40_000)
            dialog._seek_released()
            self.assertEqual(player.positions[-1], 40_000)
            self.assertEqual(dialog._preview_end_ms, 60_000)
            dialog._seek_preview_relative(-10_000)
            self.assertEqual(player.positions[-1], 30_000)
            dialog.close()

    def test_result_rows_drag_original_audio_as_copy_only(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "first.mp3", Path(directory) / "second.wav"]
            for path in paths:
                path.write_bytes(b"audio")
            dialog = SmartMusicSearchDialog(
                {"library_root": directory},
                index=MusicIndex(Path(directory) / "index"), encoder=_FakeEncoder(),
            )
            dialog._show_results([
                {"path": str(path), "score": 0.9, "duration": 60.0,
                 "start": 0.0, "short": False}
                for path in paths
            ])
            selection = dialog.table.selectionModel()
            flags = (QtCore.QItemSelectionModel.SelectionFlag.Select
                     | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            for row in range(2):
                selection.select(dialog.table.model().index(row, 0), flags)
            mime = dialog.table.mimeData(dialog.table.selectedItems())
            self.assertEqual(
                [Path(url.toLocalFile()) for url in mime.urls()],
                paths,
            )
            captured = []

            class FakeDrag:
                def __init__(self, _parent):
                    pass

                def setMimeData(self, data):
                    captured.append(data)

                def exec(self, supported, default):
                    captured.append((supported, default))
                    return default

            with patch("app_plugins.builtin.smart_music_search.ui.QtGui.QDrag", FakeDrag):
                dialog.table.startDrag(QtCore.Qt.DropAction.MoveAction)
            self.assertEqual(captured[1], (
                QtCore.Qt.DropAction.CopyAction,
                QtCore.Qt.DropAction.CopyAction,
            ))
            self.assertEqual([Path(url.toLocalFile()) for url in captured[0].urls()], paths)
            self.assertTrue(all(path.exists() for path in paths))
            dialog.close()

    def test_popup_tags_become_removable_input_chips(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = SmartMusicSearchDialog(
                {"library_root": directory}, index=MusicIndex(Path(directory) / "index"),
                encoder=_FakeEncoder(),
            )
            dialog.show()
            dialog.tag_input.tag_button.click()
            picker = dialog.tag_input.picker
            self.assertTrue(picker.isVisible())
            picker.buttons[("mood", "懊悔自责")].click()
            picker.buttons[("sound", "钢琴")].click()
            self.assertEqual(dialog._selected_tags(), (("懊悔自责",), ("钢琴",)))
            self.assertEqual(dialog.tag_input.chip_layout.count(), 2)
            self.app.processEvents()
            self.assertGreater(dialog.tag_input.chip_area.width(), 0)
            self.assertTrue(picker.isVisible())
            picker.close()
            dialog.query.keyPressEvent(QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Backspace,
                QtCore.Qt.KeyboardModifier.NoModifier,
            ))
            self.assertEqual(dialog._selected_tags(), (("懊悔自责",), ()))
            dialog.tag_input.chip_layout.itemAt(0).widget().click()
            self.assertEqual(dialog._selected_tags(), ((), ()))
            dialog.tag_input.show_picker()
            dialog.close()
            self.assertFalse(picker.isVisible())

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

    def test_filename_match_is_optional_separate_and_keeps_short_song(self):
        with tempfile.TemporaryDirectory() as directory:
            index = MusicIndex(Path(directory) / "index")
            song = Path(directory) / "PYZ-懊悔自责（加长版）_20230317.wav"
            other = Path(directory) / "明亮.mp3"
            with index._connect() as db:
                for path, vector in ((song, [0, 1]), (other, [1, 0])):
                    path.write_bytes(b"fixture")
                    db.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?)",
                               (str(path), 7, 0, 81.0, "test", "", 0.0))
                    db.execute("INSERT INTO segments VALUES (?,?,?)",
                               (str(path), 0.0,
                                np.asarray(vector, dtype=np.float16).tobytes()))
            self.assertEqual(Path(index.search("test", [1, 0])[0]["path"]), other)
            hits = index.search_filename("懊悔自责", seconds=120)
            self.assertEqual([row["path"] for row in hits], [str(song)])
            self.assertEqual(hits[0]["match_type"], "filename")
            self.assertTrue(hits[0]["short"])
            self.assertEqual(index.search_filename("懊悔-自责")[0]["path"], str(song))
            self.assertEqual(index.search_filename(song.name)[0]["path"], str(song))

            class Encoder:
                model_id = "test"
                needs_translation = staticmethod(lambda _query: False)
                translate = staticmethod(lambda query: query)

                @staticmethod
                def text(query):
                    return np.asarray([1, 0], dtype=np.float32), query

            dialog = SmartMusicSearchDialog(
                {"library_root": directory}, index=index, encoder=Encoder(),
            )
            self.assertFalse(dialog.filename_match.isChecked())
            dialog.filename_match.setChecked(True)
            dialog.query.setText("懊悔自责")

            def run_now(kind, task):
                dialog._task_kind = kind
                dialog._completed(task(lambda *_args: None, lambda: False))

            dialog._start = run_now
            dialog.search()
            self.assertEqual(dialog._results[0]["path"], str(song))
            self.assertEqual(dialog.table.item(0, 0).text(), "文件名命中")
            self.assertEqual(len(dialog._results), 2)
            dialog.close()


if __name__ == "__main__":
    unittest.main()

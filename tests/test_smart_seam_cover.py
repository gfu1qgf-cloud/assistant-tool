"""B-roll covers edit joins without changing interview audio or the timeline."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_video_editor import engine, background, seam_cover as cover
from app_plugins.builtin.smart_video_editor.seam_cover_ui import SeamCoverControls
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog


class SeamCoverPlanTests(unittest.TestCase):
    def test_defaults_and_invalid_duration_are_normalized(self):
        self.assertEqual(cover.normalize_options({}), cover.DEFAULTS)
        for value in (None, "bad", float("nan"), float("inf"), {}):
            self.assertEqual(cover.normalize_options({cover.DURATION: value})[cover.DURATION], 1500)
        self.assertEqual(cover.normalize_options({cover.DURATION: 50})[cover.DURATION], 500)
        self.assertEqual(cover.normalize_options({cover.DURATION: 99999})[cover.DURATION], 5000)
        settings = engine.normalize_smart_video_editor_settings({cover.ENABLED: True, cover.SOURCE: " a.mp4 "})
        self.assertEqual(settings[cover.SOURCE], "a.mp4")
        self.assertFalse(set(cover.DEFAULTS) & set(engine._ANALYSIS_SETTING_KEYS))

    def test_windows_straddle_every_known_join_and_advance_only_while_visible(self):
        self.assertEqual(cover.plan_windows([60, 60, 60], 30, 1000),
                         (cover.CoverWindow(45, 75, 0), cover.CoverWindow(105, 135, 30)))

    def test_close_joins_merge_and_clip_to_the_existing_timeline(self):
        self.assertEqual(cover.plan_windows([5, 5, 5], 30, 1500), (cover.CoverWindow(0, 15, 0),))
        self.assertEqual(cover.plan_windows([100], 30, 1500), ())
        self.assertEqual(cover.plan_windows([], 30, 1500), ())

    def test_source_cursor_continues_across_a_seam_and_wraps(self):
        plan = cover.CoverPlan("cover.mp4", 20, 30,
                               (cover.CoverWindow(45, 75, 0), cover.CoverWindow(105, 135, 30)))
        self.assertEqual(list(plan.pieces(0, 60)), [cover.CoverWindow(45, 60, 0)])
        self.assertEqual(list(plan.pieces(60, 120)),
                         [cover.CoverWindow(0, 15, 15), cover.CoverWindow(45, 60, 10)])
        self.assertEqual(list(plan.pieces(120, 180)), [cover.CoverWindow(0, 15, 5)])

    def test_disabled_nonjoined_or_single_segment_does_not_probe_or_decode_asset(self):
        for options, counts in (({}, [30, 30]), ({cover.ENABLED: True, "export_mode": "srt_only"}, [30, 30]),
                                ({cover.ENABLED: True, "export_mode": "segments"}, [30, 30]),
                                ({cover.ENABLED: True}, [30])):
            with patch.object(cover, "validate_source", side_effect=AssertionError("no cover work")):
                self.assertIsNone(cover.prepare_cover(options, counts, 320, 180, 30, ".", "ffmpeg",
                                                      Mock(), Mock(), lambda: False))

    def test_cover_inputs_map_no_audio_and_stop_overlay_at_its_end(self):
        plan = cover.CoverPlan("cover.mp4", 60, 30, (cover.CoverWindow(10, 20, 0),))
        inputs, options = cover.segment_video_args(plan, 0, 30, "fps=30,setpts=PTS-STARTPTS")
        self.assertIn("-stream_loop", inputs)
        self.assertNotIn("-map", inputs)
        self.assertIn("repeatlast=0", options[1])
        self.assertIn("eof_action=pass", options[1])
        self.assertNotIn(":a", options[1])
        self.assertEqual(cover.segment_video_args(plan, 30, 30, "fps=30"),
                         ([], ["-map", "0:v:0", "-vf", "fps=30"]))

    def test_cancellation_before_asset_probe_needs_no_decoder_or_file(self):
        with patch.object(cover, "validate_source", side_effect=AssertionError("already cancelled")):
            with self.assertRaises(InterruptedError):
                cover.prepare_cover({cover.ENABLED: True}, [30, 30], 160, 90, 30,
                                    ".", "ffmpeg", Mock(), Mock(), lambda: True)


class SeamCoverInputTests(unittest.TestCase):
    def test_queue_fingerprints_include_only_an_active_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, asset = Path(temporary)/"main.mp4", Path(temporary)/"cover.mp4"
            source.write_bytes(b"original")
            asset.write_bytes(b"cover")
            task = {"clips": [{"source": str(source)}]}
            bundle = {"tasks": [task], "settings": {cover.ENABLED: True, cover.SOURCE: str(asset)}}
            self.assertEqual(len(background.render_inputs(bundle)), 2)
            self.assertEqual(len(background.render_inputs(bundle, include_seam_cover=False)), 1)
            self.assertEqual(len(background.render_inputs(bundle, dict(bundle["settings"], export_mode="srt_only"))), 1)
            self.assertEqual(len(background.render_inputs(bundle, dict(bundle["settings"], export_mode="segments"))), 1)
            engine.set_smart_video_missing_review(task, "skipped")
            asset.unlink()
            self.assertEqual(background.render_inputs(bundle), {})

    def test_changed_cover_is_rejected_before_any_media_or_model_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary)/"cover.mp4"
            asset.write_bytes(b"initial")
            before = background.version_of(asset)
            asset.write_bytes(b"asset was replaced while waiting")
            payload = {"phase": "export", "settings": {}, "bundle": {"tasks": []},
                       "source_versions": {str(asset): before}}
            with patch.object(background, "export_smart_video_bundle", side_effect=AssertionError("must not export")):
                with self.assertRaisesRegex(RuntimeError, "排队期间源视频已变化"):
                    background.run_smart_video_job(payload, Mock())


class SeamCoverUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_review_preserves_asset_and_duration_when_switching_export_modes(self):
        options = {cover.ENABLED: True, cover.SOURCE: "example.mp4", cover.DURATION: 2000}
        dialog = SmartVideoReviewDialog({"tasks": [], "summary": {}, "settings": options})
        try:
            controls = dialog.seam_cover_controls
            self.assertTrue(controls.isEnabled())
            for mode in ("segments", "srt_only"):
                dialog.export_mode_combo.setCurrentIndex(dialog.export_mode_combo.findData(mode))
                self.assertFalse(controls.isEnabled())
                self.assertEqual({key: dialog.collect()["settings"][key] for key in options}, options)
            dialog.export_mode_combo.setCurrentIndex(0)
            self.assertTrue(controls.isEnabled())
            self.assertEqual(controls.selection(), options)
        finally:
            dialog.reject()

    def test_settings_roundtrip_and_default_off(self):
        page = SmartVideoEditorSettingsPage()
        try:
            self.assertFalse(page.settings()[cover.ENABLED])
            options = {cover.ENABLED: True, cover.SOURCE: "sample.mp4", cover.DURATION: 1000}
            page.load_config({engine.SMART_VIDEO_EDITOR_CONFIG_KEY: options})
            self.assertEqual({key: page.settings()[key] for key in options}, options)
            page.export_mode_combo.setCurrentIndex(page.export_mode_combo.findData("srt_only"))
            self.assertFalse(page.seam_cover_controls.isEnabled())
        finally:
            page.widget.close()

    def test_missing_asset_is_only_validated_when_requested(self):
        controls = SeamCoverControls({cover.ENABLED: True})
        try:
            self.assertTrue(controls.selection()[cover.ENABLED])
            with self.assertRaisesRegex(ValueError, "选择一个存在的视频"):
                controls.selection(validate=True)
            controls.enabled_checkbox.setChecked(False)
            controls.selection(validate=True)
        finally:
            controls.close()

    def test_compact_settings_dialog_only_applies_accepted_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary)/"cover.mp4"
            asset.write_bytes(b"UI selection fixture")
            original = {cover.ENABLED: True, cover.SOURCE: str(asset), cover.DURATION: 1500}
            controls = SeamCoverControls(original, compact=True)
            try:
                def edit_and_finish(accept):
                    dialog = self.app.activeModalWidget()
                    fields = dialog.findChildren(SeamCoverControls)[0]
                    fields.duration_spinbox.setValue(2.0)
                    box = dialog.findChild(QtWidgets.QDialogButtonBox)
                    box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok if accept else
                               QtWidgets.QDialogButtonBox.StandardButton.Cancel).click()
                QtCore.QTimer.singleShot(0, lambda: edit_and_finish(False))
                controls._configure()
                self.assertEqual(controls.selection(), original)
                QtCore.QTimer.singleShot(0, lambda: edit_and_finish(True))
                controls._configure()
                self.assertEqual(controls.selection()[cover.DURATION], 2000)
            finally:
                controls.close()


class SeamCoverNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("NODE_FFMPEG") or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.ffprobe = str(Path(cls.ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_ffmpeg(self, args, capture=False):
        return subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                              check=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.PIPE)

    def make_main(self):
        path = self.root/"main.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=white:s=160x90:r=30:d=3",
                         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)])
        return path

    def export(self, main, asset, *, enabled=True, ranges=None, name="export"):
        task = {"task_id": name, "output_dir": str(self.root/name), "clips": [{
            "source": str(main), "file_name": main.name, "included": True,
            "kept_ranges": ranges or [[.2, 2.2]]*5, "export_order": 1}]}
        options = engine.normalize_smart_video_editor_settings({cover.ENABLED: enabled, cover.SOURCE: str(asset),
                         cover.DURATION: 1500, "existing_output": "overwrite"})
        before = (hashlib.sha256(main.read_bytes()).hexdigest(), hashlib.sha256(asset.read_bytes()).hexdigest())
        result = engine._export_task(task, options, self.ffmpeg, lambda _: None, lambda: False, generate_srt=False)
        self.assertEqual(before, (hashlib.sha256(main.read_bytes()).hexdigest(), hashlib.sha256(asset.read_bytes()).hexdigest()))
        return Path(result["video"])

    def frames(self, path):
        import numpy as np
        raw = self.run_ffmpeg(["-i", str(path), "-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], True).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape((-1, 90, 160, 3))

    def audio(self, path):
        return self.run_ffmpeg(["-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "s16le", "-"], True).stdout

    def test_real_export_covers_all_joins_rotates_loops_and_preserves_audio_exactly(self):
        import numpy as np
        main, asset = self.make_main(), self.root/"cover-with-sound.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=54x96:r=25:d=0.8",
                         "-f", "lavfi", "-i", "color=lime:s=54x96:r=25:d=0.8",
                         "-f", "lavfi", "-i", "color=blue:s=54x96:r=25:d=0.8",
                         "-f", "lavfi", "-i", "sine=frequency=1500:sample_rate=44100:duration=2.4",
                         "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(asset)])
        original = self.export(main, asset, enabled=False, name="baseline")
        result = self.export(main, asset)
        self.assertEqual(self.audio(original), self.audio(result), "B-roll's sound must never replace or shift interview audio")
        frames, baseline = self.frames(result), self.frames(original)
        self.assertEqual(frames.shape, baseline.shape)
        self.assertEqual(len(frames), 300)
        windows = cover.plan_windows([60]*5, 30, 1500)
        for index in range(300):
            window = next((w for w in windows if w.start <= index < w.end), None)
            pixel = frames[index, 45, 80].astype(int)
            if window is None:
                self.assertTrue(np.all(pixel > 230), (index, pixel))
            else:
                source_frame = (window.source_start + index-window.start) % 72
                # Sample away from source colour boundaries, allowing frame-rate conversion rounding.
                if source_frame % 24 in range(2, 22):
                    channel = source_frame//24
                    self.assertGreater(pixel[channel], 180, (index, source_frame, pixel))
                    self.assertLess(max(np.delete(pixel, channel)), 60, (index, source_frame, pixel))
                self.assertLess(int(frames[index, 45, 3].max()), 20, "portrait cover must fit with black margins")
        self.assertFalse(list(result.parent.glob("smart_edit_*")))

    def test_asset_shorter_than_one_cover_loops_without_freezing_or_exposing_join(self):
        main, asset = self.make_main(), self.root/"short-silent.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=160x90:r=30:d=0.1",
                         "-f", "lavfi", "-i", "color=blue:s=160x90:r=30:d=0.1",
                         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                         "-c:v", "libx264", str(asset)])
        result = self.export(main, asset, ranges=[[.2, 2.2]]*2)
        frames = self.frames(result)
        self.assertEqual(len(frames), 120)
        window = cover.plan_windows([60, 60], 30, 1500)[0]
        seen = set()
        for index in range(window.start+1, window.end-1):
            pixel = frames[index, 45, 80]
            self.assertLess(int(pixel[1]), 60, (index, pixel))
            seen.add(int(pixel.argmax()))
        self.assertEqual(seen, {0, 2}, "short cover must keep moving through its loop")

    def test_invalid_asset_fails_without_overwriting_existing_output(self):
        main, asset = self.make_main(), self.root/"not-video.mp4"
        asset.write_bytes(b"not a video")
        folder = self.root/"export"
        folder.mkdir()
        previous = folder/"export_智能剪辑.mp4"
        previous.write_bytes(b"previous final video")
        with self.assertRaises(ValueError):
            self.export(main, asset)
        self.assertEqual(previous.read_bytes(), b"previous final video")
        self.assertEqual(list(folder.iterdir()), [previous])

    def test_cancellation_during_cover_preparation_preserves_old_video_and_cleans_stage(self):
        main, asset = self.make_main(), self.root/"cover.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=160x90:r=30:d=1",
                         "-an", "-c:v", "libx264", str(asset)])
        output = self.root/"output"
        output.mkdir()
        previous = output/"demo_智能剪辑.mp4"
        previous.write_bytes(b"previous final video")
        task = {"task_id": "demo", "output_dir": str(output), "clips": [{
            "source": str(main), "file_name": main.name, "kept_ranges": [[0, 1], [2, 3]]}]}
        options = engine.normalize_smart_video_editor_settings({cover.ENABLED: True, cover.SOURCE: str(asset),
                                                               "existing_output": "overwrite"})
        original_run = engine._run_process
        def cancel_after_preparing(args, progress, cancelled):
            original_run(args, progress, cancelled)
            if Path(args[-1]).name == "seam_cover.mp4":
                raise InterruptedError("cancel fixture after asset preparation")
        with patch.object(engine, "_run_process", side_effect=cancel_after_preparing):
            with self.assertRaises(InterruptedError):
                engine._export_task(task, options, self.ffmpeg, lambda _: None, lambda: False, generate_srt=False)
        self.assertEqual(previous.read_bytes(), b"previous final video")
        self.assertEqual(list(output.iterdir()), [previous])

    def test_dense_short_segments_merge_cover_and_keep_frame_count(self):
        main, asset = self.make_main(), self.root/"cover.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=blue:s=160x90:r=30:d=0.3",
                         "-an", "-c:v", "libx264", str(asset)])
        result = self.export(main, asset, ranges=[[.2, .5]]*5)
        frames = self.frames(result)
        self.assertEqual(len(frames), 45)
        for frame in frames:
            self.assertGreater(int(frame[45, 80, 2]), 180)
            self.assertLess(int(frame[45, 80, 0]), 60)

    def test_fractional_frame_rate_and_irregular_cuts_keep_original_audio_and_frames(self):
        main, asset = self.root/"fractional.mp4", self.root/"cover.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=white:s=160x90:r=30000/1001:d=3",
                         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(main)])
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=160x90:r=25:d=0.4",
                         "-an", "-c:v", "libx264", str(asset)])
        ranges = [[.12, .87], [1.1, 1.94], [2.1, 2.73]]
        baseline = self.export(main, asset, enabled=False, ranges=ranges, name="baseline")
        result = self.export(main, asset, ranges=ranges)
        self.assertEqual(self.audio(baseline), self.audio(result))
        self.assertEqual(self.frames(baseline).shape, self.frames(result).shape)

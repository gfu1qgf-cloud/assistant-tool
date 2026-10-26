"""Real decoder/Qt playback with controlled AI; no model downloads or movie caches."""
import copy
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtTest, QtWidgets
from app_plugins.builtin.batch_text_video import matting_render as render
from app_plugins.builtin.batch_text_video.matting_ui import MattingDialog
from app_plugins.builtin.batch_text_video.matting_video import MattingVideoWorker
from app_plugins.builtin.batch_text_video.matting_settings import normalize
import test_matting_studio as fixture

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class VideoPredictor:
    calls, sessions = 0, 0
    def __init__(self, *args, **kwargs): type(self).sessions += 1; self.device = 'CPU'
    def predict(self, rgb):
        type(self).calls += 1
        return rgb.copy(), (rgb[:, :, 0] > rgb[:, :, 1]).astype(np.float32)


@unittest.skipUnless(Path(FFMPEG).is_file(), 'Explicit FFmpeg required')
class WorkbenchVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    wait = fixture.MattingTests.wait

    def setUp(self):
        fixture.MattingTests.setUp(self)
        self.preview_ai = patch.object(render, 'Predictor', VideoPredictor); self.preview_ai.start(); self.addCleanup(self.preview_ai.stop)
        VideoPredictor.calls = VideoPredictor.sessions = 0
        self.frames = []
        for index in range(6):
            data = np.full((48, 64, 4), (0, 220, 0, 255), np.uint8)
            data[10:38, 8+index*4:28+index*4, :3] = (110+index*20, 80, 60)
            Image.fromarray(data).save(self.root/f'frame{index}.png'); self.frames.append(data)
        self.clip = self.make_video('5', 'moving.mov')

    def make_video(self, rate, name):
        path = self.root/name
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-v', 'error', '-framerate', rate,
            '-i', str(self.root/'frame%d.png'), '-frames:v', '6', '-c:v', 'png', '-pix_fmt', 'rgba', str(path)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace')); return path

    def dialog(self, source=None):
        dialog = MattingDialog([str(source or self.clip)], catalogue=self.catalogue, ffmpeg=FFMPEG)
        def cleanup():
            dialog.cancel_work(); self.idle(dialog); dialog.close(); dialog.deleteLater()
        self.addCleanup(cleanup); dialog.show(); self.idle(dialog); return dialog

    def idle(self, dialog):
        self.wait(lambda: dialog.worker is None and dialog.video_worker is None and not dialog.timer.isActive()
                  and not dialog.video_seek_timer.isActive() and dialog._pending is None and dialog._pending_video is None)

    def check_frame(self, dialog, index):
        self.assertAlmostEqual(dialog.video_position, index/dialog.video_info['fps'], places=6)
        np.testing.assert_array_equal(render.image_array(dialog.canvas.original), self.frames[index])

    def test_video_load_has_timeline_but_does_not_run_ai_until_requested(self):
        dialog = self.dialog()
        self.assertTrue(dialog.video_controls.isVisible()); self.assertTrue(dialog.video_play.isEnabled())
        self.assertEqual(dialog.video_info['frames'], 6); self.assertEqual(dialog.video_slider.maximum(), 1200)
        self.assertEqual(VideoPredictor.calls, 0); self.assertIsNone(dialog.base); self.check_frame(dialog, 0)

    def test_images_retain_static_preview_without_video_controls(self):
        dialog = self.dialog(self.source)
        self.assertTrue(dialog.video_controls.isHidden()); self.assertIsNone(dialog.video_info)
        dialog.controls['opacity'].setValue(50); self.idle(dialog)
        self.assertIsNotNone(dialog.base); self.assertIsNone(dialog.video_worker)

    def test_seek_step_and_mask_views_use_actual_target_frame(self):
        dialog = self.dialog(); dialog.seek_video(.6); self.idle(dialog); self.check_frame(dialog, 3)
        dialog.video_next.click(); self.idle(dialog); self.check_frame(dialog, 4)
        dialog.video_previous.click(); self.idle(dialog); self.check_frame(dialog, 3)
        dialog.view.setCurrentIndex(dialog.view.findData('mask'))
        self.assertEqual(dialog.canvas.mask.pixelColor(30, 20).red(), 255)
        self.assertEqual(dialog.canvas.mask.pixelColor(0, 0).red(), 0)
        dialog.view.setCurrentIndex(dialog.view.findData('overlay'))
        self.assertEqual(dialog.canvas.view, 'overlay')
        dialog.seek_video(999); self.idle(dialog); self.check_frame(dialog, 5)

    def test_play_pause_advances_without_persisting_and_reuses_one_predictor_per_run(self):
        dialog = self.dialog(); before = set(self.root.rglob('*'))
        dialog.video_play.click(); self.wait(lambda: dialog.video_position >= .4)
        dialog.video_play.click(); self.idle(dialog)
        self.assertFalse(dialog._video_playing); frozen = dialog.video_position
        self.assertEqual(VideoPredictor.sessions, 1)
        self.assertTrue(dialog.brush_buttons['remove'].isEnabled())
        self.check_frame(dialog, round(frozen*5))
        self.assertEqual(set(self.root.rglob('*')), before); self.assertFalse(dialog.history.past)
        dialog.video_play.click(); self.wait(lambda: dialog.video_position > frozen or not dialog._video_playing)
        dialog.pause_video(); self.idle(dialog)

    def test_parameter_edit_and_model_rebuild_stay_at_paused_frame(self):
        dialog = self.dialog(); dialog.seek_video(.6); self.idle(dialog); calls = VideoPredictor.calls
        dialog.controls['opacity'].setValue(25); self.idle(dialog)
        self.check_frame(dialog, 3); self.assertEqual(VideoPredictor.calls, calls)
        self.assertEqual(render.image_array(dialog.canvas.cutout)[:, :, 3].max(), 64)
        dialog.controls['quality'].setCurrentIndex(dialog.controls['quality'].findData('fast')); self.idle(dialog)
        self.check_frame(dialog, 3); self.assertEqual(dialog.base['quality'], 'fast')
        np.testing.assert_array_equal(render.image_array(dialog.canvas.cutout), render.refined(dialog.base, dialog.values))

    def test_original_playback_needs_no_ai_and_switch_back_builds_current_matte(self):
        dialog = self.dialog(); dialog.view.setCurrentIndex(dialog.view.findData('original'))
        with patch.object(render, 'Predictor', side_effect=AssertionError('Original playback must not initialize AI')):
            dialog.video_play.click(); self.wait(lambda: dialog.video_position >= .4)
            dialog.pause_video(); self.idle(dialog)
        position = dialog.video_position; self.assertIsNone(dialog.base); self.assertEqual(VideoPredictor.calls, 0)
        dialog.view.setCurrentIndex(dialog.view.findData('composite')); self.idle(dialog)
        self.assertAlmostEqual(dialog.video_position, position); self.assertIsNotNone(dialog.base)

    def test_save_after_seek_publishes_whole_source_parameters_without_video_file(self):
        dialog = self.dialog(); dialog.seek_video(.6); self.idle(dialog)
        dialog.controls['opacity'].setValue(35); self.idle(dialog)
        dialog.save_button.click(); self.idle(dialog)
        record = self.catalogue.lookup(self.clip)
        self.assertEqual(record['mode'], 'dynamic'); self.assertEqual(record['parameters']['opacity'], 35)
        self.assertNotIn('position', record['parameters']); self.assertNotIn('result', record)
        self.assertFalse((self.catalogue.root/'assets').exists())
        np.testing.assert_array_equal(render.load_source(self.clip, ffmpeg=FFMPEG)['source'], self.frames[0])

    def test_worker_applies_backpressure_and_releases_decoder_on_cancel(self):
        loaded = render.load_source(self.clip, ffmpeg=FFMPEG)
        request = dict(path=str(self.clip), values=normalize(dict(model='rvm')), info=loaded['video'], position=0.,
                       play=True, generation=1, expected=loaded['expected'], ffmpeg=FFMPEG, models=None)
        worker = MattingVideoWorker(request); frames = []; worker.frameReady.connect(frames.append)
        self.addCleanup(worker.stop)
        worker.start(); self.wait(lambda: len(frames) == 1)
        QtTest.QTest.qWait(300)
        self.assertEqual(len(frames), 1); self.assertEqual(VideoPredictor.calls, 1)
        worker.stop(); self.wait(lambda: not worker.isRunning())
        self.assertIsNone(worker._process); worker.deleteLater()

    def gate_predictor(self):
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        class Slow(VideoPredictor):
            def predict(self, rgb):
                entered.set()
                if not release.wait(5): raise RuntimeError('Test gate timeout')
                return super().predict(rgb)
        return Slow, entered, release

    def test_latest_seek_wins_and_old_frame_never_overwrites_new_target(self):
        dialog = self.dialog(); slow, entered, release = self.gate_predictor()
        with patch.object(render, 'Predictor', slow):
            dialog.seek_video(.2); self.wait(entered.is_set)
            dialog.seek_video(.8); self.assertIsNotNone(dialog._pending_video)
            release.set(); self.idle(dialog)
        self.check_frame(dialog, 4); self.assertEqual(dialog.base['position'], .8)

    def test_switching_to_image_cancels_video_and_drops_its_frame(self):
        dialog = self.dialog(); slow, entered, release = self.gate_predictor()
        with patch.object(render, 'Predictor', slow):
            dialog.seek_video(.2); self.wait(entered.is_set)
            dialog.add_sources([str(self.source)]); dialog.files.setCurrentRow(1)
            release.set(); self.idle(dialog)
        self.assertTrue(dialog.video_controls.isHidden()); self.assertEqual(dialog.video_position, 0)
        np.testing.assert_array_equal(render.image_array(dialog.canvas.original), self.pixels)

    def test_close_waits_for_its_video_thread_without_blocking_ui(self):
        dialog = self.dialog(); slow, entered, release = self.gate_predictor()
        with patch.object(render, 'Predictor', slow):
            dialog.seek_video(.2); self.wait(entered.is_set)
            dialog.close(); self.assertTrue(dialog._closing); self.assertTrue(dialog.isVisible())
            release.set(); self.idle(dialog); self.wait(lambda: not dialog.isVisible())

    def test_fractional_frame_rate_seek_in_loader_and_stream_matches(self):
        clip = self.make_video('24000/1001', 'fractional.mov')
        loaded = render.load_source(clip, ffmpeg=FFMPEG, position=5*1001/24000)
        self.assertEqual(loaded['video']['frames'], 6)
        np.testing.assert_array_equal(loaded['source'], self.frames[5])
        dialog = self.dialog(clip); dialog.seek_video(3*1001/24000); self.idle(dialog); self.check_frame(dialog, 3)
        dialog.seek_video(999); self.idle(dialog); self.check_frame(dialog, 5)

    def test_edit_during_play_pauses_and_refines_last_displayed_frame(self):
        dialog = self.dialog(); dialog.video_play.click(); self.wait(lambda: dialog.video_position >= .4)
        position = dialog.video_position; dialog.controls['opacity'].setValue(35); self.idle(dialog)
        self.assertFalse(dialog._video_playing); self.assertAlmostEqual(dialog.video_position, position)
        self.check_frame(dialog, round(position*5)); self.assertEqual(render.image_array(dialog.canvas.cutout)[:, :, 3].max(), 89)

    def test_original_to_mask_while_playing_changes_stream_and_keeps_playing(self):
        dialog = self.dialog(); dialog.view.setCurrentIndex(dialog.view.findData('original'))
        dialog.video_play.click(); self.wait(lambda: dialog.video_position >= .2)
        dialog.view.setCurrentIndex(dialog.view.findData('mask'))
        self.wait(lambda: dialog.base is not None and dialog.video_position >= .4)
        self.assertFalse(dialog.canvas.mask.isNull()); self.assertEqual(dialog.canvas.view, 'mask')
        dialog.pause_video(); self.idle(dialog)

    def test_missing_source_failure_is_visible_and_releases_worker(self):
        dialog = self.dialog(); self.clip.unlink()
        dialog.video_play.click(); self.idle(dialog)
        self.assertIn('视频预览失败', dialog.status.text()); self.assertFalse(dialog._video_playing)

    def test_cancel_clears_queued_seek_and_does_not_restart(self):
        dialog = self.dialog(); slow, entered, release = self.gate_predictor()
        with patch.object(render, 'Predictor', slow):
            dialog.seek_video(.2); self.wait(entered.is_set)
            dialog.seek_video(.8); dialog.cancel_work(); release.set(); self.idle(dialog)
            QtTest.QTest.qWait(200)
        self.assertIsNone(dialog.video_worker); self.assertIsNone(dialog._pending_video); self.assertFalse(dialog._video_playing)

    def test_paused_video_brush_and_ctrl_z_keep_frame_position(self):
        dialog = self.dialog(); dialog.seek_video(.6); self.idle(dialog)
        dialog.brush_buttons['remove'].click(); rect = dialog.canvas.image_rect()
        point = QtCore.QPointF(rect.left()+30/64*rect.width(), rect.top()+24/48*rect.height()).toPoint()
        QtTest.QTest.mouseClick(dialog.canvas, QtCore.Qt.MouseButton.LeftButton, pos=point); self.idle(dialog)
        self.assertEqual(len(dialog.values['strokes']), 1); self.assertEqual(dialog.canvas.cutout.pixelColor(30, 24).alpha(), 0)
        QtTest.QTest.keyClick(dialog.canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.idle(dialog); self.assertFalse(dialog.values['strokes']); self.check_frame(dialog, 3)
        self.assertEqual(dialog.canvas.cutout.pixelColor(30, 24).alpha(), 255)
        screenshot = os.environ.get('LZX_MATTING_VIDEO_SCREENSHOT')
        if screenshot: self.assertTrue(dialog.grab().save(screenshot))

    def test_hiding_studio_stops_background_video_and_reopen_does_not_autoplay(self):
        dialog = self.dialog(); dialog.video_play.click(); self.wait(lambda: dialog.video_position >= .2)
        dialog.hide(); self.idle(dialog); self.assertFalse(dialog._video_playing)
        dialog.show(); QtTest.QTest.qWait(100); self.assertIsNone(dialog.video_worker)


import test_batch_review_regressions as host_fixture


@unittest.skipUnless(Path(FFMPEG).is_file(), 'Explicit FFmpeg required')
class WorkbenchVideoHostTests(unittest.TestCase):
    setUp = host_fixture.BatchReviewRegressions.setUp
    tearDown = host_fixture.BatchReviewRegressions.tearDown
    wait = host_fixture.BatchReviewRegressions.wait
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def studio(self):
        path = Path(self.temp.name)/'clip.mp4'
        subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x48:r=5:d=1',
                        '-c:v', 'libx264', str(path)], check=True, capture_output=True, timeout=30)
        self.dialog.ffmpeg.setText(FFMPEG); self.dialog.edit_timer.stop()
        studio = self.dialog.open_matting([str(path)]); self.wait(lambda: studio.worker is None)
        return studio

    def test_host_waits_for_running_video_and_cancels_only_its_preview(self):
        studio = self.studio(); entered, release = threading.Event(), threading.Event()
        class Slow(VideoPredictor):
            def predict(self, rgb):
                entered.set(); release.wait(5); return super().predict(rgb)
        with patch.object(render, 'Predictor', Slow):
            try:
                studio.seek_video(.2); self.wait(entered.is_set)
                self.assertTrue(self.dialog.has_local_work())
                self.assertFalse(self.plugin.can_close()[0]); self.assertTrue(studio.video_worker.cancel.is_set())
            finally:
                release.set(); self.wait(lambda: studio.video_worker is None)
        self.assertTrue(self.plugin.can_close()[0]); studio.close()

    def test_host_cancels_debounced_video_seek_before_shutdown(self):
        studio = self.studio(); studio.video_slider.setValue(400)
        self.assertTrue(studio.video_seek_timer.isActive()); self.assertFalse(self.plugin.can_close()[0])
        QtTest.QTest.qWait(200)
        self.assertFalse(studio.video_seek_timer.isActive()); self.assertIsNone(studio.video_worker)
        self.assertTrue(self.plugin.can_close()[0]); studio.close()

"""Qt6-native preview: real presented frames, resize, seeks and range lifecycle."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets, QtMultimedia
from model.MediaPlayback import QtMediaReviewSurface, FfplayReviewSurface

FFMPEG = os.environ.get("NODE_FFMPEG", "")


class NativePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.surface = QtMediaReviewSurface(preload_enabled=False)
        self.backend = self.surface.backend

    def tearDown(self):
        self.surface.close()
        self.surface.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def frame(self, seconds, color="red"):
        image = QtGui.QImage(160, 90, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(color))
        frame = QtMultimedia.QVideoFrame(image)
        frame.setStartTime(round(seconds * 1000000))
        return frame

    def ready_for_frames(self, start=1, end=8, target=4):
        self.backend.source = self.backend._sources[0] = "synthetic.mp4"
        self.backend.range_start, self.backend.range_end = start, end
        self.backend.position = target - start
        self.backend._target_positions[0] = target * 1000
        self.backend._seek_pending[0] = True
        self.backend._stopped = False

    def test_legacy_name_is_one_native_implementation_without_external_process(self):
        self.assertIs(FfplayReviewSurface, QtMediaReviewSurface)
        self.assertEqual(self.surface._backend_kind, "qt")
        self.assertIsInstance(self.backend.players[0], QtMultimedia.QMediaPlayer)
        self.assertFalse(self.surface.findChildren(QtCore.QProcess))

    def test_old_position_and_old_frame_cannot_overwrite_requested_seek(self):
        self.ready_for_frames()
        changes = []
        self.surface.positionChanged.connect(changes.append)
        self.backend._position_changed(0, 1000)
        self.backend._frame_probed(0, self.frame(1))
        self.assertEqual(self.backend.position, 3)
        self.assertEqual(changes, [])
        self.backend._frame_probed(0, self.frame(4, "blue"))
        self.assertFalse(self.backend._seek_pending[0])
        self.assertEqual(changes, [3])
        self.assertIs(self.backend.stack.currentWidget(), self.backend.video_widgets[0])

    def test_cursor_follows_presented_frame_instead_of_player_seek_signal(self):
        self.ready_for_frames()
        self.backend._frame_probed(0, self.frame(4))
        with patch.object(self.backend.players[0], "hasVideo", return_value=True):
            self.backend._position_changed(0, 6000)
        self.assertEqual(self.backend.position, 3)
        self.backend._frame_probed(0, self.frame(4.1))
        self.assertAlmostEqual(self.backend.position, 3.1)

    def test_range_finish_callback_is_canceled_by_new_seek(self):
        self.ready_for_frames()
        finished = []
        self.surface.rangeFinished.connect(lambda: finished.append(True))
        self.backend._finish_current_range(0)
        self.backend._display_generation += 1
        self.app.processEvents()
        self.assertEqual(finished, [])

    def test_stop_and_release_discard_late_frames_and_release_sources(self):
        self.ready_for_frames()
        self.backend.stop()
        self.backend._frame_probed(0, self.frame(4))
        self.assertIs(self.backend.stack.currentWidget(), self.backend.placeholder)
        self.surface.release()
        self.assertEqual(self.backend._sources, [""])
        self.assertEqual(self.backend._last_frames, [None])
        self.assertTrue(self.backend.players[0].source().isEmpty())

    def test_small_pane_ignores_source_size_hint_and_keeps_entire_aspect(self):
        self.surface.resize(150, 100)
        self.surface.show()
        self.ready_for_frames()
        self.backend._frame_probed(0, self.frame(4))
        self.app.processEvents()
        video = self.backend.video_widgets[0]
        self.assertEqual(video.sizePolicy().horizontalPolicy(), QtWidgets.QSizePolicy.Policy.Ignored)
        self.assertLessEqual(video.width(), 150)
        self.assertLessEqual(video.height(), 100)
        self.assertLessEqual(self.surface.minimumSizeHint().width(), 150)

    def test_decode_error_is_visible_and_never_starts_external_fallback(self):
        errors = []
        self.surface.errorOccurred.connect(errors.append)
        self.backend._play_requested = True
        self.backend._player_error(0)
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.surface.is_playing())
        self.assertEqual(self.surface._backend_kind, "qt")
        self.assertFalse(self.surface.findChildren(QtCore.QProcess))


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "Synthetic video generator not configured")
class RealNativePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for name, shape in (("portrait", "160x284"), ("landscape", "320x180")):
            args = [FFMPEG, "-v", "error", "-y"]
            for color in ("red", "green", "blue"):
                args += ["-f", "lavfi", "-i", f"color=c={color}:s={shape}:r=10:d=2"]
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                     "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=6[v]",
                     "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-g", "200", "-keyint_min", "200",
                     "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.root / (name + ".mp4"))]
            subprocess.run(args, check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run([FFMPEG, "-v", "error", "-y", "-display_rotation:v:0", "90", "-i", str(cls.root / "landscape.mp4"),
                        "-c", "copy", str(cls.root / "rotated.mp4")],
                       check=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls.temporary.cleanup()

    def setUp(self):
        self.surfaces = []

    def tearDown(self):
        for surface in self.surfaces:
            surface.release()
            surface.close()
            surface.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def surface(self, preload=False):
        surface = QtMediaReviewSurface(preload_enabled=preload)
        surface.resize(220, 300)
        surface.show()
        surface.errors = []
        surface.errorOccurred.connect(surface.errors.append)
        self.surfaces.append(surface)
        return surface

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate(), [(surface.errors, surface.backend.players[surface.backend._active].mediaStatus(),
            surface.backend.players[surface.backend._active].playbackState(), surface.backend._pending_positions,
            surface.backend._seek_pending, surface.backend.players[surface.backend._active].position(),
            surface.backend.video_probes[surface.backend._active].videoFrame().isValid()) for surface in self.surfaces])

    def seek(self, surface, source, absolute, start=0, end=6):
        surface.prepare(str(source), start, end, absolute - start)
        backend = surface.backend
        self.wait(lambda: not backend._seek_pending[backend._active]
                  and backend._last_frames[backend._active] is not None)
        frame = backend._last_frames[backend._active]
        self.assertAlmostEqual(frame.startTime() / 1000000, absolute, delta=.11)
        self.assertAlmostEqual(backend.position, absolute - start, delta=.11)
        return frame.toImage()

    def test_long_gop_midclip_pause_seek_and_rapid_gray_area_changes_show_correct_frame(self):
        surface = self.surface()
        source = self.root / "portrait.mp4"
        image = self.seek(surface, source, 4.5, start=1, end=5.5)
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertGreater(color.blue(), 220)
        self.assertLess(color.red(), 30)
        for target in (1.2, 4.2, 3.2, .7, 4.7):
            surface.prepare(str(source), 0, 6, target)
        self.wait(lambda: not surface.backend._seek_pending[0])
        self.assertAlmostEqual(surface.backend._last_frames[0].startTime() / 1000000, 4.7, delta=.11)
        self.assertFalse(surface.is_playing())

    def test_playback_cursor_follows_frames_and_reaches_range_end_once(self):
        surface = self.surface()
        self.seek(surface, self.root / "portrait.mp4", 1.4, start=1, end=2)
        finished, positions = [], []
        surface.rangeFinished.connect(lambda: finished.append(True))
        surface.positionChanged.connect(positions.append)
        surface.play()
        self.wait(lambda: len(finished) == 1)
        self.assertFalse(surface.is_playing())
        self.assertAlmostEqual(surface.backend.position, 1)
        self.assertTrue(all(0.35 <= pos <= 1 for pos in positions), positions)
        self.app.processEvents()
        self.assertEqual(len(finished), 1)

    def test_queued_next_file_and_reset_replace_native_sources(self):
        surface = self.surface(preload=True)
        self.seek(surface, self.root / "portrait.mp4", 1)
        surface.queue(str(self.root / "landscape.mp4"), 4, 5.5)
        self.wait(lambda: surface.backend._last_frames[1] is not None)
        image = self.seek(surface, self.root / "landscape.mp4", 4.5, start=4, end=5.5)
        self.assertGreater(image.width(), image.height())
        self.assertEqual(surface.backend._active, 1)
        surface.reset_source()
        self.assertTrue(all(player.source().isEmpty() for player in surface.backend.players))

    def test_stop_then_play_and_replay_after_range_end_seek_to_range_start(self):
        surface = self.surface()
        self.seek(surface, self.root / "portrait.mp4", 3.5, start=3, end=4)
        surface.stop()
        surface.play()
        self.wait(lambda: not surface.backend._seek_pending[0] and surface.backend.position > .1)
        self.assertFalse(surface.backend._stopped)
        self.assertGreaterEqual(surface.backend._last_frames[0].startTime(), 3000000)
        self.wait(lambda: surface.backend._finishing_range)
        surface.play()
        self.wait(lambda: not surface.backend._seek_pending[0] and .05 < surface.backend.position < .7)
        self.assertTrue(surface.is_playing())

    def test_portrait_and_landscape_frames_fit_small_pane_after_resizes(self):
        for name, bounds in (("portrait", (320, 140)), ("landscape", (140, 320))):
            with self.subTest(name=name):
                surface = self.surface()
                image = self.seek(surface, self.root / (name + ".mp4"), 3)
                # The synthetic border proves all source edges reached the sink.
                for point in ((2, 2), (image.width()-3, 2), (2, image.height()-3), (image.width()-3, image.height()-3)):
                    self.assertGreater(image.pixelColor(*point).red(), 225)
                for width, height in (bounds, (180, 120), bounds):
                    surface.resize(width, height)
                    self.app.processEvents()
                    video = surface.backend.video_widgets[surface.backend._active]
                    self.assertLessEqual(video.width(), width)
                    self.assertLessEqual(video.height(), height)
                    self.assertAlmostEqual(surface.backend.position, 3, delta=.11)
                    # Inspect the actual painted pane, not only the source or
                    # a requested scaling option. Every white border survives.
                    painted = video.grab().toImage()
                    white = [(x, y) for y in range(painted.height()) for x in range(painted.width())
                             if painted.pixelColor(x, y).red() > 225 and painted.pixelColor(x, y).blue() > 225]
                    self.assertTrue(white)
                    left, right = min(x for x, y in white), max(x for x, y in white)
                    top, bottom = min(y for x, y in white), max(y for x, y in white)
                    self.assertAlmostEqual((right-left+1)/(bottom-top+1), image.width()/image.height(), delta=.06)
                    for x, y in ((left+1, top+1), (right-1, top+1), (left+1, bottom-1), (right-1, bottom-1)):
                        self.assertGreater(painted.pixelColor(x, y).red(), 210)
                if os.environ.get("LZX_QT_PREVIEW_SCREENSHOT"):
                    surface.grab().save(os.environ["LZX_QT_PREVIEW_SCREENSHOT"] + "-" + name + ".png")

    def test_rotation_metadata_paints_complete_portrait_without_cropping(self):
        surface = self.surface()
        self.seek(surface, self.root / "rotated.mp4", 3)
        surface.resize(220, 180)
        self.app.processEvents()
        painted = surface.backend.video_widgets[0].grab().toImage()
        white = [(x, y) for y in range(painted.height()) for x in range(painted.width())
                 if painted.pixelColor(x, y).red() > 225 and painted.pixelColor(x, y).blue() > 225]
        self.assertTrue(white)
        left, right = min(x for x, y in white), max(x for x, y in white)
        top, bottom = min(y for x, y in white), max(y for x, y in white)
        self.assertAlmostEqual((right-left+1)/(bottom-top+1), 180/320, delta=.04)

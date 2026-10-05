import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.effects import blur_filter, blur_strength
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, PreviewWorker


class FakePlayer(QtWidgets.QFrame):
    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent)
        self.playing, self.released = False, False
        self.preparations = []

    def is_playing(self):
        return self.playing

    def prepare(self, source, start, end, position=0):
        self.preparations.append((source, start, end, position))
        self.positionChanged.emit(position)

    def play(self):
        self.playing = True
        self.playingChanged.emit(True)

    def pause(self):
        self.playing = False
        self.playingChanged.emit(False)

    def release(self):
        self.released = True
        self.pause()


class PlaybackBlurTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def state(self):
        state = fresh_state()
        state["settings"]["layers"] = componentize_layers(None, state["settings"])
        state["music"] = [{"id":"m", "path":"song.wav", "cursor":12}]
        state["next_music_id"] = "m"
        return state

    def test_blur_is_scaled_and_only_enabled_when_requested(self):
        self.assertEqual(blur_filter({}, 540), "")
        self.assertEqual(blur_filter({"background_blur":10}, 540), "gblur=sigma=5.000000:steps=2")
        self.assertEqual(blur_strength({"background_blur":4.5}), 4.5)
        for value in (True, -1, 41, float("nan"), float("inf"), None, "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                blur_strength({"background_blur":value})

    def test_new_setting_migrates_inactive_profiles_and_remains_independent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = store.save(fresh_state(),0)
            first = state["active_profile"]
            state = store.create_profile(state,"Old profile",False)
            second = state["active_profile"]
            state["settings"]["background_blur"] = 12
            state = store.save(state,state["revision"])
            state["profiles"][0]["data"]["settings"].pop("background_blur")
            store.path.write_text(json.dumps(state),"utf8")
            first_state = store.select_profile(store.load(),first)
            self.assertEqual(first_state["settings"]["background_blur"],0)
            second_state = store.select_profile(first_state,second)
            self.assertEqual(second_state["settings"]["background_blur"],12)

    def test_invalid_blur_does_not_overwrite_existing_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = store.save(fresh_state(),0)
            before = store.path.read_bytes()
            state["settings"]["background_blur"] = float("nan")
            with self.assertRaises(ValueError):
                store.save(state,state["revision"])
            self.assertEqual(store.path.read_bytes(),before)

    def test_first_frame_worker_uses_same_background_blur(self):
        results = []
        worker = PreviewWorker("video.mp4","",blur=8)
        worker.ready.connect(results.append)
        with patch("app_plugins.builtin.batch_text_video.ui.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                patch("app_plugins.builtin.batch_text_video.ui.probe",return_value={"video":{},"duration":2}), \
                patch("app_plugins.builtin.batch_text_video.ui.subprocess.run",return_value=SimpleNamespace(returncode=0,stdout=b"png")) as run:
            worker.run()
        args = run.call_args.args[0]
        self.assertIn("gblur=sigma=5.333333:steps=2",args[args.index("-vf")+1])
        self.assertEqual(results[0]["blur"],8)

    def test_still_preview_fits_canvas_before_blurring_so_cover_does_not_overblur(self):
        for fit in ("contain","cover"):
            worker = PreviewWorker("video.mp4","",blur=8,frame_settings={"aspect":"portrait","fit":fit})
            with patch("app_plugins.builtin.batch_text_video.ui.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                    patch("app_plugins.builtin.batch_text_video.ui.probe",return_value={"video":{"width":1920,"height":1080},"duration":2}), \
                    patch("app_plugins.builtin.batch_text_video.ui.subprocess.run",return_value=SimpleNamespace(returncode=0,stdout=b"png")) as run:
                worker.run()
            args = run.call_args.args[0]
            graph = args[args.index("-vf")+1]
            self.assertIn("scale=720:1280",graph)
            self.assertIn("crop=720:1280" if fit == "cover" else "pad=720:1280",graph)
            self.assertTrue(graph.endswith("gblur=sigma=5.333333:steps=2"))

    def test_cache_identity_tracks_effects_files_and_rotation_but_not_output_folder(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"video.mp4"
            path.write_bytes(b"video")
            job = {"path":str(path), "title":"Title", "body":"Body"}
            state = self.state()
            original = preview_key(job,state)
            other = copy.deepcopy(state)
            other["settings"].update(output_dir="elsewhere",overwrite=True,show_layer_bounds=False)
            self.assertEqual(preview_key(job,other),original)
            other["settings"]["background_blur"] = 8
            self.assertNotEqual(preview_key(job,other),original)
            other = copy.deepcopy(state)
            other["music"][0]["cursor"] += 1
            self.assertNotEqual(preview_key(job,other),original)
            self.assertNotEqual(preview_key({**job,"body":"Changed"},state),original)
            path.write_bytes(b"new video content")
            self.assertNotEqual(preview_key(job,state),original)

    def test_background_result_from_old_blur_setting_is_ignored(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.controls["background_blur"].setValue(8)
            dialog.preview.background = QtGui.QImage(2,2,QtGui.QImage.Format.Format_RGB32)
            dialog.preview.background.fill(QtGui.QColor("green"))
            before = dialog.preview.background.copy()
            with patch.object(dialog,"active_entry",return_value={"path":"video.mp4"}):
                dialog._background_ready({"path":"video.mp4","blur":0,"image":b"bad","media":None})
            self.assertEqual(dialog.preview.background,before)
            dialog.close()

    def test_preview_worker_does_not_save_state_or_advance_music_or_text(self):
        with tempfile.TemporaryDirectory() as root:
            state = self.state()
            job = {"path":"video.mp4", "name":"Real", "task_dir":"original", "title":"Title", "body":"Body"}
            before_state, before_job = copy.deepcopy(state), copy.deepcopy(job)
            worker = PlaybackWorker(job,state,root,"a"*64)
            results = []
            worker.ready.connect(results.append)
            def renderer(job, state, directory, *args, **kwargs):
                self.assertEqual(Path(job["task_dir"]),Path(root)/"playback-cache")
                self.assertTrue(kwargs["preview"])
                output = Path(job["task_dir"])/(job["name"]+".mp4")
                output.write_bytes(b"generated proxy")
                return output,{"id":"m","cursor":999}
            with patch("app_plugins.builtin.batch_text_video.playback.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                    patch("app_plugins.builtin.batch_text_video.playback.probe",return_value={"duration":2}), \
                    patch("app_plugins.builtin.batch_text_video.playback.render_static_text",side_effect=renderer), \
                    patch.object(Store,"save",side_effect=AssertionError("preview must never save ledger")):
                worker.run()
            self.assertNotIn("error",results[0])
            self.assertEqual(state,before_state)
            self.assertEqual(job,before_job)
            self.assertEqual(results[0]["duration"],2)

    def test_preview_uses_original_voice_folder_before_redirecting_proxy_output(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)/"task"
            folder.mkdir()
            voice = folder/"task_audio.wav"
            voice.write_bytes(b"fixture")
            state = self.state()
            state["settings"]["use_voice"] = True
            worker = PlaybackWorker({"path":"video.mp4","task_dir":str(folder)},state,root,"b"*64)
            renderer = Mock(side_effect=RuntimeError("stop after inspecting renderer call"))
            with patch("app_plugins.builtin.batch_text_video.playback.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                    patch("app_plugins.builtin.batch_text_video.playback.render_static_text",renderer):
                worker.run()
            self.assertEqual(renderer.call_args.args[0]["voice_path"],str(voice.resolve()))

    def test_cached_preview_is_reused_and_cleanup_only_removes_generated_files(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root)/"playback-cache"
            cache.mkdir()
            key = "c"*64
            for index in range(20):
                (cache/(f"{index:064x}"+".mp4")).write_bytes(b"fixture")
            target = cache/(key+".mp4")
            target.write_bytes(b"fixture")
            manual = cache/"do-not-delete.mp4"
            manual.write_bytes(b"fixture")
            worker = PlaybackWorker({"path":"video.mp4"},self.state(),root,key)
            results = []
            worker.ready.connect(results.append)
            with patch("app_plugins.builtin.batch_text_video.playback.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                    patch("app_plugins.builtin.batch_text_video.playback.probe",return_value={"duration":2}), \
                    patch("app_plugins.builtin.batch_text_video.playback.render_static_text") as renderer:
                worker.run()
            renderer.assert_not_called()
            self.assertNotIn("error",results[0])
            self.assertTrue(target.exists())
            self.assertTrue(manual.exists())
            self.assertEqual(len(list(cache.glob("*.mp4"))),13)

    def test_cancelled_worker_never_calls_renderer(self):
        with tempfile.TemporaryDirectory() as root:
            worker = PlaybackWorker({"path":"video.mp4"},self.state(),root,"d"*64)
            worker.cancel.set()
            results = []
            worker.ready.connect(results.append)
            with patch("app_plugins.builtin.batch_text_video.playback.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                    patch("app_plugins.builtin.batch_text_video.playback.render_static_text") as renderer:
                worker.run()
            renderer.assert_not_called()
            self.assertTrue(results[0]["cancelled"])

    def test_player_buttons_seek_and_return_to_editor(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            source = Path(root)/"video.mp4"
            source.write_bytes(b"fixture")
            store = Store(Path(root)/"data")
            state = fresh_state()
            state["jobs"] = [{"id":"j","path":str(source),"name":"Test","title":"Title","body":"Body","status":"待生成"}]
            store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            job = {**dialog.active_entry(),"title":dialog.title.toPlainText(),"body":dialog.body.toPlainText()}
            state = copy.deepcopy(dialog.state)
            state["settings"] = dialog.read_settings()
            key = preview_key(job,state)
            proxy = Path(root)/"proxy.mp4"
            proxy.write_bytes(b"fixture")
            dialog._playback_key = key
            with patch("app_plugins.builtin.smart_video_editor.player.FfplayReviewSurface",FakePlayer):
                dialog._playback_ready({"key":key,"path":str(proxy),"duration":10})
            player = dialog.playback_surface
            self.assertTrue(player.is_playing())
            self.assertEqual(dialog.preview_stack.currentWidget(),player)
            dialog.play_preview_button.click()
            self.assertFalse(player.is_playing())
            dialog.play_preview_button.click()
            self.assertTrue(player.is_playing())
            dialog.playback_slider.setValue(4200)
            dialog.seek_preview()
            self.assertEqual(player.preparations[-1][-1],4.2)
            self.assertTrue(player.is_playing())
            dialog.edit_preview_button.click()
            self.assertFalse(player.is_playing())
            self.assertEqual(dialog.preview_stack.currentWidget(),dialog.preview)
            dialog.play_preview_button.click()
            self.assertEqual(dialog.preview_stack.currentWidget(),player)
            self.assertTrue(player.is_playing())
            dialog.close()
            self.assertTrue(player.released)
            self.assertIsNone(dialog.playback_surface)

    def test_new_edits_invalidate_old_player_and_stale_ready_is_ignored(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog._playback_key = "old"
            dialog.playback_surface = FakePlayer(parent=dialog.preview_stack)
            dialog.preview_stack.addWidget(dialog.playback_surface)
            old = dialog.playback_surface
            dialog.controls["background_blur"].setValue(6)
            self.assertIsNone(dialog._playback_key)
            self.assertTrue(old.released)
            self.assertIsNone(dialog.playback_surface)
            dialog._playback_ready({"key":"old","error":"stale error"})
            self.assertNotIn("stale error",dialog.playback_status.text())
            dialog.close()

    def test_close_cancels_preparation_and_preserves_resource_lists(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            worker = Mock()
            dialog.playback_worker = worker
            before = copy.deepcopy(dialog.state["backgrounds"])
            self.assertTrue(dialog.is_busy())
            dialog.close()
            worker.cancel.set.assert_called()
            self.assertIsNone(dialog._playback_key)
            self.assertEqual(dialog.state["backgrounds"],before)
            dialog.playback_worker = None

    def test_batch_generation_waits_for_cancelled_preview_without_second_click(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            worker = Mock()
            dialog.playback_worker = worker
            dialog.start_batch(False)
            self.assertIs(dialog._batch_after_preview,False)
            worker.cancel.set.assert_called()
            with patch.object(dialog,"start_batch") as starter:
                dialog._playback_finished()
                self.app.processEvents()
                starter.assert_called_once_with(False)
            dialog.close()

    def test_closing_window_cancels_queued_batch_start(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.playback_worker = Mock()
            dialog.start_batch(False)
            dialog._playback_finished()
            dialog.close()
            with patch.object(dialog,"start_batch") as starter:
                self.app.processEvents()
                starter.assert_not_called()


if __name__ == "__main__":
    unittest.main()

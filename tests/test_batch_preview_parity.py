"""Canonical text layout and stable embedded-black-border removal."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import background_framing as framing
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.engine import probe, render_static_text
from app_plugins.builtin.batch_text_video.ui import PreviewWorker, BatchTextVideoDialog

FFMPEG = os.environ.get("NODE_FFMPEG", "")


class FramingTests(unittest.TestCase):
    def setUp(self):
        framing._MEMORY.clear()

    def test_stable_opposing_borders_use_outermost_content(self):
        self.assertEqual(framing.stable_crop([(256,568,32,0),(248,564,36,2)],320,568), (256,568,32,0))
        self.assertEqual(framing.stable_crop([(320,480,0,44)]*5,320,568), (320,480,0,44))

    def test_dark_one_sided_full_blank_and_excessive_crops_are_rejected(self):
        for samples in ([None],[(320,568,0,0)],[(256,568,0,0)],[(120,568,100,0)],
                        [(256,568,32,0),(320,568,0,0)],[(0,0,0,0)],[(322,568,0,0)]):
            with self.subTest(samples=samples):
                self.assertIsNone(framing.stable_crop(samples,320,568))

    def test_fit_policy_never_crops_contain_and_applies_crop_before_cover(self):
        settings = {"fit":"cover", "_background_crop":(256,568,32,0)}
        self.assertTrue(framing.framing_filter(1080,1920,settings).startswith("crop=256:568:32:0,scale="))
        self.assertNotIn("crop=",framing.framing_filter(1080,1920,{**settings,"fit":"contain"}))

    def test_detection_cache_avoids_rescans_and_invalidates_changed_source(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.mp4"; source.touch()
            media = {"duration":10,"video":{"width":320,"height":568}}
            with patch("app_plugins.builtin.batch_text_video.engine._run", return_value="crop=256:568:32:0") as run:
                self.assertEqual(framing.detect_background_crop(source,media,"ffmpeg",root),(256,568,32,0))
                self.assertEqual(run.call_count,5)
                framing._MEMORY.clear()
                self.assertEqual(framing.detect_background_crop(source,media,"ffmpeg",root),(256,568,32,0))
                self.assertEqual(run.call_count,5)
                source.write_bytes(b"changed")
                framing.detect_background_crop(source,media,"ffmpeg",root)
                self.assertEqual(run.call_count,10)

    def test_uncropped_media_only_needs_one_sample_and_setting_can_disable(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.mp4"; source.touch()
            media = {"duration":10,"video":{"width":320,"height":568}}
            with patch("app_plugins.builtin.batch_text_video.engine._run", return_value="crop=320:568:0:0") as run:
                self.assertIsNone(framing.detect_background_crop(source,media,"ffmpeg"))
                self.assertEqual(run.call_count,1)
                for settings in ({"fit":"contain"},{"fit":"cover","auto_crop_black_bars":False}):
                    self.assertNotIn("_background_crop",framing.prepare_background_settings(source,media,settings,"ffmpeg"))
                self.assertEqual(run.call_count,1)


class PreviewParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        dialogs = [w for w in self.app.topLevelWidgets() if isinstance(w,BatchTextVideoDialog)]
        for dialog in dialogs:
            dialog.cancel_work()
            for timer in dialog.findChildren(QtCore.QTimer): timer.stop()
        deadline = time.monotonic()+5
        while any(d.is_busy() for d in dialogs) and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.002)
        self.assertFalse(any(d.is_busy() for d in dialogs))
        for dialog in dialogs: dialog.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def test_ui_cover_toggle_is_immediate_and_crop_option_is_visible(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            check = dialog.controls["auto_crop_black_bars"]
            self.assertTrue(check.isChecked()); self.assertFalse(check.isEnabled())
            with patch.object(dialog,"update_preview") as update:
                dialog.controls["fit"].setCurrentIndex(dialog.controls["fit"].findData("cover"))
                self.assertTrue(check.isEnabled()); update.assert_called()
            dialog.close()

    def test_geometry_change_drops_stale_padded_poster_even_while_worker_busy(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=Store(root))
            dialog.active_entry = Mock(return_value={"path":"source.mp4"})
            dialog.active_voice = Mock(return_value="")
            dialog._preview_path = ("source.mp4","","","portrait","contain",True,0)
            dialog.preview.background = QtGui.QImage(180,320,QtGui.QImage.Format.Format_RGB32)
            dialog.preview.background.fill(QtGui.QColor("red"))
            dialog.preview_worker = SimpleNamespace()
            blocker = QtCore.QSignalBlocker(dialog.controls["fit"])
            dialog.controls["fit"].setCurrentIndex(dialog.controls["fit"].findData("cover")); del blocker
            dialog.request_background("source.mp4")
            self.assertTrue(dialog.preview.background.isNull())
            self.assertEqual(dialog._preview_pending[4],"cover")
            dialog.preview_worker = None; dialog._preview_pending = None
            dialog.close()

    def test_cache_key_tracks_crop_option(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        state = fresh_state(); job = {"path":"","title":"Title","body":"Body"}
        original = preview_key(job,state)
        state["settings"]["auto_crop_black_bars"] = False
        self.assertNotEqual(original,preview_key(job,state))

    def test_cancelled_poster_is_requeued_when_user_switches_back(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=Store(root))
            dialog.active_entry = Mock(return_value={"path":"source.mp4"})
            dialog.active_voice = Mock(return_value="")
            request = ("source.mp4","","","portrait","contain",True,0,1080,1920)
            dialog._preview_path = request
            dialog._preview_pending = ("source.mp4","","","portrait","cover",True,0)
            cancel = threading.Event(); cancel.set()
            dialog.preview_worker = SimpleNamespace(cancel=cancel)
            dialog.request_background("source.mp4")
            self.assertEqual(dialog._preview_pending,request)
            dialog.preview_worker = None; dialog._preview_pending = None; dialog.close()

    def test_cancelled_worker_does_not_start_media_processes(self):
        results = []; worker = PreviewWorker("source.mp4","")
        worker.cancel.set(); worker.ready.connect(results.append)
        with patch("app_plugins.builtin.batch_text_video.ui.resolve_tools") as resolve:
            worker.run(); resolve.assert_not_called()
        self.assertTrue(results[0]["cancelled"])


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),"real FFmpeg not configured")
class PreviewParityMediaTests(PreviewParityTests):
    def ffmpeg(self,args):
        result = subprocess.run([FFMPEG,"-nostdin","-hide_banner","-loglevel","error",*args],
            capture_output=True,timeout=90,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,0,result.stderr.decode("utf8","replace"))
        return result.stdout

    def source(self,root):
        path = Path(root)/"padded.mp4"
        self.ffmpeg(["-y","-f","lavfi","-i","color=c=0x427d91:s=256x568:r=10:d=1",
            "-vf","pad=320:568:32:0:color=black","-c:v","libx264","-pix_fmt","yuv420p",str(path)])
        return path

    def frame(self,path):
        raw = self.ffmpeg(["-i",str(path),"-vf","scale=540:960:flags=lanczos","-frames:v","1",
                            "-pix_fmt","rgb24","-f","rawvideo","pipe:1"])
        return np.frombuffer(raw,np.uint8).reshape(960,540,3)

    def test_actual_preview_and_export_layout_are_identical_and_black_borders_removed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.source(root); before = path.read_bytes()
            state = fresh_state(); state["settings"].update(ffmpeg_path=FFMPEG,fit="cover",music_enabled=False,darkness=0)
            layer = new_text_component()
            layer.update(id="parity",text="Neposúvaj ďalej, satan sa najviac bojí chvíle tvojho úprimného pokánia. "*3,
                font="Open Sans",font_min=35,font_max=67,width=84,height=50,outline_width=0)
            state["settings"]["layers"] = normalize_layers([layer]+[dict(item,enabled=False) for item in DEFAULT_LAYERS])
            job = {"id":"j","path":str(path),"title":"","body":"","task_dir":root,"name":"final"}
            state["jobs"] = [job]
            with patch("app_plugins.builtin.batch_text_video.engine.render_overlay",wraps=render_overlay) as layout:
                final,_ = render_static_text(job,state,root)
                played,_ = render_static_text({**job,"name":"preview"},state,root,preview=True)
                self.assertEqual([call.args[2:4] for call in layout.call_args_list],[(1080,1920),(1080,1920)])
                self.assertEqual(layout.call_args_list[0].args[4],layout.call_args_list[1].args[4])
            a,b = self.frame(final),self.frame(played)
            # Includes the entire text area: compression differs, layout doesn't.
            self.assertLess(np.abs(a.astype(float)-b).mean(),3)
            self.assertGreater(a[:,3,:].mean(),40); self.assertGreater(b[:,-4,:].mean(),40)
            self.assertEqual(path.read_bytes(),before)
            meta = probe(played,str(Path(FFMPEG).with_name("ffprobe.exe")))
            self.assertEqual((meta["video"]["width"],meta["video"]["height"]),(540,960))

    def test_actual_still_preview_cover_removes_embedded_bars_but_contain_preserves_them(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.source(root)
            for fit in ("cover","contain"):
                results = []
                worker = PreviewWorker(str(path),FFMPEG,frame_settings={"aspect":"portrait","fit":fit,"auto_crop_black_bars":True},cache_directory=root)
                worker.ready.connect(results.append); worker.run()
                self.assertNotIn("error",results[0])
                image = QtGui.QImage.fromData(results[0]["image"])
                edge = image.pixelColor(3,image.height()//2)
                if fit == "cover":
                    self.assertGreater(edge.blue(),70); self.assertIsNotNone(results[0]["source_crop"])
                else:
                    self.assertLess(edge.blue(),10); self.assertIsNone(results[0]["source_crop"])

    def test_crop_is_shared_by_fade_cycle_and_slow_interpolation(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.source(root)
            state = fresh_state()
            layer = new_text_component(); layer.update(id="text",text="Amen",outline_width=0)
            state["settings"].update(ffmpeg_path=FFMPEG,aspect="source",fit="cover",music_enabled=False,darkness=0,
                unvoiced_duration_mode="fixed",unvoiced_duration_seconds=2,transition_seconds=.2,
                layers=normalize_layers([layer]+[dict(item,enabled=False) for item in DEFAULT_LAYERS]))
            job = {"id":"j","path":str(path),"title":"","body":"","task_dir":root,"name":"fade"}
            state["jobs"] = [job]
            for strategy, interpolation in (("loop","repeat"),("slow","blend")):
                state["settings"].update(duration_strategy=strategy,slow_interpolation=interpolation)
                out,_ = render_static_text({**job,"name":strategy},state,root)
                self.assertGreater(self.frame(out)[:,3,:].mean(),40)
            state["settings"].update(duration_strategy="loop",slow_interpolation="repeat",auto_crop_black_bars=False)
            out,_ = render_static_text({**job,"name":"preserved"},state,root)
            self.assertLess(self.frame(out)[:,3,:].mean(),5)
            self.assertEqual(len(list((Path(root)/"video-cache").glob("*.mp4"))),2)

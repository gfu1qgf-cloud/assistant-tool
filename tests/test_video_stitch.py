import importlib.util
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "video_stitch_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "app_plugins" / "builtin" / "video_stitch")]
sys.modules[PACKAGE] = package


def load(name):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, Path(package.__path__[0]) / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


engine = load("engine")
plugin_module = load("plugin")


def media(path="test.mp4", width=160, height=90, duration=2, fps=25, audio=-1):
    return engine.Media(path, width, height, duration, Fraction(fps), 0, audio)


class StitchPlanTests(unittest.TestCase):
    def test_width_sum_height_max_and_frame_basis(self):
        plan = engine.make_plan(media(width=1080, height=1920, fps=30),
                                media(width=1920, height=1080, duration=3, fps=24))
        self.assertEqual((plan["width"], plan["height"]), (3000, 1920))
        self.assertEqual(plan["duration"], 2)
        self.assertEqual(plan["fps"], "30")
        self.assertFalse(plan["loop"])

    def test_right_basis_loops_left_without_changing_speed(self):
        plan = engine.make_plan(media(duration=1), media(duration=3, fps=24), basis=1)
        self.assertTrue(plan["loop"])
        self.assertEqual(plan["frames"], 72)
        command = engine.build_command("ffmpeg", [media(duration=1), media(duration=3, fps=24)], plan, "out.mp4")
        self.assertEqual(command.count("-stream_loop"), 1)
        self.assertLess(command.index("-stream_loop"), command.index("-i"))
        self.assertIn("fps=24", command[command.index("-filter_complex") + 1])
        self.assertNotIn("-shortest", command)

    def test_odd_size_can_keep_exact_dimensions(self):
        left, right = media(width=121, height=91), media(width=120, height=90)
        strict = engine.make_plan(left, right, compatible=False)
        self.assertEqual((strict["width"], strict["height"]), (241, 91))
        self.assertEqual(strict["pixel_format"], "yuv444p")
        compatible = engine.make_plan(left, right)
        self.assertEqual((compatible["width"], compatible["height"]), (242, 92))
        self.assertEqual(compatible["pixel_format"], "yuv420p")

    def test_audio_basis_mix_and_missing(self):
        left, right = media(audio=-1), media(audio=1)
        self.assertTrue(engine.make_plan(left, right)["missing_audio"])
        self.assertEqual(engine.make_plan(left, right, basis=1)["audio_indices"], [1])
        self.assertEqual(engine.make_plan(left, right, audio="mix")["audio_indices"], [1])
        self.assertEqual(engine.make_plan(left, right, audio="mute")["audio_indices"], [])
        plan = engine.make_plan(media(audio=1), right, audio="mix")
        graph = engine.build_command("ffmpeg", [media(audio=1), right], plan, "out.mp4")
        self.assertIn("amix=inputs=2", graph[graph.index("-filter_complex") + 1])

    def test_rotation_sar_duration_prefer_video_not_container(self):
        data = {"streams": [{"codec_type": "video", "index": 2, "width": 100, "height": 80,
                             "duration": "2", "sample_aspect_ratio": "2:1", "avg_frame_rate": "30000/1001",
                             "side_data_list": [{"rotation": -90}]},
                            {"codec_type": "audio", "index": 3}], "format": {"duration": "8"}}
        result = engine.media_from_data("test.mp4", data)
        self.assertEqual((result.width, result.height), (80, 200))
        self.assertEqual(result.duration, 2)
        self.assertEqual(result.fps, Fraction(30000, 1001))
        self.assertEqual((result.video_index, result.audio_index), (2, 3))

    def test_matroska_duration_and_cover_art(self):
        result = engine.media_from_data("test.mkv", {"streams": [
            {"codec_type": "video", "index": 0, "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "index": 1, "width": 160, "height": 90,
             "avg_frame_rate": "0/0", "r_frame_rate": "24/1", "tags": {"DURATION": "00:00:03.500"}},
        ]})
        self.assertEqual(result.video_index, 1)
        self.assertEqual(result.duration, 3.5)

    def test_invalid_metadata_and_parameters_fail_explicitly(self):
        for data in ({"streams": []}, {"streams": [{"codec_type": "video", "index": 0, "width": 160, "height": 90}]}):
            with self.assertRaises(ValueError):
                engine.media_from_data("bad.mp4", data)
        with self.assertRaises(ValueError):
            engine.make_plan(media(), media(), basis=2)

    def test_cancel_before_process_launch(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(engine.Canceled):
                engine.run_process(["ffmpeg"], event)
            popen.assert_not_called()


class StitchMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = engine.resolve_tools()
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.left = cls.root / "左 side.mp4"
        cls.right = cls.root / "右短.mp4"
        cls.longer = cls.root / "long.mkv"
        # Short clip changes blue->green, so checking a later frame proves looping, not freeze-frame padding.
        cls.make(cls.left, "color=red:s=160x90:r=25:d=2", audio=True)
        cls.make(cls.right, "color=blue:s=120x160:r=12:d=0.5[b];color=lime:s=120x160:r=12:d=0.5[g];[b][g]concat=n=2:v=1:a=0", complex_filter=True)
        cls.make(cls.longer, "color=yellow:s=100x60:r=30:d=3", audio=True)

    @classmethod
    def make(cls, output, source, audio=False, complex_filter=False, extra=()):
        args = [cls.ffmpeg, "-v", "error", "-y"]
        if complex_filter:
            args += ["-filter_complex", source]
        else:
            args += ["-f", "lavfi", "-i", source]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-shortest", "-c:a", "aac"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "2", *extra, str(output)]
        subprocess.run(args, check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def pixels(self, path, seconds):
        return engine.run_process([self.ffmpeg, "-v", "error", "-ss", str(seconds), "-i", str(path),
                                  "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])

    def test_real_repeat_padding_audio_progress_and_sources_untouched(self):
        before = [engine.signature(p) for p in (self.left, self.right)]
        output = self.root / "loop.mp4"
        progress = []
        result = engine.export_pair(self.left, self.right, output, progress=progress.append)
        info = engine.probe(output, self.ffprobe)
        self.assertEqual((info.width, info.height), (280, 160))
        self.assertAlmostEqual(info.duration, 2, delta=0.08)
        self.assertEqual(info.fps, 25)
        self.assertGreaterEqual(info.audio_index, 0)
        self.assertEqual(progress[-1], 100)
        self.assertTrue(result["plan"]["loop"])
        self.assertEqual(before, [engine.signature(p) for p in (self.left, self.right)])
        # Left is vertically centered: black above it, red in its middle.
        rgb = self.pixels(output, 0.2)
        def pixel(x, y):
            offset = (y * info.width + x) * 3
            return tuple(rgb[offset:offset + 3])
        self.assertTrue(max(pixel(80, 10)) < 15)
        self.assertGreater(pixel(80, 80)[0], 200)
        # At 1.2 seconds, the second cycle has returned to blue, not stayed green.
        rgb = self.pixels(output, 1.2)
        offset = (80 * info.width + 220) * 3
        color = tuple(rgb[offset:offset + 3])
        self.assertGreater(color[2], 180, color)
        self.assertLess(color[1], 50, color)

    def test_real_longer_video_is_trimmed_and_right_basis_works(self):
        output = self.root / "trim.mp4"
        engine.export_pair(self.longer, self.left, output, basis=1, audio="mix")
        info = engine.probe(output, self.ffprobe)
        self.assertEqual((info.width, info.height), (260, 90))
        self.assertAlmostEqual(info.duration, 2, delta=0.08)
        self.assertEqual(info.fps, 25)
        self.assertGreaterEqual(info.audio_index, 0)

    def test_real_no_audio_choice_does_not_shorten_video(self):
        output = self.root / "mute.mp4"
        engine.export_pair(self.left, self.right, output, audio="right")
        info = engine.probe(output, self.ffprobe)
        self.assertEqual(info.audio_index, -1)
        self.assertAlmostEqual(info.duration, 2, delta=0.08)

    def test_loop_uses_video_duration_even_if_audio_is_longer(self):
        longer_audio = self.root / "short-video-long-audio.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-i", str(self.right), "-f", "lavfi", "-i",
                        "sine=frequency=880:sample_rate=48000:duration=3", "-c:v", "copy", "-c:a", "aac",
                        str(longer_audio)], check=True, capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for audio in ("basis", "right", "mix"):
            with self.subTest(audio=audio):
                output = self.root / f"loop-long-audio-{audio}.mp4"
                engine.export_pair(self.left, longer_audio, output, audio=audio)
                info = engine.probe(output, self.ffprobe)
                rgb = self.pixels(output, 1.2)
                offset = (80 * info.width + 220) * 3
                color = tuple(rgb[offset:offset + 3])
                self.assertGreater(color[2], 180, color)
                self.assertLess(color[1], 50, color)

    def test_real_phone_rotation(self):
        rotated = self.root / "rotated.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-display_rotation", "90", "-i", str(self.left),
                        "-c", "copy", str(rotated)], check=True, capture_output=True,
                       timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = engine.probe(rotated, self.ffprobe)
        self.assertEqual((info.width, info.height), (90, 160))
        output = self.root / "rotation-output.mp4"
        engine.export_pair(rotated, self.right, output)
        result = engine.probe(output, self.ffprobe)
        self.assertEqual((result.width, result.height), (210, 160))

    def test_real_odd_size_strict_and_compatible(self):
        odd = self.root / "odd.mkv"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=purple:s=120x90:r=25:d=0.5,format=yuv444p,pad=121:91", "-c:v", "ffv1", str(odd)],
                       check=True, capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        measured = engine.probe(odd, self.ffprobe)
        self.assertEqual((measured.width, measured.height), (121, 91))
        for compatible in (False, True):
            output = self.root / f"odd-{compatible}.mp4"
            result = engine.export_pair(odd, self.right, output, compatible=compatible)
            info = engine.probe(output, self.ffprobe)
            width = measured.width + 120
            self.assertEqual(info.width, width + width % 2 if compatible else width)
            self.assertEqual((info.width, info.height), (result["plan"]["width"], result["plan"]["height"]))

    def test_existing_output_source_overwrite_and_cancel_protection(self):
        output = self.root / "existing.mp4"
        output.write_bytes(b"original-result")
        with self.assertRaises(FileExistsError):
            engine.export_pair(self.left, self.right, output)
        self.assertEqual(output.read_bytes(), b"original-result")
        with self.assertRaises(ValueError):
            engine.export_pair(self.left, self.right, self.left, overwrite=True)
        event = threading.Event()
        def cancel_after_metadata(_media, _plan):
            event.set()
        with self.assertRaises(engine.Canceled):
            engine.export_pair(self.left, self.right, output, overwrite=True, cancel=event, report=cancel_after_metadata)
        self.assertEqual(output.read_bytes(), b"original-result")
        self.assertFalse(list(self.root.glob(".video-stitch-*")))

    def test_encoding_failure_never_replaces_existing_file(self):
        output = self.root / "failure-existing.mp4"
        output.write_bytes(b"old")
        real = engine.run_process
        def fail_export(args, *rest, **kwargs):
            if "-filter_complex" in args:
                Path(args[-1]).write_bytes(b"partial")
                raise RuntimeError("encoder failure")
            return real(args, *rest, **kwargs)
        with mock.patch.object(engine, "run_process", side_effect=fail_export):
            with self.assertRaises(RuntimeError):
                engine.export_pair(self.left, self.right, output, overwrite=True)
        self.assertEqual(output.read_bytes(), b"old")
        self.assertFalse(list(self.root.glob(".video-stitch-*")))

    def test_thumbnail_has_correct_oriented_aspect(self):
        from qt_compat import QtGui
        source = engine.probe(self.right, self.ffprobe)
        image = QtGui.QImage.fromData(engine.thumbnail(source, self.ffmpeg))
        self.assertFalse(image.isNull())
        self.assertAlmostEqual(image.width() / image.height(), 120 / 160, delta=0.01)


class StitchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.ui = load("ui")

    def context(self):
        return types.SimpleNamespace(parent_widget=None, log=mock.Mock(), register_command=mock.Mock())

    def test_plugin_registers_tool_only_and_keeps_window(self):
        from app_plugins.api import TOOLS_MENU
        p = plugin_module.VideoStitchPlugin()
        context = self.context()
        p.register(context)
        command = context.register_command.call_args.args[0]
        self.assertEqual(command.locations, frozenset({TOOLS_MENU}))
        first = p.open_dialog()
        self.assertFalse(first.isModal())
        first.close()
        self.assertIs(p.open_dialog(), first)
        self.assertEqual(p.can_close(), (True, ""))
        p.stop()
        first.deleteLater()
        self.app.processEvents()

    def test_swap_preserves_chosen_baseline_and_audio_source(self):
        d = self.ui.VideoStitchDialog(self.context())
        d.paths[0].setText("left.mp4")
        d.paths[1].setText("right.mp4")
        d.audio.setCurrentIndex(1)
        d._swap()
        d.timer.stop()
        self.assertEqual([p.text() for p in d.paths], ["right.mp4", "left.mp4"])
        self.assertEqual(d.basis.currentIndex(), 1)
        self.assertEqual(d.audio.currentData(), "right")
        d.close()
        d.deleteLater()

    def test_worker_emits_gui_signals_not_messageboxes(self):
        from qt_compat import QtCore, QtWidgets
        worker = self.ui.StitchWorker({"left": "missing", "right": "missing", "basis": 0,
                                      "audio": "basis", "compatible": True, "output": "out.mp4"}, render=True)
        failures = []
        worker.failed.connect(lambda message, detail: failures.append((message, detail)))
        loop = QtCore.QEventLoop()
        worker.finished.connect(loop.quit)
        with mock.patch.object(QtWidgets.QMessageBox, "warning", side_effect=AssertionError("worker must not use Qt UI")), \
             self.assertLogs(self.ui.logger, level="ERROR"):
            worker.start()
            QtCore.QTimer.singleShot(10000, loop.quit)
            loop.exec()
        self.app.processEvents()
        self.assertFalse(worker.isRunning())
        self.assertEqual(len(failures), 1)
        self.assertIn("Traceback", failures[0][1])
        worker.deleteLater()

    def test_closing_busy_window_requests_cancel_without_wait(self):
        from qt_compat import QtGui
        d = self.ui.VideoStitchDialog(self.context())
        worker = mock.Mock()
        worker.cancel = threading.Event()
        d._worker = worker
        event = QtGui.QCloseEvent()
        d.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(worker.cancel.is_set())
        worker.wait.assert_not_called()
        self.assertTrue(d._close_after)
        d._finished()
        self.assertFalse(d.is_busy())
        self.assertFalse(d.isVisible())
        d.deleteLater()

    def test_dialog_reads_frames_exports_and_opens_correct_folder(self):
        from qt_compat import QtCore, QtGui
        ffmpeg, ffprobe = engine.resolve_tools()
        def wait_worker(dialog):
            worker = dialog._worker
            self.assertIsNotNone(worker)
            loop = QtCore.QEventLoop()
            worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(12000, loop.quit)
            loop.exec()
            self.app.processEvents()
            self.assertFalse(dialog.is_busy(), dialog.status.text())

        with tempfile.TemporaryDirectory() as root:
            left, right = Path(root) / "left.mp4", Path(root) / "right.mp4"
            for path, size in ((left, "160x90"), (right, "90x160")):
                subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                                f"testsrc2=s={size}:r=25:d=0.8", "-c:v", "libx264", "-threads", "2", str(path)],
                               check=True, capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            context = self.context()
            dialog = self.ui.VideoStitchDialog(context)
            dialog.paths[0].setText(str(left))
            dialog.paths[1].setText(str(right))
            dialog.timer.stop()
            dialog.show()
            dialog._inspect()
            wait_worker(dialog)
            self.assertFalse(dialog.frames[0].image.isNull())
            self.assertFalse(dialog.frames[1].image.isNull())
            self.assertIn("250 × 160", dialog.plan_label.text())
            self.assertTrue(dialog.export.isEnabled())
            dialog._export()
            self.assertFalse(dialog.paths[0].isEnabled())
            wait_worker(dialog)
            self.assertEqual(dialog.progress.value(), 100)
            self.assertTrue(Path(dialog._last_output).is_file())
            self.assertTrue(dialog.paths[0].isEnabled())
            self.assertTrue(dialog.export.isEnabled())
            self.assertFalse(dialog.cancel_button.isEnabled())
            info = engine.probe(dialog._last_output, ffprobe)
            self.assertEqual((info.width, info.height), (250, 160))
            with mock.patch.object(QtGui.QDesktopServices, "openUrl", return_value=True) as opener:
                dialog._open_folder()
                self.assertEqual(Path(opener.call_args.args[0].toLocalFile()), Path(root))
            self.assertTrue(any("视频拼接完成" in c.args[0] for c in context.log.call_args_list))
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_host_tools_menu_accepts_plugin_and_startup_wiring_is_present(self):
        import ast
        from qt_compat import QtWidgets
        from app_plugins.host import PluginHost
        window = QtWidgets.QWidget()
        window.appendLog = mock.Mock()
        host = PluginHost(window)
        plugin = host.install(plugin_module.VideoStitchPlugin())
        menu = QtWidgets.QMenu(window)
        host.attach_tools_menu(menu)
        self.assertIn("视频拼接…", [action.text() for action in menu.actions()])
        self.assertIs(host.plugin("video_stitch"), plugin)
        # Parse staged/installed startup source without starting unrelated network/model services.
        source = ROOT / "main_pyui.py" if (ROOT / "main_pyui.py").is_file() else ROOT / "PYUI" / "main_pyui.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "app_plugins.builtin"
                            and any(alias.name == "VideoStitchPlugin" for alias in node.names) for node in ast.walk(tree)))
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "install" and any(isinstance(arg, ast.Call)
                            and isinstance(arg.func, ast.Name) and arg.func.id == "VideoStitchPlugin" for arg in node.args)
                            for node in ast.walk(tree)))
        window.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()

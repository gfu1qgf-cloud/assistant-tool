import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_core_module(name):
    path = Path(__file__).resolve().parents[1] / "app_plugins" / "builtin" / "facebook_contact_sheet" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"contact_sheet_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


download = _load_core_module("download")
engine = _load_core_module("engine")
_package_name = "contact_sheet_test_package"
_package = types.ModuleType(_package_name)
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "app_plugins" / "builtin" / "facebook_contact_sheet")]
sys.modules[_package_name] = _package
sys.modules[f"{_package_name}.download"] = download
sys.modules[f"{_package_name}.engine"] = engine
_cache_spec = importlib.util.spec_from_file_location(
    f"{_package_name}.task_cache", Path(_package.__path__[0]) / "task_cache.py"
)
task_cache = importlib.util.module_from_spec(_cache_spec)
sys.modules[_cache_spec.name] = task_cache
_cache_spec.loader.exec_module(task_cache)
_batch_spec = importlib.util.spec_from_file_location(
    f"{_package_name}.batch", Path(_package.__path__[0]) / "batch.py"
)
batch = importlib.util.module_from_spec(_batch_spec)
sys.modules[_batch_spec.name] = batch
_batch_spec.loader.exec_module(batch)
download_facebook_video = download.download_facebook_video
is_facebook_video_url = download.is_facebook_video_url
Cut = engine.Cut
detect_cuts = engine.detect_cuts
render_contact_sheets = engine.render_contact_sheets


class FacebookLinkTests(unittest.TestCase):
    def test_accepts_facebook_and_rejects_lookalikes(self):
        self.assertTrue(is_facebook_video_url("https://www.facebook.com/reel/123"))
        self.assertTrue(is_facebook_video_url("https://fb.watch/abc/"))
        self.assertTrue(is_facebook_video_url("https://www.fb.watch/abc/"))
        self.assertFalse(is_facebook_video_url("https://facebook.com.evil.example/video"))
        self.assertFalse(is_facebook_video_url("file:///C:/secret"))
        self.assertFalse(is_facebook_video_url("https://user:password@facebook.com/video"))

    def test_bad_host_never_calls_extractor(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                download_facebook_video("https://example.com/video", folder)

    def test_extractor_downloads_into_cache_without_cookie_file(self):
        options_seen = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                options_seen.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                self_test.assertTrue(download)
                Path(options_seen["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
                return {"title": "测试视频"}

        self_test = self
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)}):
                path, title = download_facebook_video(
                    "https://www.facebook.com/reel/123", folder,
                    browser="chrome", profile="Profile 1",
                )
            self.assertEqual(title, "测试视频")
            self.assertEqual(path.name, "source.mp4")
            self.assertEqual(options_seen["cookiesfrombrowser"][:2], ("chrome", "Profile 1"))
            self.assertFalse(any(item.suffix == ".txt" for item in Path(folder).iterdir()))

    def test_task_reference_cache_uses_video_identity_and_valid_files(self):
        first = "https://www.facebook.com/reel/123456/?fbclid=one"
        second = "https://www.facebook.com/reel/123456/?fbclid=two"
        self.assertEqual(task_cache.reference_key(first), task_cache.reference_key(second))
        rich_text = f"参考视频\n{first}\n{second}\nhttps://drive.google.com/file/d/abc/view"
        self.assertEqual(task_cache.extract_facebook_references(rich_text), [first])
        with tempfile.TemporaryDirectory() as folder:
            target = task_cache.cache_directory(folder, first)
            self.assertEqual(task_cache.cached_sheets(target, second), [])
            target.mkdir(parents=True)
            sheet = target / "preview.jpg"
            sheet.write_bytes(b"jpeg-test")
            task_cache.remember_sheets(target, first, [sheet])
            self.assertEqual(task_cache.cached_sheets(target, second), [sheet])
            sheet.unlink()
            self.assertEqual(task_cache.cached_sheets(target, first), [])

    def test_batch_uses_all_selected_tasks_and_skips_valid_cache(self):
        first = "https://www.facebook.com/reel/101/"
        second = "https://www.facebook.com/reel/202/"
        with tempfile.TemporaryDirectory() as root:
            targets = [
                {"task": types.SimpleNamespace(task_reference_link=f"{first}\n{second}"),
                 "target_dir": str(Path(root) / "one"), "label": "一"},
                {"task": types.SimpleNamespace(task_reference_link=first),
                 "target_dir": str(Path(root) / "two"), "label": "二"},
            ]
            jobs = batch.unique_jobs(targets + targets[:1])
            self.assertEqual(len(jobs), 3)
            cached = jobs[0].folder
            cached.mkdir(parents=True)
            image = cached / "old.jpg"
            image.write_bytes(b"jpeg")
            task_cache.remember_sheets(cached, first, [image])

            calls = []

            def fake_download(url, destination, **_kwargs):
                calls.append(url)
                if url == second:
                    raise RuntimeError("暂时无法读取")
                video = Path(destination) / "source.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"video")
                return video, "标题"

            def fake_render(_video, _duration, _cuts, folder, **_kwargs):
                output = Path(folder) / "sheet.jpg"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"jpeg")
                return [output]

            with mock.patch.object(batch, "download_facebook_video", side_effect=fake_download), \
                 mock.patch.object(batch, "detect_cuts", return_value=(5.0, [])), \
                 mock.patch.object(batch, "render_contact_sheets", side_effect=fake_render):
                with self.assertLogs(batch.logger, level="ERROR"):
                    outcomes = batch.run_batch(jobs)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(outcomes), 3)
            self.assertTrue(outcomes[0].startswith("已缓存"))
            self.assertTrue(outcomes[1].startswith("失败"))
            self.assertTrue(outcomes[2].startswith("完成"))
            self.assertEqual(len(task_cache.cached_sheets(jobs[2].folder, first)), 1)

    def test_old_analysis_cache_is_rebuilt_without_deleting_old_image(self):
        url = "https://www.facebook.com/reel/2484678672046814/"
        with tempfile.TemporaryDirectory() as root:
            folder = task_cache.cache_directory(root, url)
            folder.mkdir(parents=True)
            image = folder / "old.jpg"
            image.write_bytes(b"old contact sheet")
            manifest = folder / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "key": task_cache.reference_key(url),
                                           "files": [image.name]}), encoding="utf-8")
            self.assertEqual(task_cache.cached_sheets(folder, url), [])
            self.assertTrue(image.is_file())
            task_cache.remember_sheets(folder, url, [image])
            self.assertEqual(task_cache.cached_sheets(folder, url), [image])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["analysis_version"] += 1
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(task_cache.cached_sheets(folder, url), [])


@unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV not available")
class ContactSheetVideoTests(unittest.TestCase):
    def test_opening_keeps_first_frame_even_if_later_frames_are_sharper(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "opening.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            opening = np.full((90, 160, 3), (0, 0, 230), dtype=np.uint8)
            later = np.full((90, 160, 3), (0, 230, 0), dtype=np.uint8)
            later[::4, ::4] = 255
            try:
                for frame in [opening] * 3 + [later] * 27:
                    writer.write(frame)
            finally:
                writer.release()
            capture = cv2.VideoCapture(str(video))
            try:
                frame = engine._representative_frame(capture, 0, 3, opening=True)
                self.assertGreater(float(frame[:, :, 2].mean()), 220)
                self.assertLess(float(frame[:, :, 1].mean()), 10)
            finally:
                capture.release()
            paths = render_contact_sheets(video, 3, [], folder)
            from PIL import Image
            with Image.open(paths[0]) as sheet:
                red, green, _blue = sheet.getpixel((169, 120))
                self.assertGreater(red, 200)
                self.assertLess(green, 30)

    def test_opening_skips_black_intro_but_keeps_first_real_picture(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "black-intro.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            frames = [np.zeros((90, 160, 3), dtype=np.uint8)] * 5
            frames += [np.full((90, 160, 3), (230, 0, 0), dtype=np.uint8)] * 4
            frames += [np.full((90, 160, 3), (0, 230, 0), dtype=np.uint8)] * 21
            try:
                for frame in frames:
                    writer.write(frame)
            finally:
                writer.release()
            capture = cv2.VideoCapture(str(video))
            try:
                frame = engine._representative_frame(capture, 0, 3, opening=True)
                self.assertGreater(float(frame[:, :, 0].mean()), 220)
                self.assertLess(float(frame[:, :, 1].mean()), 10)
            finally:
                capture.release()

    def test_opening_read_can_be_canceled(self):
        capture = mock.Mock()
        capture.get.return_value = 10
        with self.assertRaises(engine.AnalysisCanceled):
            engine._representative_frame(capture, 0, 3, opening=True, canceled=lambda: True)
        capture.read.assert_not_called()

    def test_four_similar_color_scenes_with_subtle_dissolves(self):
        import numpy as np

        frames = []
        previous = None
        expected = []
        for shade in (55, 115, 175, 235):
            frame = np.full((90, 160, 3), shade, dtype=np.uint8)
            if previous is not None:
                expected.append(len(frames) * .2 + .2)
                frames += [np.uint8(previous.astype(float) * (1-part) + frame.astype(float)*part)
                           for part in (1/3, 2/3)]
            frames += [frame] * 12
            previous = frame
        samples = [(index * .2, engine._descriptor(frame)) for index, frame in enumerate(frames)]
        cuts = engine.classify_cuts(samples)
        self.assertEqual(len(cuts), 3, cuts)
        for cut, seconds in zip(cuts, expected):
            self.assertAlmostEqual(cut.seconds, seconds, delta=.3)

    def test_moderate_abrupt_cut_is_not_lost_between_soft_and_hard_limits(self):
        import numpy as np

        frames = [np.full((90, 160, 3), shade, dtype=np.uint8)
                  for shade in ([100] * 12 + [160] * 12)]
        samples = [(index * .2, engine._descriptor(frame)) for index, frame in enumerate(frames)]
        cuts = engine.classify_cuts(samples)
        self.assertEqual(len(cuts), 1, cuts)
        self.assertEqual(cuts[0].kind, "硬切")
        self.assertAlmostEqual(cuts[0].seconds, 2.3)

    def test_continuous_motion_and_subtitle_changes_are_not_scene_changes(self):
        import cv2
        import numpy as np

        rng = np.random.default_rng(42)
        canvas = cv2.GaussianBlur(rng.integers(40, 210, (120, 460, 3), dtype=np.uint8), (9, 9), 0)
        frames = []
        for index in range(60):
            frame = canvas[:, index * 3:index * 3 + 160].copy()
            if index % 8 < 4:
                cv2.putText(frame, str(index // 8), (50, 110), cv2.FONT_HERSHEY_SIMPLEX,
                            .6, (255, 255, 255), 2)
            frames.append(frame)
        samples = [(index * .2, engine._descriptor(frame)) for index, frame in enumerate(frames)]
        self.assertEqual(engine.classify_cuts(samples), [])

    def test_brief_brightness_pulse_returns_to_same_scene(self):
        import numpy as np

        frames = [np.full((90, 160, 3), shade, dtype=np.uint8)
                  for shade in ([100] * 12 + [160] + [100] * 12)]
        samples = [(index * .2, engine._descriptor(frame)) for index, frame in enumerate(frames)]
        self.assertEqual(engine.classify_cuts(samples), [])

    def test_detects_short_cross_dissolve(self):
        import numpy as np

        first = np.full((90, 160, 3), (30, 40, 180), dtype=np.uint8)
        second = np.full((90, 160, 3), (180, 140, 30), dtype=np.uint8)
        frames = [first] * 6
        frames += [np.uint8(first.astype(float) * (1 - part) + second.astype(float) * part)
                   for part in (0.2, 0.4, 0.6, 0.8)]
        frames += [second] * 6
        samples = [(index * 0.2, engine._descriptor(frame)) for index, frame in enumerate(frames)]
        cuts = engine.classify_cuts(samples, sensitivity=5, min_scene_seconds=0.5)
        self.assertTrue(any(cut.kind == "叠化/渐变" and 1.1 <= cut.seconds <= 2.1 for cut in cuts), cuts)

    def test_detects_hard_cut_and_exports_sheet(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "sample.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            try:
                for color in ((0, 0, 255), (0, 255, 0), (255, 0, 0)):
                    frame = np.full((90, 160, 3), color, dtype=np.uint8)
                    for _ in range(15):
                        writer.write(frame)
            finally:
                writer.release()
            duration, cuts = detect_cuts(video, sensitivity=5, min_scene_seconds=0.5)
            self.assertAlmostEqual(duration, 4.5, delta=0.2)
            self.assertTrue(any(abs(cut.seconds - 1.5) < 0.4 for cut in cuts), cuts)
            self.assertTrue(any(abs(cut.seconds - 3.0) < 0.4 for cut in cuts), cuts)
            paths = render_contact_sheets(video, duration, cuts, folder, title="测试视频")
            self.assertEqual(len(paths), 1)
            self.assertGreater(paths[0].stat().st_size, 1000)

    def test_manual_cut_produces_two_panels(self):
        import cv2
        import numpy as np
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "static.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (100, 80))
            self.assertTrue(writer.isOpened())
            try:
                frame = np.full((80, 100, 3), 150, dtype=np.uint8)
                for _ in range(30):
                    writer.write(frame)
            finally:
                writer.release()
            paths = render_contact_sheets(video, 3.0, [Cut(1.5, "手动")], folder)
            with Image.open(paths[0]) as image:
                self.assertGreater(image.width, 600)
                self.assertGreater(image.height, 250)

    def test_long_static_shot_gets_labeled_overview_frames(self):
        cuts = engine.add_overview_frames([], 35, max_interval=15)
        self.assertEqual(len(cuts), 2)
        self.assertTrue(all(cut.kind == "定时补帧" for cut in cuts))


@unittest.skipUnless(importlib.util.find_spec("PyQt6"), "PyQt6 not available")
class ContactSheetUiTests(unittest.TestCase):
    @staticmethod
    def _ui_module():
        path = Path(_package.__path__[0]) / "ui.py"
        spec = importlib.util.spec_from_file_location(f"{_package_name}.ui", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _plugin_module():
        path = Path(_package.__path__[0]) / "plugin.py"
        spec = importlib.util.spec_from_file_location(f"{_package_name}.plugin", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_batch_window_lists_multiple_tasks_without_starting_network(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from qt_compat import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        path = Path(_package.__path__[0]) / "batch_ui.py"
        spec = importlib.util.spec_from_file_location(f"{_package_name}.batch_ui", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            dialog = module.BatchContactSheetDialog()
            jobs = [
                batch.BatchJob("任务 1", "https://www.facebook.com/reel/101/", folder),
                batch.BatchJob("任务 2", "https://www.facebook.com/reel/202/", folder),
            ]
            with mock.patch.object(module, "run_batch", side_effect=AssertionError("must wait for Start")):
                self.assertTrue(dialog.set_jobs(jobs))
                self.assertEqual(dialog.table.rowCount(), 2)
                self.assertFalse(dialog.is_busy())
            dialog.close()
        app.processEvents()

    def test_view_starts_background_generation_when_cache_missing(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from qt_compat import QtGui, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        plugin = self._plugin_module().FacebookContactSheetPlugin()
        with tempfile.TemporaryDirectory() as folder:
            plugin.context = types.SimpleNamespace(
                parent_widget=None,
                task_targets=lambda _rows: [{
                    "task": types.SimpleNamespace(task_reference_link="https://www.facebook.com/reel/789/"),
                    "target_dir": folder,
                    "label": "789",
                }],
                log=lambda _message: None,
            )
            dialog = mock.Mock()
            dialog.is_busy.return_value = False
            dialog.start_task_reference.return_value = True
            with mock.patch.object(QtWidgets.QMessageBox, "information") as notice, \
                 mock.patch.object(plugin, "_show_dialog", return_value=dialog), \
                 mock.patch.object(QtGui.QDesktopServices, "openUrl") as opener:
                self.assertIs(plugin.view_task_reference([0]), dialog)
                dialog.start_task_reference.assert_called_once_with(
                    "https://www.facebook.com/reel/789/",
                    task_cache.cache_directory(folder, "https://www.facebook.com/reel/789/"),
                )
                notice.assert_not_called()
                opener.assert_not_called()
        app.processEvents()

    def test_view_can_choose_uncached_reference_among_cached_ones(self):
        from qt_compat import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        plugin = self._plugin_module().FacebookContactSheetPlugin()
        first = "https://www.facebook.com/reel/101/"
        second = "https://www.facebook.com/reel/202/"
        with tempfile.TemporaryDirectory() as root:
            target = {"task": types.SimpleNamespace(task_reference_link=f"{first}\n{second}"),
                      "target_dir": root, "label": "任务"}
            plugin.context = types.SimpleNamespace(parent_widget=None, log=lambda _message: None,
                                                   task_targets=lambda _rows: [target])
            folder = task_cache.cache_directory(root, first)
            folder.mkdir(parents=True)
            image = folder / "old.jpg"
            image.write_bytes(b"jpeg")
            task_cache.remember_sheets(folder, first, [image])
            dialog = mock.Mock()
            dialog.is_busy.return_value = False
            dialog.start_task_reference.return_value = True

            def choose_missing(_parent, _title, _text, labels, *_args):
                self.assertEqual(len(labels), 2)
                self.assertIn("已缓存", labels[0])
                self.assertIn("自动生成", labels[1])
                return labels[1], True

            with mock.patch.object(QtWidgets.QInputDialog, "getItem", side_effect=choose_missing), \
                 mock.patch.object(plugin, "_show_dialog", return_value=dialog):
                self.assertIs(plugin.view_task_reference([0]), dialog)
                dialog.start_task_reference.assert_called_once_with(second, task_cache.cache_directory(root, second))
            dialog.reset_mock()
            with mock.patch.object(QtWidgets.QInputDialog, "getItem", return_value=("", False)), \
                 mock.patch.object(plugin, "_show_dialog") as show:
                self.assertIsNone(plugin.view_task_reference([0]))
                show.assert_not_called()
                dialog.start_task_reference.assert_not_called()
        app.processEvents()

    def test_view_does_not_replace_busy_generation_or_duplicate_batch(self):
        from qt_compat import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        plugin = self._plugin_module().FacebookContactSheetPlugin()
        with tempfile.TemporaryDirectory() as root:
            plugin.context = types.SimpleNamespace(
                parent_widget=None, log=mock.Mock(), task_targets=lambda _rows: [{
                    "task": types.SimpleNamespace(task_reference_link="https://www.facebook.com/reel/101/"),
                    "target_dir": root, "label": "任务",
                }],
            )
            # A QWidget parent is required by real Qt message boxes; these notices are mocked.
            dialog = mock.Mock()
            dialog.is_busy.return_value = True
            with mock.patch.object(QtWidgets.QMessageBox, "information") as notice, \
                 mock.patch.object(plugin, "_show_dialog", return_value=dialog):
                self.assertIsNone(plugin.view_task_reference([0]))
                notice.assert_called_once()
                dialog.start_task_reference.assert_not_called()
            plugin.batch_dialog = mock.Mock()
            plugin.batch_dialog.is_busy.return_value = True
            with mock.patch.object(QtWidgets.QMessageBox, "information") as notice, \
                 mock.patch.object(plugin, "_show_dialog") as show:
                self.assertIsNone(plugin.view_task_reference([0]))
                notice.assert_called_once()
                show.assert_not_called()
            plugin.context.log.assert_not_called()
        app.processEvents()

    def test_view_unsupported_reference_does_not_generate(self):
        from qt_compat import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        plugin = self._plugin_module().FacebookContactSheetPlugin()
        plugin.context = types.SimpleNamespace(parent_widget=None, log=mock.Mock(), task_targets=lambda _rows: [{
            "task": types.SimpleNamespace(task_reference_link="https://example.com/video"),
            "target_dir": "unused", "label": "任务",
        }])
        with mock.patch.object(QtWidgets.QMessageBox, "information") as notice, \
             mock.patch.object(plugin, "_show_dialog") as show:
            self.assertIsNone(plugin.view_task_reference([0]))
            notice.assert_called_once()
            show.assert_not_called()
        app.processEvents()

    def test_window_opens_offscreen(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from qt_compat import QtWidgets

        module = self._ui_module()
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        dialog = module.ContactSheetDialog()
        self.assertIn("Facebook", dialog.windowTitle())
        self.assertFalse(dialog.is_busy())
        dialog.close()
        dialog.dispose()
        app.processEvents()

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV not available")
    def test_local_video_worker_exports_without_blocking_ui(self):
        import cv2
        import numpy as np
        from qt_compat import QtCore, QtWidgets

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        module = self._ui_module()
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "sample.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            try:
                for color in ((0, 0, 255), (0, 255, 0)):
                    frame = np.full((90, 160, 3), color, dtype=np.uint8)
                    for _ in range(12):
                        writer.write(frame)
            finally:
                writer.release()
            dialog = module.ContactSheetDialog()
            dialog.source_edit.setText(str(video))
            dialog.output_edit.setText(folder)
            dialog._analyze()
            loop = QtCore.QEventLoop()
            dialog._worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(10000, loop.quit)
            loop.exec()
            app.processEvents()
            self.assertIsNotNone(dialog._result, dialog.status.text())
            self.assertTrue(Path(dialog._result["paths"][0]).is_file())
            dialog.sensitivity.setValue(8)
            dialog._analyze()
            again = QtCore.QEventLoop()
            dialog._worker.finished.connect(again.quit)
            QtCore.QTimer.singleShot(10000, again.quit)
            again.exec()
            app.processEvents()
            self.assertIsNotNone(dialog._result, dialog.status.text())
            dialog.close()
            dialog.dispose()

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV not available")
    def test_task_action_saves_cache_then_reuses_it_without_downloading(self):
        import cv2
        import numpy as np
        from qt_compat import QtCore, QtGui, QtWidgets

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        module = self._ui_module()
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        url = "https://www.facebook.com/reel/789123/"
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "sample.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            try:
                frame = np.full((90, 160, 3), (50, 120, 200), dtype=np.uint8)
                for _ in range(20):
                    writer.write(frame)
            finally:
                writer.release()
            folder = task_cache.cache_directory(Path(root) / "task", url)
            dialog = module.ContactSheetDialog()
            with mock.patch.object(module, "download_facebook_video", return_value=(video, "测试")) as downloader:
                with mock.patch.object(QtGui.QDesktopServices, "openUrl", return_value=True):
                    self.assertTrue(dialog.start_task_reference(url, folder))
                    loop = QtCore.QEventLoop()
                    dialog._worker.finished.connect(loop.quit)
                    QtCore.QTimer.singleShot(10000, loop.quit)
                    loop.exec()
                    app.processEvents()
            downloader.assert_called_once()
            sheets = task_cache.cached_sheets(folder, url)
            self.assertEqual(len(sheets), 1)
            self.assertTrue(sheets[0].is_file())
            dialog.close()
            dialog.dispose()

            plugin_path = Path(_package.__path__[0]) / "plugin.py"
            spec = importlib.util.spec_from_file_location(f"{_package_name}.plugin", plugin_path)
            plugin_module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = plugin_module
            spec.loader.exec_module(plugin_module)
            plugin = plugin_module.FacebookContactSheetPlugin()
            logs = []
            plugin.context = types.SimpleNamespace(
                parent_widget=None,
                task_targets=lambda _rows: [{
                    "task": types.SimpleNamespace(task_reference_link=url),
                    "target_dir": str(Path(root) / "task"),
                    "label": "789",
                }],
                log=logs.append,
            )
            with mock.patch.object(plugin, "_show_dialog", side_effect=AssertionError("must not redownload")):
                with mock.patch.object(QtGui.QDesktopServices, "openUrl", return_value=True) as opener:
                    self.assertEqual(plugin.view_task_reference([0]), sheets)
                    opener.assert_called_once()
            self.assertTrue(any("未访问 Facebook" in item for item in logs))

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV not available")
    def test_view_generates_real_sheet_then_reuses_cache(self):
        import cv2
        import numpy as np
        from qt_compat import QtCore, QtGui, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        module = self._ui_module()
        plugin = self._plugin_module().FacebookContactSheetPlugin()
        url = "https://www.facebook.com/reel/303/"
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "sample.avi"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            try:
                for color in ((30, 80, 160), (160, 80, 30)):
                    for _ in range(10):
                        writer.write(np.full((90, 160, 3), color, dtype=np.uint8))
            finally:
                writer.release()
            task_dir = Path(root) / "task"
            plugin.context = types.SimpleNamespace(parent_widget=None, log=lambda _message: None,
                                                   task_targets=lambda _rows: [{
                                                       "task": types.SimpleNamespace(task_reference_link=url),
                                                       "target_dir": str(task_dir), "label": "任务303",
                                                   }])
            with mock.patch.object(module, "download_facebook_video", return_value=(video, "测试")) as downloader, \
                 mock.patch.object(QtGui.QDesktopServices, "openUrl", return_value=True) as opener:
                dialog = plugin.view_task_reference([0])
                self.assertIs(dialog, plugin.dialog)
                self.assertTrue(dialog.is_busy())
                loop = QtCore.QEventLoop()
                dialog._worker.finished.connect(loop.quit)
                QtCore.QTimer.singleShot(10000, loop.quit)
                loop.exec()
                app.processEvents()
                self.assertFalse(dialog.is_busy())
                sheets = task_cache.cached_sheets(task_cache.cache_directory(task_dir, url), url)
                self.assertTrue(sheets, dialog.status.text())
                self.assertGreater(sheets[0].stat().st_size, 1000)
                opener.assert_called_once()
                self.assertEqual(Path(opener.call_args.args[0].toLocalFile()), sheets[0])
                self.assertEqual(plugin.view_task_reference([0]), sheets)
                self.assertEqual(opener.call_count, 2)
                downloader.assert_called_once()
            dialog.close()
            dialog.dispose()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()

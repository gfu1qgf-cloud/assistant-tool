import importlib.util
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


@unittest.skipUnless(importlib.util.find_spec("cv2"), "OpenCV not available")
class ContactSheetVideoTests(unittest.TestCase):
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

    def test_view_only_does_not_download_when_cache_missing(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from qt_compat import QtWidgets

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
            with mock.patch.object(QtWidgets.QMessageBox, "information") as notice:
                with mock.patch.object(plugin, "_show_dialog", side_effect=AssertionError("view must not generate")):
                    self.assertIsNone(plugin.view_task_reference([0]))
                    notice.assert_called_once()
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


if __name__ == "__main__":
    unittest.main()

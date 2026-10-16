import hashlib
import ast
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PIL import Image
from qt_compat import QtCore, QtWidgets, QtTest
from model.image_retrieval.encoder import ChineseImageEncoder
from model.video_retrieval.frames import sample_times, scene_times, video_duration, extract_frame, Canceled, run_process
from model.video_retrieval.index import VideoSearchIndex, discover_videos
from app_plugins.builtin.smart_search.plugin import SmartSearchPlugin
from app_plugins.builtin.smart_search.settings import normalize_video_settings, SmartSearchSettingsPage
from app_plugins.builtin.smart_search.video_ui import VideoSearchPanel, fitted_icon, QueryWorker
from model.BackgroundTasks import BackgroundTaskManager

ROOT = Path(r'C:\Users\RTV\pythonPro\辅助小工具')
FFMPEG = str(ROOT / 'runtime/ffmpeg/ffmpeg.exe')
FFPROBE = str(ROOT / 'runtime/ffmpeg/ffprobe.exe')


class Encoder:
    model_key = 'large336'; model_id = 'synthetic/colors'
    def images(self, paths):
        self.calls += len(paths)
        vectors = []
        for path in paths:
            with Image.open(path) as image:
                mean = np.asarray(image.resize((1, 1)), dtype=np.float32).reshape(-1)
                vectors.append(mean / np.linalg.norm(mean))
        return vectors
    def __init__(self): self.calls = 0
    def text(self, value): return np.array([0, 0, 1], dtype=np.float32)
    def model_is_cached(self): return True
    def release(self): pass


def synthetic_frame(path, seconds, ffmpeg, canceled):
    return Image.new('RGB', (200, 100), 'red' if seconds < 3 else 'blue')


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.library = self.root / 'library'; self.library.mkdir()
        self.video = self.library / 'one.mp4'; self.video.write_bytes(b'not-a-real-video')
        self.index = VideoSearchIndex(self.root / 'index'); self.encoder = Encoder()
        self.settings = normalize_video_settings({'detect_scenes': False, 'interval': 2})

    def tearDown(self): self.temp.cleanup()

    def sync(self, paths=None, cancel=lambda: False):
        with patch('model.video_retrieval.index.video_duration', return_value=6), \
             patch('model.video_retrieval.index.extract_frame', side_effect=synthetic_frame):
            return self.index.sync([str(self.video)] if paths is None else paths, self.encoder,
                                   self.settings, 'ffmpeg', 'ffprobe', canceled=cancel)

    def test_start_end_and_whole_video_coverage_even_under_cap(self):
        times = sample_times(3600, interval=.5, max_frames=8, cuts=[1, 20, 100, 200, 700, 2000])
        self.assertLessEqual(len(times), 8); self.assertEqual(times[0], 0)
        self.assertAlmostEqual(times[-1], 3599.9)
        self.assertGreater(max(times), 3500)
        self.assertIn(1.25, sample_times(10, cuts=[1, 5]))

    def test_chinese_library_paths_recursive_dedup_no_non_video_files(self):
        child = self.library / '子目录'; child.mkdir(); (child / '二.MOV').write_bytes(b'x')
        (child / 'a.jpg').write_bytes(b'x')
        self.assertEqual(len(discover_videos([self.library, child])), 2)

    def test_unavailable_root_fails_not_silently_empty(self):
        with self.assertRaises(FileNotFoundError): discover_videos([self.library, self.root / 'missing'])

    def test_incremental_update_no_reencoding_unchanged_and_source_untouched(self):
        digest = hashlib.sha256(self.video.read_bytes()).digest()
        first = self.sync(); calls = self.encoder.calls
        second = self.sync()
        self.assertEqual(first['updated'], 1); self.assertEqual(second['unchanged'], 1)
        self.assertEqual(self.encoder.calls, calls); self.assertEqual(hashlib.sha256(self.video.read_bytes()).digest(), digest)

    def test_best_frame_search_finds_tail_not_just_first_and_groups_videos(self):
        self.sync(); rows = self.index.search([0, 0, 1], self.encoder.model_id)
        self.assertEqual(len(rows), 1); self.assertGreaterEqual(rows[0]['seconds'], 3)
        self.assertGreater(rows[0]['score'], .98); self.assertGreater(len(rows[0]['hits']), 1)
        self.assertTrue(Path(rows[0]['thumbnail']).is_file())

    def test_changed_file_excluded_until_index_updated_and_deleted_pruned(self):
        self.sync(); self.video.write_bytes(b'changed')
        self.assertEqual(self.index.search([0, 0, 1], self.encoder.model_id), [])
        self.assertEqual(self.sync()['updated'], 1)
        self.video.unlink(); self.assertEqual(self.index.search([0, 0, 1], self.encoder.model_id), [])
        self.sync([]); self.assertEqual(self.index.count(), (0, 0))

    def test_cancel_does_not_prune_or_replace_previous_frames(self):
        self.sync(); counts = self.index.count(); self.video.write_bytes(b'changed')
        result = self.sync([], cancel=lambda: True)
        self.assertTrue(result['canceled']); self.assertEqual(self.index.count(), counts)

    def test_failed_new_decode_preserves_previous_records(self):
        self.sync(); counts = self.index.count(); self.video.write_bytes(b'changed')
        with patch('model.video_retrieval.index.video_duration', side_effect=ValueError('bad')):
            result = self.index.sync([str(self.video)], self.encoder, self.settings, 'f', 'p')
        self.assertEqual(len(result['errors']), 1); self.assertEqual(self.index.count(), counts)

    def test_indexes_models_and_thumbnails_do_not_overwrite_each_other(self):
        self.sync(); thumbnail = self.index.search([0, 0, 1], self.encoder.model_id)[0]['thumbnail']
        second = VideoSearchIndex(self.index.root, 'base'); second._cleanup_thumbnails()
        self.assertTrue(Path(thumbnail).is_file()); self.assertEqual(second.count(), (0, 0))

    def test_common_video_library_does_not_depend_on_plugins(self):
        import model.video_retrieval.index as module
        for file in Path(module.__file__).parent.glob('*.py'):
            for node in ast.walk(ast.parse(file.read_text('utf-8'))):
                names = [item.name for item in node.names] if isinstance(node, ast.Import) else \
                        [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                self.assertFalse(any(name.startswith('app_plugins') for name in names), file)

    def test_bad_features_and_wrong_dimensions_rejected(self):
        self.sync()
        for query in ([0, 0, 0], [float('nan'), 0, 1], [1, 0]):
            with self.assertRaises(ValueError): self.index.search(query, self.encoder.model_id)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_combined_registration_is_one_plugin_one_menu_one_settings(self):
        plugin = SmartSearchPlugin(); context = Mock()
        config = {'smart_image_search': {'model': 'large'},
                  'smart_music_search': {'library_root': 'D:/custom'}, 'other': 9}
        context.load_config.return_value = config; plugin.register(context)
        self.assertEqual(context.register_command.call_count, 1)
        self.assertEqual(context.register_settings_page.call_count, 1)
        self.assertEqual(context.register_command.call_args.args[0].title, '智能搜索…')
        saved = plugin.update_config(dict(config))
        self.assertEqual(saved['smart_image_search']['model'], 'large')
        self.assertEqual(saved['smart_music_search']['library_root'], 'D:/custom')
        self.assertEqual(saved['other'], 9); self.assertIsNone(plugin.image.dialog)
        self.assertIsNone(plugin.music.dialog); self.assertIsNone(plugin.video)

    def test_settings_round_trip_and_high_precision_default(self):
        page = SmartSearchSettingsPage(); page.load_config({'smart_image_search': {'model': 'large'},
            'smart_video_search': {'library_roots': ['D:/视频'], 'max_frames': 500}})
        value = page.update_config({'keep': True})
        self.assertEqual(value['smart_video_search']['model'], 'large336')
        self.assertEqual(value['smart_video_search']['max_frames'], 500)
        self.assertEqual(value['smart_video_search']['library_roots'], ['D:/视频'])
        self.assertEqual(value['smart_image_search']['model'], 'large'); self.assertTrue(value['keep'])
        page.widget.deleteLater()

    def test_invalid_settings_bounded_and_not_crashing(self):
        value = normalize_video_settings({'interval': 'nan', 'max_frames': 'bad',
            'result_limit': float('inf'), 'library_roots': 'wrong'})
        self.assertEqual(value['interval'], 3); self.assertEqual(value['max_frames'], 240)
        self.assertEqual(value['library_roots'], [])

    def test_disabled_legacy_plugins_stay_disabled_after_merge(self):
        from app_plugins.host import plugin_enabled_states
        self.assertFalse(plugin_enabled_states({'plugin_enabled': {
            'smart_image_search': False, 'smart_music_search': False}})['smart_search'])
        self.assertTrue(plugin_enabled_states({'plugin_enabled': {
            'smart_image_search': True, 'smart_music_search': False}})['smart_search'])
        self.assertFalse(plugin_enabled_states({'plugin_enabled': {
            'smart_search': False, 'smart_image_search': True}})['smart_search'])

    def test_merge_close_cancels_all_before_release(self):
        plugin = SmartSearchPlugin(); plugin.image.dialog = Mock(); plugin.music.dialog = Mock(); plugin.video = Mock()
        for page in (plugin.image.dialog, plugin.music.dialog, plugin.video): page.is_busy.return_value = True
        self.assertFalse(plugin.can_close()[0])
        for page in (plugin.image.dialog, plugin.music.dialog, plugin.video):
            page.cancel_work.assert_called_once(); page.shutdown.assert_not_called()
        with self.assertRaises(RuntimeError): plugin.stop()

    def test_video_paging_drag_copy_preview_timestamp_and_safe_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'v.mp4'; path.write_bytes(b'x')
            thumbnail = root / 'portrait.jpg'; Image.new('RGB', (100, 200), 'red').save(thumbnail)
            index = VideoSearchIndex(root / 'index'); encoder = Encoder()
            panel = VideoSearchPanel({'result_limit': 20}, Mock(), index=index, encoder=encoder, store=Mock())
            row = {'path': str(path), 'thumbnail': str(thumbnail), 'seconds': 4.5,
                   'duration': 10, 'score': .8, 'hits': [{'seconds': 4.5, 'score': .8}]}
            panel.show_results([dict(row) for _ in range(23)])
            self.assertEqual(panel.results.count(), 20); panel.append_page(); self.assertEqual(panel.results.count(), 23)
            mime = panel.results.mimeData([panel.results.item(0)])
            self.assertEqual(Path(mime.urls()[0].toLocalFile()), path)
            self.assertEqual(panel.results.defaultDropAction(), QtCore.Qt.DropAction.CopyAction)
            panel.results.setCurrentRow(0)
            with patch.object(panel.player, 'prepare') as prepare, patch.object(panel.player, 'play'), \
                 patch('app_plugins.builtin.smart_search.video_ui.resolve_tools', return_value=('f', 'p')):
                panel.preview_selected(False)
                self.assertEqual(prepare.call_args.args, (str(path), 0, 10, 4.5))
            icon = fitted_icon(thumbnail).pixmap(240, 160).toImage()
            self.assertEqual(icon.pixelColor(10, 80).alpha(), 0)
            self.assertGreater(icon.pixelColor(120, 80).red(), 200)
            panel.worker = Mock(); self.assertFalse(panel.shutdown()); panel.worker.requestInterruption.assert_called_once()
            panel.worker = None; self.assertTrue(panel.shutdown()); panel.deleteLater()

    def test_video_thread_release_only_after_native_join(self):
        with tempfile.TemporaryDirectory() as directory:
            panel = VideoSearchPanel({}, Mock(), index=VideoSearchIndex(directory), encoder=Encoder(), store=Mock())
            started, allow_finish = threading.Event(), threading.Event()
            def task(): started.set(); allow_finish.wait(3); return [], 0
            panel.worker = QueryWorker(task, panel)
            connect = __import__('model.QtThreadLifecycle', fromlist=['connect_thread_finished']).connect_thread_finished
            connect(panel.worker, panel, 'worker', panel._finished, pass_thread=True)
            panel.worker.start(); self.assertTrue(started.wait(1)); self.assertFalse(panel.shutdown())
            allow_finish.set()
            limit = time.monotonic() + 5
            while panel.worker is not None and time.monotonic() < limit: QtTest.QTest.qWait(10)
            self.assertIsNone(panel.worker); self.assertTrue(panel.shutdown()); panel.deleteLater()

    def test_tabs_lazy_embedded_and_window_hide_preserves_host_index_job(self):
        plugin = SmartSearchPlugin(); parent = QtWidgets.QWidget(); context = Mock()
        context.parent_widget = parent; context.load_config.return_value = {}
        plugin.register(context)
        dialog = plugin.open_dialog()
        self.assertIsNotNone(plugin.image.dialog); self.assertIsNone(plugin.music.dialog)
        self.assertFalse(plugin.image.dialog.isWindow())
        dialog.ensure_tab(1); self.assertIsNotNone(plugin.music.dialog)
        self.assertFalse(plugin.music.dialog.isWindow()); self.assertIsNone(plugin.video)
        dialog.ensure_tab(2); self.assertIsNotNone(plugin.video)
        plugin.video.worker = Mock(); dialog.close(); self.app.processEvents()
        plugin.video.worker.requestInterruption.assert_not_called()
        plugin.video.worker = None; plugin.stop()
        dialog.deleteLater(); parent.deleteLater()

    def test_video_index_submits_host_job_and_completion_updates_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Mock(); context.submit_background_task.return_value = Mock()
            index = VideoSearchIndex(Path(directory) / 'index')
            panel = VideoSearchPanel({'include_inventory': False, 'library_roots': [directory]},
                                     context, index=index, encoder=Encoder(), store=Mock())
            panel.update_index()
            args = context.submit_background_task.call_args.kwargs
            self.assertEqual(args['execution'], 'parallel')
            self.assertEqual(args['payload']['roots'], [directory])
            self.assertNotEqual(args['resources'], ())
            self.assertIs(args['runner'], __import__('app_plugins.builtin.smart_search.video_jobs', fromlist=['run_video_index_job']).run_video_index_job)
            panel._indexed({'updated': 2, 'unchanged': 1, 'removed': 0, 'errors': []})
            self.assertIn('更新 2', panel.status.text())
            panel.worker = None; panel.shutdown(); panel.deleteLater()

    def test_real_host_manager_continues_indexing_with_hidden_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); library = root / 'library'; library.mkdir()
            (library / 'video.mp4').write_bytes(b'synthetic')
            manager = BackgroundTaskManager(history_path=root / 'tasks.json')
            context = Mock()
            context.submit_background_task.side_effect = lambda **kwargs: manager.submit(owner='smart_search', **kwargs)
            encoder = Encoder(); index = VideoSearchIndex(root / 'index')
            panel = VideoSearchPanel({'include_inventory': False, 'library_roots': [str(library)],
                'detect_scenes': False}, context, index=index, encoder=encoder, store=Mock())
            with patch('app_plugins.builtin.smart_search.video_jobs.ChineseImageEncoder', return_value=encoder), \
                 patch('app_plugins.builtin.smart_search.video_jobs.resolve_tools', return_value=('f', 'p')), \
                 patch('model.video_retrieval.index.video_duration', return_value=6), \
                 patch('model.video_retrieval.index.extract_frame', side_effect=synthetic_frame):
                panel.update_index(); panel.hide()
                limit = time.monotonic() + 5
                while panel.worker is not None and time.monotonic() < limit: QtTest.QTest.qWait(10)
                self.assertIsNone(panel.worker)
                self.assertEqual(index.count()[0], 1)
                self.assertIn('更新 1', panel.status.text())
                self.assertFalse(manager.is_busy())
            panel.shutdown(); panel.deleteLater(); manager.deleteLater()


@unittest.skipUnless(Path(FFMPEG).is_file(), 'bundled FFmpeg needed')
class RealMediaTests(unittest.TestCase):
    def test_real_decode_and_scene_transition_tail_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / '开头红尾部蓝.mp4'
            subprocess.run([FFMPEG, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=160x90:d=2:r=10',
                '-f', 'lavfi', '-i', 'color=blue:s=160x90:d=2:r=10', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
                '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertAlmostEqual(video_duration(video, FFPROBE), 4, places=1)
            cuts = scene_times(video, FFMPEG); self.assertTrue(any(1.5 <= t <= 2.5 for t in cuts), cuts)
            settings = normalize_video_settings({'interval': 1, 'max_frames': 8})
            index = VideoSearchIndex(root / 'index'); encoder = Encoder()
            result = index.sync([str(video)], encoder, settings, FFMPEG, FFPROBE)
            self.assertEqual(result['errors'], []); self.assertEqual(result['updated'], 1)
            found = index.search([0, 0, 1], encoder.model_id)
            self.assertGreaterEqual(found[0]['seconds'], 2)
            self.assertGreater(found[0]['score'], .98)
            frame = extract_frame(video, 0, FFMPEG)
            self.assertGreater(np.asarray(frame)[:, :, 0].mean(), 200); frame.close()


if __name__ == '__main__': unittest.main()

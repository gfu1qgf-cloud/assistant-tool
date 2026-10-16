import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from app_plugins.builtin.smart_video_editor import engine
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog, SmartVideoExportResultDialog
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage


class SegmentExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {'task_id': '7', 'output_dir': str(self.root), 'clips': [
            {'file_name': 'second.mp4', 'source': 'second.mp4', 'export_order': 2, 'kept_ranges': [[0, 1]]},
            {'file_name': 'first.mp4', 'source': 'first.mp4', 'export_order': 1, 'kept_ranges': [[0, 1]]},
            {'file_name': 'excluded.mp4', 'source': 'excluded.mp4', 'export_order': 3, 'included': False},
        ]}
        self.settings = engine.normalize_smart_video_editor_settings({'export_mode': 'segments'})

    @staticmethod
    def fake_render(task, settings, ffmpeg, progress, cancelled, **kwargs):
        path = Path(task['output_dir']) / (kwargs['output_basename'] + '.mp4')
        path.write_text(task['clips'][0]['file_name'])
        return {'video': str(path)}

    def export(self, renderer=None, cancelled=None):
        with patch.object(engine, '_export_task', side_effect=renderer or self.fake_render):
            return engine._export_segmented_task(self.task, self.settings, 'ffmpeg', lambda _: None, cancelled or (lambda: False))

    def test_order_numbering_exclusions_and_manifest(self):
        r = self.export()
        folder = Path(r['video'])
        self.assertEqual(r['segment_count'], 2)
        self.assertEqual((folder/'001.mp4').read_text(), 'first.mp4')
        self.assertEqual((folder/'002.mp4').read_text(), 'second.mp4')
        self.assertFalse((folder/'003.mp4').exists())
        manifest = json.loads((folder/'分段清单.json').read_text())
        self.assertEqual(manifest['segments'][0]['source_name'], 'first.mp4')
        self.assertEqual(r['srt'], '')

    def test_segments_do_not_request_subtitles(self):
        with patch.object(engine, '_export_task', side_effect=self.fake_render) as render:
            engine._export_segmented_task(self.task, self.settings, 'ffmpeg', lambda _: None, lambda: False)
        for call in render.call_args_list:
            self.assertFalse(call.kwargs['generate_srt'])
            self.assertEqual(len(call.args[0]['clips']), 1)

    def test_version_skip_overwrite_do_not_mix_old_segments(self):
        first = self.export()
        second = self.export()
        self.assertNotEqual(first['video'], second['video'])
        self.settings['existing_output'] = 'skip'
        self.assertEqual(self.export()['status'], 'skipped')
        self.settings['existing_output'] = 'overwrite'
        self.task['clips'] = self.task['clips'][1:2]
        third = self.export()
        self.assertEqual(third['video'], first['video'])
        self.assertFalse((Path(first['video'])/'002.mp4').exists())
        self.assertEqual((Path(first['video'])/'001.mp4').read_text(), 'first.mp4')

    def test_user_added_files_are_never_removed_by_overwrite(self):
        r = self.export()
        folder = Path(r['video'])
        (folder/'important.txt').write_text('keep')
        self.settings['existing_output'] = 'overwrite'
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual((folder/'important.txt').read_text(), 'keep')
        self.assertEqual((folder/'001.mp4').read_text(), 'first.mp4')

    def test_failure_keeps_previous_complete_export(self):
        r = self.export()
        self.settings['existing_output'] = 'overwrite'
        calls = []
        def render(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError('synthetic failure')
            return self.fake_render(*args, **kwargs)
        with self.assertRaises(RuntimeError):
            self.export(render)
        self.assertEqual((Path(r['video'])/'002.mp4').read_text(), 'second.mp4')
        self.assertFalse(list(self.root.glob('smart_segments_*')))

    def test_cancel_publishes_no_partial_directory(self):
        calls = []
        def render(*args, **kwargs):
            calls.append(True)
            return self.fake_render(*args, **kwargs)
        with self.assertRaises(InterruptedError):
            self.export(render, lambda: bool(calls))
        self.assertFalse((self.root/'7_智能剪辑_分段').exists())
        self.assertFalse(list(self.root.glob('smart_segments_*')))

    def test_publish_failure_rolls_back_old_directory(self):
        r = self.export()
        self.settings['existing_output'] = 'overwrite'
        real_replace = os.replace
        def replace(source, target):
            if Path(source).name == 'ready':
                raise OSError('synthetic publish failure')
            return real_replace(source, target)
        with patch.object(engine.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError):
                self.export()
        self.assertEqual((Path(r['video'])/'001.mp4').read_text(), 'first.mp4')

    def test_segment_mode_uses_existing_review_validation(self):
        with patch.object(engine, 'validate_smart_video_bundle_for_export', side_effect=ValueError('blocked')):
            with self.assertRaises(ValueError):
                engine.export_smart_video_bundle({'tasks': [self.task]}, self.settings)

    def test_export_mode_default_unchanged_and_dispatch(self):
        self.assertEqual(engine.normalize_smart_video_editor_settings({})['export_mode'], 'joined')
        self.assertEqual(engine.normalize_smart_video_editor_settings({'export_mode': 'invalid'})['export_mode'], 'joined')
        with patch.object(engine, 'validate_smart_video_bundle_for_export'), patch.object(engine, '_resolve_ffmpeg', return_value='ffmpeg'), \
             patch.object(engine, '_export_task') as joined, patch.object(engine, '_export_segmented_task', return_value={'status': 'completed'}) as segmented:
            engine.export_smart_video_bundle({'tasks': [self.task]}, self.settings)
        joined.assert_not_called()
        segmented.assert_called_once()

    def test_split_export_worker_does_not_load_whisper(self):
        loader = Mock(side_effect=AssertionError('should not load'))
        thread = engine.SmartVideoEditorThread('export', settings=self.settings, bundle={'tasks': []}, model_loader=loader)
        with patch.object(engine, 'export_smart_video_bundle', return_value={}):
            thread.run()
        loader.assert_not_called()

    def test_real_ffmpeg_mixed_specs_keep_separate_durations_and_frame_shapes(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg unavailable')
        import cv2
        first, second = self.root/'red.mp4', self.root/'blue.mp4'
        for path, color, size, fps in ((first, 'red', '180x320', 30), (second, 'blue', '320x180', 25)):
            subprocess.run([ffmpeg, '-y', '-f', 'lavfi', '-i', f'color=c={color}:s={size}:r={fps}:d=1',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
                            '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(path)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        before = (first.read_bytes(), second.read_bytes())
        self.task['clips'] = [
            {'file_name': second.name, 'source': str(second), 'export_order': 2, 'kept_ranges': [[0, 0.5]]},
            {'file_name': first.name, 'source': str(first), 'export_order': 1, 'kept_ranges': [[0.2, 0.6], [0.8, 1.0]]},
        ]
        with patch.object(engine, '_generate_final_aligned_srt', side_effect=AssertionError('no subtitles')):
            r = engine._export_segmented_task(self.task, self.settings, ffmpeg, lambda _: None, lambda: False)
        for name, shape, duration in (('001.mp4', (180, 320), 0.6), ('002.mp4', (320, 180), 0.48)):
            cap = cv2.VideoCapture(str(Path(r['video'])/name))
            try:
                ok, frame = cap.read()
                self.assertTrue(ok)
                self.assertEqual((frame.shape[1], frame.shape[0]), shape)
                actual = cap.get(cv2.CAP_PROP_FRAME_COUNT)/cap.get(cv2.CAP_PROP_FPS)
                self.assertAlmostEqual(actual, duration, delta=0.04)
            finally:
                cap.release()
        self.assertEqual(before, (first.read_bytes(), second.read_bytes()))
        self.assertFalse(list(Path(r['video']).glob('*.srt')))


class SegmentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_review_export_choice_is_saved_in_bundle(self):
        d = SmartVideoReviewDialog({'settings': {}, 'tasks': [], 'summary': {}})
        try:
            self.assertEqual(d.export_mode_combo.currentData(), 'joined')
            d.export_mode_combo.setCurrentIndex(1)
            self.assertEqual(d.collect()['settings']['export_mode'], 'segments')
            self.assertIn('分段', d.export_button.text())
        finally:
            d.reject()

    def test_settings_round_trip(self):
        parent = QtWidgets.QWidget()
        page = SmartVideoEditorSettingsPage(parent)
        try:
            page.load_config({engine.SMART_VIDEO_EDITOR_CONFIG_KEY: {'export_mode': 'segments'}})
            self.assertEqual(page.settings()['export_mode'], 'segments')
        finally:
            parent.close()

    def test_result_opens_segment_folder(self):
        with tempfile.TemporaryDirectory() as root:
            d = SmartVideoExportResultDialog({'completed': [{'task_id': '7', 'status': 'completed',
                'export_mode': 'segments', 'segment_count': 2, 'video': root}]})
            try:
                tree = d.findChild(QtWidgets.QTreeWidget)
                row = tree.topLevelItem(0)
                self.assertEqual(row.text(1), '已生成 2 段')
                with patch('app_plugins.builtin.smart_video_editor.ui.QtGui.QDesktopServices.openUrl') as open_url:
                    d._open_item(row, 0)
                open_url.assert_called_once()
            finally:
                d.close()

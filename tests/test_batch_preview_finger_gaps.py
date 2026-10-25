"""Assigned-task trial navigation and source-guided screen gaps regressions."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtWidgets
import test_batch_pool_preview as pool_fixture
from app_plugins.builtin.batch_text_video.matting_settings import refine, normalize
from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
from app_plugins.builtin.batch_text_video.render_assignment import apply as assigned


class AssignedPoolPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()
    setUp = pool_fixture.PoolPreviewDialogTests.setUp
    tearDown = pool_fixture.PoolPreviewDialogTests.tearDown
    wait = pool_fixture.PoolPreviewDialogTests.wait
    setup_pool = pool_fixture.PoolPreviewDialogTests.setup_pool

    def job(self, items):
        return dict(id='bound', path='', title='', body='', name='assigned',
                    render_assignment=dict(version=1, sources={'one': dict(field='path', value=items[0]['path'])},
                                           background='', voice='', music=None))

    def color(self):
        image = self.dialog.preview.overlay
        self.assertFalse(image.isNull(), self.dialog._overlay_status)
        return image.pixelColor(image.width()//2, image.height()//2)

    def test_assigned_job_next_previous_and_reset_change_design_without_saving(self):
        items = self.setup_pool(); owner = self.dialog; job = self.job(items)
        before = copy.deepcopy(owner.state); saved_job = copy.deepcopy(job)
        with patch.object(owner, 'active_entry', return_value=job), patch.object(owner, 'persist', side_effect=AssertionError('trial saved')):
            owner.update_preview(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().red(), 240)
            owner.pool_preview.next.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertTrue(owner.pool_preview.choices)
            self.assertGreater(self.color().blue(), 240)
            owner.pool_preview.previous.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().red(), 240)
            owner.pool_preview.next.click(); self.wait(lambda: owner.overlay_worker is None)
            owner.pool_preview.reset.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().red(), 240)
        self.assertEqual(job, saved_job); self.assertEqual(owner.state, before)

    def test_playback_preserves_trial_through_engine_assignment_and_default_cursor(self):
        items = self.setup_pool(); owner = self.dialog; job = self.job(items)
        job['render_assignment']['sources']['one']['value'] = items[1]['path']
        before = copy.deepcopy(owner.state); saved_job = copy.deepcopy(job)
        with patch.object(owner, 'active_entry', return_value=job):
            owner.update_preview(); self.wait(lambda: owner.overlay_worker is None)
            self.assertIn('任务分配', owner.pool_preview.item_combo.currentText())
            owner.pool_preview.next.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertEqual(owner.pool_preview.item_combo.currentData(), '2')
            with patch.object(PlaybackWorker, 'start'): owner.play_preview()
            worker = owner.playback_worker
            self.assertIsNotNone(worker, owner.playback_status.text())
            try:
                effective_job, effective = assigned(worker.job, worker.state)
                self.assertEqual(effective['settings']['layers'][0]['path'], items[2]['path'])
                key = preview_key(effective_job, effective)
                default_job, default = assigned(job, {**before, 'settings': owner.read_settings()})
                self.assertNotEqual(key, preview_key(default_job, default))
            finally:
                worker.deleteLater(); owner.playback_worker = None
        self.assertEqual(job, saved_job); self.assertEqual(owner.state, before)

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_green_video_navigation_and_real_playback_follow_trial_but_export_stays_assigned(self):
        import test_chroma_edge_despill as helper
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        processor = helper.EdgeDespillTests(); items = self.setup_pool(); owner = self.dialog
        root = Path(self.temp.name)
        for index, color in enumerate(((220, 0, 0), (0, 0, 220), (220, 160, 0))):
            data = np.full((60, 80, 4), (0, 255, 0, 255), np.uint8)
            data[10:50, 12:68, :3] = color
            image = root/f'green{index}.png'; helper.picture(data).save(str(image))
            video = root/f'green{index}.mov'
            processor.ffmpeg(['-loop', '1', '-i', str(image), '-t', '0.2', '-r', '10', '-c:v', 'png', str(video)])
            items[index]['path'] = str(video)
        layers = owner.layer_panel.values()
        layers[0].update(key_method='normal', key_mode='green', key_similarity=.05, key_softness=.1,
                         key_despill=True, key_despill_mode='edge')
        owner.layer_panel.set_layers(layers); owner.layer_panel.select_layer_id('one'); owner.edit_timer.stop()
        job = self.job(items); background = root/'gray.mp4'
        processor.ffmpeg(['-f', 'lavfi', '-i', 'color=c=gray:s=320x200:r=10:d=0.2',
                          '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(background)])
        job['path'] = job['render_assignment']['background'] = str(background)
        saved = copy.deepcopy(job); before = copy.deepcopy(owner.state)
        with patch.object(owner, 'active_entry', return_value=job):
            owner.update_preview(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().red(), 200)
            owner.pool_preview.next.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().blue(), 200)
            state = {**owner.state, 'settings': owner.read_settings()}
            state['settings'].update(aspect='source', darkness=0, ffmpeg_path=os.environ['NODE_FFMPEG'],
                                     music_enabled=False, use_voice=False, output_dir=str(root/'out'))
            trial_job, trial = owner.pool_preview.prepare(job, state)
            key = preview_key(trial_job, trial)
            worker = PlaybackWorker(trial_job, trial, root/'private', key)
            ready = []; worker.ready.connect(ready.append); worker.run(); worker.deleteLater()
            self.assertNotIn('error', ready[0], ready[0].get('error'))
            formal, _ = render_static_text({**job, 'name': 'assigned'}, state, root/'private')
            for path, channel in ((ready[0]['path'], 2), (formal, 0)):
                raw = processor.ffmpeg(['-i', str(path), '-vf', 'scale=320:200', '-frames:v', '1',
                                        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
                frame = np.frombuffer(raw, np.uint8).reshape(200, 320, 3)
                self.assertGreater(frame[100, 160, channel], 200)
                self.assertLess(frame[100, 160, (channel+2)%3], 20)
                self.assertLess(int(frame[10, 10].max())-int(frame[10, 10].min()), 10)
            owner.pool_preview.previous.click(); self.wait(lambda: owner.overlay_worker is None)
            self.assertGreater(self.color().red(), 200)
        self.assertEqual(job, saved); self.assertEqual(owner.state, before)


def hand():
    rgb = np.full((64, 80, 3), (0, 220, 0), np.uint8)
    alpha = np.zeros((64, 80), np.float32)
    rgb[6:58, 8:72] = (190, 155, 120); alpha[6:58, 8:72] = 1
    # Tiny enclosed finger gap, plus an unrelated AI error inside skin.
    rgb[15:25, 22:25] = (0, 220, 0); alpha[15:25, 22:25] = .05
    alpha[35:42, 39:46] = .05
    # Open gap has an opaque green spill along its border.
    rgb[:25, 58:62] = (0, 220, 0); alpha[:25, 59:61] = 0
    return rgb, alpha, np.full(alpha.shape, 255, np.uint8)


class ScreenGapTests(unittest.TestCase):
    def test_fill_restores_skin_but_does_not_fill_green_finger_gap(self):
        rgb, alpha, native = hand()
        result = refine(rgb, alpha, native, dict(fill_holes=True, despill='green'), source_rgb=rgb)
        self.assertEqual(result[19, 23, 3], 0)
        self.assertEqual(result[38, 42, 3], 255)
        self.assertEqual(result[15, 58, 3], 0)
        self.assertEqual(result[12, 15, 3], 255)

    def test_blue_screen_symmetric_and_opaque_green_core_stays(self):
        rgb, alpha, native = hand(); rgb[35:42, 15:25] = (0, 220, 0)
        for channel in ('green', 'blue'):
            source = rgb if channel == 'green' else rgb[:, :, [0, 2, 1]].copy()
            result = refine(source, alpha, native, dict(fill_holes=True, despill=channel), source_rgb=source)
            self.assertEqual(result[19, 23, 3], 0)
            self.assertEqual(result[38, 20, 3], 255)

    def test_option_off_zero_strength_and_brush_override(self):
        rgb, alpha, native = hand()
        off = refine(rgb, alpha, native, dict(fill_holes=True, despill='green', screen_cleanup=False), source_rgb=rgb)
        self.assertEqual(off[19, 23, 3], 255)
        zero = refine(rgb, alpha, native, dict(fill_holes=True, despill='green', despill_amount=0), source_rgb=rgb)
        self.assertEqual(zero[19, 23, 3], 255)
        native[20, 23] = 0
        result = refine(rgb, alpha, native, dict(fill_holes=True, despill='green', strokes=[
            dict(mode='keep', radius=.02, points=[[23/79, 19/63]])]), source_rgb=rgb)
        self.assertEqual(result[19, 23, 3], 255); self.assertEqual(result[20, 23, 3], 0)

    def test_foreground_estimator_despilled_rgb_cannot_hide_source_screen(self):
        rgb, alpha, native = hand()
        estimated = rgb.copy(); estimated[15:25, 22:25] = 128
        result = refine(estimated, alpha, native, dict(fill_holes=True, despill='green'), source_rgb=rgb)
        self.assertEqual(result[19, 23, 3], 0)
        self.assertEqual(result[38, 42, 3], 255)

    def test_parameter_is_validated(self):
        with self.assertRaises(ValueError): normalize(dict(screen_cleanup=1))

    def test_control_setting_is_saved_and_used_by_inline_ai(self):
        from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.store import Store, fresh_state
        from app_plugins.builtin.batch_text_video import matting_assets as assets
        import test_chroma_edge_despill as helper
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'hand.png'; rgb, alpha, native = hand()
            helper.picture(np.dstack([rgb, native])).save(str(path))
            panel = VisualProperties()
            try:
                value = normalize_layers([dict(id='hand', kind='image', path=str(path),
                    key_method='ai', key_mode='green', key_despill=True)])[0]
                panel.set_layer(value)
                self.assertTrue(panel.controls['key_screen_cleanup'].isEnabled())
                panel.controls['key_screen_cleanup'].setChecked(False)
                state = fresh_state(); state['settings']['layers'] = [copy.deepcopy(panel.layer)]
                store = Store(Path(folder)/'isolated'); store.save(state, 0)
                saved = store.load()['settings']['layers'][0]
                self.assertFalse(saved['key_screen_cleanup'])
                with patch.object(assets.Catalogue, 'lookup', return_value=None): bound = assets.apply_saved(saved)
                self.assertFalse(bound['_matting_binding']['parameters']['screen_cleanup'])
            finally: panel.deleteLater(); app.processEvents()

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), 'Explicit FFmpeg required')
    def test_real_video_decode_uses_same_source_guided_mask_as_poster(self):
        import test_chroma_edge_despill as helper
        from app_plugins.builtin.batch_text_video import matting_assets as assets, matting_live as live
        rgb, alpha, native = hand(); p = normalize(dict(fill_holes=True, despill='green'))
        class Predictor:
            def __init__(self, *args, **kwargs): pass
            def predict(self, source): return source.copy(), alpha.copy()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); image = root/'hand.png'; helper.picture(np.dstack([rgb, native])).save(str(image))
            video = root/'hand.mov'; processor = helper.EdgeDespillTests()
            processor.ffmpeg(['-loop', '1', '-i', str(image), '-t', '0.2', '-r', '10', '-c:v', 'png', str(video)])
            binding = dict(id='hand', mode='dynamic', source=assets.fingerprint(video), parameters=p)
            with patch('app_plugins.builtin.batch_text_video.matting_render.Predictor', Predictor), \
                 patch('app_plugins.builtin.batch_text_video.matting_render.infer', return_value=dict(rgb=rgb, alpha=alpha, source=np.dstack([rgb, native]))):
                poster = live.poster(binding, QtCore.QSize(80, 64))
                feed = live.Feed(live.StreamInput(binding, .2, False, 10, os.environ['NODE_FFMPEG']))
                try: frames = list(feed.frames())
                finally: feed.close()
            self.assertEqual(len(frames), 2)
            expected = helper.pixels(poster)
            for frame in frames:
                actual = np.frombuffer(frame, np.uint8).reshape(64, 80, 4)
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(actual[19, 23, 3], 0); self.assertEqual(actual[38, 42, 3], 255)

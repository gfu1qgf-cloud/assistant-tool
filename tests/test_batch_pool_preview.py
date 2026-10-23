"""Pool trial choices affect previews, never persistence or success cursors."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.pool_preview import PoolPreview
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.playback import preview_key, PlaybackWorker


def layer(identifier='one', **values):
    result = dict(id=identifier, kind='image', name=identifier, source='pool', pool_id='pool',
                  path='unused.png', pool_name='测试池', sequence_cursor=0,
                  sequence_items=[dict(id=str(i), path=f'{i}.png', name=f'图片{i}') for i in range(3)])
    result.update(values)
    return normalize_layers([result])[0]


class PoolPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.widget = PoolPreview(); self.addCleanup(self.widget.deleteLater)

    def test_dropdown_previous_next_and_reset_do_not_mutate_sources(self):
        original = [layer(sequence_cursor=1)]; before = copy.deepcopy(original)
        signals = []; self.widget.selectionChanged.connect(lambda: signals.append(True))
        self.widget.sync(original, 'one')
        self.assertFalse(signals); self.assertIn('第2项', self.widget.item_combo.currentText())
        self.widget.next.click()
        selected = self.widget.apply({'layers': original})
        self.assertEqual(selected['layers'][0]['sequence_cursor'], 2)
        self.widget.next.click(); self.assertEqual(self.widget.apply({'layers': original})['layers'][0]['sequence_cursor'], 0)
        self.widget.previous.click(); self.assertEqual(self.widget.item_combo.currentData(), '2')
        self.assertEqual(original, before)
        self.widget.reset.click(); self.assertFalse(self.widget.choices)
        self.assertEqual(self.widget.apply({'layers': original}), {'layers': original})

    def test_choices_are_per_layer_and_stable_across_reorder(self):
        layers = [layer('one'), layer('two')]
        self.widget.sync(layers, 'one'); self.widget.item_combo.setCurrentIndex(3)
        self.widget.sync(layers, 'two'); self.widget.item_combo.setCurrentIndex(2)
        layers[0]['sequence_items'].reverse()
        self.widget.sync(layers, 'one')
        result = self.widget.apply({'layers': layers})['layers']
        resolved, _ = resolve_sources(result)
        self.assertEqual([item['path'] for item in resolved], ['2.png', '1.png'])
        self.assertEqual(self.widget.item_combo.currentData(), '2')

    def test_removed_item_changed_pool_and_empty_pool_clear_stale_choice(self):
        value = layer(); self.widget.sync([value]); self.widget.next.click()
        value['sequence_items'] = [value['sequence_items'][0]]
        self.widget.sync([value]); self.assertFalse(self.widget.choices)
        self.assertFalse(self.widget.next.isEnabled())
        value['sequence_items'] = []; self.widget.sync([value]); self.assertFalse(self.widget.item_combo.isEnabled())
        value = layer(); self.widget.sync([value]); self.widget.next.click()
        value['pool_id'] = 'other'; self.widget.sync([value]); self.assertFalse(self.widget.choices)
        value['source'] = 'fixed'; self.widget.sync([value]); self.assertTrue(self.widget.isHidden())

    def test_rebinding_and_resolving_a_trial_preserves_selected_id_and_real_cursor(self):
        state = fresh_state()
        state['resource_pools'] = [dict(id='pool', name='池', kind='visual', items=layer()['sequence_items'], next_id='0')]
        layers = bind_pool_layers([layer()], state)
        self.widget.sync(layers); self.widget.next.click()
        selected = self.widget.apply({'layers': layers})['layers']
        resolved, _ = resolve_sources(bind_pool_layers(selected, state))
        self.assertEqual(resolved[0]['path'], '1.png')
        self.assertEqual(bind_pool_layers(layers, state)[0]['sequence_cursor'], 0)
        self.assertEqual(state['resource_pools'][0]['next_id'], '0')

    def test_new_widget_has_no_saved_trial_selection(self):
        self.widget.sync([layer()]); self.widget.next.click()
        other = PoolPreview(); self.addCleanup(other.deleteLater); other.sync([layer()])
        self.assertFalse(other.choices); self.assertEqual(other.item_combo.currentIndex(), 0)

    def test_switching_projects_with_copied_layer_ids_clears_trial(self):
        self.widget.sync([layer()], scope='project-one'); self.widget.next.click()
        self.assertTrue(self.widget.choices)
        self.widget.sync([layer()], scope='project-two')
        self.assertFalse(self.widget.choices); self.assertEqual(self.widget.item_combo.currentIndex(), 0)


import test_batch_review_regressions as fixture


class PoolPreviewDialogTests(unittest.TestCase):
    setUp = fixture.BatchReviewRegressions.setUp
    tearDown = fixture.BatchReviewRegressions.tearDown
    wait = fixture.BatchReviewRegressions.wait

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def setup_pool(self):
        root = Path(self.temp.name)
        items = []
        for index, color in enumerate(('red', 'blue', '#40aa50')):
            image = QtGui.QImage(80, 60, QtGui.QImage.Format.Format_RGBA8888); image.fill(QtGui.QColor(color))
            path = root/f'picture{index}.png'; self.assertTrue(image.save(str(path)))
            items.append(dict(id=str(index), path=str(path), name=f'素材{index+1}'))
        state = fresh_state()
        state['resource_pools'] = [dict(id='pool', name='试看图片池', kind='visual', items=items, next_id='')]
        visual = layer(path=items[0]['path'], sequence_items=items, x=50, y=50, width=100, height=100)
        state['settings'].update(layers=[visual]+[dict(item, enabled=False) for item in DEFAULT_LAYERS], darkness=0)
        self.dialog.state = state
        self.dialog.layer_panel.set_pool_state(state)
        self.dialog.layer_panel.set_layers(state['settings']['layers'])
        self.dialog.layer_panel.select_layer_id('one')
        self.dialog.edit_timer.stop()
        self.dialog.update_preview(); self.wait(lambda: self.dialog.overlay_worker is None)
        return items

    def test_real_design_preview_changes_image_without_saving_or_advancing(self):
        self.setup_pool(); dialog = self.dialog
        source = dialog.layer_panel.content_editor
        self.assertIs(dialog.pool_preview.parentWidget(), source)
        self.assertIs(source.layout().itemAt(1).widget(), dialog.pool_preview)
        self.assertTrue(dialog.pool_preview.isVisibleTo(source))
        self.assertTrue(dialog.pool_preview.layer_combo.isHidden())
        before = copy.deepcopy(dialog.state)
        layers = dialog.layer_panel.values()
        initial = dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2)
        self.assertGreater(initial.red(), 240)
        with patch.object(dialog, 'persist', side_effect=AssertionError('trial must not save')):
            dialog.pool_preview.next.click(); self.wait(lambda: dialog.overlay_worker is None)
        color = dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2)
        self.assertGreater(color.blue(), 240)
        self.assertEqual(dialog.state, before)
        self.assertEqual(dialog.layer_panel.values(), layers)
        self.assertEqual(dialog.read_settings()['layers'][0]['sequence_cursor'], 0)
        dialog.pool_preview.reset.click(); self.wait(lambda: dialog.overlay_worker is None)
        color = dialog.preview.overlay.pixelColor(dialog.preview.overlay.width()//2, dialog.preview.overlay.height()//2)
        self.assertGreater(color.red(), 240)

    def test_chooser_follows_current_source_and_keeps_trial_when_selecting_other_controls(self):
        self.setup_pool(); dialog = self.dialog
        dialog.pool_preview.next.click(); self.wait(lambda: dialog.overlay_worker is None)
        selected = copy.deepcopy(dialog.pool_preview.choices)
        other = next(value for value in dialog.layer_panel.values() if value['kind'] != 'image')
        dialog.layer_panel.select_layer_id(other['id'])
        self.assertTrue(dialog.pool_preview.isHidden())
        self.assertEqual(dialog.pool_preview.choices, selected)
        dialog.layer_panel.select_layer_id('one')
        self.assertIs(dialog.pool_preview.parentWidget(), dialog.layer_panel.content_editor)
        self.assertFalse(dialog.pool_preview.isHidden())
        self.assertEqual(dialog.pool_preview.item_combo.currentData(), '1')
        dialog.layer_panel.content_editor.source.setCurrentIndex(
            dialog.layer_panel.content_editor.source.findData('fixed'))
        dialog.edit_timer.stop()
        self.assertTrue(dialog.pool_preview.isHidden())
        self.wait(lambda: dialog.overlay_worker is None)

    def test_playback_uses_trial_and_distinct_cache_key_without_committing(self):
        items = self.setup_pool(); dialog = self.dialog
        source = Path(self.temp.name)/'background.mp4'; source.write_bytes(b'not decoded by mocked worker')
        job = dict(id='job', path=str(source), title='', body='', name='background')
        before = copy.deepcopy(dialog.state)
        default = copy.deepcopy(before); default['settings'] = dialog.read_settings()
        default_key = preview_key(job, default)
        dialog.pool_preview.next.click(); self.wait(lambda: dialog.overlay_worker is None)
        with patch.object(dialog, 'active_entry', return_value=job), patch.object(PlaybackWorker, 'start'):
            dialog.play_preview()
        worker = dialog.playback_worker
        self.assertIsNotNone(worker, dialog.playback_status.text())
        try:
            resolved, _ = resolve_sources(worker.state['settings']['layers'])
            self.assertEqual(resolved[0]['path'], items[1]['path'])
            self.assertNotEqual(default_key, dialog._playback_key)
            self.assertEqual(dialog.state, before)
        finally:
            worker.deleteLater(); dialog.playback_worker = None

    def test_quick_export_does_not_silently_export_a_different_pool_item(self):
        self.setup_pool(); dialog = self.dialog
        dialog.pool_preview.next.click(); self.wait(lambda: dialog.overlay_worker is None)
        with patch.object(dialog, '_commit_edit') as commit:
            dialog.quick_export_current()
        commit.assert_not_called()
        self.assertIn('恢复生成顺序', dialog.status.text())

    @unittest.skipUnless(Path(os.environ.get('NODE_FFMPEG', '')).is_file(), '需要真实FFmpeg')
    def test_encoded_trial_uses_chosen_picture_and_cleanup_without_changing_real_export(self):
        import numpy as np
        import test_chroma_cleanup as cleanup
        import test_chroma_edge_despill as helper
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        ffmpeg = os.environ['NODE_FFMPEG']; processor = helper.EdgeDespillTests()
        root = Path(self.temp.name); background = root/'background.mp4'
        processor.ffmpeg(['-f', 'lavfi', '-i', 'color=c=gray:s=320x200:r=10:d=0.4',
                         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(background)])
        items = []
        for index, color in enumerate(((220, 0, 0), (0, 0, 220))):
            data = np.full((100, 160, 4), (0, 255, 0, 255), np.uint8)
            data[cleanup.matte() > 0, :3] = color
            path = root/f'keyed{index}.png'; helper.picture(data).save(str(path))
            items.append(dict(id=str(index), path=str(path), name=f'抠像图{index}'))
        state = fresh_state()
        state['resource_pools'] = [dict(id='pool', name='抠像池', kind='visual', items=items, next_id='')]
        visual = layer(path=items[0]['path'], sequence_items=items, width=100, height=100, x=50, y=50,
                       key_mode='green', key_similarity=.01, key_softness=0, key_cleanup=True)
        state['settings'].update(ffmpeg_path=ffmpeg, aspect='source', darkness=0, music_enabled=False,
                                 use_voice=False, output_dir=str(root/'out'), layers=[visual])
        state['settings']['layers'] = bind_pool_layers(state['settings']['layers'], state)
        before = copy.deepcopy(state)
        chooser = PoolPreview(); self.addCleanup(chooser.deleteLater)
        chooser.sync(state['settings']['layers']); chooser.next.click()
        trial = copy.deepcopy(state); trial['settings'] = chooser.apply(state['settings'])
        job = dict(id='job', path=str(background), name='trial', title='', body='', task_dir='')
        worker = PlaybackWorker(job, trial, root/'private', preview_key(job, trial))
        ready = []; worker.ready.connect(ready.append); worker.run(); worker.deleteLater()
        self.assertNotIn('error', ready[0], ready[0].get('error'))
        formal, _ = render_static_text({**job, 'name': 'formal'}, state, root/'private')
        for path, channel in ((ready[0]['path'], 2), (formal, 0)):
            raw = processor.ffmpeg(['-i', str(path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
            frame = np.frombuffer(raw, np.uint8).reshape(200, 320, 3)
            self.assertGreater(frame[140, 150, channel], 200)
            self.assertLess(int(frame[26, 192].max())-int(frame[26, 192].min()), 8)
            self.assertGreater(frame[26, 192].mean(), 100)  # removed fragment exposes gray background
        self.assertEqual(state, before)

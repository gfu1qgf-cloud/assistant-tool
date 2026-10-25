import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.store import Store, DEFAULTS
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.row_progress import PROGRESS_ROLE, set_row_progress


class QueueInspectorLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_queue_is_in_right_tab_and_progress_covers_the_row_without_widgets(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(BatchTextVideoDialog, 'request_background'), \
             patch.object(BatchTextVideoDialog, '_start_overlay'):
            store = Store(directory)
            state = store.load()
            state['jobs'] = [dict(id=str(i), path=str(Path(directory)/f'background-{i}.mp4'),
                name=f'1016-reels{8000+i}', title='Nádej', body='Pokoj a nádej.',
                status='待生成', output='', error='') for i in range(3)]
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.resize(1250, 850)
            dialog.show()
            dialog.open_global_queue()
            self.app.processEvents()
            self.assertIs(dialog.right_tabs.currentWidget(), dialog.render_queue_panel)
            self.assertTrue(dialog.render_queue_panel.isAncestorOf(dialog.jobs))
            self.assertEqual(dialog.tabs.indexOf(dialog.jobs), -1)
            self.assertEqual(dialog.jobs.columnCount(), 4)
            self.assertEqual(dialog.jobs.horizontalHeaderItem(3).text(), '耗时')
            self.assertGreaterEqual(dialog.jobs.columnWidth(0), 200)
            self.assertEqual(dialog.jobs.horizontalScrollBar().maximum(), 0)
            dialog._host_item_progress('default', '1', 50)
            self.assertEqual(dialog.jobs.item(1, 0).data(PROGRESS_ROLE), 50)
            self.assertEqual(dialog.jobs.item(1, 2).text(), '50%')
            self.assertEqual(dialog.jobs.findChildren(QtWidgets.QProgressBar), [])
            self.app.processEvents()
            image = dialog.jobs.viewport().grab().toImage()
            rect = dialog.jobs.visualItemRect(dialog.jobs.item(1, 0))
            y = rect.bottom()-4
            left = image.pixelColor(8, y)
            right = image.pixelColor(image.width()-8, y)
            self.assertNotEqual(left, right)
            dialog._host_item_stage('default', '0', '等待字幕模型')
            dialog._host_item_stage('default', '1', '编码中')
            dialog._host_item_stage('default', '2', '字幕对齐中')
            self.assertEqual(dialog.jobs.item(0, 1).text(), '等待字幕模型')
            self.assertEqual(dialog.jobs.horizontalScrollBar().maximum(), 0)
            if os.environ.get('LZX_QUEUE_SCREENSHOT'):
                self.assertTrue(dialog.grab().save(os.environ['LZX_QUEUE_SCREENSHOT']))
            if os.environ.get('LZX_CAPTION_SCREENSHOT'):
                caption = new_caption()
                dialog.layer_panel.set_layers([caption])
                dialog.layer_panel.select_layer_id(caption['id'])
                editor = dialog.layer_panel.editor
                editor.sections['basic'].set_expanded(False)
                editor.sections['font'].set_expanded(True)
                editor.sections['font_size'].set_expanded(True)
                dialog.right_tabs.setCurrentIndex(0)
                dialog.layer_scroll.verticalScrollBar().setValue(0)
                self.app.processEvents()
                self.assertTrue(dialog.grab().save(os.environ['LZX_CAPTION_SCREENSHOT']))
            dialog.close()

    def test_render_controls_are_global_and_available_while_current_project_is_locked(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(BatchTextVideoDialog, 'request_background'), \
             patch.object(BatchTextVideoDialog, '_start_overlay'):
            store = Store(directory)
            state = store.save(store.load(), 0)
            state = store.create_profile(state, '第二项目')
            second = state['active_profile']
            store.select_profile(state, 'default')
            dialog = BatchTextVideoDialog(store=store)
            dialog.controls['export_workers'].setValue(4)
            dialog.controls['export_quality'].setCurrentIndex(dialog.controls['export_quality'].findData('low'))
            self.assertTrue(dialog.save_render_settings())
            dialog.switch_profile(second)
            self.assertEqual(dialog.controls['export_workers'].value(), 4)
            self.assertEqual(dialog.controls['export_quality'].currentData(), 'low')
            dialog._set_work_controls(True)
            self.assertTrue(dialog.controls['export_workers'].isEnabled())
            self.assertTrue(dialog.render_queue_panel.settings_section.content.isAncestorOf(dialog.controls['video_encoder']))
            dialog._set_work_controls(False)
            dialog.close()

    def test_busy_queue_scrolls_to_last_job_without_editing_or_toggling(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(BatchTextVideoDialog, 'request_background'), \
             patch.object(BatchTextVideoDialog, '_start_overlay'):
            store = Store(directory)
            state = store.load()
            state['jobs'] = [dict(id=str(i), path='', name=f'video-{i}', title=f'title-{i}', body='',
                status='待生成', output='', error='', render_checked=True) for i in range(680)]
            store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.resize(1250, 850); dialog.show(); dialog.open_global_queue()
            dialog.jobs.setCurrentCell(0, 0)
            initial_id = dialog.current_id
            dialog._render_progress['default'] = dict.fromkeys(map(str, range(680)), 0)
            dialog._render_stages['default'] = dict.fromkeys(map(str, range(680)), '排队中')
            with patch.object(dialog, 'is_batch_busy', return_value=True):
                dialog._set_work_controls(True)
                self.app.processEvents()
                self.assertTrue(dialog.jobs.isEnabled())
                self.assertTrue(dialog.jobs.verticalScrollBar().isEnabled())
                with patch.object(dialog, '_commit_edit') as commit, patch.object(dialog, '_show_job') as show:
                    dialog.jobs.setFocus()
                    for key, mods in ((QtCore.Qt.Key.Key_End, QtCore.Qt.KeyboardModifier.ControlModifier),
                                      (QtCore.Qt.Key.Key_Space, QtCore.Qt.KeyboardModifier.NoModifier),
                                      (QtCore.Qt.Key.Key_Delete, QtCore.Qt.KeyboardModifier.NoModifier),
                                      (QtCore.Qt.Key.Key_Up, QtCore.Qt.KeyboardModifier.AltModifier)):
                        self.app.sendEvent(dialog.jobs, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods))
                    self.app.processEvents()
                    self.assertEqual(dialog.jobs.currentRow(), 679)
                    self.assertGreater(dialog.jobs.verticalScrollBar().value(), 0)
                    self.assertEqual(dialog.current_id, initial_id)
                    commit.assert_not_called(); show.assert_not_called()
                item = dialog.jobs.item(679, 0)
                self.assertFalse(item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                self.assertEqual(item.checkState(), QtCore.Qt.CheckState.Checked)
                self.assertEqual(len(dialog.state['jobs']), 680)
                for i in range(25):
                    dialog._host_item_progress('default', str(i), 100)
                    dialog._host_item_stage('default', str(i), '已完成')
                dialog._host_item_stage('default', '25', '编码等待：30秒未推进')
                self.assertIn('已保存 25/680', dialog.render_queue_panel.current_label.text())
                self.assertIn('处理中 1', dialog.render_queue_panel.current_label.text())
                self.assertIn('排队 654', dialog.render_queue_panel.current_label.text())
                dialog._host_item_progress('default', '679', 70)
                self.assertEqual(dialog.jobs.item(679, 0).data(PROGRESS_ROLE), 70)
                if os.environ.get('LZX_BUSY_QUEUE_SCREENSHOT'):
                    self.app.processEvents()
                    self.assertTrue(dialog.grab().save(os.environ['LZX_BUSY_QUEUE_SCREENSHOT']))
                dialog.refresh_tables()
                self.assertFalse(dialog.jobs.item(679, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            dialog._set_work_controls(False)
            self.assertTrue(dialog.jobs.item(679, 0).flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            self.assertEqual(dialog.current_id, '679')
            dialog.close()

    def test_caption_sections_are_ordered_and_font_effects_are_nested(self):
        layer = new_caption()
        panel = LayerPanel([layer], '.', settings=DEFAULTS)
        panel.select_layer_id(layer['id'])
        editor = panel.editor
        self.assertIs(editor.body.itemAt(0).widget(), editor.content_editor)
        self.assertIs(editor.body.itemAt(1).widget(), editor.sections['basic'])
        for child in (editor.geometry_editor, panel.caption_properties):
            self.assertTrue(editor.sections['basic'].content.isAncestorOf(child))
        text = panel.text_properties
        font = editor.sections['font']
        for key in ('outline', 'shadow', 'background'):
            self.assertTrue(font.content.isAncestorOf(editor.sections[key]))
        self.assertNotIn('random_size',editor.sections)
        self.assertIs(text.font_size_section,font)
        self.assertTrue(font.content.isAncestorOf(panel.caption_properties.controls['caption_random_size']))
        self.assertTrue(font.content.isAncestorOf(text.controls['font_min']))
        self.assertTrue(font.content.isAncestorOf(text.controls['font_max']))
        self.assertNotIn('caption_help', editor.sections)
        font.set_expanded(False)
        panel.reveal_property('font')
        self.assertTrue(font.toggle.isChecked())

    def test_header_toggles_change_existing_features_and_preserve_detail_parameters(self):
        panel = LayerPanel(componentize_layers(None, DEFAULTS), '.', settings=DEFAULTS)
        panel.select_layer_id('title')
        text = panel.text_properties
        text.controls['shadow_x'].setValue(13)
        text.controls['shadow_enabled'].setChecked(True)
        self.assertIs(text.style_sections.sections['shadow'].enable_control, text.controls['shadow_enabled'])
        text.controls['shadow_enabled'].setChecked(False)
        self.assertEqual(panel.current()['shadow_x'], 13)
        text.controls['outline_width'].setValue(6)
        text.outline_toggle.setChecked(False)
        self.assertEqual(panel.current()['outline_width'], 0)
        text.outline_toggle.setChecked(True)
        self.assertEqual(panel.current()['outline_width'], 6)
        scroll = panel.editor.scroll_properties
        scroll.mode.setCurrentIndex(scroll.mode.findData('down'))
        scroll.controls['scroll_end_offset'].setValue(35)
        scroll.enable_control.setChecked(False)
        self.assertEqual(panel.current()['text_animation'], 'none')
        scroll.enable_control.setChecked(True)
        self.assertEqual(panel.current()['text_animation'], 'down')
        self.assertEqual(panel.current()['scroll_end_offset'], 35)
        panel.select_layer_id('body')
        panel.select_layer_id('title')
        self.assertEqual(panel.current()['outline_width'], 6)
        self.assertEqual(panel.current()['scroll_end_offset'], 35)

    def test_visual_animation_header_retains_strength_when_disabled(self):
        layer = dict(id='picture', kind='image', name='图片', enabled=True, opacity=100,
                     source='fixed', path='example.png', x=50, y=50, width=40, height=30)
        panel = LayerPanel([layer], '.', settings=DEFAULTS)
        panel.select_layer_id('picture')
        visual = panel.visual_properties
        visual.controls['visual_animation'].setCurrentIndex(visual.controls['visual_animation'].findData('breathe'))
        visual.controls['animation_strength'].setValue(23)
        visual.animation_toggle.setChecked(False)
        self.assertEqual(panel.current()['visual_animation'], 'none')
        visual.animation_toggle.setChecked(True)
        self.assertEqual(panel.current()['visual_animation'], 'breathe')
        self.assertEqual(panel.current()['animation_strength'], 23)

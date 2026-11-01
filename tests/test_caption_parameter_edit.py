import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets, QtTest
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, effects, colors
from app_plugins.builtin.batch_text_video.caption_parameter_values import VALUE_ROLE
from app_plugins.builtin.batch_text_video.caption_parameter_edit import apply_value, token_for, ColorDialog
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layout import ensure_fonts


class ParameterEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self,**values):
        layer=apply_style(new_caption(),'modern:talk_highlight',reset_layout=True)
        layer.update(values)
        return normalize_layers([layer])[0]

    def panel(self,layer=None):
        layer=layer or self.layer()
        other=self.layer();other['id']='other-caption'
        panel=LayerPanel([layer,other],'.');panel.select_layer_id(layer['id']);panel.hide_catalog()
        panel.resize(440,900);panel.show();self.addCleanup(panel.close)
        owner=panel.caption_properties;owner.show_parameters()
        window=owner.parameter_window;self.addCleanup(window.close)
        window.only_changed.setChecked(False);self.app.processEvents()
        return panel,window

    def item(self,window,key):
        for i in range(window.table.topLevelItemCount()):
            item=window.table.topLevelItem(i)
            if tuple(item.data(0,QtCore.Qt.ItemDataRole.UserRole))==key:return item
        self.fail('Parameter not visible: '+repr(key))

    def editor(self,window,key):
        window.table.setCurrentItem(self.item(window,key),1)
        window.edit_current();self.app.processEvents()
        self.assertIsNotNone(window.delegate.editor)
        return window.delegate.editor

    def commit(self,window,key,value):
        editor=self.editor(window,key)
        if isinstance(editor,QtWidgets.QAbstractSpinBox):editor.setValue(value)
        elif isinstance(editor,QtWidgets.QComboBox):editor.setCurrentIndex(editor.findData(value))
        else:editor.setText(value)
        QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Return)
        self.app.processEvents();self.app.processEvents()
        return editor

    def test_double_click_current_number_edits_and_name_still_locates(self):
        panel,window=self.panel();key=('effect','floating_amplitude')
        item=self.item(window,key);window.table.scrollToItem(item);self.app.processEvents()
        rect=window.table.visualItemRect(item)
        x=window.table.columnViewportPosition(1)+110
        QtTest.QTest.mouseClick(window.table.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=QtCore.QPoint(x,rect.center().y()))
        QtTest.QTest.mouseDClick(window.table.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=QtCore.QPoint(x,rect.center().y()))
        self.app.processEvents();editor=window.delegate.editor;self.assertIsNotNone(editor)
        editor.setValue(37);QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Return)
        self.app.processEvents();self.app.processEvents()
        self.assertEqual(effects(panel.current())['floating_amplitude'],37)
        self.assertTrue(window.isVisible())
        self.assertEqual(window.owner.effects_editor.controls['floating_amplitude'].value(),37)
        item=self.item(window,key);window.activate_item(item,0);QtTest.QTest.qWait(80)
        self.assertIsNotNone(window.flash)
        self.assertIs(window.flash.parentWidget(),window.owner.effects_editor.controls['floating_amplitude'])

    def test_root_value_bool_choice_text_and_weight_use_normal_inspector_updates(self):
        panel,window=self.panel();changes=[];panel.changed.connect(lambda:changes.append(1))
        values=[(('layer','outline_width'),7.5),(('effect','keyword_emphasis_enabled'),True),
                (('layer','outline_position'),'center'),(('effect','keyword_terms'),'nádej, hope, 希望')]
        for key,value in values:
            self.commit(window,key,value)
            actual=effects(panel.current())[key[1]] if key[0]=='effect' else panel.current()[key[1]]
            self.assertEqual(actual,value)
        self.assertEqual(len(changes),len(values))
        editor=self.editor(window,('layer','font_weight'));editor.setEditText('650')
        QtTest.QTest.keyClick(editor.lineEdit(),QtCore.Qt.Key.Key_Return)
        self.app.processEvents();self.assertEqual(panel.current()['font_weight'],650)
        editor=self.editor(window,('layer','font'))
        target=next(editor.itemText(i) for i in range(editor.count()) if editor.itemText(i)!=panel.current()['font'])
        editor.setCurrentFont(QtGui.QFont(target))
        QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Return);self.app.processEvents()
        self.assertEqual(panel.current()['font'],target)

    def test_cancel_invalid_joint_values_and_read_only_columns_never_modify_state(self):
        panel,window=self.panel();before=copy.deepcopy(panel.current());changes=[]
        panel.changed.connect(lambda:changes.append(1))
        editor=self.editor(window,('layer','outline_width'));editor.setValue(12)
        QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Escape);self.app.processEvents()
        self.assertEqual(panel.current(),before);self.assertFalse(changes)
        self.commit(window,('layer','font_min'),150)
        self.assertEqual(panel.current(),before);self.assertFalse(changes)
        self.assertFalse(window.edit_error.isHidden());self.assertIn('最小字号',window.edit_error.text())
        window.mode.setCurrentIndex(window.mode.findData('preset'));self.app.processEvents()
        self.assertFalse(window.edit_button.isEnabled())
        item=self.item(window,('layer','font_min'));window.table.editItem(item,1)
        self.assertIsNone(window.delegate.editor)
        window.mode.setCurrentIndex(window.mode.findData('current'));self.app.processEvents()
        window.table.editItem(self.item(window,('layer','font_min')),2)
        self.assertIsNone(window.delegate.editor);self.assertEqual(panel.current(),before)

    def test_opening_fractional_preset_value_does_not_round_it_or_create_an_edit(self):
        panel,window=self.panel();key=('effect','opacity_high_bg')
        before=copy.deepcopy(panel.current());changes=[];panel.changed.connect(lambda:changes.append(1))
        self.assertEqual(effects(before)['opacity_high_bg'],76.5)
        editor=self.editor(window,key)
        self.assertEqual(editor.value(),76.5)
        QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Return);self.app.processEvents()
        self.assertEqual(panel.current(),before);self.assertFalse(changes)
        self.commit(window,key,80.5)
        self.assertEqual(effects(panel.current())['opacity_high_bg'],80.5)
        self.assertEqual(len(changes),1)

    def test_geometry_percentages_and_side_anchor_resize_are_consistent_in_pixel_mode(self):
        from app_plugins.builtin.batch_text_video.geometry_anchors import distance
        panel,window=self.panel(self.layer(x=40,width=50,position_anchor_x='left'))
        panel.editor.geometry_editor.units.setCurrentIndex(panel.editor.geometry_editor.units.findData('pixels'))
        before=distance(panel.current(),'x')
        self.commit(window,('layer','width'),60)
        self.assertAlmostEqual(panel.current()['width'],60)
        self.assertAlmostEqual(distance(panel.current(),'x'),before)
        self.commit(window,('layer','x'),55)
        self.assertAlmostEqual(panel.current()['x'],55)
        self.assertEqual(self.item(window,('layer','x')).text(1),'55 %')
        self.assertAlmostEqual(panel.editor.geometry_editor.properties['x'].value(),distance(panel.current(),'x')*10.8,places=2)

    def test_refresh_preserves_draft_but_same_field_change_or_selection_discards_it(self):
        panel,window=self.panel();key=('effect','floating_amplitude')
        editor=self.editor(window,key);editor.setValue(37)
        panel.text_properties.controls['outline_width'].setValue(9)
        self.app.processEvents();self.assertIs(window.delegate.editor,editor)
        QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Return);self.app.processEvents();self.app.processEvents()
        self.assertEqual(panel.current()['outline_width'],9)
        self.assertEqual(effects(panel.current())['floating_amplitude'],37)
        editor=self.editor(window,key);token=copy.deepcopy(editor._parameter_token);editor.setValue(77)
        window.owner.effects_editor.controls['floating_amplitude'].setValue(11)
        self.app.processEvents();self.assertIsNone(window.delegate.editor)
        self.assertEqual(effects(panel.current())['floating_amplitude'],11)
        with self.assertRaises(ValueError):apply_value(window,token,77)
        editor=self.editor(window,key);token=copy.deepcopy(editor._parameter_token);editor.setValue(66)
        panel.select_layer_id('other-caption');self.app.processEvents()
        self.assertIsNone(window.delegate.editor)
        with self.assertRaises(ValueError):apply_value(window,token,66)
        self.assertNotEqual(effects(panel.current())['floating_amplitude'],66)

    def test_color_and_gradient_dialogs_commit_once_and_cancel_keeps_source(self):
        panel,window=self.panel();changes=[];panel.changed.connect(lambda:changes.append(1))
        def choose(key,text,accept):
            def respond():
                dialog=next(w for w in self.app.topLevelWidgets() if isinstance(w,ColorDialog) and w.isVisible())
                dialog.colors.setText(text)
                dialog.accept() if accept else dialog.reject()
            QtCore.QTimer.singleShot(40,respond)
            window.table.setCurrentItem(self.item(window,key),1);window.edit_current()
            self.app.processEvents();self.app.processEvents()
        before=copy.deepcopy(panel.current())
        choose(('effect','color_high'),'#ff0000,#0000ff',False)
        self.assertEqual(panel.current(),before);self.assertFalse(changes)
        choose(('effect','color_high'),'#ff0000,#0000ff',True)
        self.assertEqual(colors(effects(panel.current())['color_high']),['#ff0000','#0000ff'])
        self.assertEqual(len(changes),1)
        choose(('layer','caption_group_palette'),'#ff0000,#00ff00',True)
        self.assertEqual(panel.current()['caption_group_palette'],['#ff0000','#00ff00'])
        self.assertEqual(len(changes),2)

    def test_wheel_cannot_change_inline_numbers_and_disabled_controls_cannot_be_edited(self):
        panel,window=self.panel();editor=self.editor(window,('layer','outline_width'))
        original=editor.value();position=QtCore.QPointF(editor.rect().center())
        event=QtGui.QWheelEvent(position,QtCore.QPointF(editor.mapToGlobal(editor.rect().center())),QtCore.QPoint(),QtCore.QPoint(0,120),
            QtCore.Qt.MouseButton.NoButton,QtCore.Qt.KeyboardModifier.NoModifier,QtCore.Qt.ScrollPhase.NoScrollPhase,False)
        self.app.sendEvent(editor,event);self.assertEqual(editor.value(),original)
        token=copy.deepcopy(editor._parameter_token);window.delegate.cancel()
        panel.setEnabled(False)
        with self.assertRaises(ValueError):apply_value(window,token,12)
        self.assertNotEqual(panel.current()['outline_width'],12)

    def test_node_editor_uses_the_same_live_parameter_change_path(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=self.layer(caption_timing='task');changes=[]
        editor=CaptionNodeEditor({'caption':layer},changes.append);self.addCleanup(editor.close)
        owner=editor.panel.caption_properties;owner.show_parameters();window=owner.parameter_window
        self.addCleanup(window.close);window.only_changed.setChecked(False)
        self.commit(window,('layer','outline_width'),8)
        self.assertEqual(changes[-1]['caption']['outline_width'],8)

    def test_native_subtitle_output_changes_from_inline_value_without_realigning(self):
        panel,window=self.panel();words=[dict(text='Hope',start=0,end=1)]
        with tempfile.TemporaryDirectory() as folder:
            before,after=Path(folder)/'before.ass',Path(folder)/'after.ass'
            write_caption_ass(before,words,panel.current(),540,960,2.)
            self.commit(window,('layer','outline_width'),10)
            write_caption_ass(after,words,panel.current(),540,960,2.)
            self.assertNotEqual(before.read_bytes(),after.read_bytes())
        qa=os.environ.get('LZX_INLINE_CAPTION_QA')
        if qa:
            self.editor(window,('layer','outline_width')).setValue(11)
            window.resize(1220,730);self.app.processEvents()
            self.assertTrue(window.grab().save(str(Path(qa)/'inline-editor.png')))
            window.delegate.cancel()


class MainEditorHistoryTests(unittest.TestCase):
    setUpClass=classmethod(ParameterEditingTests.setUpClass.__func__)
    layer=ParameterEditingTests.layer
    item=ParameterEditingTests.item
    editor=ParameterEditingTests.editor
    commit=ParameterEditingTests.commit
    def test_inline_changes_save_preview_and_have_separate_undo_redo_in_the_small_window(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        with tempfile.TemporaryDirectory() as folder,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            layer=self.layer();state=fresh_state();state['settings']['layers']=normalize_layers([layer])
            store=Store(folder);store.save(state,0)
            dialog=BatchTextVideoDialog(store=store);self.addCleanup(dialog.close)
            dialog.layer_panel.select_layer_id(layer['id']);dialog.reset_editor_history()
            owner=dialog.layer_panel.caption_properties;owner.show_parameters();window=owner.parameter_window
            self.addCleanup(window.close);window.only_changed.setChecked(False)
            with patch.object(dialog,'invalidate_playback',wraps=dialog.invalidate_playback) as invalidated:
                original=dialog.layer_panel.current()['outline_width']
                self.commit(window,('layer','outline_width'),8)
                self.commit(window,('layer','outline_width'),11)
                self.assertEqual(len(dialog.edit_history.past),2)
                self.commit(window,('layer','outline_width'),11)
                self.assertEqual(len(dialog.edit_history.past),2)
                self.assertGreaterEqual(invalidated.call_count,2)
                self.assertTrue(dialog.edit_timer.isActive())
            self.assertTrue(dialog.persist())
            saved=next(v for v in store.load()['settings']['layers'] if v['id']==layer['id'])
            self.assertEqual(saved['outline_width'],11)
            QtTest.QTest.keyClick(window.table,QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.app.processEvents();self.assertEqual(dialog.layer_panel.current()['outline_width'],8)
            QtTest.QTest.keyClick(window.table,QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.app.processEvents();self.assertEqual(dialog.layer_panel.current()['outline_width'],original)
            QtTest.QTest.keyClick(window.table,QtCore.Qt.Key.Key_Y,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.app.processEvents();self.assertEqual(dialog.layer_panel.current()['outline_width'],8)
            self.assertTrue(window.isVisible())
            editor=self.editor(window,('layer','outline_width'));line=editor.lineEdit();line.selectAll()
            QtTest.QTest.keyClicks(line,'10');self.assertTrue(line.isUndoAvailable())
            past=len(dialog.edit_history.past)
            QtTest.QTest.keyClick(line,QtCore.Qt.Key.Key_Z,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(dialog.layer_panel.current()['outline_width'],8)
            self.assertEqual(len(dialog.edit_history.past),past)
            QtTest.QTest.keyClick(editor,QtCore.Qt.Key.Key_Escape);self.app.processEvents()
            token=token_for(window,('layer','outline_width'))
            with patch.object(dialog,'is_batch_busy',return_value=True),self.assertRaises(ValueError):apply_value(window,token,13)
            self.assertEqual(dialog.layer_panel.current()['outline_width'],8)
            token=token_for(window,('layer','outline_width'))
            dialog.current_id='different-task'
            with self.assertRaises(ValueError):apply_value(window,token,13)

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video import layer_clipboard as clip
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.watermarks import new_watermark
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources
from app_plugins.builtin.batch_text_video.custom_pools import create_pool
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layout import render_overlay, ensure_fonts


class Clipboard:
    def __init__(self): self.value = QtCore.QMimeData()
    def mimeData(self): return self.value
    def setMimeData(self, value): self.value = value


class ControlClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.clipboard = Clipboard()
        patcher = patch.object(QtWidgets.QApplication, 'clipboard', return_value=self.clipboard)
        patcher.start(); self.addCleanup(patcher.stop)

    def watermark(self):
        layer=new_watermark();layer.update(enabled=True,opacity=71.,show_start=1.,show_end=-2.)
        for i,part in enumerate(layer['corners'].values()):
            part.update(enabled=True,text='Watermark '+str(i),color='#b67332',font_min=20,font_max=28,
                        watermark_inset_x=19.+i,watermark_inset_y=23.+i,outline_width=2)
        return normalize_layers([layer])[0]

    def payload(self, layer, state=None, scope='source'):
        state=state or fresh_state()
        return clip.decode(clip.encode(clip.capture(layer,state,scope)))

    def test_watermark_snapshot_preserves_all_corners_style_timing_and_exact_render(self):
        source=self.watermark();state=fresh_state();before=copy.deepcopy(source)
        payload=self.payload(source,state)
        source['corners']['top_left']['text']='changed after copy'
        result=clip.prepare([],{},payload,profile='other',scope='target')
        pasted=next(x for x in result['layers'] if x['kind']=='watermark')
        self.assertNotEqual(pasted['id'],before['id'])
        self.assertEqual(pasted['corners'],before['corners'])
        self.assertEqual((pasted['opacity'],pasted['show_start'],pasted['show_end']),(71.,1.,-2.))
        settings=fresh_state()['settings'];settings.update(darkness=0,background_tint_strength=0)
        images=[]
        for layer in (before,pasted):
            local=dict(settings,layers=[layer]+[dict(x,enabled=False) for x in DEFAULT_LAYERS])
            images.append(render_overlay('','',540,960,local,ImageCache())[0])
        self.assertEqual(images[0],images[1])

    def test_singletons_request_replacement_and_other_layers_keep_identity(self):
        existing=self.watermark();existing['name']='Existing watermark'
        oldtext=dict(new_text_component(),id='other',text='Keep me')
        incoming=self.watermark();incoming['corners']['top_left']['text']='Replacement'
        result=clip.prepare([existing,oldtext],{},self.payload(incoming),profile='other',scope='target')
        self.assertEqual(result['replacements'],['Existing watermark'])
        self.assertEqual(sum(x['kind']=='watermark' for x in result['layers']),1)
        pasted=next(x for x in result['layers'] if x['kind']=='watermark')
        self.assertEqual(pasted['id'],existing['id'])
        self.assertEqual(next(x for x in result['layers'] if x['id']=='other')['text'],'Keep me')
        self.assertNotEqual(existing['corners']['top_left']['text'],'Replacement')

    def test_ordinary_copy_has_new_id_and_runtime_keys_are_not_portable(self):
        layer=normalize_layers([dict(new_text_component(),text='Body',_frozen_source='old',
                                     _matting_binding={'private':'runtime'})])[0]
        state=fresh_state();payload=self.payload(layer,state,'same')
        self.assertFalse(any(k.startswith('_') for k in payload['layer']))
        result=clip.prepare([layer],{},payload,profile=state['active_profile'],scope='same')
        pasted=next(x for x in result['layers'] if x['id']==result['selected'])
        self.assertNotEqual(pasted['id'],layer['id']);self.assertEqual(pasted['text'],'Body')
        self.assertEqual(len([x for x in result['layers'] if x['kind']=='text']),2)

    def test_foreign_pool_becomes_independent_sequence_and_keeps_selected_item(self):
        state=fresh_state();pool=create_pool(state,'Shared original','visual')
        pool['items']=[dict(id=str(i),path=f'C:/fixtures/{i}.png',name=f'Image{i}') for i in range(3)]
        layer=normalize_layers([dict(id='image',kind='image',path='',source='pool',pool_id=pool['id'])])[0]
        layer=bind_pool_layers([layer],state)[0];layer['sequence_cursor']=1
        payload=self.payload(layer,state,'source')
        result=clip.prepare([],{},payload,profile='target',scope='source')
        pasted=next(x for x in result['layers'] if x['id']==result['selected'])
        self.assertEqual(pasted['source'],'sequence');self.assertEqual(pasted['pool_id'],'')
        self.assertEqual(pasted['sequence_cursor'],1)
        self.assertTrue(all(x['id'] not in {'0','1','2'} for x in pasted['sequence_items']))
        self.assertEqual(resolve_sources([pasted])[0][0]['path'],'C:/fixtures/1.png')
        local=clip.prepare([],{},payload,profile=state['active_profile'],scope='source')
        pasted=next(x for x in local['layers'] if x['id']==local['selected'])
        self.assertEqual((pasted['source'],pasted['pool_id']),('pool',pool['id']))
        self.assertEqual(pool['items'][1]['id'],'1')

    def test_task_text_follows_target_task_and_foreign_exclusion_targets_do_not_leak(self):
        layer=normalize_layers([dict(new_text_component(),source='pool',copy_binding='task',wrap_enabled=True,
                                     wrap_targets=['old-image'])])[0]
        result=clip.prepare([],{},self.payload(layer),profile='other',scope='target')
        pasted=next(x for x in result['layers'] if x['id']==result['selected'])
        self.assertEqual(pasted['source'],'pool')
        self.assertFalse(pasted['wrap_enabled']);self.assertEqual(pasted['wrap_targets'],[])
        self.assertIn('TARGET',resolve_sources([pasted],'TITLE','TARGET',
            job=dict(copy_id='paired',title='TITLE',body='TARGET'))[0][0]['text'])

    def test_invalid_clipboards_and_component_limit_are_atomic(self):
        for value in (b'[]',b'{}',b'null',b'NaN',b'\xff'):
            with self.assertRaises(ValueError):clip.decode(value)
        with patch.object(clip,'MAX_BYTES',8):
            with self.assertRaises(ValueError):clip.decode(b'0123456789')
        layers=normalize_layers([dict(id=str(i),kind='image',path=f'{i}.png') for i in range(32)])
        before=copy.deepcopy(layers)
        with self.assertRaises(ValueError):clip.prepare(layers,{},self.payload(layers[0]),profile='other',scope='target')
        self.assertEqual(layers,before)

    def editor(self, store):
        dialog=BatchTextVideoDialog(store=store)
        self.addCleanup(self.destroy_editor,dialog)
        return dialog

    def destroy_editor(self, dialog):
        dialog.cancel_work()
        for thread in dialog.findChildren(QtCore.QThread):self.assertTrue(thread.wait(2000))
        dialog.deleteLater();QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_real_keyboard_cross_project_watermark_paste_undo_redo_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            store=Store(root);state=fresh_state();state['settings']['layers']=normalize_layers([self.watermark()])
            state=store.save(state,0);original_id=state['active_profile']
            source_before=copy.deepcopy(state['settings']['layers'])
            state=store.create_profile(state,'Destination');target_id=state['active_profile']
            store.select_profile(state,original_id)
            dialog=self.editor(store);dialog.layer_panel.select_layer_id('corner_watermarks')
            self.assertTrue(dialog.persist())
            source_before=copy.deepcopy(dialog.state['settings']['layers'])
            QTest.keyClick(dialog.layer_panel.list,QtCore.Qt.Key.Key_C,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertTrue(self.clipboard.mimeData().hasFormat(clip.MIME))
            dialog.switch_profile(target_id)
            QTest.keyClick(dialog.layer_panel.list,QtCore.Qt.Key.Key_V,QtCore.Qt.KeyboardModifier.ControlModifier)
            pasted=next(x for x in dialog.layer_panel.values() if x['kind']=='watermark')
            self.assertEqual(pasted['corners'],self.watermark()['corners'])
            self.assertEqual(len(dialog.edit_history.past),1)
            self.assertTrue(dialog.undo_edit())
            self.assertFalse(any(x['kind']=='watermark' for x in dialog.layer_panel.values()))
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(next(x for x in dialog.layer_panel.values() if x['kind']=='watermark')['corners'],pasted['corners'])
            saved=store.load();original=next(x for x in saved['profiles'] if x['id']==original_id)
            self.assertEqual(original['data']['settings']['layers'],source_before)
            self.assertEqual(saved['active_profile'],target_id)
            if os.environ.get('LZX_CONTROL_CLIPBOARD_QA'):
                menu=QtWidgets.QMenu();from app_plugins.builtin.batch_text_video.layer_clipboard_ui import add_actions
                add_actions(dialog.layer_panel,menu);menu.popup(QtCore.QPoint(100,100));self.app.processEvents()
                menu.grab().save(str(Path(os.environ['LZX_CONTROL_CLIPBOARD_QA'])/'control-clipboard-menu.png'));menu.close();menu.deleteLater()

    def test_existing_watermark_requires_confirmation_and_can_be_undone(self):
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            store=Store(root);state=fresh_state();state['settings']['layers']=normalize_layers([self.watermark()]);store.save(state,0)
            dialog=self.editor(store);dialog.layer_panel.select_layer_id('corner_watermarks')
            self.assertTrue(dialog.layer_panel.copy_component())
            current=dialog.layer_panel.current();current['corners']['top_left']['text']='Keep existing'
            dialog.layer_panel.rebuild(current['id']);dialog.persist()
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No):
                self.assertFalse(dialog.layer_panel.paste_component())
            self.assertEqual(dialog.layer_panel.current()['corners']['top_left']['text'],'Keep existing')
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                self.assertTrue(dialog.layer_panel.paste_component())
            self.assertEqual(dialog.layer_panel.current()['corners']['top_left']['text'],'Watermark 0')
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(dialog.layer_panel.current()['corners']['top_left']['text'],'Keep existing')

    def test_canvas_shortcuts_duplicate_control_and_clipboard_stays_distinct_from_resources(self):
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            store=Store(root);state=fresh_state();state['settings']['layers']=normalize_layers([dict(new_text_component(),id='custom',text='Hello')]);store.save(state,0)
            dialog=self.editor(store);dialog.layer_panel.select_layer_id('custom')
            QTest.keyClick(dialog.preview,QtCore.Qt.Key.Key_C,QtCore.Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(dialog.preview,QtCore.Qt.Key.Key_V,QtCore.Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(len([x for x in dialog.layer_panel.values() if x['kind']=='text']),2)
            from app_plugins.builtin.batch_text_video.pool_copy import MIME as POOL_MIME
            data=QtCore.QMimeData();data.setData(POOL_MIME,b'{}');self.clipboard.value=data
            before=dialog.layer_panel.values()
            with patch.object(QtWidgets.QMessageBox,'warning') as warning:
                self.assertFalse(dialog.layer_panel.paste_component());self.assertIn('没有控件',warning.call_args.args[2])
            self.assertEqual(dialog.layer_panel.values(),before)

    def test_save_failure_rolls_back_controls_selection_and_history(self):
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            store=Store(root);state=fresh_state();state['settings']['layers']=normalize_layers([dict(new_text_component(),id='custom',text='Hello')]);store.save(state,0)
            dialog=self.editor(store);panel=dialog.layer_panel;panel.select_layer_id('custom');panel.copy_component()
            before=panel.values();history=copy.deepcopy(dialog.edit_history.__dict__)
            with patch.object(store,'save_controls',side_effect=OSError('disk full')),patch.object(QtWidgets.QMessageBox,'warning'):
                self.assertFalse(panel.paste_component())
            self.assertEqual(panel.values(),before);self.assertEqual(panel.current()['id'],'custom')
            self.assertEqual(dialog.edit_history.__dict__,history)
            self.assertEqual(store.load()['settings']['layers'],before)

    def test_store_guard_rejects_render_submitted_after_initial_ui_check(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);state=store.save(fresh_state(),0)
            state['render_plans']=[dict(id='plan',profile_id=state['active_profile'],status='submitted')]
            state=store.save(state,state['revision']);before=store.path.read_bytes()
            with self.assertRaisesRegex(ValueError,'正在渲染'):store.save_controls(state,state['revision'])
            self.assertEqual(store.path.read_bytes(),before)

    def test_title_style_replaces_target_slot_and_background_keeps_target_source(self):
        from app_plugins.builtin.batch_text_video.background_visual import control,values
        title=normalize_layers([dict(new_text_component(),id='title',kind='title',source='job',
                                     color='#abcdef',show_end=5.)])[0]
        result=clip.prepare(normalize_layers(),{},self.payload(title),profile='target',scope='target')
        self.assertEqual(len([x for x in result['layers'] if x['kind']=='title']),1)
        pasted=next(x for x in result['layers'] if x['kind']=='title')
        self.assertEqual((pasted['source'],pasted['color'],pasted['show_end']),('job','#abcdef',5.))
        background=control({'background_visual':dict(visual_animation='breathe',animation_strength=17.,show_end=-2.)})
        result=clip.prepare([],values(control({})),self.payload(background),profile='target',scope='target')
        self.assertEqual((result['background']['visual_animation'],result['background']['show_end']),('breathe',-2.))
        self.assertNotIn('background_pool_id',result['background'])

    def test_background_keyboard_and_undo_restore_motion_and_window(self):
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            store=Store(root);state=fresh_state();state['settings']['background_visual']=dict(visual_animation='breathe',animation_strength=17.,show_end=-2.)
            store.save(state,0);dialog=self.editor(store);panel=dialog.layer_panel;panel.select_background()
            self.assertTrue(panel.copy_component())
            panel.set_background({'background_visual':dict(visual_animation='none')});dialog.persist()
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                self.assertTrue(panel.paste_component())
            self.assertEqual(panel.background_values()['animation_strength'],17.)
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(panel.background_values()['visual_animation'],'none')

    def test_project_conflict_does_not_move_history_or_overwrite_saved_content(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);initial=store.save(fresh_state(),0)
            newer=copy.deepcopy(initial);newer['settings']['background_visual']['show_end']=2.
            store.save(newer,newer['revision']);before=store.path.read_bytes()
            with self.assertRaisesRegex(ValueError,'被其他窗口'):
                store.save_controls(initial,initial['revision'])
            self.assertEqual(store.path.read_bytes(),before)


if __name__=='__main__':unittest.main()

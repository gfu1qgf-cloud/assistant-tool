import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video import pool_copy as pc
from app_plugins.builtin.batch_text_video import pool_copy_ui as ui
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, get_pool
from app_plugins.builtin.batch_text_video.pools import new_copy, bind_pool_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class Clipboard:
    """Exercise real Qt events without replacing the user's system clipboard."""
    def __init__(self):
        self.value = QtCore.QMimeData()

    def mimeData(self):
        return self.value

    def setMimeData(self, value):
        self.value = value


class PoolCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.clipboard = Clipboard()
        self.clip_patch = patch.object(QtWidgets.QApplication, 'clipboard', return_value=self.clipboard)
        self.clip_patch.start()
        self.addCleanup(self.clip_patch.stop)

    def state(self):
        state = fresh_state()
        state['backgrounds'] = ['C:/fixtures/a.mov', 'C:/fixtures/b.png']
        state['image_pool'] = [dict(id='pic', path='C:/fixtures/c.png', name='透明人物',
                                   thumbnail='C:/fixtures/thumb.png', extra={'preserve': [1, 2]})]
        visual = create_pool(state, '视觉池', 'visual')
        voice = create_pool(state, '人声池', 'voice')
        music = create_pool(state, '音乐池', 'music')
        return state, visual, voice, music

    def payload(self, state, key, ids):
        return pc.decode(pc.encode(pc.capture(state, key, ids)))

    def test_visual_multi_copy_in_both_directions_preserves_metadata_and_ids_are_independent(self):
        state, visual, _, _ = self.state()
        before = copy.deepcopy(state)
        payload = self.payload(state, 'backgrounds', state['backgrounds'])
        changed, result = pc.paste_records(state, 'pool:'+visual['id'], payload)
        self.assertEqual(result, dict(added=2, skipped=0))
        self.assertEqual(state, before)
        items = get_pool(changed, visual['id'])['items']
        self.assertEqual([i['path'] for i in items], state['backgrounds'])
        self.assertEqual(len({i['id'] for i in items}), 2)
        changed, _ = pc.paste_records(changed, 'pool:'+visual['id'], self.payload(state, 'image_pool', ['pic']))
        clone = get_pool(changed, visual['id'])['items'][-1]
        self.assertNotEqual(clone['id'], 'pic')
        self.assertEqual(clone['extra'], {'preserve': [1, 2]})
        clone['extra']['preserve'].append(3)
        self.assertEqual(state['image_pool'][0]['extra']['preserve'], [1, 2])
        back = self.payload(changed, 'pool:'+visual['id'], [i['id'] for i in items])
        dest, result = pc.paste_records(fresh_state(), 'image_pool', back)
        self.assertEqual(result['added'], 2)
        self.assertEqual([i['path'] for i in dest['image_pool']], state['backgrounds'])

    def test_repeated_paste_and_case_variants_skip_same_original_without_overwrite(self):
        state, visual, _, _ = self.state()
        visual['items'] = [dict(id='existing', path='C:/FIXTURES/A.MOV', name='保留名称')]
        changed, result = pc.paste_records(state, 'pool:'+visual['id'], self.payload(state, 'backgrounds', state['backgrounds']))
        self.assertEqual(result, dict(added=1, skipped=1))
        self.assertEqual(get_pool(changed, visual['id'])['items'][0]['name'], '保留名称')
        again, result = pc.paste_records(changed, 'pool:'+visual['id'], self.payload(state, 'backgrounds', state['backgrounds']))
        self.assertEqual(result, dict(added=0, skipped=2))
        self.assertEqual(again, changed)

    def test_incompatible_mixed_selection_and_full_pool_are_atomic(self):
        state, visual, voice, _ = self.state()
        before = copy.deepcopy(state)
        payload = self.payload(state, 'backgrounds', state['backgrounds'])
        with self.assertRaisesRegex(ValueError, '不兼容'):
            pc.paste_records(state, 'pool:'+voice['id'], payload)
        self.assertEqual(state, before)
        visual['items'] = [dict(id=str(i), path=f'C:/fixtures/{i}.png') for i in range(1000)]
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError, '1000'):
            pc.paste_records(state, 'pool:'+visual['id'], payload)
        self.assertEqual(state, before)

    def test_music_voice_interop_resets_only_new_entry_history(self):
        state, _, voice, music = self.state()
        state['music'] = [dict(id='song', path='C:/fixtures/a.wav', cursor=42., last_used='yesterday', audio_key='old')]
        music.update(next_id='old', items=[dict(id='old', path='C:/fixtures/old.wav', cursor=23.)])
        changed, _ = pc.paste_records(state, 'pool:'+music['id'], self.payload(state, 'music', ['song']))
        target = get_pool(changed, music['id'])
        self.assertEqual(target['next_id'], 'old')
        self.assertEqual(target['items'][0]['cursor'], 23.)
        self.assertEqual(target['items'][1]['cursor'], 0.)
        self.assertEqual(target['items'][1]['audio_key'], '')
        changed, _ = pc.paste_records(changed, 'pool:'+voice['id'], self.payload(state, 'music', ['song']))
        self.assertNotIn('cursor', get_pool(changed, voice['id'])['items'][0])
        changed, result = pc.paste_records(changed, 'pool:'+voice['id'], self.payload(state, 'backgrounds', [state['backgrounds'][0]]))
        self.assertEqual(result['added'], 1)  # Videos can supply a voice source.

    def test_text_file_and_subtitle_pools_remain_typed(self):
        state = fresh_state()
        text = create_pool(state, '文本一', 'text')
        text['items'] = [dict(id='txt', path='C:/fixtures/a.txt')]
        other = create_pool(state, '文本二', 'text')
        subtitle = create_pool(state, '字幕', 'subtitle')
        payload = self.payload(state, 'pool:'+text['id'], ['txt'])
        changed, result = pc.paste_records(state, 'pool:'+other['id'], payload)
        self.assertEqual(result['added'], 1)
        for key in ('copy_pool', 'pool:'+subtitle['id']):
            with self.assertRaisesRegex(ValueError, '不兼容'):
                pc.paste_records(changed, key, payload)

    def test_inline_copy_cross_project_is_snapshot_and_deduplicates_origins(self):
        source = fresh_state()
        source['copy_pool'] = [dict(new_copy(), title='标题', body='正文', voice_path='voice.wav',
                                    task_dir='C:/fixtures/task', subtitle_path='words.srt')]
        payload = self.payload(source, 'copy_pool', [source['copy_pool'][0]['id']])
        source['copy_pool'][0]['body'] = '复制后的修改'
        target = fresh_state()
        target['active_profile'] = 'other'
        changed, result = pc.paste_records(target, 'copy_pool', payload)
        self.assertEqual(result['added'], 1)
        record = changed['copy_pool'][0]
        self.assertEqual((record['title'], record['body'], record['voice_path']), ('标题', '正文', 'voice.wav'))
        self.assertNotEqual(record['id'], payload['records'][0]['id'])
        self.assertEqual(pc.paste_records(changed, 'copy_pool', payload)[1]['skipped'], 1)
        relay = self.payload(changed, 'copy_pool', [record['id']])
        self.assertEqual(pc.paste_records(source, 'copy_pool', relay)[1]['skipped'], 1)

    def test_random_order_next_item_fixed_jobs_and_rebind_are_preserved(self):
        state, visual, _, _ = self.state()
        visual['items'] = [dict(id='second', path='C:/fixtures/second.png'), dict(id='first', path='C:/fixtures/first.png')]
        key = 'pool:'+visual['id']
        state['resource_orders'] = {key: ['first', 'second']}
        state['jobs'] = [dict(id='job', render_assignment={'path': 'fixed.mov'})]
        layers = [dict(kind='image', source='pool', pool_id=visual['id'], sequence_items=copy.deepcopy(visual['items']), sequence_cursor=1)]
        changed, _ = pc.paste_records(state, key, self.payload(state, 'image_pool', ['pic']))
        bound = pc.rebind(layers, changed, key)
        self.assertEqual(bound[0]['sequence_items'][bound[0]['sequence_cursor']]['id'], 'first')
        self.assertEqual(len(bound[0]['sequence_items']), 3)
        self.assertEqual(changed['jobs'], state['jobs'])
        self.assertEqual(changed['resource_orders'][key][:2], ['first', 'second'])
        self.assertEqual(len(changed['resource_orders'][key]), 3)
        # An unrelated missing text source must not block copying visual records.
        text = create_pool(changed, '缺失文本', 'text')
        text['items'] = [dict(id='missing', path='C:/missing.txt')]
        unrelated = dict(kind='text', source='pool', pool_id=text['id'], sequence_items=[], sequence_cursor=0)
        self.assertEqual(pc.rebind([unrelated], changed, key), [unrelated])

    def test_background_trim_carries_between_projects_and_target_cut_wins(self):
        state, _, _, _ = self.state()
        rule = dict(path=state['backgrounds'][0], start=1., end=3., duration=5., size=123, mtime_ns=456)
        state['settings']['background_trims'] = [rule]
        payload = self.payload(state, 'backgrounds', [rule['path']])
        target = fresh_state()
        changed, _ = pc.paste_records(target, 'backgrounds', payload)
        self.assertEqual(changed['settings']['background_trims'], [rule])
        target['settings']['background_trims'] = [dict(rule, start=2.)]
        changed, _ = pc.paste_records(target, 'backgrounds', payload)
        self.assertEqual(changed['settings']['background_trims'][0]['start'], 2.)

    def test_clipboard_rejects_invalid_unbounded_or_nonfinite_payloads(self):
        for value in (b'[]', b'{}', b'null', b'{"version":2}', b'{"kind":[]}', b'NaN', b'\xff'):
            with self.assertRaises(ValueError):
                pc.decode(value)
        with patch.object(pc, 'MAX_CLIPBOARD_BYTES', 8):
            with self.assertRaisesRegex(ValueError, '过大'):
                pc.decode(b'0123456789')
        state, _, _, _ = self.state()
        with self.assertRaisesRegex(ValueError, '变化'):
            pc.capture(state, 'image_pool', ['gone'])

    def editor(self, root, state=None):
        store = Store(Path(root)/'store')
        if state is not None:
            store.save(state, 0)
        dialog = BatchTextVideoDialog(store=store)
        self.addCleanup(self.destroy_editor, dialog)
        return dialog

    def destroy_editor(self, dialog):
        # Never close/save after TemporaryDirectory and preview patches have ended.
        # A failed late close would show the dialog and start an unrelated preview.
        dialog.cancel_work()
        for thread in dialog.findChildren(QtCore.QThread):
            self.assertTrue(thread.wait(2000), 'Test-owned preview thread did not finish')
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_actual_keyboard_cross_pool_copy_paste_without_importing_or_touching_files(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            state, visual, _, _ = self.state()
            state['image_pool'][0]['path'] = str(Path(root)/'source.png')
            image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor('red'))
            image.save(state['image_pool'][0]['path'])
            original = Path(state['image_pool'][0]['path']).read_bytes()
            dialog = self.editor(root, state)
            with patch.object(dialog, 'import_pool_images', side_effect=AssertionError('Must not import')):
                dialog.image_pool.selectAll()
                QTest.keyClick(dialog.image_pool, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier)
                self.assertTrue(self.clipboard.mimeData().hasFormat(pc.MIME))
                table = dialog.custom_tables[visual['id']]
                QTest.keyClick(table, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(table.rowCount(), 1)
                self.assertEqual(get_pool(dialog.store.load(), visual['id'])['items'][0]['name'], '透明人物')
                QTest.keyClick(table, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(table.rowCount(), 1)
                self.assertIn('跳过', dialog.status.text())
            self.assertEqual(Path(state['image_pool'][0]['path']).read_bytes(), original)
            self.assertFalse((dialog.store.directory/'layer-assets').exists())

    def test_actual_cross_project_paste_keeps_source_and_other_projects_unchanged(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            state, _, _, _ = self.state()
            store = Store(Path(root)/'store')
            state = store.save(state, 0)
            source_id = state['active_profile']
            state = store.create_profile(state, '第二项目')
            target_id = state['active_profile']
            state = store.select_profile(state, source_id)
            dialog = BatchTextVideoDialog(store=store)
            self.addCleanup(self.destroy_editor, dialog)
            dialog.image_pool.selectAll()
            self.assertTrue(ui.copy_selected(dialog, 'image_pool'))
            dialog.switch_profile(target_id)
            self.assertEqual(dialog.state['active_profile'], target_id)
            self.assertTrue(ui.paste(dialog, 'image_pool'))
            saved = store.load()
            source = next(p['data'] for p in saved['profiles'] if p['id'] == source_id)
            self.assertEqual(source['image_pool'], state['image_pool'])
            self.assertEqual(len(saved['image_pool']), 1)
            self.assertNotEqual(saved['image_pool'][0]['id'], source['image_pool'][0]['id'])
            self.assertEqual(saved['active_profile'], target_id)

    def test_incompatible_and_busy_paste_show_popup_without_saving(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            state, _, _, _ = self.state()
            dialog = self.editor(root, state)
            dialog.image_pool.selectAll(); ui.copy_selected(dialog, 'image_pool')
            before = dialog.store.path.read_bytes()
            with patch.object(QtWidgets.QMessageBox, 'warning') as warning, patch.object(dialog, 'persist') as persist:
                self.assertFalse(ui.paste(dialog, 'music'))
                self.assertIn('不兼容', warning.call_args.args[2]); persist.assert_not_called()
                with patch.object(dialog, 'is_batch_busy', return_value=True):
                    self.assertFalse(ui.paste(dialog, 'backgrounds'))
                persist.assert_not_called()
            self.assertEqual(dialog.store.path.read_bytes(), before)

    def test_failed_final_save_rolls_back_all_records_and_bindings(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            state, visual, _, _ = self.state()
            dialog = self.editor(root, state)
            dialog.image_pool.selectAll(); ui.copy_selected(dialog, 'image_pool')
            layers = dialog.layer_panel.values()
            with patch.object(dialog.store, 'paste_pool_records', side_effect=OSError('disk full')), patch.object(QtWidgets.QMessageBox, 'warning') as warning:
                self.assertFalse(ui.paste(dialog, 'pool:'+visual['id']))
                self.assertEqual(get_pool(dialog.state, visual['id'])['items'], [])
                self.assertEqual(get_pool(dialog.store.load(), visual['id'])['items'], [])
                self.assertEqual(dialog.layer_panel.values(), layers)
                warning.assert_called_once()

    def test_transaction_write_failure_preserves_entire_saved_file(self):
        from app_plugins.builtin.batch_text_video import store as storage
        with tempfile.TemporaryDirectory() as root:
            state, visual, _, _ = self.state()
            store = Store(root)
            state = store.save(state, 0)
            payload = self.payload(state, 'image_pool', ['pic'])
            before = store.path.read_bytes()
            data_file = storage.data_file
            class Unwritable:
                def __init__(self, path): self.original = data_file(path)
                def __getattr__(self, name): return getattr(self.original, name)
                def write_json(self, *_args, **_kwargs): raise OSError('disk full')
            with patch.object(storage, 'data_file', side_effect=Unwritable):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    store.paste_pool_records(state['active_profile'], 'pool:'+visual['id'], payload,
                        expected_profile_revision=state['profiles'][0]['revision'])
            self.assertEqual(store.path.read_bytes(), before)

    def test_transaction_rechecks_running_plan_and_revision_without_overwriting_other_project(self):
        with tempfile.TemporaryDirectory() as root:
            state, visual, _, _ = self.state()
            store = Store(root)
            state = store.save(state, 0)
            source = state['active_profile']
            payload = self.payload(state, 'image_pool', ['pic'])
            stale_revision = state['profiles'][0]['revision']
            state = store.save(state, state['revision'])
            before = store.path.read_bytes()
            with self.assertRaisesRegex(ValueError, '其他窗口'):
                store.paste_pool_records(source, 'pool:'+visual['id'], payload, expected_profile_revision=stale_revision)
            self.assertEqual(store.path.read_bytes(), before)
            # A render can start after the editor's first busy check.
            state['render_plans'] = [dict(id='running', profile_id=source, status='submitted')]
            state = store.save(state, state['revision'])
            before = store.path.read_bytes()
            revision = state['profiles'][0]['revision']
            with self.assertRaisesRegex(ValueError, '正在渲染'):
                store.paste_pool_records(source, 'pool:'+visual['id'], payload, expected_profile_revision=revision)
            self.assertEqual(store.path.read_bytes(), before)
            state['render_plans'] = []
            state = store.save(state, state['revision'])
            state = store.create_profile(state, '独立项目')
            other = state['active_profile']
            state = store.select_profile(state, source)
            other_before = copy.deepcopy(next(p for p in state['profiles'] if p['id'] == other))
            state, result = store.paste_pool_records(source, 'pool:'+visual['id'], payload,
                expected_profile_revision=next(p['revision'] for p in state['profiles'] if p['id'] == source))
            self.assertEqual(result['added'], 1)
            self.assertEqual(next(p for p in state['profiles'] if p['id'] == other), other_before)

    def test_empty_system_clipboard_and_plain_text_show_clear_message(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            dialog = self.editor(root)
            for value in (None, QtCore.QMimeData()):
                self.clipboard.value = value
                with patch.object(QtWidgets.QMessageBox, 'warning') as warning:
                    self.assertFalse(ui.paste(dialog, 'image_pool'))
                    self.assertIn('没有素材记录', warning.call_args.args[2])
                menu = QtWidgets.QMenu()
                ui.add_actions(dialog, menu, 'image_pool')
                self.assertFalse(menu.actions()[1].isEnabled())
                menu.deleteLater()

    def test_right_click_exposes_plain_copy_and_paste_without_target_submenus(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
            state, visual, _, _ = self.state()
            dialog = self.editor(root, state)
            dialog.image_pool.selectAll(); ui.copy_selected(dialog, 'image_pool')
            table = dialog.custom_tables[visual['id']]
            menu = QtWidgets.QMenu()
            ui.add_actions(dialog, menu, 'pool:'+visual['id'])
            self.assertEqual([a.text() for a in menu.actions()], ['复制\tCtrl+C', '粘贴\tCtrl+V'])
            self.assertFalse(any(a.menu() for a in menu.actions()))
            self.assertTrue(menu.actions()[1].isEnabled())
            menu.actions()[1].trigger()
            self.assertEqual(table.rowCount(), 1)
            if os.environ.get('LZX_POOL_COPY_QA'):
                menu.popup(QtCore.QPoint(100, 100)); self.app.processEvents()
                menu.grab().save(str(Path(os.environ['LZX_POOL_COPY_QA'])/'pool-copy-menu.png'))
                menu.close()
            menu.deleteLater()


if __name__ == '__main__':
    unittest.main()

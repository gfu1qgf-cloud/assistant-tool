import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video import pool_order as order
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, accepted_suffixes
from app_plugins.builtin.batch_text_video.custom_pool_ui import NewPoolDialog
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers, new_copy, pairing_plan
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources


class Reverse:
    def shuffle(self, items): items.reverse()


class PoolOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def state(self):
        state = fresh_state()
        state['backgrounds'] = ['a.mp4', 'b.mp4', 'c.mp4']
        state['copy_pool'] = [dict(new_copy(), id=str(i), title=str(i)) for i in range(3)]
        state['image_pool'] = [dict(id=str(i), path=f'{i}.png') for i in range(3)]
        state['music'] = [dict(id=str(i), path=f'{i}.wav', cursor=i+2., last_used='before') for i in range(3)]
        for kind in ('image', 'video', 'visual', 'text', 'subtitle', 'music', 'voice'):
            pool = create_pool(state, kind, kind)
            pool['items'] = [dict(id=str(i), path=f'{i}.mp4', cursor=i+2.) for i in range(3)]
        return state

    def test_all_libraries_restore_the_first_order_after_multiple_shuffles(self):
        state = self.state()
        for key, items in order.libraries(state).items():
            before = copy.deepcopy(items)
            self.assertTrue(order.reorder(state, key, rng=Reverse()))
            self.assertEqual(items, before[::-1])
            self.assertTrue(order.reorder(state, key, rng=Reverse()))
            self.assertTrue(order.active(state, key))
            self.assertTrue(order.reorder(state, key, restore=True))
            self.assertEqual(items, before)
            self.assertFalse(order.active(state, key))

    def test_removed_items_never_reappear_new_items_append_and_deleted_pool_pruned(self):
        state = self.state(); key = 'image_pool'
        order.reorder(state, key, rng=Reverse())
        state[key] = [state[key][0], state[key][2], dict(id='new', path='new.png')]
        order.normalize(state)
        order.reorder(state, key, restore=True)
        self.assertEqual([i['id'] for i in state[key]], ['0', '2', 'new'])
        pool = state['resource_pools'][0]; key = 'pool:'+pool['id']
        order.reorder(state, key, rng=Reverse())
        state['resource_pools'].clear(); order.normalize(state)
        self.assertNotIn(key, state['resource_orders'])

    def test_fixed_jobs_and_playback_positions_unchanged(self):
        state = self.state(); state['jobs'] = [dict(id='job', render_assignment={'voice':'fixed.wav'}, path='fixed.mp4')]
        jobs = copy.deepcopy(state['jobs']); original = copy.deepcopy(state['music'])
        order.reorder(state, 'music', rng=Reverse())
        self.assertEqual(state['music'], original[::-1])
        self.assertEqual(state['next_music_id'], '2')
        self.assertEqual(state['jobs'], jobs)
        order.reorder(state, 'backgrounds', rng=Reverse())
        self.assertEqual([p['path'] for p in pairing_plan([], state['backgrounds'], 'videos')], state['backgrounds'])

    def test_binding_restart_affects_only_matching_pool_and_actual_resolution(self):
        state = self.state()
        layers = [dict(new_text_component(), source='pool', pool_field='title')]
        layers = bind_pool_layers(layers, state)
        layers[0]['sequence_cursor'] = 1
        other = dict(layers[0], id='other', pool_id=state['resource_pools'][3]['id'])
        order.reorder(state, 'copy_pool', rng=Reverse())
        restarted = order.restart_layers(layers+[other], 'copy_pool')
        self.assertEqual(restarted[1], other)
        bound = bind_pool_layers(restarted[:1], state)
        resolved, _ = resolve_sources(bound)
        self.assertEqual(resolved[0]['text'], '2')
        self.assertEqual(bound[0]['sequence_cursor'], 0)

    def test_saved_per_project_with_no_disk_path_scans(self):
        state = self.state()
        with patch.object(Path, 'stat', side_effect=AssertionError('Unexpected path scan')):
            order.reorder(state, 'backgrounds', rng=Reverse())
            order.normalize(state)
        with tempfile.TemporaryDirectory() as root:
            store=Store(root); state=store.save(state, 0)
            saved=store.load(); self.assertEqual(saved['backgrounds'], ['c.mp4','b.mp4','a.mp4'])
            self.assertEqual(saved['resource_orders']['backgrounds'], ['a.mp4','b.mp4','c.mp4'])
            clone=store.create_profile(saved,'copy',clone=True)
            self.assertEqual(clone['resource_orders'],saved['resource_orders'])
            order.reorder(clone,'backgrounds',restore=True)
            self.assertTrue(order.active(saved,'backgrounds'))

    def test_small_empty_unsupported_and_corrupt_rules(self):
        state=fresh_state()
        self.assertFalse(order.reorder(state,'jobs'))
        self.assertFalse(order.reorder(state,'image_pool'))
        state['image_pool']=[dict(id='only',path='a.png')]
        self.assertFalse(order.reorder(state,'image_pool'))
        state['resource_orders']={'image_pool':'broken'}
        with self.assertRaises(ValueError): order.normalize(state)

    def test_new_visual_pool_has_one_choice_and_old_kinds_accept_mixed_media(self):
        dialog=NewPoolDialog()
        self.assertEqual([dialog.kind.itemData(i) for i in range(dialog.kind.count())],['visual'])
        for kind in ('image','video','visual'):
            self.assertTrue({'.png','.gif','.mov'} <= accepted_suffixes(kind))
        dialog.deleteLater()

    def test_real_editor_menu_ranks_restore_and_persistence_failure_roll_back(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.pool_order_ui import change
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);state=fresh_state()
            state['copy_pool']=[dict(new_copy(),id=str(i),title=str(i)) for i in range(4)]
            store.save(state,0)
            dialog=BatchTextVideoDialog(store=store)
            try:
                with patch.object(dialog,'update_preview'), patch('app_plugins.builtin.batch_text_video.pool_order.random.SystemRandom',return_value=Reverse()):
                    self.assertTrue(change(dialog,'copy_pool'))
                    self.assertEqual(dialog.copy_pool.verticalHeaderItem(0).text(),'随机 1')
                    self.assertEqual(dialog.state['copy_pool'][0]['id'],'3')
                    if os.environ.get('LZX_POOL_ORDER_QA'):
                        dialog.resources.setCurrentWidget(dialog.copy_pool)
                        dialog.show_resources()
                        dialog.resize(1400,900);dialog.show();self.app.processEvents()
                        dialog.resource_pane_layout.parentWidget().grab().save(str(Path(os.environ['LZX_POOL_ORDER_QA'])/'pool-order-ui.png'))
                    self.assertEqual(store.load()['copy_pool'][0]['id'],'3')
                    from app_plugins.builtin.batch_text_video.pool_order_ui import refresh
                    with patch.object(dialog.copy_pool, 'setVerticalHeaderLabels') as labels:
                        refresh(dialog)
                        labels.assert_not_called()
                    self.assertTrue(change(dialog,'copy_pool',restore=True))
                    self.assertEqual(dialog.copy_pool.verticalHeaderItem(0).text(),'1')
                    self.assertEqual(dialog.state['copy_pool'][0]['id'],'0')
                    with patch.object(dialog,'persist',side_effect=[True,False]):
                        self.assertFalse(change(dialog,'copy_pool'))
                    self.assertEqual(dialog.state['copy_pool'][0]['id'],'0')
                    self.assertFalse(order.active(dialog.state,'copy_pool'))
                    with patch.object(dialog,'is_batch_busy',return_value=True):
                        self.assertFalse(change(dialog,'copy_pool'))
                self.assertEqual(dialog.pool_order_button.text(),'顺序')
                self.assertTrue(dialog.pool_tools.isAncestorOf(dialog.pool_order_button))
                dialog.pool_order_button.menu().aboutToShow.emit()
                self.assertEqual([a.text() for a in dialog.pool_order_button.menu().actions()],['互斥选择…','随机使用顺序','恢复原顺序'])
            finally:
                dialog.close();dialog.deleteLater()


if __name__=='__main__': unittest.main()

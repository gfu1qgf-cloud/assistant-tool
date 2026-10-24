"""A custom voice/image/etc pool determines length, even with no base video."""
import copy
from pathlib import Path
import tempfile
import unittest
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.pools import apply_pairs, pairing_plan, new_copy
from app_plugins.builtin.batch_text_video.quantity_basis import entries, choices
from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
from app_plugins.builtin.batch_text_video.background import validate_batch_outputs


class QuantityBasisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def state(self, root, count=63, kind='voice'):
        state = fresh_state(); state['settings'].update(background_pool_id='none', output_dir=str(root),
            voice_pool_mode='rotate', voice_pool_id='narration', use_voice=True)
        files = []
        for i in range(count):
            path = root/f'voice-{i}.mp3'; path.touch(); files.append(dict(id=f'voice-{i}', path=str(path)))
        state['resource_pools'] = [dict(id='narration', name='背景朗读', kind=kind, items=files, next_id='')]
        return state

    def test_sixty_three_voice_items_produce_sixty_three_distinct_repeatable_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(Path(directory)); before = copy.deepcopy(state)
            dialog = PairingDialog([], [], state=state)
            try:
                dialog.quantity_basis.setCurrentIndex(dialog.quantity_basis.findData('pool:narration'))
                self.assertEqual(len(dialog.pairs), 63)
                self.assertEqual([pair['voice_path'] for pair in dialog.pairs], [item['path'] for item in state['resource_pools'][0]['items']])
                self.assertTrue(all(pair['path'] == '' for pair in dialog.pairs))
                self.assertEqual(state, before)
                self.assertEqual(apply_pairs(state, dialog.pairs), (63, 0))
                identifiers = [j['id'] for j in state['jobs']]
                self.assertEqual(apply_pairs(state, dialog.pairs), (0, 0))
                self.assertEqual([j['id'] for j in state['jobs']], identifiers)
                self.assertEqual(len({j['name'] for j in state['jobs']}), 63)
                validate_batch_outputs(state, identifiers)
                queued, same = prepare_current_export(state, state['jobs'][20], queue_id=identifiers[20])
                self.assertEqual(same, identifiers[20]); self.assertEqual(len(queued['jobs']), 63)
            finally: dialog.deleteLater()

    def test_large_voice_basis_does_not_duplicate_full_voice_options_in_every_row(self):
        state = fresh_state()
        state['settings'].update(background_pool_id='none', voice_pool_id='voices', voice_pool_mode='rotate')
        state['resource_pools'] = [dict(id='voices', name='人声', kind='voice', next_id='',
            items=[dict(id=str(i), path=f'voice-{i}.wav') for i in range(1000)])]
        dialog = PairingDialog([], [], state=state)
        try:
            dialog.quantity_basis.setCurrentIndex(dialog.quantity_basis.findData('pool:voices'))
            self.assertEqual(len(dialog.pairs), 1000)
            self.assertTrue(all(dialog.table.cellWidget(i, 4).count() == 4 for i in range(1000)))
            combo = dialog.table.cellWidget(600, 4)
            self.assertGreaterEqual(combo.findData('voice-999.wav'), 0)
            self.assertGreater(combo.count(), 1000)
            self.assertEqual(dialog.table.cellWidget(601, 4).count(), 4)
        finally: dialog.deleteLater()

    def test_fewer_copies_backgrounds_and_voices_loop_to_explicit_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = self.state(root, count=7, kind='image')
            copies = [new_copy(), new_copy()]; videos = [str(root/'background.mp4')]
            Path(videos[0]).touch()
            basis = entries(state, 'pool:narration', copies, videos)
            pairs = pairing_plan(copies, videos, 'one_to_one', quantity=basis)
            self.assertEqual(len(pairs), 7)
            self.assertEqual([p['copy']['id'] for p in pairs], [copies[i%2]['id'] for i in range(7)])
            self.assertEqual(apply_pairs(state, pairs), (7, 0))
            self.assertEqual(apply_pairs(state, list(reversed(pairs))), (0, 0))
            single = pairing_plan(copies, videos+['other.mp4'], 'single', quantity=basis)
            self.assertTrue(all(pair['path'] == videos[0] for pair in single))

    def test_empty_missing_and_other_pool_kinds_are_explicit(self):
        state = fresh_state()
        with self.assertRaisesRegex(ValueError, '为空'): entries(state, 'builtin:image_pool', [], [])
        with self.assertRaises(ValueError): entries(state, 'pool:missing', [], [])
        for kind in ('image', 'video', 'visual', 'text', 'subtitle', 'voice', 'music'):
            state['resource_pools'] = [dict(id='any', name=kind, kind=kind, items=[dict(id='one', path='test')])]
            self.assertIn('pool:any', dict(choices(state)))
            self.assertEqual(entries(state, 'pool:any', [], []), [('pool:any', 'one')])
        dialog = PairingDialog([], [], state=fresh_state())
        try:
            dialog.quantity_basis.setCurrentIndex(dialog.quantity_basis.findData('builtin:image_pool'))
            self.assertEqual(dialog.pairs, [])
            self.assertFalse(dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled())
            self.assertIn('为空', dialog.status.text())
        finally: dialog.deleteLater()


if __name__ == '__main__': unittest.main()

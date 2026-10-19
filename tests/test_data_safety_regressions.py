"""Synthetic files and keys only; never reads real private data or accounts."""
import copy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.AppDataStorage import ApplicationStorage
from model.ApiKeyHelper import (API_KEY_STATUSES_CONFIG_KEY as STATUSES, api_key_id,
                                record_api_key_status, select_api_keys_for_attempt)
from model.InventoryManager import InventoryStore
from model.ProtectedStorage import StorageUnavailable
from model.TaskResultExporter import export_task, output_name
from qt_compat import QtCore, QtWidgets


class OutputNameSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = SimpleNamespace(task_id='7', task_name='Long customer title ' * 12,
            admin='Alice', creator='Operator', task_date='1017', task_type='video')
        self.profile = {'mode': 'all_videos', 'task_dir': 'tasks',
                        'output_name_template': '{task_id}-{task_name}-{index}.mp4'}
        self.config = {'task_output_filename_max_length': 50}
        folder = self.root / 'tasks/7'
        folder.mkdir(parents=True)
        self.sources = [folder / 'first.mp4', folder / 'second.mp4']
        for index, source in enumerate(self.sources):
            source.write_bytes(b'original-video-' + b'x' * (index + 1))
            os.utime(source, (time.time() - 60, time.time() - 60))

    def export(self):
        return export_task(self.task, date(2026, 10, 17), self.root, self.root / 'result',
                           self.root / 'wsp', self.profile, self.config, False)

    def test_long_title_keeps_each_video_index_and_length_limit(self):
        names = [output_name(self.task, date(2026, 10, 17), self.config, self.profile, index=n)
                 for n in (1, 2, 12)]
        self.assertEqual(len(set(names)), 3)
        for n, name in zip((1, 2, 12), names):
            self.assertLessEqual(len(name), 50)
            self.assertTrue(name.endswith(f'-{n}.mp4'), name)

    def test_two_videos_export_separately_and_rerun_does_not_duplicate(self):
        outputs = self.export()
        self.assertEqual(len(set(outputs)), 2)
        self.assertEqual({path.read_bytes() for path in outputs}, {path.read_bytes() for path in self.sources})
        self.assertEqual(len(list((self.root / 'result').glob('*.mp4'))), 2)
        self.assertEqual(self.export(), [])

    def test_index_at_the_front_is_kept_without_an_extra_tail_index(self):
        self.profile['output_name_template'] = '{index:03d}-{task_id}-{task_name}.mp4'
        name = output_name(self.task, date(2026, 10, 17), self.config, self.profile, index=12)
        self.assertTrue(name.startswith('012-7-'))
        self.assertLessEqual(len(name), 50)
        self.assertFalse(name.endswith('-12.mp4'))

    def test_multi_video_template_without_index_gets_a_distinct_suffix(self):
        self.profile['output_name_template'] = '{task_id}.mp4'
        self.assertEqual({path.name for path in self.export()}, {'7-1.mp4', '7-2.mp4'})

    def test_destination_collision_is_reported_before_copying_any_video(self):
        self.task.task_name = 'short'
        self.profile['output_name_template'] = '{task_id}-{index!s:.0}.mp4'
        with self.assertRaisesRegex(ValueError, '同一|重复|冲突'):
            self.export()
        self.assertFalse(list((self.root / 'result').glob('*.mp4')))

    def test_too_short_limit_does_not_truncate_the_index_or_copy_anything(self):
        self.config['task_output_filename_max_length'] = 12
        self.profile['output_name_template'] = '{admin}-fixed-{task_name}-{index}.mp4'
        with self.assertRaisesRegex(ValueError, '保留视频序号'):
            self.export()
        self.assertFalse(list((self.root / 'result').glob('*.mp4')))


class AvatarStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / 'encrypted-test-volume/AssistantToolData'
        self.private.mkdir(parents=True)
        self.available = True
        def check(*_args, **_kwargs):
            if not self.available:
                raise StorageUnavailable('synthetic volume unavailable')
        self.storage = ApplicationStorage(self.private, guard=SimpleNamespace(check=check))
        binding = patch('model.AppDataStorage._APPLICATION_STORAGE', self.storage)
        binding.start()
        self.addCleanup(binding.stop)
        self.store = InventoryStore(self.private / 'InventoryState.json', self.root / 'external-materials')
        self.source = self.root / 'user-original.png'
        self.source.write_bytes(b'user-selected original must remain intact')

    def test_new_avatar_copy_is_private_while_materials_and_original_stay_external(self):
        material = self.root / 'source-video.mp4'
        material.write_bytes(b'external large material')
        person = self.store.add_person('New person', self.source, source_paths=[material])
        avatar = Path(person['avatar_path'])
        self.assertTrue(avatar.is_relative_to(self.private / 'PluginData/inventory/avatars'), avatar)
        self.assertEqual(avatar.read_bytes(), self.source.read_bytes())
        self.assertFalse(list(self.store.people_root.rglob('*.png')))
        self.assertFalse(Path(person['material_path']).is_relative_to(self.private))
        self.assertEqual((Path(person['material_path']) / material.name).read_bytes(), material.read_bytes())

    def test_failed_record_write_rolls_back_both_new_avatar_and_material_directory(self):
        for error in (OSError('disk full'), ValueError('invalid state')):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.store, '_write', side_effect=error):
                    with self.assertRaises(type(error)):
                        self.store.add_person('Failed person', self.source)
                self.assertFalse([p for p in self.store.people_root.rglob('*') if p.is_file()])
                self.assertFalse([p for p in (self.private / 'PluginData/inventory/avatars').rglob('*') if p.is_file()])
                self.assertEqual(self.source.read_bytes(), b'user-selected original must remain intact')
                self.assertEqual(self.store.list_people(), [])

    def test_missing_volume_cannot_create_a_plaintext_avatar_fallback(self):
        self.available = False
        with self.assertRaises(StorageUnavailable):
            self.store.add_person('Unavailable', self.source)
        self.assertFalse(list(self.store.people_root.rglob('*.png')))
        self.assertTrue(self.source.is_file())

    def test_add_and_update_use_the_same_private_avatar_location(self):
        person = self.store.add_person('Person', self.source)
        replacement = self.root / 'replacement.jpg'
        replacement.write_bytes(b'replacement avatar')
        updated = self.store.update_person_profile(person['id'], 'Person', avatar_path=replacement)
        self.assertEqual(Path(updated['avatar_path']).parent, Path(person['avatar_path']).parent)
        self.assertTrue(Path(updated['avatar_path']).is_relative_to(self.private))
        self.assertTrue(self.source.is_file())
        self.assertTrue(replacement.is_file())

    def test_post_commit_checkpoint_error_does_not_delete_saved_avatar_or_materials(self):
        with patch.object(self.store, '_clear_download_checkpoint', side_effect=OSError('cleanup failed')):
            with self.assertRaises(OSError):
                self.store.add_person('Committed person', self.source)
        person = self.store.list_people()[0]
        self.assertEqual(Path(person['avatar_path']).read_bytes(), self.source.read_bytes())
        self.assertTrue(Path(person['material_path']).is_dir())


class ElevenLabsResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.json'
        self.first, self.second = 'synthetic-reset-key', 'synthetic-other-key'
        self.original = {'run_upload': False, 'elevenlabs_api_keys': [self.first, self.second],
            'business': {'keep': [1, 2]}, STATUSES: {
                api_key_id(self.first): {'status': 'invalid'},
                api_key_id(self.second): {'status': 'available', 'used': 1}}}
        self.path.write_text(json.dumps(self.original), encoding='utf8')
        setting_path = patch('PYUI.main_setting_pyui.CONFIG_PATH', str(self.path))
        setting_path.start(); self.addCleanup(setting_path.stop)
        from PYUI.main_setting_pyui import MainSettingDialog
        self.dialog = MainSettingDialog()
        self.addCleanup(self.close_dialog)

    def close_dialog(self):
        self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def reset_first(self):
        self.dialog.elevenlabs_api_key_list.item(0).setSelected(True)
        self.dialog.reset_api_key_status_btn.click()

    def test_reset_persists_and_stale_subsequent_save_cannot_restore_invalid_status(self):
        self.reset_first()
        record_api_key_status(self.path, self.second, status='exhausted', used=100)
        self.assertTrue(self.dialog.save_config())
        saved = json.loads(self.path.read_text('utf8'))
        self.assertNotIn(api_key_id(self.first), saved[STATUSES])
        self.assertEqual(saved[STATUSES][api_key_id(self.second)]['used'], 100)
        self.assertIn(self.first, select_api_keys_for_attempt([self.first, self.second], saved[STATUSES])[0])
        from PYUI.main_setting_pyui import _write_config_with_rolling_backups
        _write_config_with_rolling_backups(self.path, copy.deepcopy(self.original))
        again = json.loads(self.path.read_text('utf8'))
        self.assertNotIn(api_key_id(self.first), again[STATUSES])
        self.assertEqual(again['business'], self.original['business'])
        self.assertEqual(again['elevenlabs_api_keys'], self.original['elevenlabs_api_keys'])

    def test_explicit_reset_does_not_conflict_with_a_concurrent_status_update(self):
        self.reset_first()
        record_api_key_status(self.path, self.first, status='invalid', checked=123)
        with patch('PYUI.main_setting_pyui.QMessageBox.warning') as warning:
            self.assertTrue(self.dialog.save_config())
        warning.assert_not_called()
        self.assertNotIn(api_key_id(self.first), json.loads(self.path.read_text('utf8'))[STATUSES])

    def test_cancel_does_not_persist_a_reset(self):
        self.reset_first()
        self.dialog.reject()
        self.assertEqual(json.loads(self.path.read_text('utf8')), self.original)

    def test_existing_host_session_honors_reset_without_recreating_the_binding(self):
        from model.ElevenLabsService import ElevenLabsService
        service = ElevenLabsService()
        binding = service.bind(self.original, self.path)
        self.assertNotIn(self.first, service.tts_credentials(binding)[0])
        self.reset_first()
        self.assertTrue(self.dialog.save_config())
        self.assertIn(self.first, service.tts_credentials(binding)[0])

    def test_queued_audio_snapshot_honors_reset_instead_of_restoring_old_status(self):
        from app_plugins.builtin.task_audio_subtitle import _AudioJobService
        worker = _AudioJobService({'model_name': 'small', 'config': self.original,
            'config_path': str(self.path), 'audio_settings': {}}, SimpleNamespace(report=lambda *_: None))
        self.reset_first()
        self.assertTrue(self.dialog.save_config())
        self.assertNotIn(api_key_id(self.first), worker.context.load_config()[STATUSES])

    def test_failed_save_keeps_reset_pending_for_the_next_attempt(self):
        self.reset_first()
        with patch('PYUI.main_setting_pyui._write_config_with_rolling_backups_locked', side_effect=OSError('disk unavailable')), \
                patch('PYUI.main_setting_pyui.QMessageBox.critical'):
            self.assertFalse(self.dialog.save_config())
        self.assertIn(api_key_id(self.first), self.dialog._reset_api_key_ids)
        self.assertEqual(json.loads(self.path.read_text('utf8')), self.original)
        self.assertTrue(self.dialog.save_config())
        self.assertNotIn(api_key_id(self.first), json.loads(self.path.read_text('utf8'))[STATUSES])


if __name__ == '__main__':
    unittest.main()

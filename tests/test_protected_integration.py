"""Protected host config/log/UI paths with synthetic data only."""
import json
import gc
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage
from model.ProtectedStorage import migrate, VolumeSession, bind, read_marker, StorageUnavailable
from test_protected_storage import Provider


class ProtectedIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.old=self.root/'old';self.volume=self.root/'volume'
        self.old.mkdir();self.volume.mkdir()
        self.old.joinpath('config.json').write_text(json.dumps({'gemini_api_keys':['synthetic-gemini'],
            'elevenlabs_api_keys':['synthetic-ele'],'unrelated':['preserve']}))
        self.provider=Provider(self.volume)
        self.space=patch('model.ProtectedStorage.shutil.disk_usage',return_value=SimpleNamespace(free=10**10))
        self.space.start();self.addCleanup(self.space.stop)
        self.data,self.marker=migrate(self.old,self.provider,self.provider.volume)
        self.guard=VolumeSession(self.provider,self.provider.volume,self.data,marker=self.marker)
        self.storage=ApplicationStorage(self.data,guard=self.guard,redirects=[(self.old/'config.json',self.data/'config.json')])
        self.addCleanup(self.storage.close)
        self.patch=patch('model.AppDataStorage._APPLICATION_STORAGE',self.storage)
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_main_config_save_keeps_both_providers_and_old_plaintext_untouched(self):
        from PYUI.main_pyui import MainDialog
        from model.GeminiKeyManager import GeminiKeyManager
        from model.AppDataStorage import data_file
        original=self.old.joinpath('config.json').read_bytes()
        manager=GeminiKeyManager(data_file(self.old/'config.json').read_json())
        fake=SimpleNamespace(config_name=str(self.old/'config.json'),gemini_keys=manager,appendLog=Mock())
        fake._save_current_config_locked=MethodType(MainDialog._save_current_config_locked,fake)
        fake.dump=lambda:manager.update_config(data_file(fake.config_name).read_json())
        self.assertTrue(MainDialog.saveCurrentConfig(fake,show_errors=False))
        saved=self.data.joinpath('config.json').read_text()
        self.assertIn('synthetic-gemini',saved);self.assertIn('synthetic-ele',saved)
        self.assertEqual(original,self.old.joinpath('config.json').read_bytes())
        before=self.data.joinpath('config.json').read_bytes();self.provider.available=False
        self.assertFalse(MainDialog.saveCurrentConfig(fake,show_errors=False))
        self.assertEqual(before,self.data.joinpath('config.json').read_bytes())
        self.assertFalse(self.old.joinpath('config.json.main.tmp').exists())

    def test_logger_has_no_plaintext_fallback_and_closes_native_fault_descriptors(self):
        from model import AppLogger
        logger=logging.getLogger('vc-isolated-test');logger.handlers=[]
        with patch.object(AppLogger,'_log_file_path',None),patch.object(AppLogger,'_fault_stream',None), \
             patch.object(AppLogger,'_fault_fd',None),patch.object(AppLogger.logging,'getLogger',return_value=logger), \
             patch.object(AppLogger.faulthandler,'enable'),patch.object(AppLogger.faulthandler,'disable'):
            _,path=AppLogger.configure_application_logging()
            self.assertTrue(path.is_relative_to(self.data/'logs'))
            descriptor=AppLogger._fault_fd;stream=AppLogger._fault_stream
            before=path.read_bytes();self.provider.available=False
            logger.info('must-not-write-after-loss')
            self.assertEqual(path.read_bytes(),before)
            AppLogger.shutdown_application_logging(close=True)
            self.assertTrue(stream.closed)
            with self.assertRaises(OSError):os.fstat(descriptor)
            with self.assertRaises(StorageUnavailable):list(AppLogger._candidate_log_directories())
        self.assertFalse((self.old/'logs').exists())

    def test_bind_refuses_missing_config_instead_of_starting_with_empty_keys(self):
        (self.data/'config.json').unlink()
        with self.assertRaisesRegex(StorageUnavailable,'空配置'):
            bind(self.old,self.provider,self.provider.volume,self.data)

    def test_search_database_and_thumbnail_paths_redirect_without_rebuilding(self):
        import numpy as np
        from model.image_retrieval.index import ImageSearchIndex
        redirects=[(self.old/'SmartImageSearch',self.data/'SmartImageSearch')]
        storage=ApplicationStorage(self.data,guard=self.guard,redirects=redirects)
        self.addCleanup(storage.close)
        with patch('model.AppDataStorage._APPLICATION_STORAGE',storage):
            index=ImageSearchIndex(self.old/'SmartImageSearch')
            self.assertEqual(index.root,self.data/'SmartImageSearch')
            original_picture=self.root/'picture.png';original_picture.write_bytes(b'external-synthetic')
            thumbnail=self.data/'SmartImageSearch/thumbnails/keep_v2.jpg';thumbnail.write_bytes(b'keep')
            old_thumbnail=self.old/'SmartImageSearch/thumbnails/keep_v2.jpg'
            with index._connect() as db:
                db.execute('INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (str(original_picture),1,1,'folder','synthetic','test-model',np.array([1,0],dtype=np.float16).tobytes(),str(old_thumbnail),'',1))
            hits=index.search('test-model',[1,0])
            self.assertEqual(hits[0]['thumbnail'],str(thumbnail))
            self.assertFalse((self.old/'SmartImageSearch').exists())
            self.provider.available=False
            # The context manager acquires the handle on entry, not when its
            # inert descriptor is created. Losing the volume must fail before SQL.
            with self.assertRaises(StorageUnavailable):
                with index._connect():self.fail('unmounted volume was opened')

    def test_closed_database_connections_are_not_kept_alive_by_tracking(self):
        db=self.storage.for_plugin('test').file('db.sqlite3').connect()
        db.execute('create table records (value)');db.commit()
        self.assertEqual(len(self.storage.connections),1)
        db.close();del db;gc.collect()
        self.assertEqual(len(self.storage.connections),0)

    def test_saved_codex_profile_paths_use_encrypted_archive_without_changing_metadata(self):
        from app_plugins.builtin.codex_account_switcher.store import AccountProfile
        original=self.old/'archive/credentials/profile'
        mapped=self.data/'CodexAccountProfiles/credentials/profile'
        storage=ApplicationStorage(self.data,guard=self.guard,redirects=[(self.old/'archive',self.data/'CodexAccountProfiles')])
        with patch('model.AppDataStorage._APPLICATION_STORAGE',storage):
            profile=AccountProfile('id','synthetic',str(original),'synthetic-date')
            self.assertEqual(profile.home_path,mapped);self.assertEqual(profile.codex_home,str(mapped))
            self.assertEqual(profile.to_dict()['codex_home'],str(original))

    def test_background_submission_stops_immediately_when_volume_is_lost(self):
        from model.BackgroundTasks import BackgroundTaskManager
        manager=BackgroundTaskManager()
        self.provider.available=False
        with self.assertRaises(StorageUnavailable):
            manager.submit(owner='test',title='synthetic',runner=lambda *_:None)
        self.assertEqual(manager._queue,[])
        manager.deleteLater();self.app.processEvents()

    def test_large_json_save_keeps_checks_but_does_not_probe_driver_per_token(self):
        original=self.guard.check
        count=[0]
        def check(*args,**kwargs):count[0]+=1;return original(*args,**kwargs)
        with patch.object(self.guard,'check',side_effect=check):
            self.storage.for_plugin('test').file('large.json').write_json({'items':[{'text':'synthetic'}]*1000},indent=2)
        self.assertLess(count[0],10)

    def test_chooser_only_shows_verified_volumes_and_rejects_wrong_identity(self):
        from PYUI.protected_storage_pyui import StorageChooser
        from qt_compat import QtWidgets
        self.provider.mounted=lambda:[self.provider.volume]
        chooser=StorageChooser(self.old,{'storage_id':'a'*32,'volume_id':self.marker['volume_id']},self.provider)
        self.assertEqual(chooser.volumes.count(),1)
        with patch.object(QtWidgets.QMessageBox,'warning') as alert:
            chooser.use_existing();alert.assert_called_once()
        self.assertIsNone(chooser.selection)
        chooser.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()

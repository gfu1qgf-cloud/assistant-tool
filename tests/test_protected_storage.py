import ctypes
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.VeraCryptVolumes import (MountList, Properties, MountedVolume, VeraCryptVolumes, VolumeError)
from model.ProtectedStorage import (migrate, read_marker, read_bootstrap, commit_bootstrap,
    VolumeSession, StorageUnavailable, MIN_FREE)
from model.AppDataStorage import ApplicationStorage, data_file, data_io
from model.PrivateDataCatalog import catalog, enumerate_files


class FakeDriver:
    def __init__(self):
        self.closed=0; self.patch=(1,26,7,0); self.mapping=r'\Device\VeraCryptVolumeV'
        self.props=Properties(); self.props.drive=21; self.props.unique_id=9; self.props.length=1024**3
        self.props.volume_id[:]=bytes(range(1,33))
        self.list=MountList(); self.list.mask=1<<21; self.list.lengths[21]=self.props.length
        ctypes.memmove(ctypes.addressof(self.list.ids[21]),bytes(self.props.volume_id),32)
    def version(self): return self.patch
    def open_handle(self,*args,**kwargs): return 1
    def close(self,*args): self.closed+=1
    def device(self,*args): return self.mapping
    def request(self,_handle,number,_size,initial=b''):
        return {1:struct.pack('<i',0x126),6:bytes(self.list),7:bytes(self.props)}[number]


class DriverContractTests(unittest.TestCase):
    def test_official_1267_packed_windows_abi(self):
        self.assertEqual(ctypes.sizeof(MountList),17424); self.assertEqual(ctypes.sizeof(Properties),706)
        self.assertEqual(Properties.volume_id.offset,670)
        driver=FakeDriver(); volumes=VeraCryptVolumes(driver).mounted()
        self.assertEqual(len(volumes),1); self.assertEqual(volumes[0].root,'V:\\')
        self.assertEqual(volumes[0].volume_id,bytes(range(1,33)).hex()); self.assertEqual(driver.closed,1)
    def test_faked_normal_network_subst_mapping_is_rejected(self):
        for mapping in ('\\Device\\HarddiskVolume9','\\??\\C:\\folder','\\Device\\Mup\\share'):
            driver=FakeDriver(); driver.mapping=mapping
            with self.subTest(mapping=mapping),self.assertRaises(VolumeError): VeraCryptVolumes(driver).mounted()
            self.assertEqual(driver.closed,1)
    def test_unknown_patch_identity_mismatch_and_readonly_rejected(self):
        driver=FakeDriver(); driver.patch=(1,26,99,0)
        with self.assertRaises(VolumeError): VeraCryptVolumes(driver).mounted()
        driver.patch=(1,26,7,0); driver.props.volume_id[0]=99
        with self.assertRaises(VolumeError): VeraCryptVolumes(driver).mounted()
        driver=FakeDriver(); driver.props.readonly=1
        with self.assertRaises(VolumeError): VeraCryptVolumes(driver).verify('V:')
    def test_no_drive_folder_and_mount_generation_mismatch_rejected(self):
        provider=VeraCryptVolumes(FakeDriver())
        for root in ('C:','V:\\folder','\\\\server\\x'):
            with self.assertRaises(VolumeError): provider.verify(root)
        with self.assertRaises(VolumeError): provider.verify('V:',unique_id=123)


class Provider:
    def __init__(self,root):
        self.volume=MountedVolume(str(root),'1'*64,9,'fake-test-only',10**9)
        self.available=True
    def verify(self,root,*,identity=None,unique_id=None):
        if not self.available or identity and identity!=self.volume.volume_id or unique_id is not None and unique_id!=self.volume.unique_id:
            raise VolumeError('lost or replaced')
        return self.volume


class ProtectedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.old=self.base/'program'; self.vol=self.base/'volume'
        self.old.mkdir(); self.vol.mkdir(); self.provider=Provider(self.vol)
        self.old.joinpath('config.json').write_text(json.dumps({'gemini_api_keys':['synthetic-key'],
            'elevenlabs_api_keys':['synthetic-ele'],'business':['保留文案']}),encoding='utf8')
        self.space=patch('model.ProtectedStorage.shutil.disk_usage',return_value=SimpleNamespace(free=10**10))
        self.space.start(); self.addCleanup(self.space.stop)
    def migration(self): return migrate(self.old,self.provider,self.provider.volume)
    def storage(self):
        root,marker=self.migration()
        guard=VolumeSession(self.provider,self.provider.volume,root,marker=marker)
        self.addCleanup(guard.close)
        return ApplicationStorage(root,guard=guard,redirects=[(self.old/'config.json',root/'config.json')])
    def test_copy_hashes_keys_indexes_and_old_backups_remain_untouched(self):
        for folder in ('SmartImageSearch','SmartMusicSearch','NodeWorkflows','PluginData'):
            target=self.old/folder; target.mkdir(); (target/'keep.bin').write_bytes(b'index-or-cache')
        self.old.joinpath('config.json.bak-old').write_bytes(b'old credentials synthetic')
        for folder in ('runtime','MaterialLibrary','tasks'):
            target=self.old/folder; target.mkdir(); (target/'large-media.bin').write_bytes(b'do not migrate')
        before={str(x.relative_to(self.old)):x.read_bytes() for x in self.old.rglob('*') if x.is_file()}
        root,marker=self.migration()
        self.assertEqual((root/'config.json').read_bytes(),self.old.joinpath('config.json').read_bytes())
        self.assertEqual((root/'SmartImageSearch/keep.bin').read_bytes(),b'index-or-cache')
        self.assertEqual((root/'config.json.bak-old').read_bytes(),b'old credentials synthetic')
        self.assertFalse((root/'MaterialLibrary').exists()); self.assertFalse((root/'runtime').exists())
        self.assertEqual(before,{str(x.relative_to(self.old)):x.read_bytes() for x in self.old.rglob('*') if x.is_file()})
        self.assertEqual(marker,read_marker(root))
    def test_database_snapshot_includes_uncheckpointed_wal(self):
        db=sqlite3.connect(self.old/'DeliveryTodos.sqlite3'); self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL'); db.execute('create table tasks (text)')
        db.execute('insert into tasks values (?)',('保留数据',)); db.commit()
        root,_=self.migration()
        copied=sqlite3.connect(root/'DeliveryTodos.sqlite3')
        try:
            self.assertEqual(copied.execute('select text from tasks').fetchall(),[('保留数据',)])
            self.assertEqual(copied.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        finally: copied.close()
    def test_failure_and_source_change_never_publish_or_delete_sources(self):
        before=self.old.joinpath('config.json').read_bytes()
        with patch('model.ProtectedStorage.shutil.copy2',side_effect=OSError('disk failure')),self.assertRaises(OSError): self.migration()
        self.assertFalse((self.vol/'AssistantToolData').exists()); self.assertEqual(before,self.old.joinpath('config.json').read_bytes())
        self.assertTrue(list(self.vol.glob('.AssistantToolData-migration-*')))
    def test_resume_rechecks_bytes_and_never_overwrites_an_unrelated_directory(self):
        with patch('model.ProtectedStorage.shutil.copy2',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):self.migration()
        stage=next(self.vol.glob('.AssistantToolData-migration-*'))
        (stage/'config.json').write_bytes(b'partial')
        root,_=migrate(self.old,self.provider,self.provider.volume,resume=stage)
        self.assertEqual((root/'config.json').read_bytes(),(self.old/'config.json').read_bytes())
        with self.assertRaises(StorageUnavailable):migrate(self.old,self.provider,self.provider.volume,resume=root)
    def test_missing_or_corrupt_data_never_initializes_blank_keys(self):
        self.old.joinpath('config.json').write_bytes(b'{bad')
        with self.assertRaises(ValueError): self.migration()
        self.old.joinpath('config.json').unlink()
        with self.assertRaises(StorageUnavailable): self.migration()
        self.assertFalse((self.vol/'AssistantToolData').exists())
    def test_bootstrap_has_no_password_paths_keys_or_business_and_corrupt_fails_closed(self):
        root,marker=self.migration(); commit_bootstrap(self.old,marker)
        value=read_bootstrap(self.old)
        self.assertEqual(set(value),{'version','mode','storage_id','volume_id'})
        (self.old/'storage_bootstrap.json').write_text('{}')
        with self.assertRaises(StorageUnavailable): read_bootstrap(self.old)
    def test_old_config_and_temporary_siblings_redirect_to_volume(self):
        storage=self.storage()
        storage.file_at(self.old/'config.json.main.tmp').write_text('temporary')
        self.assertFalse((self.old/'config.json.main.tmp').exists())
        self.assertEqual((storage.root/'config.json.main.tmp').read_text(),'temporary')
        self.assertEqual(storage.file_at(self.old/'config.json').read_json()['business'],['保留文案'])
    def test_loss_is_latched_even_after_volume_reappears_and_no_plaintext_fallback(self):
        storage=self.storage(); before=(storage.root/'config.json').read_bytes()
        self.provider.available=False
        with self.assertRaises(StorageUnavailable): storage.file_at(self.old/'config.json').write_json({'wrong':True})
        self.provider.available=True
        with self.assertRaises(StorageUnavailable): storage.file_at(self.old/'config.json').read_json()
        self.assertEqual(before,(storage.root/'config.json').read_bytes())
        self.assertIn('synthetic-key',self.old.joinpath('config.json').read_text())
    def test_existing_handle_and_sqlite_commit_stop_on_loss(self):
        storage=self.storage()
        with storage.for_plugin('test').file('file.txt').open('w') as handle:
            handle.write('keep'); handle.flush(); self.provider.available=False
            with self.assertRaises(StorageUnavailable): handle.write('must-not-write')
        self.provider.available=True
        # New independent session, as a real application restart would use.
        guard=VolumeSession(self.provider,self.provider.volume,storage.root,marker=read_marker(storage.root))
        self.addCleanup(guard.close); new=ApplicationStorage(storage.root,guard=guard)
        db=new.for_plugin('test').file('db.sqlite3').connect(); self.addCleanup(db.close)
        db.execute('create table records (value)'); db.commit(); db.execute('insert into records values (1)')
        self.provider.available=False
        with self.assertRaises(StorageUnavailable): db.commit()
    def test_marker_swap_and_space_exhaustion_suspend_writes(self):
        storage=self.storage(); marker=read_marker(storage.root)
        (storage.root/'storage.identity.json').write_text(json.dumps({**marker,'storage_id':'2'*32}))
        with self.assertRaises(StorageUnavailable): storage.check()
        storage=self.storage_after_new_root()
        with patch('model.ProtectedStorage.shutil.disk_usage',return_value=SimpleNamespace(free=MIN_FREE-1)):
            with self.assertRaises(StorageUnavailable): storage.for_plugin('demo').file('new').write_text('x')
    def storage_after_new_root(self):
        marker=read_marker(self.vol/'AssistantToolData')
        guard=VolumeSession(self.provider,self.provider.volume,self.vol/'AssistantToolData',marker=marker)
        self.addCleanup(guard.close); return ApplicationStorage(guard.root,guard=guard)
    def test_host_default_temporary_files_stay_on_volume(self):
        storage=self.storage()
        with patch('model.AppDataStorage._APPLICATION_STORAGE',storage):
            with data_io.TemporaryDirectory(prefix='private-') as folder:
                self.assertTrue(Path(folder).is_relative_to(storage.root/'Temp'))
                data_file(Path(folder)/'private.txt').write_text('private')
            self.assertFalse(Path(folder).exists())

    def test_temporary_finalizers_do_not_unlink_after_volume_loss(self):
        storage=self.storage()
        with patch('model.AppDataStorage._APPLICATION_STORAGE',storage):
            temporary=data_io.NamedTemporaryFile(prefix='private-')
            path=Path(temporary.name); temporary.write(b'private'); temporary.flush()
            directory=data_io.TemporaryDirectory(prefix='private-')
            folder=Path(directory.name)
            self.provider.available=False
            temporary.close(); directory.cleanup()
            self.assertTrue(path.exists()); self.assertTrue(folder.exists())

    def test_bootstrap_and_marker_require_hex_ids_not_just_matching_lengths(self):
        root,marker=self.migration()
        for value in ([], None, {**marker,'storage_id':'x'*32}):
            (root/'storage.identity.json').write_text(json.dumps(value))
            with self.assertRaises(StorageUnavailable): read_marker(root)
        (self.old/'storage_bootstrap.json').write_text(json.dumps({'version':1,'mode':'veracrypt','storage_id':'x'*32,'volume_id':'1'*64}))
        with self.assertRaises(StorageUnavailable): read_bootstrap(self.old)


if __name__=='__main__': unittest.main()

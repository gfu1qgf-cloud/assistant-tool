"""Synthetic remounts: never open an old drive, move media or use real OAuth."""
import copy
import json
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace,MethodType
import tempfile
import unittest
from unittest.mock import Mock,patch

from model.AppDataStorage import ApplicationStorage,data_file,data_directory,data_io
from model.PrivateDataPaths import remounted_data_path,private_config,private_path,remap_path_fields
from model.ProtectedStorage import VolumeSession,StorageUnavailable,migrate
from model.PrivateDataCatalog import PRIVATE_KEYS
from test_protected_storage import Provider


class PureRemountTests(unittest.TestCase):
    def test_data_namespace_is_not_a_hardcoded_letter_or_a_substring_replace(self):
        for old,current in (('Z:','E:'),('R:','V:'),('e:','D:')):
            actual=remounted_data_path(old+'/AssistantToolData/GoogleSheetsCredentials.json',current+'/AssistantToolData')
            self.assertEqual(PureWindowsPath(actual),PureWindowsPath(current+'/AssistantToolData/GoogleSheetsCredentials.json'))
        for raw in ('Z:/Tasks/video.mp4','Z:/Backup/AssistantToolData/key.json',
                    'Z:/AssistantToolData-copy/key.json','//server/AssistantToolData/key.json',
                    'Z:AssistantToolData/key.json','https://example.com/AssistantToolData/key.json'):
            self.assertEqual(remounted_data_path(raw,'E:/AssistantToolData'),raw)

    def test_case_slashes_complete_prefix_and_escape(self):
        self.assertEqual(PureWindowsPath(remounted_data_path(r'z:\assistanttooldata\PluginData\test\a.json',
            'E:/AssistantToolData')),PureWindowsPath('E:/AssistantToolData/PluginData/test/a.json'))
        with self.assertRaises(ValueError):remounted_data_path('Z:/AssistantToolData/../secret','E:/AssistantToolData')


class PrivateRemountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.volume=self.base/'verified-volume'; self.volume.mkdir()
        self.root=self.volume/'AssistantToolData'; self.root.mkdir()
        self.provider=Provider(self.volume)
        self.guard=VolumeSession(self.provider,self.provider.volume,self.root)
        self.storage=ApplicationStorage(self.root,guard=self.guard)
        self.addCleanup(self.storage.close)
        self.space=patch('model.ProtectedStorage.shutil.disk_usage',return_value=SimpleNamespace(free=10**10))
        self.space.start();self.addCleanup(self.space.stop)
        self.bound=patch('model.AppDataStorage._APPLICATION_STORAGE',self.storage)
        self.bound.start();self.addCleanup(self.bound.stop)
        # Keep the same directory namespace but a nonexistent former letter.
        self.old=PureWindowsPath('Q:/').joinpath(*self.root.parts[1:])

    def old_path(self,relative):return str(self.old/relative)

    def test_files_temporary_siblings_wal_and_plugin_directories_rebase_without_old_drive_probe(self):
        target=self.root/'PluginData/test/config.json'
        target.parent.mkdir(parents=True);target.write_text('{"kept":true}',encoding='utf8')
        self.assertEqual(data_file(self.old_path('PluginData/test/config.json')).path,target)
        self.assertEqual(data_file(self.old_path('PluginData/test/config.json')).read_json(),{'kept':True})
        for suffix in ('.main.tmp','-wal','-shm','-journal'):
            self.assertEqual(self.storage.resolve(self.old_path('PluginData/test/config.json')+suffix),Path(str(target)+suffix))
        self.assertEqual(data_directory(self.old_path('PluginData/test')).path,target.parent)
        data_file(self.old_path('PluginData/test/new.json')).write_json({'safe':True})
        self.assertTrue((target.parent/'new.json').is_file())
        self.assertNotEqual(Path(self.old_path('PluginData/test/new.json')),target.parent/'new.json')

    def test_all_registered_private_config_fields_are_portable_and_runtime_values_follow_mount(self):
        values={key:self.old_path('records/'+key+'.json') for key in PRIVATE_KEYS}
        values.update(gemini_api_keys=['synthetic'],task_path='D:/Tasks',video_sorting_station_path='D:/Media',
                      prompt=self.old_path('this-is-text-not-a-path-field'),unknown={'keep':True})
        before=copy.deepcopy(values)
        runtime=private_config(values)
        portable=private_config(runtime,portable=True)
        for key in PRIVATE_KEYS:
            self.assertEqual(runtime[key],str(self.root/'records'/(key+'.json')))
            self.assertEqual(portable[key],'records/'+key+'.json')
        for key in ('gemini_api_keys','task_path','video_sorting_station_path','prompt','unknown'):
            self.assertEqual(portable[key],before[key])
        self.assertEqual(values,before)
        self.assertEqual(private_config(portable),runtime)

    def test_private_settings_do_not_accept_escape_new_plaintext_location_or_drive_relative_path(self):
        for raw in ('../credentials.json',str(self.base/'outside.json'),'Q:credentials.json','file:stream'):
            with self.subTest(raw=raw),self.assertRaises((StorageUnavailable,ValueError)):
                private_path(raw)
            with self.subTest(raw=raw),self.assertRaises((StorageUnavailable,ValueError)):
                private_config({'review_sheet_credentials_file':raw})

    def test_volume_loss_is_latched_and_never_switches_to_old_mount(self):
        (self.root/'private.json').write_text('{}')
        self.provider.available=False
        with self.assertRaises(StorageUnavailable):data_file(self.old_path('private.json')).read_json()
        with self.assertRaises(StorageUnavailable):private_config({'review_sheet_token_file':'token.json'})
        self.provider.available=True
        with self.assertRaises(StorageUnavailable):data_file(self.old_path('new.json')).write_json({})
        self.assertFalse((self.root/'new.json').exists())

    def test_only_schema_path_fields_are_remapped_not_external_tasks_code_or_text(self):
        value={'path':self.old_path('BatchTextVideo/assets/a.png'),'thumbnail':self.old_path('BatchTextVideo/assets/t.png'),
               'text':self.old_path('a-string'),'nested':[{'path':'D:/Tasks/input.mp4','code':self.old_path('code')}],
               'caption_file':'D:/Tasks/task_audio.srt'}
        remap_path_fields(value,{'path','thumbnail','caption_file'})
        self.assertEqual(value['path'],str(self.root/'BatchTextVideo/assets/a.png'))
        self.assertEqual(value['thumbnail'],str(self.root/'BatchTextVideo/assets/t.png'))
        self.assertEqual(value['text'],self.old_path('a-string'))
        self.assertEqual(value['nested'][0]['path'],'D:/Tasks/input.mp4')
        self.assertEqual(value['nested'][0]['code'],self.old_path('code'))

    def test_inventory_avatar_is_mapped_in_memory_without_erasing_or_moving_external_material(self):
        from model.InventoryManager import InventoryStore
        avatar=self.root/'PluginData/inventory/avatars/picture.png';avatar.parent.mkdir(parents=True);avatar.write_bytes(b'fixture')
        target=self.root/'InventoryState.json'
        raw={'version':4,'items':[],'materials':[],'people':[dict(id='person',name='fixture',path='D:/People',
            material_path='D:/People/Materials',avatar_path=self.old_path('PluginData/inventory/avatars/picture.png'))]}
        target.write_text(json.dumps(raw),encoding='utf8');before=target.read_bytes()
        store=InventoryStore(self.old_path('InventoryState.json'),material_root='D:/Materials')
        person=store.load()['people'][0]
        self.assertEqual(person['avatar_path'],str(avatar));self.assertEqual(person['path'],'D:/People')
        self.assertEqual(target.read_bytes(),before)
        self.provider.available=False
        with self.assertRaises(StorageUnavailable):store.load()

    def test_batch_layer_assets_and_thumbnails_rebase_before_raw_exists_checks(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        state=fresh_state();asset=self.root/'BatchTextVideo/layer-assets/a.png'
        asset.parent.mkdir(parents=True);asset.write_bytes(b'fixture')
        layer=dict(id='image',kind='image',source='fixed',path=self.old_path('BatchTextVideo/layer-assets/a.png'),
                   thumbnail=self.old_path('BatchTextVideo/layer-assets/a.thumb.png'))
        state['settings']['layers']=normalize_layers([layer])
        self.assertEqual(state['settings']['layers'][0]['path'],str(asset))
        store=Store(self.old_path('BatchTextVideo'))
        self.assertEqual(store.directory,self.root/'BatchTextVideo')
        store.path.write_text(json.dumps(state),encoding='utf8');before=store.path.read_bytes()
        self.assertEqual(store.load()['settings']['layers'][0]['path'],str(asset))
        self.assertEqual(store.path.read_bytes(),before)

    def test_private_old_mount_is_still_rejected_for_large_video_output(self):
        from model.MediaStorage import require_media_output
        with self.assertRaisesRegex(ValueError,'大型媒体'):
            require_media_output(self.old_path('output.mp4'))
        self.assertEqual(self.storage.resolve('D:/Tasks/output.mp4'),Path('D:/Tasks/output.mp4'))

    def test_existing_image_index_and_thumbnail_references_follow_mount_without_rebuilding(self):
        import numpy as np
        from model.image_retrieval.index import ImageSearchIndex
        index=ImageSearchIndex(self.old_path('SmartImageSearch'))
        picture=self.base/'external-picture.png';picture.write_bytes(b'external-fixture')
        thumbnail=self.root/'SmartImageSearch/thumbnails/keep_v2.jpg';thumbnail.write_bytes(b'existing-thumbnail')
        with index._connect() as db:
            db.execute('INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?)',
                (str(picture),1,1,'folder','fixture','test-model',np.array([1,0],dtype=np.float16).tobytes(),
                 self.old_path('SmartImageSearch/thumbnails/keep_v2.jpg'),'',1))
        hits=index.search('test-model',[1,0])
        self.assertEqual(hits[0]['thumbnail'],str(thumbnail))
        self.assertEqual(thumbnail.read_bytes(),b'existing-thumbnail')
        self.assertEqual(index.root,self.root/'SmartImageSearch')

    def test_credential_archive_profile_uses_current_mount_but_keeps_original_record(self):
        from app_plugins.builtin.codex_account_switcher.store import AccountProfile
        raw=self.old_path('CodexAccountProfiles/credentials/fixture')
        profile=AccountProfile('fixture','fixture',raw,'fixture-date')
        self.assertEqual(profile.home_path,self.root/'CodexAccountProfiles/credentials/fixture')
        self.assertEqual(profile.to_dict()['codex_home'],raw)

    def test_google_sheet_monitor_reuses_current_token_for_review_and_task_submission_without_oauth(self):
        from model.GoogleSheetsHelper import load_sheets_service
        token=self.root/'GoogleSheetsToken.json';token.write_text('{}')
        credentials=SimpleNamespace(valid=True)
        config={'review_sheet_token_file':self.old_path('GoogleSheetsToken.json'),
                'review_sheet_credentials_file':self.old_path('GoogleSheetsCredentials.json'),
                'task_submission_sheet_token_file':self.old_path('GoogleSheetsToken.json'),
                'task_submission_sheet_credentials_file':self.old_path('GoogleSheetsCredentials.json')}
        with (patch('google.oauth2.credentials.Credentials.from_authorized_user_file',return_value=credentials) as read,
              patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file') as authorize,
              patch('googleapiclient.discovery.build',return_value='fixture-service'),
              patch('model.GoogleSheetsHelper.require_online')):
            for prefix in ('review_sheet','task_submission_sheet','sheet_monitor'):
                self.assertEqual(load_sheets_service(config,prefix),'fixture-service')
            self.assertEqual(read.call_count,3)
            self.assertTrue(all(c.args[0]==str(token) for c in read.call_args_list))
            authorize.assert_not_called()

    def test_main_config_load_and_save_keep_keys_but_store_private_names_relative(self):
        from PYUI.main_pyui import MainDialog
        from model.GeminiKeyManager import GeminiKeyManager
        config=self.root/'config.json'
        original=dict(gemini_api_keys=['synthetic'],review_sheet_token_file=self.old_path('GoogleSheetsToken.json'),
                      task_path='D:/Tasks',unrelated={'keep':'yes'})
        config.write_text(json.dumps(original),encoding='utf8')
        fake=SimpleNamespace(config_name=str(config),gemini_keys=GeminiKeyManager(original),appendLog=Mock())
        fake.load_config=MethodType(MainDialog.load_config,fake)
        fake.dump=fake.load_config
        fake._save_current_config_locked=MethodType(MainDialog._save_current_config_locked,fake)
        self.assertEqual(fake.load_config()['review_sheet_token_file'],str(self.root/'GoogleSheetsToken.json'))
        self.assertTrue(MainDialog.saveCurrentConfig(fake,show_errors=False))
        saved=json.loads(config.read_text('utf8'))
        self.assertEqual(saved['review_sheet_token_file'],'GoogleSheetsToken.json')
        self.assertEqual(saved['gemini_api_keys'],['synthetic'])
        self.assertEqual(saved['task_path'],'D:/Tasks');self.assertEqual(saved['unrelated'],{'keep':'yes'})

    def test_config_hot_reload_treats_absolute_and_relative_private_setting_as_same_runtime_value(self):
        from model.ConfigHotReload import ConfigHotReload
        config=self.root/'config.json';config.write_text(json.dumps({'review_sheet_token_file':self.old_path('token.json')}))
        runtime=private_config(json.loads(config.read_text()))
        watch=ConfigHotReload(config,lambda:dict(runtime),Mock(),Mock());watch.timer.stop()
        self.addCleanup(watch.deleteLater)
        first=watch.read();config.write_text(json.dumps({'review_sheet_token_file':'token.json'}))
        self.assertEqual(watch.read(),first)

    def test_new_migration_writes_relative_private_settings_preserving_old_data(self):
        program=self.base/'program';program.mkdir()
        credentials=program/'GoogleSheetsCredentials.json';credentials.write_bytes(b'synthetic-oauth')
        original={'review_sheet_credentials_file':str(credentials),'elevenlabs_api_keys':['synthetic-ele']}
        config=program/'config.json';config.write_text(json.dumps(original));before=config.read_bytes()
        volume=self.base/'new-volume';volume.mkdir();provider=Provider(volume)
        target,_=migrate(program,provider,provider.volume)
        saved=json.loads((target/'config.json').read_text('utf8'))
        self.assertEqual(saved['review_sheet_credentials_file'],'GoogleSheetsCredentials.json')
        self.assertEqual(saved['elevenlabs_api_keys'],['synthetic-ele'])
        self.assertEqual(config.read_bytes(),before)

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import data_file
from app_plugins.builtin.batch_text_video.store import Store, ProfileStore, DEFAULTS, fresh_state
from app_plugins.builtin.batch_text_video.pools import snapshot, RESOURCE_KEYS
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog

SCHEMAS=json.loads((Path(__file__).parent/'fixtures/batch_legacy_schema.json').read_text(encoding='utf8'))


def job(identifier='one'):
    return dict(id=identifier,path='',name=identifier,title='Hello',body='A short legacy body.',status='待生成',output='',error='')


def legacy_data(schema):
    data=snapshot(fresh_state())
    data['settings']={k:copy.deepcopy(v) for k,v in data['settings'].items() if k in schema['settings_keys']}
    data['settings']['layers']=[dict(id='body',kind='body',name='正文',enabled=True),dict(id='cap',kind='caption',name='字幕',caption_preset='pop',enabled=False)]
    data['jobs']=[job()]
    data.pop('resource_orders',None)
    if schema['commit'] in {'a3b2456','49f7b47'}:data.pop('resource_pools',None)
    return data


class LegacyProjectMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'private')

    def install(self,data):
        state=fresh_state();state['settings']['layers']=[];state['jobs']=[job('current')]
        state['resource_pools']=[dict(id='private-new',name='当前池',kind='visual',items=[],next_id='')]
        state['backgrounds']=['new-a.mp4','new-b.mp4'];state['resource_orders']={'backgrounds':['new-b.mp4','new-a.mp4']}
        state['profiles'].append(dict(id='old',name='旧项目',revision=7,data=copy.deepcopy(data)))
        data_file(self.store.path).write_json(state,indent=2)
        return state

    def assert_modern(self,value):
        self.assertTrue(set(RESOURCE_KEYS)<=set(value))
        self.assertTrue(set(DEFAULTS)<=set(value['settings']))
        caption=next((l for l in value['settings']['layers'] if l['kind']=='caption'),None)
        if caption:
            self.assertIn('caption_timing',caption);self.assertIn('outline_position',caption)
        self.assertEqual(value['resource_pools'],[])
        self.assertEqual(value['resource_orders'],{})

    def test_six_historical_schemas_read_render_view_and_switch_without_inheritance(self):
        for schema in SCHEMAS:
            with self.subTest(commit=schema['commit']):
                self.install(legacy_data(schema));before=data_file(self.store.path).read_json()
                rendered=ProfileStore(self.store.directory,'old').load()
                name,view=self.store.read_profile('old')
                self.assert_modern(rendered);self.assert_modern(view)
                self.assertEqual(rendered['revision'],7);self.assertEqual(name,'旧项目')
                self.assertEqual(data_file(self.store.path).read_json(),before)
                switched=self.store.select_profile(self.store.load(),'old')
                self.assert_modern(switched)
                self.assertEqual(switched['active_profile'],'old')
                self.assertEqual(switched['profiles'][1]['revision'],7)

    def test_snapshot_of_pre_named_pool_project_does_not_require_a_switch(self):
        data=legacy_data(SCHEMAS[0]);before=copy.deepcopy(data)
        result=snapshot(data)
        self.assertEqual(data,before);self.assertEqual(result['resource_pools'],[])
        self.assertEqual(result['resource_orders'],{})

    def test_pre_profile_document_loads_and_saves_without_changing_content_or_cursor(self):
        data=legacy_data(SCHEMAS[0]);data['music']=[dict(id='song',path='old.mp3',cursor=18.5,audio_key='old',last_used='past')];data['next_music_id']='song'
        raw=dict(data,version=1,revision=12)
        data_file(self.store.path).write_json(raw)
        before=self.store.path.read_bytes();loaded=self.store.load()
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(loaded['active_profile'],'default');self.assertEqual(loaded['render_plans'],[])
        saved=self.store.save(loaded,loaded['revision'])
        self.assertEqual(saved['jobs'],data['jobs']);self.assertEqual(saved['music'],data['music'])
        self.assertEqual(saved['next_music_id'],'song')

    def test_missing_project_revision_defaults_to_zero_and_worker_commit_preserves_active(self):
        raw=self.install(legacy_data(SCHEMAS[0]));raw['profiles'][1].pop('revision')
        data_file(self.store.path).write_json(raw)
        worker=ProfileStore(self.store.directory,'old');loaded=worker.load()
        self.assertEqual(loaded['revision'],0)
        worker.save(loaded,0)
        result=self.store.load()
        self.assertEqual(result['active_profile'],'default')
        self.assertEqual(next(p['revision'] for p in result['profiles'] if p['id']=='old'),1)

    def test_delete_current_project_loads_a_normalized_replacement_without_old_resources(self):
        self.install(legacy_data(SCHEMAS[0]))
        result=self.store.delete_profile('default',expected_profile_revision=0)
        self.assertEqual(result['active_profile'],'old');self.assert_modern(result)
        self.assertEqual(result['backgrounds'],[])
        self.assertEqual(self.store.load()['settings'],result['settings'])

    def test_legacy_snapshot_with_outer_metadata_cannot_replace_project_list_or_revision(self):
        data=legacy_data(SCHEMAS[0]);data.update(version=1,revision=999,active_profile='obsolete',profiles=[],render_plans=[])
        self.install(data)
        result=self.store.delete_profile('default',expected_profile_revision=0)
        self.assertEqual(result['active_profile'],'old')
        self.assertEqual([p['id'] for p in result['profiles']],['old'])
        self.assertEqual(result['revision'],1)

    def test_delete_queue_retains_derived_legacy_copy_and_background_libraries(self):
        data=legacy_data(SCHEMAS[0]);data['jobs'][0]['path']='old-background.mp4'
        for key in ('copy_pool','image_pool','backgrounds'):data.pop(key,None)
        self.install(data)
        updated=self.store.delete_render_jobs({'old':['one']})
        old=next(p['data'] for p in updated['profiles'] if p['id']=='old')
        self.assertEqual(old['jobs'],[])
        self.assertEqual(old['copy_pool'][0]['body'],'A short legacy body.')
        self.assertEqual(old['backgrounds'],['old-background.mp4'])

    def test_legacy_queue_check_clear_and_plan_remove(self):
        for action in ('check','clear','plan'):
            with self.subTest(action=action):
                self.install(legacy_data(SCHEMAS[0]))
                self.store.set_render_checks({'old':{'one':True}})
                if action=='clear':self.store.clear_render_selection('old')
                if action=='plan':
                    plan=next(p for p in self.store.load()['render_plans'] if p['profile_id']=='old')
                    self.store.remove_render_plan(plan['id'])
                saved=data_file(self.store.path).read_json()
                old=next(p['data'] for p in saved['profiles'] if p['id']=='old')
                self.assertEqual(old['resource_pools'],[])
                self.assertEqual(old['jobs'][0]['render_checked'],action=='check')

    def test_current_and_worker_save_reload_are_idempotent_and_preserve_music_cursors(self):
        data=legacy_data(SCHEMAS[0]);data['music']=[dict(id='song',path='song.mp3',cursor=23.75,last_used='previous',audio_key='key')];data['next_music_id']='song'
        self.install(data);worker=ProfileStore(self.store.directory,'old')
        first=worker.load();self.assert_modern(first)
        saved=worker.save(first,first['revision'])
        loaded=worker.load();self.assertEqual(snapshot(loaded),snapshot(saved))
        self.assertEqual(loaded['music'],data['music']);self.assertEqual(loaded['next_music_id'],'song')
        self.assertEqual(self.store.load()['active_profile'],'default')
        switched=self.store.select_profile(self.store.load(),'old')
        again=self.store.save(switched,switched['revision'])
        self.assertEqual(snapshot(again),snapshot(switched))

    def test_old_background_motion_is_preserved_at_every_entry(self):
        data=legacy_data(SCHEMAS[0]);data['settings'].update(image_background_motion='zoom_in',image_background_strength=12,image_background_seconds=17,image_background_direction='inward')
        self.install(data)
        values=[ProfileStore(self.store.directory,'old').load(),self.store.read_profile('old')[1],self.store.select_profile(self.store.load(),'old')]
        for value in values:
            visual=value['settings']['background_visual']
            self.assertEqual(visual['visual_animation'],'zoom_in')
            self.assertEqual(visual['animation_strength'],12)
            self.assertEqual(visual['animation_zoom_seconds'],17)
            self.assertEqual(visual['animation_direction'],'inward')

    def test_global_policy_wins_but_project_canvas_colors_and_trim_values_survive(self):
        data=legacy_data(SCHEMAS[-1]);data['settings'].update(aspect='custom',canvas_width=640,canvas_height=480,body_color='#123456',music_volume=31,video_encoder='qsv',export_workers=63)
        data['resource_orders']={'backgrounds':['b.mp4','a.mp4']};data['backgrounds']=['a.mp4','b.mp4']
        self.install(data)
        state=ProfileStore(self.store.directory,'old').load();settings=state['settings']
        self.assertEqual(settings['video_encoder'],'cpu');self.assertEqual(settings['export_workers'],2)
        self.assertEqual((settings['canvas_width'],settings['canvas_height']),(640,480))
        self.assertEqual(settings['body_color'],'#123456');self.assertEqual(settings['music_volume'],31)
        self.assertEqual(state['resource_orders'],data['resource_orders'])

    def test_loading_current_project_is_lazy_about_unopened_corrupt_projects(self):
        self.install(legacy_data(SCHEMAS[0]));raw=data_file(self.store.path).read_json()
        raw['profiles'][1]['data']['settings']='broken'
        data_file(self.store.path).write_json(raw)
        before=self.store.path.read_bytes()
        self.assertEqual(self.store.load()['active_profile'],'default')
        with self.assertRaises(ValueError):self.store.read_profile('old')
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_invalid_selected_snapshot_refuses_writes_and_deletion(self):
        for key,value in (('settings',None),('music',None),('jobs',None)):
            with self.subTest(key=key):
                data=legacy_data(SCHEMAS[0]);data[key]=value;self.install(data)
                before=self.store.path.read_bytes()
                with self.assertRaises(ValueError):self.store.select_profile(self.store.load(),'old')
                self.assertEqual(self.store.path.read_bytes(),before)
                with self.assertRaises(ValueError):self.store.delete_render_jobs({'default':['current'],'old':['one']})
                self.assertEqual(self.store.path.read_bytes(),before)

    def test_legacy_ui_open_switch_edit_save_and_queue_delete(self):
        self.install(legacy_data(SCHEMAS[0]))
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=self.store)
            try:
                dialog.show();dialog.switch_profile('old');self.app.processEvents()
                dialog.jobs.setCurrentCell(0,0);dialog.body.setPlainText('Edited legacy body.')
                self.assertTrue(dialog.persist())
                self.assertEqual(self.store.load()['jobs'][0]['body'],'Edited legacy body.')
                dialog.open_global_queue();flat=dialog.render_queue_panel.flat
                flat.search.setText('旧项目');flat.table.setCurrentCell(flat.rows[('old','one')],0)
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes),patch.object(QtWidgets.QMessageBox,'warning') as warning:flat.delete.click()
                warning.assert_not_called();self.assertEqual(self.store.load()['jobs'],[])
            finally:
                dialog.close();dialog.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_migrated_backgroundless_project_previews_and_exports_with_real_ffmpeg(self):
        data=legacy_data(SCHEMAS[0]);data['settings'].update(layers=[],music_enabled=False,use_voice=False,unvoiced_duration_mode='fixed',unvoiced_duration_seconds=1,aspect='custom',canvas_width=320,canvas_height=480,output_dir=str(self.root/'out'))
        self.install(data);state=ProfileStore(self.store.directory,'old').load()
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe,resolve_tools
        for preview in (True,False):
            output,_=render_static_text(state['jobs'][0],state,self.root/('preview' if preview else 'export'),preview=preview)
            self.assertTrue(Path(output).is_file())
            _,ffprobe=resolve_tools(os.environ.get('NODE_FFMPEG',''));info=probe(output,ffprobe)
            self.assertEqual((info['video']['width'],info['video']['height']),(320,480))

    def test_user_project_schemas_can_be_normalized_read_only_in_memory(self):
        source=os.environ.get('LZX_LEGACY_AUDIT_SOURCE')
        if not source:self.skipTest('Optional read-only actual schema audit')
        path=Path(source);before=path.read_bytes();state=json.loads(before.decode('utf-8-sig'))
        from app_plugins.builtin.batch_text_video.project_migration import normalize_project
        checked=0
        for profile in state['profiles']:
            with self.subTest(profile_id=profile['id']):
                data=state if profile['id']==state['active_profile'] else profile.get('data')
                if data is None:continue
                normalized=normalize_project(copy.deepcopy(data),DEFAULTS)
                self.assertTrue(set(RESOURCE_KEYS)<=set(normalized));checked+=1
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),hashlib.sha256(before).digest())
        target=os.environ.get('LZX_LEGACY_AUDIT_REPORT')
        if target:Path(target).write_text(json.dumps(dict(checked_profiles=checked,state_sha256=hashlib.sha256(before).hexdigest(),read_only=True)),encoding='utf8')

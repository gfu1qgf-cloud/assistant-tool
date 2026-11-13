import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import numpy as np
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video import shared_music as shared,engine,render_assignment as assignment
from app_plugins.builtin.batch_text_video.store import Store,ProfileStore,fresh_state
from app_plugins.builtin.batch_text_video.custom_pools import music_source,pool_choices,get_pool
from app_plugins.builtin.batch_text_video.pools import snapshot
from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.shared_music_ui import SharedMusicDialog,MusicModel,prepare_paths,sync_owner
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class SharedMusicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=Store(self.root/'data');self.paths=[]
        for i in range(2):
            path=self.root/f'music{i}.wav'
            samples=(np.sin(2*np.pi*(440+440*i)*np.arange(48000)/8000)*12000).astype(np.int16)
            with wave.open(str(path),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(samples.tobytes())
            self.paths.append(str(path))
        state=fresh_state();state['settings'].update(music_pool_id=shared.ID,skip_silence=False,use_voice=False,
            layers=[dict(v,enabled=False) for v in DEFAULT_LAYERS],export_workers=2)
        state['jobs']=[dict(id='job',name='shared',title='',body='',path='',status='待生成')]
        state=self.store.save(state,0);pool=copy.deepcopy(shared.get(state));shared.add_paths(pool,self.paths)
        self.store.save_shared_music(pool,pool['revision'])
        state=self.store.load();self.first=state['active_profile'];state=self.store.create_profile(state,'Second',clone=True);self.second=state['active_profile']

    def prepared(self,state):
        job=state['jobs'][0]
        with patch.object(engine,'clean_music',side_effect=lambda path,*_args:(Path(path),6.,'test-key')), \
             patch('app_plugins.builtin.batch_text_video.music_preparation.current_music_session',return_value=None):
            music=engine.choose_music(state,1.,self.root/'cache','ffmpeg','ffprobe',None,lambda _:None)
        return SimpleNamespace(music=music,sequence_plan=[],output=self.root/'test.mp4',
            assignment=assignment.capture(job,state,state['settings']['layers'],music,''))

    def test_global_library_is_not_cloned_and_all_profiles_reference_one_pool(self):
        state=self.store.load();self.assertEqual(len(shared.get(state)['items']),2)
        self.assertNotIn(shared.KEY,snapshot(state))
        self.assertTrue(all(shared.KEY not in p['data'] for p in state['profiles']))
        for identifier in (self.first,self.second):
            view=ProfileStore(self.store.directory,identifier).load()
            self.assertEqual(music_source(view)[2]['id'],shared.ID)
            self.assertIn(shared.ID,dict(pool_choices(view,'music')))
            self.assertNotIn(shared.ID,dict(pool_choices(view,'voice')))
        self.assertEqual(len(shared.get(self.store.read_profile(self.first)[1])['items']),2)

    def test_reservation_advances_across_projects_and_retry_does_not_advance(self):
        for index,identifier in enumerate((self.first,self.second,self.first)):
            store=ProfileStore(self.store.directory,identifier);state=store.load()
            if index==2:
                state['jobs'].append(dict(state['jobs'][0],id='another',render_assignment=None,shared_music_assignment=None))
                state['jobs']=[state['jobs'][1]];state=store.save(state,state['revision'])
            prepared=self.prepared(state);expected=self.paths[index%2]
            self.assertEqual(prepared.assignment['music']['item']['path'],expected)
            saved,ok=assignment.reserve(store,state,state['jobs'][0]['id'],prepared);self.assertTrue(ok)
            self.assertIsNone(prepared.music);revision=shared.get(saved)['revision']
            job,retry=assignment.apply(saved['jobs'][0],saved)
            retry_plan=self.prepared(retry)
            self.assertEqual(retry_plan.music['start'],float(index//2))
            self.assertEqual(retry_plan.assignment['music']['item']['path'],expected)
            again,_=assignment.reserve(store,saved,job['id'],retry_plan)
            self.assertEqual(shared.get(again)['revision'],revision)

    def test_racing_profiles_detect_stale_pick_then_can_reprepare(self):
        first=ProfileStore(self.store.directory,self.first);second=ProfileStore(self.store.directory,self.second)
        a,b=first.load(),second.load();pa,pb=self.prepared(a),self.prepared(b)
        assignment.reserve(first,a,'job',pa)
        with self.assertRaises(shared.SharedMusicChanged):assignment.reserve(second,b,'job',pb)
        latest=second.load();pc=self.prepared(latest)
        self.assertEqual(pc.assignment['music']['item']['path'],self.paths[1])
        assignment.reserve(second,latest,'job',pc)
        self.assertEqual([v['cursor'] for v in shared.get(self.store.load())['items']],[1.,1.])

    def test_project_save_switch_undo_and_delete_cannot_reset_global_progress(self):
        stale=self.store.load();worker=ProfileStore(self.store.directory,self.first);view=worker.load()
        saved,_=assignment.reserve(worker,view,'job',self.prepared(view))
        stale['settings']['music_volume']=63;merged=self.store.save(stale,stale['revision'])
        self.assertEqual(shared.get(merged),shared.get(saved))
        selected=self.store.select_profile(merged,self.first)
        clone=self.store.create_profile(selected,'Third',clone=True)
        self.assertEqual(shared.get(clone),shared.get(saved))
        current=self.store.load();profile=next(p for p in current['profiles'] if p['id']==self.second)
        deleted=self.store.delete_profile(self.second,expected_profile_revision=profile.get('revision',0))
        self.assertEqual(shared.get(deleted),shared.get(saved))

    def test_profile_save_keeps_new_global_edits_and_edit_conflicts_are_explicit(self):
        worker=ProfileStore(self.store.directory,self.first);stale=worker.load();pool=shared.get(self.store.load())
        pool['next_id']=pool['items'][1]['id'];changed=self.store.save_shared_music(pool,pool['revision'])
        stale['settings']['music_volume']=17;result=worker.save(stale,stale['revision'])
        self.assertEqual(shared.get(result),changed)
        with self.assertRaises(shared.SharedMusicChanged):self.store.save_shared_music(pool,pool['revision'])
        raw=json.loads(self.store.path.read_text('utf8'));self.assertNotIn('_shared_music_dirty',raw)

    def test_clone_takes_new_shared_music_but_original_retry_keeps_its_song(self):
        state=self.store.load();prepared=self.prepared(state)
        original,_=assignment.reserve(self.store,state,'job',prepared)
        clone=self.store.create_profile(original,'Clone with new music',clone=True)
        self.assertNotIn('shared_music_assignment',clone['jobs'][0])
        self.assertTrue(clone['jobs'][0]['render_assignment']['music_pending'])
        job,view=assignment.apply(clone['jobs'][0],clone)
        next_music=self.prepared(view)
        self.assertEqual(next_music.assignment['music']['item']['path'],self.paths[1])
        clone,_=assignment.reserve(self.store,clone,'job',next_music)
        self.assertNotIn('music_pending',clone['jobs'][0]['render_assignment'])
        _job,old=assignment.apply(original['jobs'][0],original)
        self.assertEqual(self.prepared(old).assignment['music']['item']['path'],self.paths[0])

    def test_multiple_libraries_have_independent_playheads_and_merge_without_conflict(self):
        extra=self.store.create_shared_music('圣歌');shared.add_paths(extra,self.paths)
        extra=self.store.save_shared_music(extra,extra['revision'])
        second=ProfileStore(self.store.directory,self.second);b=second.load();b['settings']['music_pool_id']=extra['id'];b=second.save(b,b['revision'])
        first=ProfileStore(self.store.directory,self.first);a=first.load();pa,pb=self.prepared(a),self.prepared(b)
        self.assertEqual(pa.music['pool_id'],shared.ID);self.assertEqual(pb.music['pool_id'],extra['id'])
        assignment.reserve(first,a,'job',pa);assignment.reserve(second,b,'job',pb)
        current=self.store.load()
        self.assertEqual(shared.get(current)['items'][0]['cursor'],1.)
        self.assertEqual(shared.get(current,extra['id'])['items'][0]['cursor'],1.)
        self.assertEqual(len(current[shared.KEY]),2)
        self.assertTrue({shared.ID,extra['id']}<=set(dict(pool_choices(current,'music'))))

    def test_library_create_rename_delete_validates_names_and_preserves_bindings(self):
        before=self.store.path.read_bytes()
        for name in ('','x'*81,'共享背景音乐'):
            with self.assertRaises(ValueError):self.store.create_shared_music(name)
            self.assertEqual(before,self.store.path.read_bytes())
        extra=self.store.create_shared_music('教堂');extra['name']='庄严'
        extra=self.store.save_shared_music(extra,extra['revision'])
        self.assertEqual(shared.get(self.store.load(),extra['id'])['name'],'庄严')
        with self.assertRaisesRegex(ValueError,'仍在使用'):self.store.delete_shared_music(shared.ID,shared.get(self.store.load())['revision'])
        self.store.delete_shared_music(extra['id'],extra['revision'])
        with self.assertRaises(ValueError):shared.get(self.store.load(),extra['id'])

    def test_node_rotation_can_reserve_only_global_music_before_ordered_settlement(self):
        worker=ProfileStore(self.store.directory,self.first);state=worker.load();prepared=self.prepared(state)
        prepared.sequence_plan=[dict(node_source=True)]
        saved,ok=assignment.reserve(worker,state,'job',prepared)
        self.assertFalse(ok);self.assertIsNone(prepared.music);self.assertTrue(prepared.sequence_plan)
        self.assertNotIn('render_assignment',saved['jobs'][0]);self.assertIn('shared_music_assignment',saved['jobs'][0])
        job,retry=assignment.apply(saved['jobs'][0],saved)
        selected=self.prepared(retry);self.assertEqual(selected.music['start'],0.)
        again,_=assignment.reserve(worker,saved,'job',selected)
        self.assertEqual(shared.get(again)['revision'],shared.get(saved)['revision'])

    def test_append_rebases_on_live_pool_without_resurrecting_deleted_records(self):
        old=shared.get(self.store.load());value=copy.deepcopy(old)
        added=self.root/'extra.mp3';added.write_bytes(b'fake');shared.add_paths(value,[added])
        additions=[v for v in value['items'] if v['path']==str(added)]
        latest=copy.deepcopy(old);latest['items'].pop(0);self.store.save_shared_music(latest,latest['revision'])
        result=self.store.save_shared_music(value,old['revision'],additions=additions)
        self.assertEqual([v['path'] for v in result['items']],[self.paths[1],str(added)])

    def test_preview_does_not_consume_and_all_registered_shared_sources_are_protected(self):
        state=ProfileStore(self.store.directory,self.first).load();before=self.store.path.read_bytes()
        self.prepared(state);self.prepared(state)
        self.assertEqual(before,self.store.path.read_bytes())
        self.assertTrue(set(self.paths)<=set(map(str,engine.source_paths(state))))

    def test_legacy_missing_shared_field_loads_without_writes_and_corruption_is_not_discarded(self):
        raw=json.loads(self.store.path.read_text('utf8'));raw.pop(shared.KEY)
        self.store.path.write_text(json.dumps(raw),encoding='utf8');before=self.store.path.read_bytes()
        self.assertEqual(shared.get(self.store.load())['items'],[]);self.assertEqual(before,self.store.path.read_bytes())
        for value in (None,dict(shared.fresh(),revision=-1),dict(shared.fresh(),items=[dict(id='x',path='x',cursor=float('nan'))])):
            with self.assertRaises(ValueError):shared.normalize({shared.KEY:[value]})
        with self.assertRaises(ValueError):get_pool(self.store.load(),shared.ID,'voice')

    def test_folder_import_deduplicates_and_virtual_model_supports_large_libraries(self):
        folder=self.root/'folder';folder.mkdir();(folder/'nested').mkdir()
        (folder/'copy.wav').write_bytes(b'fake');(folder/'nested'/'track.mp3').write_bytes(b'fake');(folder/'ignore.png').write_bytes(b'fake')
        pool,count=prepare_paths(shared.fresh(),[folder,folder/'copy.wav'],threading.Event())
        self.assertEqual(count,2);model=MusicModel();self.addCleanup(model.deleteLater)
        pool['items']=[dict(id=str(i),path=f'X:/music/{i}.wav',cursor=0) for i in range(10000)]
        model.set_pool(pool);self.assertEqual(model.rowCount(),10000);self.assertEqual(model.data(model.index(9999,0)),'9999.wav')

    def test_manager_import_remove_reset_bind_and_source_menu(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            owner=BatchTextVideoDialog(store=self.store);dialog=SharedMusicDialog(owner)
            try:
                card=owner.audio_cards['music'];self.assertGreaterEqual(card.pool.findData(shared.ID),0)
                self.assertEqual(card.info.text().split(' · ')[-1],'2 条')
                extra=self.root/'new.wav';extra.write_bytes(b'fake');dialog.import_paths([extra]);deadline=time.monotonic()+5
                while dialog.future and time.monotonic()<deadline:self.app.processEvents();dialog.finish_import();time.sleep(.01)
                self.assertIsNone(dialog.future);self.assertEqual(len(dialog.pool['items']),3)
                dialog.table.selectRow(2);dialog.remove();self.assertEqual(len(dialog.pool['items']),2);self.assertTrue(extra.exists())
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):dialog.reset()
                order=[v['id'] for v in dialog.pool['items']];dialog.shuffle()
                self.assertEqual(dialog.shuffle_button.text(),'恢复原顺序');dialog.shuffle()
                self.assertEqual([v['id'] for v in dialog.pool['items']],order)
                dialog.use();self.assertEqual(self.store.load()['settings']['music_pool_id'],shared.ID)
                if os.environ.get('LZX_SHARED_MUSIC_QA'):
                    dialog.show();self.app.processEvents();dialog.grab().save(str(Path(os.environ['LZX_SHARED_MUSIC_QA'])/'shared-music.png'))
            finally:
                dialog.done(0);dialog.executor.shutdown(wait=True);dialog.deleteLater();owner.cancel_work();owner.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_sync_of_global_data_cannot_hide_a_conflicting_project_edit(self):
        stale=self.store.load();fresh=self.store.load();fresh['settings']['music_volume']=27;self.store.save(fresh,fresh['revision'])
        owner=SimpleNamespace(store=self.store,state=stale,audio_cards={});before=stale['revision'];sync_owner(owner)
        self.assertEqual(owner.state['revision'],before)
        with self.assertRaises(ValueError):self.store.save(stale,stale['revision'])

    def test_manager_can_create_select_rename_and_bind_multiple_libraries(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            owner=BatchTextVideoDialog(store=self.store);dialog=SharedMusicDialog(owner)
            try:
                with patch.object(QtWidgets.QInputDialog,'getText',return_value=('教堂',True)):dialog.create_library()
                identifier=dialog.pool['id'];self.assertNotEqual(identifier,shared.ID);self.assertEqual(dialog.libraries.count(),2)
                with patch.object(QtWidgets.QInputDialog,'getText',return_value=('圣歌',True)):dialog.rename_library()
                self.assertEqual(dialog.libraries.currentText(),'圣歌');dialog.use()
                self.assertEqual(self.store.load()['settings']['music_pool_id'],identifier)
                dialog.libraries.setCurrentIndex(dialog.libraries.findData(shared.ID));self.assertEqual(len(dialog.pool['items']),2)
                self.assertEqual(owner.audio_cards['music'].pool.findData(identifier)>=0,True)
                if os.environ.get('LZX_SHARED_MUSIC_QA'):
                    dialog.show();self.app.processEvents();dialog.grab().save(str(Path(os.environ['LZX_SHARED_MUSIC_QA'])/'shared-music-multiple.png'))
            finally:
                dialog.done(0);dialog.executor.shutdown(wait=True);dialog.deleteLater();owner.cancel_work();owner.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_project_create_and_rename_errors_show_visible_action_warning(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            owner=BatchTextVideoDialog(store=self.store)
            try:
                count=len(owner.state['profiles'])
                for name,part in (('', '不能为空'),('Second','已存在'),('x'*81,'不能超过')):
                    with patch.object(QtWidgets.QInputDialog,'getText',return_value=(name,True)):owner.create_named_profile(False)
                    box=owner._action_warning;self.assertEqual(box.windowTitle(),'未创建项目');self.assertIn(part,box.text())
                    self.assertEqual(len(self.store.load()['profiles']),count);self.assertTrue(box.isVisible())
                with patch.object(QtWidgets.QInputDialog,'getText',return_value=('',True)):owner.rename_profile()
                self.assertEqual(owner._action_warning.windowTitle(),'未重命名项目')
                if os.environ.get('LZX_SHARED_MUSIC_QA'):
                    owner._action_warning.grab().save(str(Path(os.environ['LZX_SHARED_MUSIC_QA'])/'project-error-warning.png'))
            finally:
                owner.cancel_work();owner.deleteLater();QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_pipeline_retries_shared_conflict_instead_of_failing(self):
        from app_plugins.builtin.batch_text_video.export_pipeline import execute_parallel_batch
        worker=ProfileStore(self.store.directory,self.first);state=worker.load();attempts=[];messages=[]
        @contextmanager
        def preparer(job,value,directory,token,*args,**kwargs):
            prepared=self.prepared(value);attempts.append(prepared.music['path'])
            prepared.overwrite=False
            if len(attempts)==1:
                other=ProfileStore(self.store.directory,self.second);other_state=other.load()
                assignment.reserve(other,other_state,'job',self.prepared(other_state))
            prepared.encode=lambda:None
            def publish():prepared.output.write_bytes(b'result');return prepared.output
            prepared.publish=publish
            yield prepared
        result,saved=execute_parallel_batch(worker,state,['job'],threading.Event(),lambda *a:None,messages.append,lambda *a:None,preparer)
        self.assertEqual((result['completed'],result['failed']),(1,0),result)
        self.assertEqual(attempts,self.paths);self.assertTrue(any('自动重新分配' in v for v in messages))

    def test_two_concurrent_project_pipelines_keep_unique_reservations(self):
        from app_plugins.builtin.batch_text_video.export_pipeline import execute_parallel_batch
        barrier=threading.Barrier(2);claims=[];lock=threading.Lock()
        def run(identifier):
            worker=ProfileStore(self.store.directory,identifier);state=worker.load()
            state['jobs']=[dict(state['jobs'][0],id=str(i),name=str(i)) for i in range(5)]
            state=worker.save(state,state['revision']);first=True
            @contextmanager
            def preparer(job,value,directory,token,*args,**kwargs):
                nonlocal first
                prepared=self.prepared(value);prepared.output=self.root/(identifier+job['id']+'.mp4');prepared.overwrite=False
                if first:first=False;barrier.wait(4)
                def encode():
                    with lock:claims.append((prepared.assignment['music']['item']['path'],prepared.assignment['music']['item']['cursor']))
                    time.sleep(.005)
                def publish():prepared.output.write_bytes(b'generated');return prepared.output
                prepared.encode=encode;prepared.publish=publish;yield prepared
            return execute_parallel_batch(worker,state,[j['id'] for j in state['jobs']],threading.Event(),lambda *a:None,lambda *a:None,lambda *a:None,preparer)[0]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(run,(self.first,self.second)))
        for result in results:self.assertEqual((result['completed'],result['failed']),(5,0),result)
        self.assertEqual(len(claims),10);self.assertEqual(len(set(claims)),10)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Explicit real FFmpeg required')
    def test_actual_exports_from_two_projects_use_different_shared_music(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        ffmpeg=os.environ['NODE_FFMPEG']
        for index,identifier in enumerate((self.first,self.second)):
            worker=ProfileStore(self.store.directory,identifier);state=worker.load()
            state['settings'].update(ffmpeg_path=ffmpeg,video_encoder='cpu',use_voice=False,skip_silence=False,
                unvoiced_duration_mode='fixed',unvoiced_duration_seconds=1,output_dir=str(self.root/str(index)),
                aspect='custom',canvas_width=320,canvas_height=180,music_volume=100,fade_seconds=0)
            layer=new_text_component();layer.update(text='Music',font='Arial',font_min=30,font_max=30)
            state['settings']['layers'].append(layer)
            state=worker.save(state,state['revision']);job=state['jobs'][0]
            with engine.prepare_static_text(job,state,self.root/'temporary') as prepared:
                state,_=assignment.reserve(worker,state,'job',prepared)
                prepared.encode();output=prepared.publish()
            worker.completed(state,'job',output,prepared.music,prepared.sequence_plan)
            run=subprocess.run([ffmpeg,'-v','error','-i',str(output),'-vn','-ar','8000','-ac','1','-f','s16le','pipe:1'],
                capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(run.returncode,0,run.stderr.decode('utf8','replace'))
            audio=np.frombuffer(run.stdout,np.int16).astype(float);self.assertGreater(len(audio),4000)
            spectrum=np.abs(np.fft.rfft(audio[:4000]));peak=np.argmax(spectrum)*8000/4000
            self.assertLess(abs(peak-(440+440*index)),5)


if __name__=='__main__':unittest.main()

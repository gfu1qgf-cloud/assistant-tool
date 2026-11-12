import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from qt_compat import QtWidgets,QtGui
from app_plugins.builtin.batch_text_video.store import fresh_state,Store
from app_plugins.builtin.batch_text_video.custom_pools import create_pool
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources,commit_sequences
from app_plugins.builtin.batch_text_video.pool_exclusion import attach,peers

FF=os.environ.get('NODE_FFMPEG','')

def visual(identifier,pool,cursor=0):return dict(id=identifier,kind='image',name=identifier,enabled=True,opacity=100,source='pool',pool_id=pool,
    path='',sequence_items=[],sequence_cursor=cursor,x=50,y=50,width=100,height=100,matting_enabled=False)

class ExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.paths=[]
        for name in ('red.png','blue.png','white.png'):
            path=self.root/name;path.write_bytes(b'original');self.paths.append(str(path))
        self.state=fresh_state()

    def tearDown(self):self.temp.cleanup()

    def pool(self,name,paths):
        pool=create_pool(self.state,name,'visual');pool['items']=[dict(id=str(i),path=path,name=Path(path).name) for i,path in enumerate(paths)];return pool['id']

    def bound(self,pools):
        layers=normalize_layers([visual('layer'+str(i),pid) for i,pid in enumerate(pools)])
        return bind_pool_layers(layers,self.state)

    def test_off_keeps_existing_selection_and_on_is_symmetric_and_preview_is_pure(self):
        a=self.pool('A',self.paths);b=self.pool('B',self.paths)
        layers=self.bound([a,b]);selected,_=resolve_sources(layers);self.assertEqual(selected[0]['path'],selected[1]['path'])
        self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        layers=self.bound([a,b]);before=copy.deepcopy(layers)
        for _ in range(2):
            selected,plan=resolve_sources(layers)
            self.assertNotEqual(selected[0]['path'],selected[1]['path']);self.assertEqual(layers,before)
        step=next(s for s in plan if 'selected_cursor' in s)
        self.assertEqual((step['cursor'],step['selected_cursor'],step['next_cursor']),(0,1,2))
        advanced=commit_sequences(layers,plan)
        self.assertEqual([l['sequence_cursor'] for l in advanced if l['id'].startswith('layer')],[1,2])
        self.assertEqual(peers(self.state['settings'])[b],{a})

    def test_joint_search_avoids_false_insufficient_result_from_greedy_order(self):
        a=self.pool('A',self.paths[:2]);b=self.pool('B',self.paths[:1])
        self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        layers=self.bound([a,b]);selected,_=resolve_sources(layers)
        self.assertEqual([l['path'] for l in selected if l['id'].startswith('layer')],list(reversed(self.paths[:2])))

    def test_pairwise_constraints_are_not_accidentally_transitive(self):
        a=self.pool('A',self.paths[:1]);b=self.pool('B',self.paths[1:2]);c=self.pool('C',self.paths[:1])
        self.state['settings']['pool_exclusions']={b:dict(enabled=True,pools=[a,c])}
        selected,_=resolve_sources(self.bound([a,b,c]));self.assertEqual(selected[0]['path'],selected[2]['path'])

    def test_insufficient_or_conflicting_frozen_sources_fail_without_advancing_or_reassigning(self):
        a=self.pool('A',self.paths[:1]);b=self.pool('B',self.paths[:1]);self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        layers=self.bound([a,b]);before=copy.deepcopy(layers)
        with self.assertRaisesRegex(ValueError,'互斥素材不足'):resolve_sources(layers)
        self.assertEqual(layers,before)
        for layer in layers:layer.update(source='fixed',path=self.paths[0],sequence_items=[])
        with self.assertRaisesRegex(ValueError,'固定素材重复'):resolve_sources(layers)
        layers[1]['enabled']=False;self.assertEqual(resolve_sources(layers)[0][0]['path'],self.paths[0])

    def test_stale_plan_and_changed_rules_cannot_commit(self):
        a=self.pool('A',self.paths);b=self.pool('B',self.paths);self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        layers=self.bound([a,b]);_,plan=resolve_sources(layers)
        changed=copy.deepcopy(layers);changed[1]['sequence_items'].reverse()
        with self.assertRaises(ValueError):commit_sequences(changed,plan)
        self.state['settings']['pool_exclusions'][a]['enabled']=False
        changed=attach(layers,self.state)
        with self.assertRaises(ValueError):commit_sequences(changed,plan)

    def test_background_reservation_and_design_preview_agree_with_export_selection(self):
        from app_plugins.builtin.batch_text_video.design_preview import resolve_preview_sources
        b=self.pool('B',self.paths[:2]);self.state['settings'].update(background_pool_id='builtin:background',
            pool_exclusions={'builtin:background':dict(enabled=True,pools=[b])})
        layers=self.bound([b]);job=dict(path=self.paths[0]);selected,_=resolve_sources(layers,job=job)
        preview,notices=resolve_preview_sources(layers,job=job)
        self.assertFalse(notices);self.assertEqual(preview[0]['path'],self.paths[1])
        self.assertEqual(preview[0]['path'],selected[0]['path'])
        self.assertEqual(resolve_sources(preview)[0][0]['path'],self.paths[1])

    def test_disabled_rules_and_unused_peers_do_not_resolve_every_file(self):
        from unittest.mock import patch
        a=self.pool('A',self.paths);b=self.pool('B',self.paths)
        self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        layers=self.bound([a])
        with patch('app_plugins.builtin.batch_text_video.store.file_identity',side_effect=AssertionError('unused relation must not scan paths')):
            resolve_sources(layers)

    def test_dialog_keeps_choices_when_disabled_and_store_round_trip_keeps_rules(self):
        from app_plugins.builtin.batch_text_video.pool_exclusion_ui import PoolExclusionDialog
        a=self.pool('A',self.paths);b=self.pool('B',self.paths)
        self.state['settings']['pool_exclusions']={a:dict(enabled=True,pools=[b])}
        dialog=PoolExclusionDialog(self.state,b)
        self.assertTrue(dialog.enabled.isChecked());self.assertIn(a,dialog.value()['pools'])
        dialog.enabled.setChecked(False);self.assertFalse(dialog.value()['enabled']);self.assertIn(a,dialog.value()['pools'])
        if os.environ.get('LZX_TOTAL_LIST_QA'):
            dialog.show();self.app.processEvents();dialog.grab().save(str(Path(os.environ['LZX_TOTAL_LIST_QA'])/'pool-exclusion.png'))
        dialog.close();dialog.deleteLater()
        store=Store(self.root/'private');self.state['settings']['layers']=self.bound([a,b]);saved=store.save(self.state,0)
        self.assertEqual(store.load()['settings']['pool_exclusions'],self.state['settings']['pool_exclusions'])
        clone=store.create_profile(saved,'copy',clone=True);self.assertEqual(clone['settings']['pool_exclusions'],self.state['settings']['pool_exclusions'])


@unittest.skipUnless(FF,'explicit local FFmpeg required')
class RealExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_parallel_export_and_failed_retry_reservations_never_pick_the_same_source(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.engine import prepare_static_text
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=[]
            for name,color in (('red.png','red'),('blue.png','blue')):
                path=root/name;image=QtGui.QImage(32,32,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtGui.QColor(color));image.save(str(path));paths.append(str(path))
            state=fresh_state();pools=[]
            for name in ('A','B'):
                pool=create_pool(state,name,'visual');pool['items']=[dict(id=str(i),name=str(i),path=p) for i,p in enumerate(paths)];pools.append(pool['id'])
            layers=normalize_layers([visual('left',pools[0]),visual('right',pools[1])])
            layers[0].update(x=25,width=50);layers[1].update(x=75,width=50)
            state['settings'].update(pool_exclusions={pools[0]:dict(enabled=True,pools=[pools[1]])},layers=layers,
                ffmpeg_path=FF,aspect='custom',canvas_width=128,canvas_height=128,unvoiced_duration_mode='fixed',unvoiced_duration_seconds=.4,
                music_enabled=False,output_dir=str(root/'out'),output_destination='directory',export_workers=2,margin_top=0,margin_bottom=0,margin_x=0)
            state['settings']['layers']=bind_pool_layers(layers,state)
            state['jobs']=[dict(id=str(i),path='',name='video'+str(i),title='',body='',status='待生成',output='',error='') for i in range(4)]
            store=Store(root/'private');state=store.save(state,0)
            result,saved=execute_batch(store,state,['0','1','2','3'],threading.Event(),lambda *_:None,lambda *_:None,lambda *_:None)
            self.assertEqual((result['completed'],result['failed']),(4,0),result)
            for job in saved['jobs']:
                chosen=job['render_assignment']['sources'];self.assertNotEqual(chosen['left']['value'],chosen['right']['value'])
                raw=subprocess.run([FF,'-nostdin','-v','error','-i',job['output'],'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,check=True,timeout=15).stdout
                left=raw[(64*128+32)*3:(64*128+32)*3+3];right=raw[(64*128+96)*3:(64*128+96)*3+3]
                self.assertNotEqual(left[0]>left[2],right[0]>right[2])
            # Preparing a retry keeps the frozen two paths, rather than selecting
            # a fresh combination from the now-advanced pool cursors.
            first=saved['jobs'][0]
            with prepare_static_text(first,saved,root/'private',threading.Event()) as prepared:
                self.assertEqual(prepared.assignment['sources'],first['render_assignment']['sources'])

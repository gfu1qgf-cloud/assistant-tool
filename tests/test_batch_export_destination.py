import copy
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video import export_destination as routing
from app_plugins.builtin.batch_text_video.background import validate_batch_outputs,execute_batch
from app_plugins.builtin.batch_text_video.engine import output_folder
from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
from app_plugins.builtin.batch_text_video.store import fresh_state,Store

FF=os.environ.get('NODE_FFMPEG','')


class ExportDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def state(self):
        state=fresh_state();state['settings'].update(output_dir=str(self.root/'成品'),output_destination='relative')
        state['jobs']=[dict(id=str(i),task_dir=str(self.root/'源项目'/'reels'/str(i)),path='',name='same') for i in range(3)]
        return state

    def test_explicit_folder_wins_over_task_and_source_without_changing_original_binding(self):
        state=self.state();state['settings']['output_destination']='directory';job=state['jobs'][0]
        before=copy.deepcopy(job)
        self.assertEqual(output_folder(job,state['settings']),str(self.root/'成品'))
        self.assertEqual(job,before)
        state['settings']['output_destination']='auto'
        self.assertEqual(output_folder(job,state['settings']),job['task_dir'])

    def test_relative_paths_use_shared_or_explicit_base_and_pure_composition_works(self):
        state=self.state();settings=routing.settings_for(state)
        self.assertEqual(Path(output_folder(state['jobs'][1],settings)),self.root/'成品'/'1')
        state['settings']['output_relative_root']=str(self.root/'源项目')
        settings=routing.settings_for(state)
        self.assertEqual(Path(output_folder(state['jobs'][1],settings)),self.root/'成品'/'reels'/'1')
        self.assertEqual(output_folder(dict(path=''),settings),str(self.root/'成品'))

    def test_relative_paths_outside_base_are_rejected_and_missing_target_is_actionable(self):
        state=self.state();state['settings']['output_relative_root']=str(self.root/'other')
        with self.assertRaisesRegex(ValueError,'基准内'):output_folder(state['jobs'][0],routing.settings_for(state))
        state['settings'].update(output_destination='directory',output_dir='')
        with self.assertRaisesRegex(ValueError,'请选择'):output_folder(state['jobs'][0],state['settings'])

    def test_default_numbering_and_task_delivery_number_family(self):
        state=self.state();state['settings']['output_destination']='directory'
        before=copy.deepcopy(state);validate_batch_outputs(state,[j['id'] for j in state['jobs']]);self.assertEqual(state,before)
        state['settings']['overwrite']=True
        with self.assertRaisesRegex(ValueError,'同一成品'):validate_batch_outputs(state,['0','1'])
        base=self.root/'2026年11月11日-reels.mp4'
        self.assertEqual(routing.numbered(base,2).name,'2026年11月11日-reels2.mp4')
        self.assertEqual(routing.numbered(self.root/'myvideo.mp4',2).name,'myvideo_2.mp4')

    def prepared(self,index,output=None):
        target=self.root/f'temporary-{index}.mp4';target.write_bytes(str(index).encode())
        return PreparedExport([],target,output or self.root/'result.mp4','',1,False,None,[],
            threading.Event(),None,lambda *_:None,False,'cpu',False,False,1,encoded=True)

    def test_publication_preserves_existing_file_even_with_many_simultaneous_writers(self):
        output=self.root/'result.mp4';output.write_bytes(b'KEEP')
        prepared=[self.prepared(i) for i in range(24)]
        with ThreadPoolExecutor(max_workers=8) as pool:paths=list(pool.map(lambda p:p.publish(),prepared))
        self.assertEqual(output.read_bytes(),b'KEEP')
        self.assertEqual(len(set(paths)),24)
        self.assertEqual({p.read_bytes() for p in paths},{str(i).encode() for i in range(24)})
        self.assertFalse(list(self.root.glob('temporary-*')))

    def test_file_appearing_at_atomic_move_is_preserved_without_reencoding(self):
        from model.AppDataStorage import data_io
        prepared=self.prepared(99);actual=data_io.rename;calls=[]
        def race(source,destination):
            calls.append(destination)
            if len(calls)==1:
                Path(destination).write_bytes(b'OTHER WRITER')
                raise FileExistsError('race')
            return actual(source,destination)
        with patch.object(data_io,'rename',side_effect=race):path=prepared.publish()
        self.assertEqual(path.name,'result_2.mp4')
        self.assertEqual((self.root/'result.mp4').read_bytes(),b'OTHER WRITER')
        self.assertEqual(path.read_bytes(),b'99')

    def test_late_conflict_keeps_numbering_from_original_stem(self):
        base=self.root/'result.mp4';base.write_bytes(b'KEEP BASE')
        assigned=self.root/'result_2.mp4';assigned.write_bytes(b'KEEP SECOND')
        prepared=self.prepared(5,assigned);prepared.requested_output=base
        self.assertEqual(prepared.publish().name,'result_3.mp4')
        self.assertEqual(assigned.read_bytes(),b'KEEP SECOND')

    def test_existing_source_stays_protected_and_explicit_overwrite_still_works(self):
        source=self.root/'result.mp4';source.write_bytes(b'SOURCE')
        prepared=self.prepared(1);prepared.source_paths=(str(source),)
        with self.assertRaisesRegex(ValueError,'覆盖源文件'):prepared.publish()
        self.assertEqual(source.read_bytes(),b'SOURCE')
        prepared.source_paths=();prepared.overwrite=True
        self.assertEqual(prepared.publish(),source)
        self.assertEqual(source.read_bytes(),b'1')

    def test_settings_survive_save_load_and_project_clone(self):
        state=self.state();state['settings']['output_relative_root']=str(self.root/'源项目')
        store=Store(self.root/'private');state=store.save(state,0)
        self.assertEqual(store.load()['settings']['output_destination'],'relative')
        clone=store.create_profile(state,'copy',clone=True)
        self.assertEqual(clone['settings']['output_relative_root'],str(self.root/'源项目'))


class DestinationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_browse_activates_explicit_destination_and_root_is_saved(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as directory,patch.object(BatchTextVideoDialog,'request_background'):
            root=Path(directory);store=Store(root/'private');dialog=BatchTextVideoDialog(store=store)
            try:
                with patch.object(QtWidgets.QFileDialog,'getExistingDirectory',return_value=str(root/'导出')):dialog.choose_output()
                self.assertEqual(dialog.controls['output_destination'].currentData(),'directory')
                dialog.controls['output_destination'].setCurrentIndex(2)
                dialog.controls['output_relative_root'].setText(str(root))
                self.assertTrue(dialog.persist())
                self.assertEqual(store.load()['settings']['output_destination'],'relative')
                self.assertEqual(store.load()['settings']['output_relative_root'],str(root))
                from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
                pairing=PairingDialog([], [], state=dialog.state)
                self.assertEqual(pairing.table.horizontalHeaderItem(3).text(),'成品目录')
                self.assertEqual(pairing.table.item(0,3).toolTip(),str(root/'导出'))
                pairing.deleteLater()
                if os.environ.get('LZX_EXPORT_DESTINATION_QA'):
                    dialog.export_destination_widget.resize(1100,80)
                    dialog.export_destination_widget.grab().save(str(Path(os.environ['LZX_EXPORT_DESTINATION_QA'])/'destination-ui.png'))
            finally:dialog.close();dialog.deleteLater()


@unittest.skipUnless(FF,'real FFmpeg required')
class RealDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.source=self.root/'source.mp4'
        subprocess.run([FF,'-nostdin','-v','error','-f','lavfi','-i','color=red:s=128x128:r=10:d=0.3',
            '-an','-c:v','libx264',str(self.source)],check=True,capture_output=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def tearDown(self):self.temp.cleanup()

    def state(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        layer=new_text_component();layer.update(text='TEST',font_min=12,font_max=18,width=80,height=70)
        state=fresh_state();state['settings'].update(ffmpeg_path=FF,aspect='source',layers=[layer],music_enabled=False,
            output_dir=str(self.root/'指定成品'),output_destination='directory',overwrite=False,export_workers=2,
            auto_crop_black_bars=False,margin_top=0,margin_bottom=0,margin_x=0)
        state['jobs']=[dict(id=str(i),path=str(self.source),name='same',title='',body='',status='待生成',error='',output='',
            task_dir=str(self.root/'原任务'/str(i))) for i in range(4)]
        return state

    def test_real_parallel_batch_same_names_export_to_chosen_directory_without_overwriting(self):
        state=self.state();store=Store(self.root/'private');state=store.save(state,0)
        output=self.root/'指定成品';output.mkdir();(output/'same.mp4').write_bytes(b'KEEP')
        result,saved=execute_batch(store,state,[j['id'] for j in state['jobs']],threading.Event(),lambda *_:None,lambda *_:None,lambda *_:None)
        self.assertEqual((result['completed'],result['failed']),(4,0),result)
        self.assertEqual((output/'same.mp4').read_bytes(),b'KEEP')
        paths=[Path(j['output']) for j in saved['jobs']]
        self.assertEqual(len(set(paths)),4)
        self.assertTrue(all(p.parent==output and p.is_file() for p in paths))
        self.assertFalse((self.root/'原任务').exists())

    def test_preview_stays_in_cache_when_export_uses_relative_destination(self):
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker,preview_key
        state=self.state();state['settings'].update(output_destination='relative',output_relative_root=str(self.root))
        job=state['jobs'][0];before=copy.deepcopy(state);results=[]
        worker=PlaybackWorker(job,state,self.root/'cache',preview_key(job,state));worker.ready.connect(results.append);worker.run();worker.deleteLater()
        self.assertEqual(len(results),1);self.assertNotIn('error',results[0],results)
        self.assertTrue(Path(results[0]['path']).is_file())
        self.assertFalse((self.root/'指定成品').exists())
        self.assertEqual(state,before)

    def test_relative_export_creates_expected_subfolders(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        state=self.state();state['settings'].update(output_destination='relative',output_relative_root=str(self.root))
        path,_=render_static_text(state['jobs'][0],state,self.root/'cache')
        self.assertEqual(path.parent,self.root/'指定成品'/'原任务'/'0')
        self.assertTrue(path.is_file())


if __name__=='__main__':unittest.main()

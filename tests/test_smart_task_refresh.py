"""Current-task refresh: cache policy, in-place GUI and real host-task lifecycle."""
import copy
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager
from app_plugins.builtin.smart_video_editor import engine, background
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog, SmartVideoSourceDialog
from app_plugins.builtin.smart_video_editor.review_reanalysis import refresh_job, merge_task_result, analysis_summary


class RefreshDataTests(unittest.TestCase):
    def test_rescans_new_files_deduplicates_and_keeps_task_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root/'old.mp4', root/'new.mp4'
            old.write_bytes(b'old'); new.write_bytes(b'new')
            (root/'removed.mp4').write_bytes(b'x'); (root/'removed.mp4').unlink()
            generated = root/'智能剪辑结果'; generated.mkdir(); (generated/'result.mp4').write_bytes(b'output')
            task = {'task_id':'same-id', 'task_name':'Test', 'label':'Current', 'task_dir':str(root),
                    'script':'alpha', 'language':'en', 'clips':[{'source':str(old)}, {'source':str(old)}, {'source':str(root/'removed.mp4')}]}
            before = copy.deepcopy(task)
            job = refresh_job(task, engine.normalize_smart_video_editor_settings({}))
            self.assertEqual(set(job['sources']), {str(old.resolve()), str(new.resolve())})
            self.assertEqual(len(job['sources']), 2)
            self.assertEqual(job['task_id'], 'same-id')
            self.assertEqual(job['script'], 'alpha')
            self.assertTrue(job['force_task_reanalysis'])
            self.assertEqual(task, before)
            with self.assertRaisesRegex(FileNotFoundError, '任务目录不存在'):
                refresh_job({**task, 'task_dir':str(root/'missing')}, {})

    def test_merge_only_target_and_clear_its_stale_manual_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = {'task_id':'other', 'task_dir':str(root/'other'), 'clips':[{'status':'green', 'review_acknowledged':True}],
                     'custom': {'keep':[1, 2]}, 'missing_review':{'decision':'approved'}}
            target = {'task_id':'target', 'task_dir':str(root/'target'), 'clips':[{'status':'pink'}],
                      'missing_review':{'decision':'approved'}}
            bundle = {'tasks':[other, target], 'settings':{'export_mode':'segments'}, 'summary':{}, 'custom_bundle':'retain'}
            before = copy.deepcopy(bundle)
            fresh = {'task_id':'target', 'task_dir':str(root/'target'), 'clips':[{'status':'green'}]}
            result = merge_task_result(bundle, 1, {'tasks':[fresh], 'settings':{'export_mode':'joined'}})
            self.assertEqual(bundle, before)
            self.assertEqual(result['tasks'][0], other)
            self.assertEqual(result['tasks'][1], fresh)
            self.assertNotIn('missing_review', result['tasks'][1])
            self.assertEqual(result['settings'], bundle['settings'])
            self.assertEqual(result['summary']['clip_count'], 2)
            result['tasks'][0]['custom']['keep'].append(3)
            self.assertEqual(bundle, before)
            for response in ({'tasks':[]}, {'tasks':[fresh, fresh]}, {'tasks':[{**fresh,'task_id':'wrong'}]},
                             {'tasks':[{**fresh,'task_dir':str(root/'elsewhere')}]}, {'tasks':[{**fresh,'clips':[]}]}):
                with self.subTest(response=response), self.assertRaises(ValueError):
                    merge_task_result(bundle, 1, response)
                self.assertEqual(bundle, before)

    def test_explicit_refresh_bypasses_whole_task_cache_but_keeps_healthy_asr(self):
        from test_smart_video_editor import _FakeWhisperModel
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'source.mp4'; source.write_bytes(b'fake')
            job = {'task_id':'1','label':'1','task_dir':str(root),'script':'Moj bože prosím zdravie',
                   'language':'sk','sources':[str(source)]}
            settings = engine.normalize_smart_video_editor_settings({'silence_detection_enabled':False,'voice_detection_enabled':False})
            model = _FakeWhisperModel()
            first = engine.analyze_smart_video_jobs([job], model, settings)
            first['tasks'][0]['clips'][0].update(included=False, review_acknowledged=True)
            engine.save_analysis_reports(first)
            ordinary = engine.analyze_smart_video_jobs([job], None, settings)
            self.assertTrue(ordinary['tasks'][0]['analysis_from_cache'])
            forced = {**job, 'force_task_reanalysis':True}
            with patch.object(engine, '_load_cached_task_analysis', side_effect=AssertionError('refresh must not reuse full report')):
                self.assertFalse(engine.smart_video_jobs_require_model([forced], settings))
                result = engine.analyze_smart_video_jobs([forced], None, settings)
            self.assertEqual(model.calls, 1)
            self.assertFalse(result['tasks'][0]['analysis_from_cache'])
            self.assertTrue(result['tasks'][0]['clips'][0]['included'])
            self.assertFalse(result['tasks'][0]['clips'][0].get('review_acknowledged'))
            self.assertTrue(result['tasks'][0]['clips'][0]['from_cache'])
            self.assertEqual(result['summary']['task_cache_count'], 0)
            self.assertEqual(result['summary']['transcription_cache_count'], 1)
            engine._cache_path(root, source, settings).unlink()
            self.assertFalse(engine.smart_video_jobs_require_model([job], settings))
            self.assertTrue(engine.smart_video_jobs_require_model([forced], settings))


class RefreshGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        if os.environ.get('SMART_REFRESH_QA') and Path(r'C:\Windows\Fonts\msyh.ttc').is_file():
            old_font = self.app.font()
            font_id = QtGui.QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc')
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families:
                self.app.setFont(QtGui.QFont(families[0], 9))
                self.addCleanup(self.app.setFont, old_font)
        self.window = QtWidgets.QWidget()
        self.manager = BackgroundTaskManager(self.window)
        self.release = threading.Event()
        self.plugin = SmartVideoEditorPlugin()
        self.context = SimpleNamespace(parent_widget=self.window, background_tasks=self.manager, log=Mock(),
            notify=Mock(), save_config=Mock(), update_command=Mock(), loaded_whisper_model_name=lambda:'base',
            whisper_model=Mock(), selected_task_rows=Mock(side_effect=AssertionError('must not reread main selection')))
        self.context.submit_background_task = lambda **kw: self.manager.submit(owner='smart_video_editor', **kw)
        self.plugin.context = self.context
        self.dialogs = []
        for target in ('app_plugins.builtin.smart_video_editor.timeline_review.FfplayReviewSurface.prepare',
                       'app_plugins.builtin.smart_video_editor.timeline_review.WaveformLoader.load_task',
                       'app_plugins.builtin.smart_video_editor.plugin.smart_video_jobs_require_model'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch('app_plugins.builtin.smart_video_editor.timeline_review._media_shape', return_value=(1080,1920,30))
        p.start(); self.addCleanup(p.stop)
        p = patch('app_plugins.builtin.smart_video_editor.ui.QtWidgets.QMessageBox.warning', return_value=QtWidgets.QMessageBox.StandardButton.Ok)
        self.warning = p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.release.set()
        self.manager.cancel_all()
        self.wait(lambda:not self.manager.is_busy())
        self.plugin.stop(); self.manager.shutdown()
        for dialog in self.dialogs: dialog.close(); dialog.deleteLater()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents(); self.temp.cleanup()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.002)
        self.app.processEvents(); self.assertTrue(predicate())

    def task(self, key, filename='old.mp4'):
        root = self.root/key; root.mkdir(exist_ok=True)
        source = root/filename; source.write_bytes(filename.encode())
        lines, words = engine.build_script_word_records('alpha')
        clip = {'source':str(source), 'file_name':filename, 'status':'green', 'included':True,
                'source_index':0,'export_order':1,'similarity':1.,'original_duration':4.,
                'trim_start':0.,'trim_end':4.,'kept_ranges':[[0.,4.]], 'removed_seconds':0.,
                'recognized_text':'alpha','expected_text':'alpha','issues':[],
                'words':[{'word':'alpha','start':.3,'end':.8}],
                'script_word_start':0,'script_word_end':0,'content_order_reliable':True,'content_order_start':0}
        return {'task_id':key,'label':key,'task_dir':str(root),'output_dir':str(root/'智能剪辑结果'),
                'script':'alpha','language':'en','script_words':words,'script_lines':lines,'clips':[clip],
                'missing_blocks':[],'unverified_blocks':[],'duplicate_count':0}

    def dialog(self):
        bundle = {'tasks':[self.task('other'),self.task('current')],
                  'settings':engine.normalize_smart_video_editor_settings({'silence_detection_enabled':False,'auto_export_clean':True}),
                  'summary':{}}
        bundle['summary']=analysis_summary(bundle)
        dialog = SmartVideoReviewDialog(bundle, reanalyze_task=self.plugin._reanalyze_review_task)
        self.dialogs.append(dialog); dialog.resize(1280,820); dialog.show()
        dialog.timeline_review._select_task_index(1)
        dialog.timeline_review._media_aspect_cache[str(self.root/'current'/'old.mp4')]='landscape'
        self.app.processEvents()
        return dialog

    def select_new(self, jobs, parent=None, **kw):
        self.assertEqual(len(jobs),1); self.assertEqual(jobs[0]['task_id'],'current')
        self.assertTrue(jobs[0]['force_task_reanalysis'])
        self.assertIn('旧人工决定',kw['notice'])
        job = copy.deepcopy(jobs[0])
        job['sources']=[str(self.root/'current'/'new.mp4')]
        return [job]

    def test_button_is_adjacent_to_combo_and_cancel_selection_does_not_collect_or_submit(self):
        dialog = self.dialog(); timeline = dialog.timeline_review
        self.assertTrue(timeline.task_refresh_button.isVisible()); self.assertTrue(timeline.task_refresh_button.isEnabled())
        self.assertGreaterEqual(timeline.task_refresh_button.geometry().left(), timeline.task_combo.geometry().right())
        before = copy.deepcopy(dialog.bundle)
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs', return_value=None), \
             patch.object(dialog,'collect',side_effect=AssertionError('cancel must leave draft untouched')):
            timeline.task_refresh_button.click()
        self.assertEqual(dialog.bundle,before)
        self.assertEqual(self.manager.records(),[])
        self.context.selected_task_rows.assert_not_called()
        self.assertFalse(dialog._task_refresh_running)

    def test_refresh_replaces_current_task_in_same_window_preserves_other_edits_and_no_auto_export(self):
        dialog = self.dialog()
        row=dialog._row_for_clip(0,0); dialog.table.item(row,dialog.COL_ORDER).setText('7')
        fresh=self.task('current','new.mp4')
        entered=threading.Event(); calls=[]
        def analyze(jobs, model, settings, **kw):
            self.assertNotEqual(QtCore.QThread.currentThread(), self.app.thread())
            calls.append(copy.deepcopy(jobs)); entered.set(); self.release.wait(4)
            return {'tasks':[fresh],'settings':settings,'summary':analysis_summary({'tasks':[fresh]})}
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs',side_effect=analyze), \
             patch.object(self.plugin,'_open_analysis_result') as open_result, \
             patch.object(self.plugin,'_handle_analysis') as auto_export:
            dialog.timeline_review.task_refresh_button.click(); self.wait(entered.is_set)
            other=copy.deepcopy(dialog.bundle['tasks'][0])
            self.assertEqual(other['clips'][0]['export_order'],7)
            self.assertFalse(dialog.export_button.isEnabled()); self.assertFalse(dialog.save_review_button.isEnabled())
            self.assertEqual(dialog.timeline_review.task_refresh_button.text(),'取消刷新')
            self.release.set(); self.wait(lambda:not dialog._task_refresh_running)
            self.assertEqual(dialog.bundle['tasks'][0],other)
            self.assertEqual(dialog.bundle['tasks'][1],fresh)
            self.assertEqual(dialog.timeline_review.task_index,1)
            self.assertIn('new.mp4',dialog.table.item(dialog._row_for_clip(1,0),dialog.COL_FILE).text())
            self.assertEqual(Path(dialog.timeline_review.segments[0]['source']).name,'new.mp4')
            self.assertTrue(dialog.isVisible()); self.assertTrue(dialog.export_button.isEnabled())
            open_result.assert_not_called(); auto_export.assert_not_called()
            self.assertFalse(next(iter(self.manager._handles.values())).has_pending_result())
            self.assertEqual(calls[0][0]['sources'],[str(self.root/'current'/'new.mp4')])
            self.assertFalse(self.plugin._analysis_jobs)
            if os.environ.get('SMART_REFRESH_QA'):
                destination=Path(os.environ['SMART_REFRESH_QA']); destination.mkdir(parents=True,exist_ok=True)
                dialog.grab().save(str(destination/'smart-task-refresh.png'))

    def test_rejects_dialog_owned_qthread_instead_of_connecting_unsafe_cleanup(self):
        dialog=self.dialog(); worker=QtCore.QThread()
        self.addCleanup(worker.deleteLater)
        dialog._reanalyze_task_callback=lambda *_:worker
        dialog.timeline_review.task_refresh_button.click()
        self.assertFalse(dialog._task_refresh_running)
        self.warning.assert_called_once()
        self.assertIn('主程序后台任务',self.warning.call_args.args[2])

    def test_stale_result_generation_cannot_replace_current_review(self):
        dialog=self.dialog(); before=copy.deepcopy(dialog.bundle)
        result={'tasks':[self.task('current','new.mp4')]}
        self.assertFalse(dialog.apply_task_reanalysis(1,result,dialog._task_refresh_generation+1))
        self.assertEqual(dialog.bundle,before)

    def test_close_after_refresh_remembers_latest_task_not_initial_analysis(self):
        dialog=self.dialog(); original=copy.deepcopy(dialog.bundle)
        latest=merge_task_result(original,1,{'tasks':[self.task('current','new.mp4')]})
        def review(bundle, parent=None, **kwargs):
            kwargs['on_closed_bundle'](latest)
            return None
        with patch.object(SmartVideoReviewDialog,'get_reviewed_bundle',side_effect=review), \
             patch.object(self.plugin,'_remember_pending_reviews') as remember:
            self.plugin._handle_analysis(original,force_review=True)
        remember.assert_called_once_with(latest)
        self.assertEqual(original['tasks'][1]['clips'][0]['file_name'],'old.mp4')

    def test_same_path_refresh_reloads_decoder_and_clears_aspect_cache(self):
        dialog=self.dialog(); source=str(self.root/'current'/'old.mp4')
        fresh=copy.deepcopy(dialog.bundle['tasks'][1]); fresh['clips'][0]['original_duration']=5.
        with patch.object(dialog.timeline_review.player,'reset_source') as reset, \
             patch.object(dialog.timeline_review,'_media_aspect_cache', {source:'landscape', 'other':'keep'}):
            self.assertTrue(dialog.apply_task_reanalysis(1,{'tasks':[fresh]},dialog._task_refresh_generation))
            reset.assert_called_once()
            self.assertEqual(dialog.timeline_review._media_aspect_cache['other'],'keep')
            self.assertNotEqual(dialog.timeline_review._media_aspect_cache.get(source),'landscape')

    def test_native_deleted_dialog_does_not_receive_worker_results(self):
        from PyQt6 import sip
        dialog=self.dialog(); fresh=self.task('current','new.mp4'); entered=threading.Event()
        def analyze(*a,**kw):
            entered.set(); self.release.wait(4); return {'tasks':[fresh],'summary':{},'settings':{}}
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs',side_effect=analyze), \
             patch.object(self.plugin,'_open_analysis_result',return_value=True) as opened:
            dialog.timeline_review.task_refresh_button.click(); self.wait(entered.is_set)
            self.dialogs.remove(dialog); dialog.close(); dialog.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(dialog))
            self.release.set(); self.wait(lambda:opened.call_count==1)
            self.assertTrue(opened.call_args.kwargs['force_review'])

    def test_player_reset_contract_for_all_existing_backends(self):
        from model.MediaPlayback import _EmbeddedMpvReviewSurface, _QtMediaReviewSurface, _EmbeddedFfplayReviewSurface, FfplayReviewSurface
        mpv=SimpleNamespace(source='old',loaded_source='old',_send=Mock(),_set_playing=Mock(),placeholder=Mock())
        _EmbeddedMpvReviewSurface.reset_source(mpv)
        self.assertEqual(mpv.loaded_source,''); self.assertEqual(mpv.source,'')
        mpv._send.assert_called_once_with(['stop'])
        qt=SimpleNamespace(source='old',position=1,release=Mock())
        _QtMediaReviewSurface.reset_source(qt); qt.release.assert_called_once(); self.assertEqual(qt.source,'')
        ff=SimpleNamespace(source='old',position=1,pause=Mock(),placeholder=Mock(),_thumbnail=Mock())
        _EmbeddedFfplayReviewSurface.reset_source(ff); ff.pause.assert_called_once_with(show_frame=False)
        ff._thumbnail.cancel.assert_called_once_with()
        self.assertEqual(ff.source,'')
        public=SimpleNamespace(_last_prepare=('old',0,1,0),backend=Mock())
        FfplayReviewSurface.reset_source(public)
        public.backend.reset_source.assert_called_once(); self.assertEqual(public._last_prepare[0],'')

    def test_queued_refresh_can_cancel_and_old_results_stay(self):
        dialog=self.dialog(); self.task('current','new.mp4')
        blocker=self.manager.submit(owner='hold', title='synthetic directory hold',kind='network',
            resources=background.render_resources({'tasks':[dialog.bundle['tasks'][1]]}),runner=lambda *_:self.release.wait(4))
        self.wait(lambda:self.manager.get(blocker.task_id)['status']=='running')
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs') as analyze:
            dialog.timeline_review.task_refresh_button.click(); before=copy.deepcopy(dialog.bundle)
            handle=dialog._task_refresh_handle
            self.assertEqual(self.manager.get(handle.task_id)['status'],'queued')
            dialog.timeline_review.task_refresh_button.click()
            self.assertEqual(self.manager.get(handle.task_id)['status'],'canceled')
            self.assertEqual(dialog.bundle,before); self.assertFalse(dialog._task_refresh_running)
            self.assertTrue(dialog.timeline_review.task_refresh_button.isEnabled())
            analyze.assert_not_called(); self.warning.assert_not_called()
        self.release.set()

    def test_cancel_running_refresh_does_not_apply_late_success(self):
        dialog=self.dialog(); fresh=self.task('current','new.mp4'); entered=threading.Event()
        def analyze(*a,**kw):
            entered.set(); self.release.wait(4); return {'tasks':[fresh],'summary':{},'settings':{}}
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs',side_effect=analyze):
            dialog.timeline_review.task_refresh_button.click(); self.wait(entered.is_set)
            before=copy.deepcopy(dialog.bundle)
            dialog.timeline_review.task_refresh_button.click(); self.release.set()
            self.wait(lambda:not dialog._task_refresh_running)
            self.assertEqual(dialog.bundle,before); self.assertIn('已取消',dialog.task_refresh_status.text())
            self.warning.assert_not_called()

    def test_failed_refresh_unfreezes_and_keeps_old_results_with_visible_error(self):
        dialog=self.dialog(); self.task('current','new.mp4')
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs',side_effect=ValueError('synthetic reanalysis failure')):
            dialog.timeline_review.task_refresh_button.click(); before=copy.deepcopy(dialog.bundle)
            self.wait(lambda:not dialog._task_refresh_running)
            self.assertEqual(dialog.bundle,before)
            self.assertTrue(dialog.isVisible()); self.assertTrue(dialog.timeline_review.task_refresh_button.isEnabled())
            self.assertIn('刷新失败',dialog.task_refresh_status.text())
            self.warning.assert_called_once()

    def test_closed_window_routes_completed_refresh_to_normal_foreground_result(self):
        dialog=self.dialog(); fresh=self.task('current','new.mp4'); entered=threading.Event()
        def analyze(*a,**kw):
            entered.set(); self.release.wait(4); return {'tasks':[fresh],'summary':{},'settings':{}}
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs',side_effect=analyze), \
             patch.object(self.plugin,'_open_analysis_result',return_value=True) as opened:
            dialog.timeline_review.task_refresh_button.click(); self.wait(entered.is_set)
            handle=dialog._task_refresh_handle; dialog.close(); self.release.set()
            self.wait(lambda:opened.call_count==1)
            self.assertEqual(self.manager.get(handle.task_id)['status'],'succeeded')
            self.assertTrue(opened.call_args.kwargs['force_review'])

    def test_refresh_selection_source_changes_while_queued_fail_without_analysis(self):
        dialog=self.dialog(); fresh=self.task('current','new.mp4')
        blocker=self.manager.submit(owner='hold',title='hold',kind='network',
            resources=background.render_resources({'tasks':[fresh]}),runner=lambda *_:self.release.wait(4))
        self.wait(lambda:self.manager.get(blocker.task_id)['status']=='running')
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoSourceDialog.get_jobs',side_effect=self.select_new), \
             patch.object(background,'analyze_smart_video_jobs') as analyze:
            dialog.timeline_review.task_refresh_button.click(); before=copy.deepcopy(dialog.bundle)
            Path(fresh['clips'][0]['source']).write_bytes(b'changed while waiting')
            self.release.set(); self.wait(lambda:not dialog._task_refresh_running)
            analyze.assert_not_called(); self.assertEqual(dialog.bundle,before)
            self.assertIn('已变化',dialog.task_refresh_status.text())


if __name__=='__main__': unittest.main()

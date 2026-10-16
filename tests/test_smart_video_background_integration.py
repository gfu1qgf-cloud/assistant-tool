import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager
from PYUI.background_tasks_pyui import BackgroundTasksWidget
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor import background


class SmartVideoBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = QtWidgets.QWidget()
        self.manager = BackgroundTaskManager(self.window)
        self.release = threading.Event()
        self.context = SimpleNamespace(parent_widget=self.window, log=Mock(), notify=Mock(),
            update_command=Mock(), save_config=Mock(), background_tasks=self.manager)
        self.context.submit_background_task = lambda **kw: self.manager.submit(owner='smart_video_editor', **kw)
        self.plugin = SmartVideoEditorPlugin()
        self.plugin.context = self.context
        self.plugin.settings['auto_export_clean'] = False
        self.bundle = {'tasks': [{'task_id': '1', 'output_dir': str(self.root), 'clips': []}]}
        # Automatic foreground windows are exercised through their real host
        # result callbacks, without entering modal loops in integration tests.
        self.review = patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoReviewDialog.get_reviewed_bundle', return_value=None).start()
        self.breath_review = patch('app_plugins.builtin.smart_video_editor.plugin.BreathCutReviewDialog.get_reviewed_bundle', return_value=None).start()
        self.export_result = patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoExportResultDialog').start()
        self.breath_result = patch('app_plugins.builtin.smart_video_editor.plugin.BreathCutResultDialog').start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.release.set()
        self.plugin.stop()
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.manager.shutdown()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 6
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_final_render_owned_by_host_and_allows_next_submission(self):
        entered = threading.Event()
        calls = []
        def export(bundle, *_args, **_kwargs):
            calls.append(bundle)
            entered.set()
            self.release.wait(4)
            return {'completed': [], 'skipped': [], 'failed': []}
        with patch.object(background, 'export_smart_video_bundle', side_effect=export):
            first = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=self.bundle)
            self.wait_until(entered.is_set)
            self.window.close()
            self.assertEqual(self.plugin._analysis_jobs, {})
            second = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=self.bundle)
            self.app.processEvents()
            self.assertEqual(self.manager.get(second.task_id)['status'], 'queued')
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.manager.get(first.task_id)['percent'], 100)
        self.assertEqual(self.plugin._render_handles, {})

    def test_partial_render_failure_opens_result_and_keeps_manual_result_button(self):
        result = {'completed': [{'video': str(self.root / 'done.mp4')}], 'skipped': [],
                  'failed': [{'task_id': '2', 'error': 'synthetic encoder failure'}]}
        with patch.object(background, 'export_smart_video_bundle', return_value=result), \
             patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoExportResultDialog') as dialog:
            handle = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=self.bundle)
            self.wait_until(lambda: not self.manager.is_busy())
            self.assertEqual(self.manager.get(handle.task_id)['status'], 'failed')
            self.assertIn('Traceback', self.manager.get(handle.task_id)['traceback'])
            self.wait_until(lambda: dialog.return_value.exec.call_count == 1)
            self.assertFalse(handle.has_pending_result())
            view = BackgroundTasksWidget(self.manager)
            view.refresh()
            self.assertFalse(view._cards[handle.task_id].result_button.isHidden())
            view._cards[handle.task_id].open_result()
            self.assertEqual(dialog.return_value.exec.call_count, 2)
            view.deleteLater()

    def test_queued_render_rejects_changed_source_instead_of_using_wrong_cut_ranges(self):
        source = self.root / 'clip.mp4'
        source.write_bytes(b'first')
        bundle = {'tasks': [{'output_dir': str(self.root), 'clips': [{'source': str(source)}]}]}
        blocker = self.manager.submit(owner='test', title='hold output', runner=lambda *_: self.release.wait(4),
            resources=background.render_resources(bundle), kind='network')
        self.wait_until(lambda: self.manager.get(blocker.task_id)['status'] == 'running')
        with patch.object(background, 'export_smart_video_bundle') as export:
            handle = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=bundle)
            source.write_bytes(b'new-content')
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        export.assert_not_called()
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'failed')
        self.assertIn('已变化', self.manager.get(handle.task_id)['error'])

    def test_analysis_also_registers_in_host_without_passing_live_model(self):
        result = {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', return_value=result), \
             patch.object(self.plugin, '_handle_analysis') as complete:
            handle = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': '1'}])
            self.assertIs(self.plugin._analysis_jobs[handle.task_id]['handle'], handle)
            self.wait_until(lambda: not self.manager.is_busy())
            self.wait_until(lambda: complete.call_count == 1)
            self.assertFalse(handle.has_pending_result())
            complete.assert_called_once_with(result)
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'succeeded')
        self.assertEqual(self.plugin._analysis_jobs, {})

    def test_analysis_batches_queue_and_keep_their_own_settings_and_results(self):
        entered, calls = threading.Event(), []
        def analyze(jobs, _model, settings, **_kwargs):
            calls.append((jobs[0]['task_id'], settings['pause_before_ms']))
            if len(calls) == 1:
                entered.set()
                self.release.wait(4)
            return {'tasks': [], 'summary': {'needs_review': True}, 'settings': settings, 'batch': jobs[0]['task_id']}
        settings = {'pause_before_ms': 111}
        with patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze), \
             patch.object(self.plugin, '_handle_analysis') as review:
            first = self.plugin._start_worker('analyze', settings, jobs=[{'task_id': '51','task_dir':str(self.root)}])
            self.wait_until(entered.is_set)
            second = self.plugin._start_worker('analyze', {'pause_before_ms': 222}, jobs=[{'task_id': '57','task_dir':str(self.root)}])
            settings['pause_before_ms'] = 999
            self.app.processEvents()
            self.assertEqual(self.manager.get(second.task_id)['status'], 'queued')
            self.assertEqual(calls, [('51', 111)])
            self.assertIn('57', self.manager.get(second.task_id)['title'])
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
            self.wait_until(lambda: review.call_count == 2)
            self.assertEqual([(c.args[0]['batch'], c.args[0]['settings']['pause_before_ms'])
                              for c in review.call_args_list], [('51', 111), ('57', 222)])
            self.assertFalse(first.has_pending_result())
            self.assertFalse(second.has_pending_result())
        self.assertEqual(calls, [('51', 111), ('57', 222)])
        self.assertEqual(self.plugin._analysis_jobs, {})

    def test_cancel_waiting_analysis_with_card_button_never_runs_or_opens_review(self):
        entered, calls = threading.Event(), []
        def analyze(jobs, *_args, **_kwargs):
            calls.append(jobs[0]['task_id'])
            entered.set()
            self.release.wait(4)
            return {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze), \
             patch.object(self.plugin, '_handle_analysis') as review, \
             patch('app_plugins.builtin.smart_video_editor.plugin.QMessageBox.critical') as alert:
            first = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'first','task_dir':str(self.root)}])
            self.wait_until(entered.is_set)
            waiting = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'waiting','task_dir':str(self.root)}])
            view = BackgroundTasksWidget(self.manager)
            view.refresh()
            card = view._cards[waiting.task_id]
            self.assertTrue(card.cancel_button.isEnabled())
            card.cancel_button.click()
            self.assertEqual(self.manager.get(waiting.task_id)['status'], 'canceled')
            self.assertNotIn(waiting.task_id, self.plugin._analysis_jobs)
            self.assertFalse(waiting.has_result_handler())
            self.assertEqual(self.manager.get(first.task_id)['status'], 'running')
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
            self.wait_until(lambda: review.call_count == 1)
            alert.assert_not_called()
            view.deleteLater()
        self.assertEqual(calls, ['first'])

    def test_cancel_running_analysis_discards_late_success_result(self):
        entered = threading.Event()
        def analyze(*_args, **_kwargs):
            entered.set()
            self.release.wait(4)
            return {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze):
            handle = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': '1'}])
            self.wait_until(entered.is_set)
            handle.requestInterruption()
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'canceled')
        self.assertFalse(handle.has_result_handler())
        self.context.notify.assert_not_called()

    def test_failed_batch_does_not_lose_or_block_next_batch(self):
        def analyze(jobs, *_args, **_kwargs):
            if jobs[0]['task_id'] == 'bad':
                raise ValueError('synthetic analysis failure')
            return {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze):
            failed = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'bad'}])
            success = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'ok'}])
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(failed.task_id)['status'], 'failed')
        self.wait_until(lambda: not success.has_pending_result())
        self.review.assert_called_once()
        self.assertFalse(failed.has_result_handler())

    def test_stop_cancels_all_queued_batches_before_any_can_start(self):
        with patch.object(background, 'analyze_smart_video_jobs') as analyze:
            handles = [self.plugin._start_worker('analyze', {}, jobs=[{'task_id': str(i)}]) for i in range(4)]
            self.plugin.stop()
            self.app.processEvents()
            analyze.assert_not_called()
        self.assertTrue(all(self.manager.get(h.task_id)['status'] == 'canceled' for h in handles))
        self.assertEqual(self.plugin._analysis_jobs, {})
        self.assertEqual(self.plugin.can_close(), (True, ''))

    def test_independent_breath_analysis_does_not_wait_for_whole_speech_analysis(self):
        entered, calls = threading.Event(), []
        def analyze(*_args, **_kwargs):
            entered.set()
            self.release.wait(4)
            return {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze), \
             patch.object(background, 'analyze_breath_cut_files', side_effect=lambda *_a, **_k: calls.append('breath') or {'summary': {}}):
            self.plugin._start_worker('analyze', {}, jobs=[{'task_id': '1'}])
            self.wait_until(entered.is_set)
            breath = self.plugin._start_worker('breath_analyze', {}, files=['clip.mp4'])
            self.wait_until(lambda: calls == ['breath'])
            self.wait_until(lambda: self.manager.get(breath.task_id)['status'] == 'succeeded')
            self.assertFalse(self.release.is_set())
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.wait_until(lambda: not breath.has_pending_result())
        self.breath_review.assert_called_once()
        self.assertEqual(calls, ['breath'])

    def test_analysis_and_render_share_the_report_output_directory_lock(self):
        resources = background.analysis_resources('analyze', {'output_folder_name': 'edit'},
            [{'task_dir': str(self.root)}], [])
        renders = background.render_resources({'tasks': [{'output_dir': str(self.root / 'edit')}]})
        self.assertTrue(set(resources) & set(renders))
        self.assertEqual(resources,renders)
        self.assertTrue(all(key.startswith('directory:') for key in resources))
        with patch.object(background,'smart_video_task_review_decision',return_value='skipped'):
            self.assertEqual(background.render_resources({'tasks':[{'output_dir':str(self.root)}]}),())

    def test_encoding_and_next_analysis_overlap_even_when_both_reuse_model(self):
        encoding, analyzing = threading.Event(), threading.Event()
        loaded = object()
        def export(*_args, **kwargs):
            self.assertIsNone(kwargs['alignment_model'])
            self.assertTrue(callable(kwargs['alignment_model_loader']))
            encoding.set()
            self.release.wait(4)
            return {'completed': [], 'skipped': [], 'failed': []}
        def analyze(jobs, model, settings, **_kwargs):
            self.assertIs(model, loaded)
            analyzing.set()
            self.release.wait(4)
            return {'tasks': [], 'summary': {'needs_review': True}}
        with patch.object(background, 'export_smart_video_bundle', side_effect=export), \
             patch.object(background, 'analyze_smart_video_jobs', side_effect=analyze), \
             patch('globalValue.globalValue.get_whisper_model', return_value=loaded) as load:
            first = self.plugin._start_worker('export', {}, bundle=self.bundle, model_loader=Mock())
            self.wait_until(encoding.is_set)
            second = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': '2', 'task_dir': str(self.root / 'next')}], model_loader=Mock())
            self.wait_until(analyzing.is_set)
            self.assertEqual(self.manager.get(first.task_id)['status'], 'running')
            self.assertEqual(self.manager.get(second.task_id)['status'], 'running')
            self.assertEqual(self.manager.get(first.task_id)['kind'], 'media')
            self.assertEqual(self.manager.get(second.task_id)['kind'], 'media')
            self.assertEqual(load.call_count, 1)  # Only analysis needs the model yet.
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())

    def test_exports_to_different_directories_encode_in_parallel(self):
        entered, calls = threading.Event(), []
        def export(bundle, *_args, **_kwargs):
            calls.append(bundle['tasks'][0]['task_id'])
            entered.set()
            self.release.wait(4)
            return {'completed': [], 'skipped': [], 'failed': []}
        other = {'tasks': [{'task_id': '2', 'output_dir': str(self.root / 'other'), 'clips': []}]}
        with patch.object(background, 'export_smart_video_bundle', side_effect=export):
            first = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=self.bundle)
            self.wait_until(entered.is_set)
            second = self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=other)
            self.wait_until(lambda: len(calls) == 2)
            self.assertEqual(self.manager.get(second.task_id)['status'], 'running')
            self.assertEqual(calls, ['1','2'])
            second.requestInterruption()
            self.assertEqual(self.manager.get(first.task_id)['status'], 'running')
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(calls, ['1','2'])

    def test_same_output_directory_still_blocks_analysis_during_export(self):
        entered, calls = threading.Event(), []
        bundle = {'tasks': [{'task_id': '1', 'output_dir': str(self.root / 'edit'), 'clips': []}]}
        def export(*_args, **_kwargs):
            entered.set()
            self.release.wait(4)
            return {'completed': [], 'skipped': [], 'failed': []}
        with patch.object(background, 'export_smart_video_bundle', side_effect=export), \
             patch.object(background, 'analyze_smart_video_jobs', side_effect=lambda *_a, **_k: calls.append('analyzed') or {'tasks': [], 'summary': {}}):
            self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=bundle)
            self.wait_until(entered.is_set)
            queued = self.plugin._start_worker('analyze', {'output_folder_name': 'edit'}, jobs=[{'task_dir': str(self.root)}])
            self.app.processEvents()
            self.assertEqual(self.manager.get(queued.task_id)['status'], 'queued')
            self.assertEqual(calls, [])
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(calls, ['analyzed'])

    def test_two_exports_encode_together_but_native_alignment_never_overlaps(self):
        from model.SubtitleHelper import generate_srt_whisper_only
        inside = threading.Event()
        active = maximum = 0
        mutex = threading.Lock()
        rendered, aligned = [], []
        loaded = object()
        class Result:
            def merge_all_segments(self): return self
            def split_by_length(self, **_kw): return self
            def to_srt_vtt(self, path, **_kw):
                Path(path).write_text('1\n00:00:00,000 --> 00:00:01,000\nAmen.\n',encoding='utf-8')
        def align(model, audio, **_kw):
            nonlocal active, maximum
            self.assertIs(model,loaded)
            with mutex:
                active += 1; maximum = max(maximum,active)
            try:
                aligned.append(audio)
                if audio == '1.mp4':
                    inside.set(); self.release.wait(4)
                return Result()
            finally:
                with mutex: active -= 1
        def export(bundle, *_args, **kwargs):
            identifier = bundle['tasks'][0]['task_id']
            rendered.append(identifier)  # Simulate the completed encoder stage.
            generate_srt_whisper_only(identifier+'.mp4','Amen.',str(self.root/(identifier+'.srt')),
                model_loader=kwargs['alignment_model_loader'],cancelled=kwargs['cancelled'],progress=kwargs['progress'])
            return {'completed':[],'skipped':[],'failed':[]}
        other = {'tasks':[{'task_id':'2','output_dir':str(self.root/'other'),'clips':[]}]}
        with patch.object(background,'export_smart_video_bundle',side_effect=export), \
             patch('stable_whisper.alignment.align',side_effect=align), \
             patch('globalValue.globalValue.get_whisper_model',return_value=loaded) as load:
            first = self.plugin._start_worker('export',{},bundle=self.bundle,model_loader=Mock())
            self.wait_until(inside.is_set)
            second = self.plugin._start_worker('export',{},bundle=other,model_loader=Mock())
            self.wait_until(lambda: self.manager.get(second.task_id)['stage'].startswith('等待字幕模型'))
            self.assertEqual(rendered,['1','2'])
            self.assertEqual(aligned,['1.mp4']); self.assertEqual(load.call_count,1)
            self.assertEqual(self.manager.get(first.task_id)['status'],'running')
            self.assertEqual(self.manager.get(second.task_id)['status'],'running')
            self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(maximum,1); self.assertEqual(aligned,['1.mp4','2.mp4'])
        self.assertEqual(load.call_count,2)
        self.assertTrue(all(self.manager.get(job.task_id)['status']=='succeeded' for job in (first,second)))

    def test_cancel_export_waiting_only_for_model_does_not_cancel_other_encoder(self):
        from model.SubtitleHelper import generate_srt_whisper_only
        from model.WhisperInference import whisper_inference
        entered = threading.Event()
        def occupy():
            with whisper_inference():
                entered.set(); self.release.wait(4)
        owner = threading.Thread(target=occupy); owner.start()
        self.assertTrue(entered.wait(2))
        other = {'tasks':[{'task_id':'2','output_dir':str(self.root/'other'),'clips':[]}]}
        def export(bundle, *_args, **kwargs):
            if bundle['tasks'][0]['task_id']=='1':
                generate_srt_whisper_only('1.mp4','Amen.',str(self.root/'cancel.srt'),
                    model_loader=kwargs['alignment_model_loader'],cancelled=kwargs['cancelled'],progress=kwargs['progress'])
            return {'completed':[],'skipped':[],'failed':[]}
        try:
            with patch.object(background,'export_smart_video_bundle',side_effect=export), \
                 patch('globalValue.globalValue.get_whisper_model') as load:
                waiting = self.plugin._start_worker('export',{},bundle=self.bundle,model_loader=Mock())
                self.wait_until(lambda: self.manager.get(waiting.task_id)['stage'].startswith('等待字幕模型'))
                independent = self.plugin._start_worker('export',{'export_mode':'segments'},bundle=other)
                self.wait_until(lambda: self.manager.get(independent.task_id)['status']=='succeeded')
                waiting.requestInterruption()
                self.wait_until(lambda: self.manager.get(waiting.task_id)['status']=='canceled')
                load.assert_not_called()
                self.assertFalse(self.release.is_set())
                self.assertFalse((self.root/'cancel.srt').exists())
        finally:
            self.release.set(); owner.join(3)
            self.assertFalse(owner.is_alive())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG') and Path(os.environ['NODE_FFMPEG']).is_file(),'FFmpeg not configured')
    def test_real_ffmpeg_second_video_finishes_encoding_while_first_waits_in_alignment(self):
        from app_plugins.builtin.smart_video_editor import engine
        ffmpeg = os.environ['NODE_FFMPEG']
        source = self.root/'source.mp4'
        result = subprocess.run([ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','color=c=0x427d91:s=160x284:r=10:d=1.2',
            '-f','lavfi','-i','sine=frequency=330:sample_rate=48000:duration=1.2',
            '-c:v','libx264','-c:a','aac',str(source)],capture_output=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        before = source.read_bytes(); entered = threading.Event()
        class Result:
            def merge_all_segments(self): return self
            def split_by_length(self,**_kw): return self
            def to_srt_vtt(self,path,**_kw):
                Path(path).write_text('1\n00:00:00,000 --> 00:00:01,000\nAmen.\n',encoding='utf-8')
        def align(_model,video,**_kw):
            if Path(video).parent.name=='one':
                entered.set(); self.release.wait(8)
            return Result()
        def bundle(identifier,folder):
            return {'tasks':[{'task_id':identifier,'script':'Amen.','output_dir':str(self.root/folder),
                'clips':[{'source':str(source),'file_name':source.name,'kept_ranges':[[0,1]],'removed_seconds':0}]}]}
        settings = {'ffmpeg_path':ffmpeg,'existing_output':'overwrite'}
        with patch.object(engine,'validate_smart_video_bundle_for_export'), \
             patch('stable_whisper.alignment.align',side_effect=align), \
             patch('globalValue.globalValue.get_whisper_model',return_value=object()):
            first = self.plugin._start_worker('export',settings,bundle=bundle('1','one'),model_loader=Mock())
            self.wait_until(entered.is_set)
            second = self.plugin._start_worker('export',settings,bundle=bundle('2','two'),model_loader=Mock())
            self.wait_until(lambda: self.manager.get(second.task_id)['stage'].startswith('等待字幕模型'))
            self.assertTrue((self.root/'two'/'2_智能剪辑.mp4').is_file())
            self.assertFalse((self.root/'two'/'2_智能剪辑.srt').exists())
            self.assertEqual(self.manager.get(first.task_id)['status'],'running')
            self.release.set(); self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(source.read_bytes(),before)
        for handle,folder,identifier in ((first,'one','1'),(second,'two','2')):
            self.assertEqual(self.manager.get(handle.task_id)['status'],'succeeded')
            self.assertTrue((self.root/folder/(identifier+'_智能剪辑.srt')).is_file())

    def test_entry_points_do_not_block_when_previous_analysis_is_running(self):
        handle = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'queued'}])
        task = SimpleNamespace(task_id='next', task_name='next', task_audio_text='Amen')
        self.context.task_targets = Mock(return_value=[{'task': task, 'target_dir': str(self.root)}])
        self.context.task_language = Mock(return_value='sk')
        with patch.object(self.plugin, '_effective_settings', return_value={}), \
             patch.object(self.plugin, '_confirm_and_analyze') as confirm, \
             patch('app_plugins.builtin.smart_video_editor.plugin.discover_task_videos', return_value=[]):
            self.plugin.start_editor([1])
            self.assertEqual(confirm.call_args.args[0][0]['task_id'], 'next')
        handle.requestInterruption()

    def test_result_review_uses_batch_settings_not_latest_plugin_settings(self):
        self.plugin.settings = {'auto_export_clean': True}
        bundle = {'tasks': [], 'summary': {}, 'settings': {'auto_export_clean': False}}
        with patch('app_plugins.builtin.smart_video_editor.plugin.SmartVideoReviewDialog.get_reviewed_bundle', return_value=None) as review:
            self.plugin._handle_analysis(bundle)
            review.assert_called_once()

    def test_nested_review_is_deferred_without_losing_pending_result(self):
        self.plugin._review_open = True
        with patch.object(background, 'analyze_smart_video_jobs', return_value={'tasks': [], 'summary': {}}):
            handle = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': '1'}])
            self.wait_until(lambda: not self.manager.is_busy())
        handle.show_result()
        self.assertTrue(handle.has_pending_result())
        self.review.assert_not_called()
        self.plugin._review_open = False
        self.wait_until(lambda: not handle.has_pending_result())
        self.review.assert_called_once()

    def test_clean_task_auto_export_preference_is_preserved(self):
        result = {'tasks': [], 'summary': {}, 'settings': {'auto_export_clean': True}}
        with patch.object(background, 'analyze_smart_video_jobs', return_value=result), \
             patch.object(self.plugin, '_handle_analysis') as complete:
            handle = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'clean'}])
            self.wait_until(lambda: not self.manager.is_busy())
            complete.assert_called_once_with(result)
        self.assertFalse(handle.has_pending_result())

    def test_missing_source_is_reported_without_escaping_qt_callback(self):
        bundle = {'tasks': [{'output_dir': str(self.root),
                            'clips': [{'source': str(self.root / 'missing.mp4')}]}]}
        self.assertIsNone(self.plugin._start_worker('export', {'export_mode': 'segments'}, bundle=bundle))
        self.context.notify.assert_called_once()
        self.assertFalse(self.manager.is_busy())

    def test_exit_does_not_start_render_or_open_review(self):
        self.manager.stop_accepting()
        self.assertIsNone(self.plugin._start_worker('export', {}, bundle=self.bundle))
        with patch('app_plugins.builtin.smart_video_editor.plugin.BreathCutReviewDialog') as review:
            self.plugin._handle_breath_analysis({'summary': {}})
            review.assert_not_called()
        self.assertFalse(self.manager.is_busy())

    def test_aborted_close_does_not_disable_future_automatic_review(self):
        result = {'tasks': [], 'summary': {}}
        with patch.object(background, 'analyze_smart_video_jobs', return_value=result):
            first = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'first'}])
            self.wait_until(lambda: self.review.call_count == 1)
            self.assertEqual(self.plugin.can_close(), (True, ''))
            self.assertFalse(self.plugin._front_results.stopped)
            self.manager.stop_accepting()
            self.plugin._front_results.enqueue('closed-gate', lambda: None)
            self.plugin._front_results._drain()
            self.manager.resume_accepting()
            second = self.plugin._start_worker('analyze', {}, jobs=[{'task_id': 'second'}])
            self.wait_until(lambda: self.review.call_count == 2)
        self.assertFalse(first.has_pending_result())
        self.assertFalse(second.has_pending_result())

    def test_host_idle_keeps_exit_gate_closed_for_deferred_review_callback(self):
        from PYUI.main_pyui import MainDialog
        window = SimpleNamespace(_background_exit_pending=True, background_tasks=self.manager, close=Mock())
        self.manager.stop_accepting()
        with patch('PYUI.main_pyui.QtCore.QTimer.singleShot') as timer:
            MainDialog.onBackgroundTasksIdle(window)
        self.assertFalse(self.manager.is_accepting())
        self.assertFalse(window._background_exit_pending)
        self.assertEqual(timer.call_args.args[1], window.close)


if __name__ == '__main__':
    unittest.main()

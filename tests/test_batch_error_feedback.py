import copy
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.store import Store,fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.export_validation import validate_caption_voice,VOICE_REQUIRED
from app_plugins.builtin.batch_text_video.export_results_ui import show_action_error
from app_plugins.builtin.batch_text_video.background import execute_batch


class ErrorFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=Store(self.root);self.state=fresh_state()
        self.layer=normalize_layers([new_caption()])[0]
        self.state['settings'].update(layers=[self.layer],use_voice=False)
        self.job=dict(id='job',path='',name='Test video',title='',body='Pane prosím',status='待生成')
        self.state['jobs']=[self.job];self.store.save(self.state,0)

    def dialog(self):
        with patch.object(BatchTextVideoDialog,'request_background'):
            dialog=BatchTextVideoDialog(store=self.store)
        self.addCleanup(dialog.deleteLater);self.addCleanup(dialog.close)
        return dialog

    def test_missing_voice_preflight_is_specific_and_never_scans_task_folders(self):
        before=copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError,'Test video'),patch('app_plugins.builtin.batch_text_video.matching.folder_entry',side_effect=AssertionError('GUI directory scan')):
            validate_caption_voice(self.state,[self.job])
        self.assertEqual(self.state,before)
        self.state['settings']['use_voice']=True
        validate_caption_voice(self.state,[{**self.job,'task_dir':'undiscovered'}])
        self.state['settings']['layers'][0]['enabled']=False
        validate_caption_voice(self.state,[self.job])

    def test_play_click_shows_actionable_popup_before_starting_any_worker(self):
        dialog=self.dialog();dialog.active_entry=lambda:dict(self.job)
        with patch('app_plugins.builtin.batch_text_video.playback.PlaybackWorker.start') as start:
            dialog.play_preview()
        self.assertFalse(start.called);self.assertIsNone(dialog.playback_worker)
        box=dialog._action_warning;self.assertTrue(box.isVisible());self.assertFalse(box.isModal())
        self.assertIn('动态字幕需要人声',box.text());self.assertIn('关闭对应',box.informativeText())
        button=next(b for b in box.buttons() if b.text()=='人声设置');button.click()
        self.assertEqual(dialog.right_tabs.currentIndex(),1)
        self.assertTrue(dialog.project_sections.sections['audio'].content.isVisibleTo(dialog.project_sections))

    def test_worker_preview_failure_visible_but_stale_or_cancelled_results_are_quiet(self):
        dialog=self.dialog();dialog._playback_key='current'
        dialog._playback_ready(dict(key='old',error=VOICE_REQUIRED));self.assertIsNone(getattr(dialog,'_action_warning',None))
        dialog._playback_ready(dict(key='current',cancelled=True));self.assertIsNone(getattr(dialog,'_action_warning',None))
        dialog._playback_ready(dict(key='current',error=VOICE_REQUIRED,traceback='Traceback diagnostic'))
        self.assertTrue(dialog._action_warning.isVisible());self.assertIn('Traceback',dialog._action_warning.detailedText())

    def test_quick_export_invalid_caption_does_not_save_or_start(self):
        dialog=self.dialog();dialog.active_entry=lambda:dict(self.job)
        revision=self.store.load()['revision']
        with patch.object(dialog,'_start_quick_export') as start:dialog.quick_export_current()
        self.assertFalse(start.called);self.assertEqual(self.store.load()['revision'],revision)
        self.assertTrue(dialog._action_warning.isVisible())

    def test_batch_preflight_stops_before_renderer_and_queue_submission(self):
        dialog=self.dialog()
        with patch('app_plugins.builtin.batch_text_video.cloud_alignment.prepare_binding') as binding:
            self.assertFalse(dialog._launch_batch(['job']))
        self.assertFalse(binding.called);self.assertTrue(dialog._action_warning.isVisible())
        renderer=Mock();messages=[]
        result,_=execute_batch(self.store,self.store.load(),['job'],threading.Event(),lambda *_:None,messages.append,lambda *_:None,{'static_text':renderer})
        self.assertFalse(renderer.called);self.assertIn('动态字幕需要人声',result['error'])

    def test_plain_backend_failure_in_other_profile_is_visible_and_reopenable(self):
        dialog=self.dialog();handle=SimpleNamespace(task_id='t',manager=SimpleNamespace(get=lambda _:dict(status='failed')),set_result_handler=Mock())
        dialog._host_batch_failed('other','磁盘空间不足',handle=handle)
        self.assertTrue(dialog._action_warning.isVisible());self.assertIn('other',dialog._action_warning.informativeText())
        self.assertTrue(handle.set_result_handler.call_args.kwargs['pending'])
        dialog._host_batch_failed('other','磁盘空间不足',handle=handle)
        self.assertEqual(handle.set_result_handler.call_count,1)

    def test_structured_partial_failure_shows_one_warning_across_completed_failed_signals(self):
        dialog=self.dialog();handle=SimpleNamespace(set_result_handler=Mock())
        result=dict(completed=1,failed=1,failures=['任务2：'+VOICE_REQUIRED],paths=[])
        profile=dialog.state['active_profile'];dialog._host_batch_result(handle,profile,result)
        box=dialog._batch_warning;self.assertTrue(box.isVisible())
        dialog._host_batch_failed(profile,'generic error',handle=handle)
        self.assertIs(dialog._batch_warning,box);self.assertIsNone(getattr(dialog,'_action_warning',None))

    def test_cancel_has_no_error_popup_and_error_window_has_no_unrelated_log(self):
        dialog=self.dialog();dialog._host_batch_failed(dialog.state['active_profile'],'操作已取消。')
        self.assertIsNone(getattr(dialog,'_action_warning',None))
        box=show_action_error(dialog,'无法播放预览',VOICE_REQUIRED,details='one trace')
        self.assertNotIn('ai_agent_assistant',box.text()+box.informativeText())
        root=os.environ.get('LZX_CURSOR_QA')
        if root:self.app.processEvents();box.grab().save(str(Path(root)/'missing-voice-warning.png'))

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.media_errors import describe,memory_failure,MediaMemoryError
from app_plugins.builtin.batch_text_video.matting_live import Inputs,Feed
from app_plugins.builtin.batch_text_video.row_progress import PROGRESS_ROLE
from test_export_total_list import TotalListTests
from test_batch_encoding_stall import EncodingStallTests


class MemoryFeedbackTests(unittest.TestCase):
    def test_blank_memory_error_retains_actionable_reason(self):
        self.assertTrue(memory_failure(MemoryError()))
        self.assertIn('系统可提交内存不足',describe(MemoryError()))
        self.assertIn('并发',describe(MemoryError()))
        self.assertEqual(describe(AssertionError()),'AssertionError')

    def test_causes_ffmpeg_enomem_and_paging_errors_are_classified_without_gpu_false_positive(self):
        outer=RuntimeError('内存AI抠像失败：');outer.__cause__=MemoryError()
        for error in (outer,RuntimeError('Task finished with error code: -12 (Cannot allocate memory)'),OSError(12,'allocation failed'),OSError('The paging file is too small')):
            self.assertTrue(memory_failure(error))
        self.assertFalse(memory_failure(RuntimeError('CUDA out of memory')))
        self.assertFalse(memory_failure(FileNotFoundError('missing video')))
        recursive=RuntimeError('bad');recursive.__cause__=recursive
        self.assertFalse(memory_failure(recursive))

    def test_feed_labels_visual_loop_separately_from_ai_and_preserves_memory_cause(self):
        with patch('app_plugins.builtin.batch_text_video.matting_pipe.MemoryPipe'):
            visual=Feed(SimpleNamespace(binding={'mode':'visual_loop'}))
            ai=Feed(SimpleNamespace(binding={'mode':'dynamic'}))
        self.assertEqual(visual.error_prefix,'视频循环叠化失败：')
        self.assertEqual(ai.error_prefix,'内存AI抠像失败：')
        visual.error=MemoryError();inputs=Inputs([]);inputs.feeds=[visual]
        with self.assertRaisesRegex(MediaMemoryError,'视频循环叠化失败.*可提交内存不足') as caught:inputs.check()
        self.assertIs(caught.exception.__cause__,visual.error)
        self.assertNotIn('AI',str(caught.exception))

    def test_unknown_blank_error_is_never_an_empty_message(self):
        inputs=Inputs([]);inputs.feeds=[SimpleNamespace(error=AssertionError(),error_prefix='视频循环叠化失败：')]
        with self.assertRaisesRegex(RuntimeError,'AssertionError'):inputs.check()

    def test_system_memory_exhaustion_never_retries_cpu_or_publishes(self):
        for failure in (MediaMemoryError('系统可提交内存不足'),RuntimeError('QSV: Cannot allocate memory')):
            prepared=EncodingStallTests().prepared()
            with patch('app_plugins.builtin.batch_text_video.engine._run',side_effect=failure) as run,patch('app_plugins.builtin.batch_text_video.engine.probe') as probe:
                with self.assertRaises(MediaMemoryError):prepared._encode()
            run.assert_called_once();probe.assert_not_called();self.assertFalse(prepared.encoded)
            with self.assertRaisesRegex(RuntimeError,'尚未编码'):prepared.publish()


class MemoryUiTests(TotalListTests):
    def test_failed_row_clears_99_and_click_displays_actionable_error(self):
        state=self.store.load();state['jobs'][0].update(status='需处理',error=describe(MemoryError()))
        self.dialog.state=self.store.save(state,state['revision'])
        self.dialog._render_progress={'default':{'same':100}}
        self.dialog.refresh_tables();self.flat.table.setCurrentCell(0,0);self.app.processEvents()
        self.assertEqual(self.flat.table.item(0,0).data(PROGRESS_ROLE),None)
        self.assertEqual(self.flat.table.item(0,3).text(),'—')
        self.assertEqual(self.flat.table.item(0,2).text(),'需处理')
        self.assertIn('降低渲染并发',self.flat.detail.text())
        self.assertEqual(self.flat.table.item(0,2).foreground().color().name(),'#b91c1c')

    def test_old_blank_ai_error_is_explained_without_guessing_cause(self):
        state=self.store.load();state['jobs'][0].update(status='需处理',error='内存AI抠像失败：')
        self.dialog.state=self.store.save(state,state['revision']);self.dialog.refresh_tables()
        self.flat.table.setCurrentCell(0,0);self.app.processEvents()
        self.assertIn('旧记录未保存详细原因',self.flat.detail.text())
        self.assertNotIn('内存不足',self.flat.detail.text())


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(MemoryFeedbackTests))
    for name in MemoryUiTests.__dict__:
        if name.startswith('test_'):suite.addTest(MemoryUiTests(name))
    return suite

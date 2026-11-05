"""Keep completed/total visible during transfer, without per-file popups."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from qt_compat import QtCore
from app_plugins.builtin.ai_agent_assistant.delivery import RECEIPT_PROGRESS_PREFIX
from app_plugins.builtin.ai_agent_assistant.notice_data import receipt_badge,notice_snapshot,run_notice_snapshot
from app_plugins.builtin.ai_agent_assistant.notices import ReceiptNotices
import test_agent_delivery as delivery
import test_agent_partial_receipts as partial
import test_agent_badges as badges


class ProgressTextTests(unittest.TestCase):
    def current(self,**changes):
        return {'status':'正在下载','received':5,'total':10,'missing':0,'view_scope_ok':True,
            'verified_received':False,'verified_partial':False,'receipt_archived':False,**changes}

    def test_pending_transfer_and_retry_keep_five_of_ten(self):
        for status,label in (('正在下载','收取中'),('等待继续下载','收取中'),
                             ('下载待重试','收取待重试'),('收取待重试','收取待重试')):
            self.assertEqual(receipt_badge(self.current(status=status),unread=5,seen_versions=[])[0],label+' 5/10')

    def test_unknown_total_is_not_false_zero_of_zero(self):
        self.assertEqual(receipt_badge(self.current(received=0,total=0),unread=0,seen_versions=[])[0],'收取中 0/?')

    def test_completed_and_explicitly_viewed_remain_short(self):
        state=self.current(status='已下载',received=10)
        self.assertEqual(receipt_badge(state,unread=10,seen_versions=[])[0],'已收取')
        self.assertEqual(receipt_badge(state,unread=0,seen_versions=[['job','video','sha']])[0],'已查看')


class ProgressWorkerTests(unittest.TestCase):
    setUp=delivery.AgentReceiptTests.setUp
    stage=delivery.AgentReceiptTests.stage
    cloud=delivery.AgentReceiptTests.cloud
    download=delivery.AgentReceiptTests.download
    receipt=delivery.AgentReceiptTests.receipt
    prepare=partial.PartialReceiptTests.prepare

    def test_worker_reports_file_counts_and_keeps_them_during_file_bytes(self):
        self.prepare(10,set())
        def downloading(*args):
            args[-1].report('正在下载视频字节',50)
            return self.download(*args)
        self.fetch.side_effect=downloading
        result=self.receipt()
        messages=[call.args[0] for call in self.run.report.call_args_list]
        self.assertIn(RECEIPT_PROGRESS_PREFIX+'任务 001 · 5/10',messages)
        self.assertIn('任务 001 · 5/10 · 正在下载视频字节',messages)
        self.assertEqual(result['task_badges']['items'][0]['text'],'收取中 8/10')


class SilentProgressDataTests(unittest.TestCase):
    setUp=badges.NoticeDataTests.setUp
    file=badges.NoticeDataTests.file
    done=badges.NoticeDataTests.done

    def test_progress_snapshot_does_not_claim_or_repeat_new_video_notifications(self):
        self.file();self.done(10);self.store.delivery_note(self.key,'正在下载','1/10')
        result=run_notice_snapshot({'root':str(self.store.root.path),'announce':False,'transient':True},
            SimpleNamespace(check_canceled=Mock()))
        self.assertEqual(result['items'][0]['text'],'收取中 1/10')
        self.assertEqual(result['notify_files'],0);self.assertTrue(result['transient'])
        self.assertEqual(notice_snapshot(self.store)['notify_files'],1)


class ProgressMonitorTests(unittest.TestCase):
    setUpClass=delivery.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=delivery.ReceiptMonitorTests.setUp
    tearDown=delivery.ReceiptMonitorTests.tearDown

    def test_progress_refreshes_main_badges_silently_and_coalesces_events(self):
        monitor=self.plugin.receipts;monitor.handle=self.handle
        with patch.object(self.plugin.notices,'refresh') as refresh:
            monitor.progress('普通字节进度',40);self.assertFalse(monitor.badge_timer.isActive())
            for index in range(20):monitor.progress(RECEIPT_PROGRESS_PREFIX+f'任务 001 · {index}/30',None)
            self.assertTrue(monitor.badge_timer.isSingleShot());self.assertTrue(monitor.badge_timer.isActive())
            refresh.assert_not_called()
            monitor.refresh_progress_badges();refresh.assert_called_once_with(announce=False)
            monitor.completed({'message':'本轮结束','downloaded':0})
            self.assertFalse(monitor.badge_timer.isActive())
        self.handle.finished.emit()

    def test_progress_snapshot_does_not_invalidate_final_receipt_or_manual_changes(self):
        parent=QtCore.QObject();self.addCleanup(parent.deleteLater)
        parent.context=SimpleNamespace(set_task_badges=Mock(),notify=Mock(),log=Mock())
        controller=ReceiptNotices(parent)
        controller.apply_snapshot({'items':[],'notify_files':0,'ui_generation':0,'transient':True})
        self.assertEqual(controller.generation,0)
        controller.apply_snapshot({'items':[],'notify_files':1,'notify_tasks':1,'ui_generation':0})
        self.assertEqual(controller.generation,1);parent.context.notify.assert_called_once()
        parent.context.set_task_badges.reset_mock()
        controller.apply_snapshot({'items':[],'notify_files':0,'ui_generation':0,'transient':True})
        parent.context.set_task_badges.assert_not_called();self.assertTrue(controller.pending)

    def test_coalescing_silent_refresh_does_not_discard_explicit_notification_request(self):
        self.context.set_task_badges=Mock()
        controller=self.plugin.notices;controller.handle=self.handle
        controller.refresh(announce=False);self.assertFalse(controller.pending_announce)
        controller.refresh();self.assertTrue(controller.pending_announce)
        controller.refresh(announce=False);self.assertTrue(controller.pending_announce)
        controller.handle=None;controller.pending=False;controller.pending_announce=False

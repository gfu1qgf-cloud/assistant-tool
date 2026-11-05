"""Production state, receipt completeness and manual review are independent."""
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot,mark_versions_seen,receipt_badge
import test_agent_delivery as delivery
import test_agent_partial_receipts as partial


class ReceiptPolicyWorkerTests(unittest.TestCase):
    setUp=delivery.AgentReceiptTests.setUp
    stage=delivery.AgentReceiptTests.stage
    cloud=delivery.AgentReceiptTests.cloud
    download=delivery.AgentReceiptTests.download
    receipt=partial.PartialReceiptTests.receipt
    prepare=partial.PartialReceiptTests.prepare
    drain=partial.PartialReceiptTests.drain

    def badge(self,result):
        badge=result['task_badges']['items'][0]
        self.assertEqual(badge['text'],result['task_badges']['job_summaries'][self.job['id']]['text'])
        return badge

    def test_ordinary_check_waits_for_execution_to_finish_without_enumerating_or_downloading(self):
        self.prepare(10,set(range(6,11)))
        for status in ('执行中','已领取','待确认','失败'):
            with self.subTest(status=status):
                self.api.rows[0][delivery.COLS['status']]=status
                result=self.receipt()
                self.fetch.assert_not_called();self.assertEqual(self.api.writes,[])
                self.assertEqual(self.badge(result)['text'],'等待制作完成 0/10')

    def test_explicit_five_of_ten_is_partial_not_completed_and_regular_poll_does_not_collect_more(self):
        self.prepare(10,set(range(6,11)));self.api.rows[0][delivery.COLS['status']]='执行中'
        result=self.drain(allow_incomplete=True)
        self.assertEqual(self.fetch.call_count,5);self.assertEqual(self.api.writes,[])
        self.assertEqual(self.badge(result)['text'],'待补齐 5/10')
        self.items.append({'id':'video_6','name':'任务001_第06段.mp4','url':delivery.LINK})
        result=self.receipt()
        self.assertEqual(self.fetch.call_count,5);self.assertEqual(self.badge(result)['text'],'待补齐 5/10')
        self.api.rows[0][delivery.COLS['status']]='已完成'
        result=self.receipt()
        self.assertEqual(self.fetch.call_count,6);self.assertEqual(self.badge(result)['text'],'待补齐 6/10')
        self.assertEqual(self.api.writes,[])

    def test_finished_cloud_task_with_missing_files_keeps_count_until_all_segments_arrive(self):
        self.prepare(10,set(range(6,11)));first=self.drain()
        self.assertEqual(self.badge(first)['text'],'待补齐 5/10')
        for number in range(6,11):
            self.items.append({'id':f'video_{number}','name':f'任务001_第{number:02}段.mp4','url':delivery.LINK})
        last=self.drain()
        self.assertEqual(self.fetch.call_count,10);self.assertEqual(self.badge(last)['text'],'已收取')
        self.assertEqual(len(self.api.writes),1)

    def test_all_current_files_while_running_are_not_a_completed_task(self):
        self.prepare(2,set());self.api.rows[0][delivery.COLS['status']]='执行中'
        result=self.drain(allow_incomplete=True)
        self.assertEqual(self.badge(result)['text'],'等待制作完成 2/2')
        self.assertEqual(self.api.writes,[])
        self.api.rows[0][delivery.COLS['status']]='已完成';result=self.drain()
        self.assertEqual(self.badge(result)['text'],'已收取');self.assertEqual(self.fetch.call_count,2)

    def test_unknown_production_total_is_a_question_mark_not_current_file_count(self):
        self.prepare(2,set());self.api.rows[0][delivery.COLS['spoken']]='Unsegmented original script.'
        self.api.rows[0][delivery.COLS['status']]='执行中'
        result=self.drain(allow_incomplete=True)
        self.assertEqual(self.badge(result)['text'],'等待制作完成 2/?')
        self.assertIn('2/?',self.badge(result)['tooltip']);self.assertEqual(self.api.writes,[])

    def test_manual_view_overrides_partial_warning_but_new_segment_restores_count(self):
        self.prepare(10,set(range(6,11)));result=self.drain()
        mark_versions_seen(self.store,result['task_badges']['items'][0]['versions'])
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['text'],'已查看')
        self.items.append({'id':'video_6','name':'任务001_第06段.mp4','url':delivery.LINK})
        result=self.drain()
        self.assertEqual(self.badge(result)['text'],'待补齐 6/10')


class ReceiptPolicyMonitorTests(unittest.TestCase):
    setUpClass=delivery.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=delivery.ReceiptMonitorTests.setUp
    tearDown=delivery.ReceiptMonitorTests.tearDown
    stage=delivery.ReceiptMonitorTests.stage

    def test_manual_force_and_automatic_do_not_imply_partial_permission(self):
        self.stage()
        for options in ({},{'manual':True},{'manual':True,'force':True,'ids':['chosen']}):
            with patch.object(self.plugin.receipts,'download_authorization',return_value=None):
                self.plugin.receipts.check(**options)
            self.assertFalse(self.context.submit_background_task.call_args.kwargs['payload']['allow_incomplete'])
            self.handle.finished.emit()

    def test_explicit_partial_requires_confirmation_and_scope(self):
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No),patch.object(self.plugin.receipts,'check') as check:
            self.plugin.request_partial_receipt(ids=['chosen']);check.assert_not_called()
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes),patch.object(self.plugin.receipts,'check') as check:
            self.plugin.request_partial_receipt(ids=['chosen'])
            check.assert_called_once_with(manual=True,ids=['chosen'],directories=None,allow_incomplete=True)
        for options in ({'manual':True},{'ids':['chosen']},{}):
            with self.assertRaises(ValueError):self.plugin.receipts.check(allow_incomplete=True,**options)

    def test_explicit_scope_is_preserved_for_continuation_but_not_later_generic_checks(self):
        self.stage();monitor=self.plugin.receipts;monitor.start()
        with patch.object(monitor,'download_authorization',return_value=None):
            monitor.check(manual=True,ids=['chosen'],allow_incomplete=True)
            self.assertTrue(self.context.submit_background_task.call_args.kwargs['payload']['allow_incomplete'])
            monitor.completed({'message':'继续','continue_ids':['chosen'],'downloaded':8})
            self.handle.finished.emit();monitor.tick()
            payload=self.context.submit_background_task.call_args.kwargs['payload']
            self.assertEqual(payload['ids'],['chosen']);self.assertTrue(payload['allow_incomplete'])
            self.handle.finished.emit();monitor.check(manual=True)
            self.assertFalse(self.context.submit_background_task.call_args.kwargs['payload']['allow_incomplete'])
            self.handle.finished.emit();monitor.stop()

    def test_assistant_receipt_cell_uses_same_summary_and_keeps_selection(self):
        self.stage();self.context.task_language=lambda _: 'sk'
        dialog=self.plugin.open_dialog();dialog.table.selectRow(0)
        selected=dialog.selected_ids();key=dialog.table.item(0,0).data(256)
        self.plugin.store.delivery_note(key,'已收取部分','5/10；仍在制作。')
        summary={'text':'待补齐 5/10','tone':'warning','tooltip':'缺少第6–10段；云端执行中。'}
        self.plugin.notices.apply_snapshot({'items':[],'job_summaries':{key:summary},'notify_files':0})
        self.assertEqual(dialog.table.item(0,6).text(),'待补齐 5/10')
        self.assertEqual(dialog.table.item(0,6).toolTip(),summary['tooltip'])
        self.assertEqual(dialog.selected_ids(),selected)
        dialog.reload();self.assertEqual(dialog.table.item(0,6).text(),'待补齐 5/10')

    def test_untouched_draft_is_not_relabelled_as_a_receipt_failure(self):
        self.stage();self.context.task_language=lambda _: 'sk'
        dialog=self.plugin.open_dialog();before=dialog.table.item(0,6).text()
        snapshot=notice_snapshot(self.plugin.store,announce=False)
        self.assertEqual(snapshot['job_summaries'],{})
        self.plugin.notices.apply_snapshot(snapshot)
        self.assertEqual(dialog.table.item(0,6).text(),before)


class LegacyReceiptPolicyTests(unittest.TestCase):
    def test_excess_files_do_not_rewrite_expected_total_or_claim_completion(self):
        current={'status':'已下载','received':12,'total':10,'missing':0,'view_scope_ok':True,
            'verified_partial':False,'verified_received':False,'receipt_archived':False}
        self.assertEqual(receipt_badge(current,unread=12,seen_versions=[])[0],'收取待核对 12/10')

    def test_partial_receipt_proof_never_means_completed_and_keeps_five_of_ten(self):
        current={'status':'已收取部分','received':5,'total':10,'missing':0,'view_scope_ok':True,
            'verified_partial':True,'verified_received':False,'receipt_archived':False}
        for status in ('已收取部分','已收取·任务未完成','收取待重试'):
            current['status']=status
            self.assertEqual(receipt_badge(current,unread=5,seen_versions=[])[0],'待补齐 5/10')

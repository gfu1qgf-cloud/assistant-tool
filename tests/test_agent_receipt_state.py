"""Exercise actual receipt lifecycle, not a blanket expected label snapshot."""
import unittest
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot,mark_versions_seen,receipt_badge
import test_agent_partial_receipts as fixture
import test_agent_delivery as delivery


class ReceiptLifecycleTests(unittest.TestCase):
    setUp=fixture.PartialReceiptTests.setUp
    stage=fixture.PartialReceiptTests.stage
    cloud=fixture.PartialReceiptTests.cloud
    download=fixture.PartialReceiptTests.download
    receipt=fixture.PartialReceiptTests.receipt
    prepare=fixture.PartialReceiptTests.prepare
    drain=fixture.PartialReceiptTests.drain
    def test_18_files_stay_receiving_during_each_download_and_budget_continuation(self):
        self.prepare(18,set())
        self.api.rows[0][delivery.COLS['ai_review']]='AI预审不通过｜第3段需复核'
        observed=[]
        def downloading(*args):
            item=notice_snapshot(self.store,announce=False)['items'][0]
            observed.append(item['text'])
            self.assertEqual(item['text'],'收取中')
            self.assertNotEqual(item['text'],'已查看')
            return self.download(*args)
        self.fetch.side_effect=downloading
        for expected_count in (8,16):
            result=self.receipt()
            self.assertEqual(self.fetch.call_count,expected_count)
            self.assertTrue(result['continue_ids'])
            item=result['task_badges']['items'][0]
            self.assertEqual(item['text'],'收取中');self.assertEqual(item['tone'],'error')
            self.assertIn(f'{expected_count}/18',item['tooltip'])
            self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'],'等待继续下载')
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_seen').fetchone()[0],0)
        result=self.receipt()
        self.assertEqual(self.fetch.call_count,18);self.assertEqual(observed,['收取中']*18)
        self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'],'已下载')
        self.assertEqual(len(self.api.writes),1)
        versions=result['task_badges']['items'][0]['versions']
        mark_versions_seen(self.store,versions)
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['text'],'已查看')

    def test_available_partial_set_is_received_only_after_its_files_finish(self):
        self.prepare(18,{9})
        for expected_count in (8,16):
            result=self.receipt()
            self.assertEqual(self.fetch.call_count,expected_count)
            self.assertEqual(result['task_badges']['items'][0]['text'],'收取中')
        result=self.receipt()
        self.assertEqual(self.fetch.call_count,17);self.assertEqual(self.api.writes,[])
        item=result['task_badges']['items'][0]
        self.assertEqual(item['text'],'已收取');self.assertEqual(item['tone'],'warning')
        self.assertIn('尚未核实段号：9',item['tooltip'])
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))

    def test_forced_recollection_does_not_display_old_completion_during_transfer(self):
        self.receipt()
        self.fetch.reset_mock()
        observed=[]
        def downloading(*args):
            observed.append(notice_snapshot(self.store,announce=False)['items'][0]['text'])
            return self.download(*args)
        self.fetch.side_effect=downloading
        self.receipt(force=True,ids=[self.job['id']])
        self.assertEqual(observed,['收取中'])


class BadgeTransitionTests(unittest.TestCase):
    def test_full_file_count_is_not_completion_while_worker_is_still_downloading(self):
        current={'status':'正在下载','received':1,'total':1,'missing':0,'view_scope_ok':True,
                 'verified_received':True,'receipt_archived':True,'verified_partial':False}
        for status in ('正在下载','等待继续下载','下载待重试'):
            current['status']=status
            text,_tone,_human=receipt_badge(current,unread=0,seen_versions=[['old','file','sha']])
            self.assertNotIn(text,{'已收取','已查看'})
        current['status']='已下载'
        self.assertEqual(receipt_badge(current,unread=1,seen_versions=[])[0],'已收取')
        self.assertEqual(receipt_badge(current,unread=0,seen_versions=[['job','file','sha']])[0],'已查看')

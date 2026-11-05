"""Incomplete oral deliveries are downloadable, never silently fully archived."""
import unittest
from app_plugins.builtin.ai_agent_assistant.notice_data import mark_versions_seen,notice_snapshot
import test_agent_delivery as fixture


class PartialReceiptTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download
    receipt=fixture.AgentReceiptTests.receipt
    def prepare(self,total,missing):
        self.api.rows[0][fixture.COLS['spoken']]='\n\n'.join(f'【第{i}段｜约10秒】\nExact segment {i}.' for i in range(1,total+1))
        self.items=[{'id':f'video_{i}','name':f'任务001_第{i:02}段.mp4','url':fixture.LINK} for i in range(1,total+1) if i not in missing]
    def drain(self,**options):
        result=self.receipt(**options)
        for _ in range(10):
            if not result['continue_ids']: return result
            result=self.receipt(ids=result['continue_ids'],**options)
        self.fail('finite continuation did not finish')
    def test_17_of_18_with_user_owned_segment_downloads_and_lists_missing_9(self):
        self.prepare(18,{9}); result=self.drain()
        self.assertEqual(self.fetch.call_count,17); self.assertEqual(self.api.writes,[])
        item=result['task_badges']['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertEqual(item['tone'],'warning'); self.assertIn('尚未核实段号：9',item['tooltip'])
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))
        self.receipt(); self.assertEqual(self.fetch.call_count,17)
    def test_later_missing_segment_only_downloads_new_one_then_can_archive(self):
        self.prepare(3,{2}); self.drain(); self.assertEqual(self.api.writes,[])
        self.items.append({'id':'video_2','name':'任务001_第02段.mp4','url':fixture.LINK})
        result=self.drain(); self.assertEqual(self.fetch.call_count,3); self.assertEqual(len(self.api.writes),1)
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'],'已下载')
    def test_manual_can_collect_uploaded_parts_while_working_or_awaiting_confirmation(self):
        self.prepare(2,{2})
        for state in ('执行中','已领取','待确认','失败'):
            self.api.rows[0][fixture.COLS['status']]=state; self.fetch.reset_mock()
            self.receipt(); self.fetch.assert_not_called()
            self.receipt(allow_incomplete=True)
            self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'],'已收取部分')
            self.assertEqual(self.api.writes,[])
    def test_manual_does_not_collect_canceled_or_unclaimed_rows(self):
        self.prepare(2,{2})
        for state in ('已取消','待领取'):
            self.api.rows[0][fixture.COLS['status']]=state; self.receipt(allow_incomplete=True)
        self.fetch.assert_not_called(); self.assertEqual(self.api.writes,[])
    def test_later_automatic_poll_does_not_erase_manual_partial_receipt(self):
        self.prepare(2,{2}); self.api.rows[0][fixture.COLS['status']]='执行中'
        self.drain(allow_incomplete=True); self.fetch.reset_mock()
        item=self.receipt()['task_badges']['items'][0]
        self.fetch.assert_not_called(); self.assertEqual(item['text'],'已收取'); self.assertIn('1/2',item['tooltip'])
        self.assertIn('尚未核实段号：2',item['tooltip']); self.assertEqual(self.api.writes,[])
    def test_full_file_count_while_executing_is_downloaded_not_archived(self):
        self.prepare(2,set()); self.api.rows[0][fixture.COLS['status']]='执行中'
        self.drain(allow_incomplete=True)
        self.assertEqual(self.fetch.call_count,2); self.assertEqual(self.api.writes,[])
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'],'已收取·任务未完成')
        self.api.rows[0][fixture.COLS['status']]='已完成'; self.drain()
        self.assertEqual(self.fetch.call_count,2); self.assertEqual(len(self.api.writes),1)
    def test_partial_seen_marker_is_preserved_but_new_segment_is_unread(self):
        self.prepare(3,{2}); self.drain()
        versions=[[self.job['id'],fid,record['sha256']] for fid,record in self.store.delivery_files(self.job['id']).items() if record.get('state')=='downloaded']
        mark_versions_seen(self.store,versions)
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['text'],'已查看')
        self.items.append({'id':'video_2','name':'任务001_第02段.mp4','url':fixture.LINK}); self.drain()
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['unread'],1)
    def test_wrong_owner_duplicate_or_out_of_range_still_do_not_guess(self):
        self.prepare(3,{2}); self.items[0]['name']='任务OTHER_第01段.mp4'
        self.receipt(allow_incomplete=True); self.fetch.assert_not_called(); self.assertEqual(self.api.writes,[])


class PartialMonitorTests(unittest.TestCase):
    setUpClass=fixture.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=fixture.ReceiptMonitorTests.setUp
    tearDown=fixture.ReceiptMonitorTests.tearDown
    stage=fixture.ReceiptMonitorTests.stage
    def test_manual_passes_explicit_partial_permission_but_automatic_does_not(self):
        from unittest.mock import patch
        self.stage()
        with patch.object(self.plugin.receipts,'download_authorization',return_value=None):
            self.plugin.receipts.check(manual=True)
            self.assertTrue(self.context.submit_background_task.call_args.kwargs['payload']['allow_incomplete'])
            self.handle.finished.emit()
            self.plugin.receipts.check()
            self.assertFalse(self.context.submit_background_task.call_args.kwargs['payload']['allow_incomplete'])
            self.handle.finished.emit()

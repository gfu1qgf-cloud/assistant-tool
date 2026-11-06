"""AI handoff is automatically receivable, not a fully completed task."""
import hashlib
import unittest
from app_plugins.builtin.ai_agent_assistant.notice_data import mark_versions_seen,unmark_versions_seen,notice_snapshot
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.sheet_profiles import ORAL_USER_FILL,completed,needs_user_segments
import test_agent_delivery as delivery
import test_agent_partial_receipts as partial


class UserFillStatusTests(unittest.TestCase):
    setUp=delivery.AgentReceiptTests.setUp
    stage=delivery.AgentReceiptTests.stage
    cloud=delivery.AgentReceiptTests.cloud
    download=delivery.AgentReceiptTests.download
    receipt=partial.PartialReceiptTests.receipt
    prepare=partial.PartialReceiptTests.prepare
    drain=partial.PartialReceiptTests.drain

    def handoff(self,total=10,missing=None):
        self.prepare(total,set(range(6,total+1)) if missing is None else missing)
        self.api.rows[0][delivery.COLS['status']]=ORAL_USER_FILL

    def badge(self,result):
        value=result['task_badges']['items'][0]
        self.assertEqual(value['text'],result['task_badges']['job_summaries'][self.job['id']]['text'])
        return value

    def test_handoff_is_oral_only_and_not_task_completion(self):
        self.assertTrue(needs_user_segments('oral',ORAL_USER_FILL))
        self.assertFalse(completed('oral',ORAL_USER_FILL))
        self.assertFalse(needs_user_segments('image',ORAL_USER_FILL))
        self.assertFalse(needs_user_segments('oral','执行中'))

    def test_regular_automatic_receipt_downloads_five_of_ten_without_archiving(self):
        self.handoff();result=self.receipt()
        self.assertEqual(result['downloaded'],5);self.assertEqual(result['partial'],1)
        self.assertEqual(self.badge(result)['text'],'需用户补段 5/10')
        self.assertIn('补完后',self.badge(result)['tooltip'])
        self.assertEqual(self.api.writes,[])
        self.assertEqual(self.api.rows[0][delivery.COLS['status']],ORAL_USER_FILL)
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))
        self.receipt();self.assertEqual(self.fetch.call_count,5)

    def test_budget_continuation_keeps_handoff_and_count_then_waits_for_user(self):
        self.handoff(18,{9})
        for count in (8,16):
            result=self.receipt();self.assertEqual(self.fetch.call_count,count)
            self.assertEqual(self.badge(result)['text'],f'补段收取中 {count}/18')
            self.assertEqual(result['continue_ids'],[self.job['id']])
        result=self.receipt();self.assertEqual(self.fetch.call_count,17)
        self.assertEqual(self.badge(result)['text'],'需用户补段 17/18')
        self.assertEqual(result['continue_ids'],[]);self.assertEqual(self.api.writes,[])

    def test_explicit_viewed_overrides_handoff_text_but_retains_warning_and_detail(self):
        self.handoff();result=self.receipt()
        mark_versions_seen(self.store,self.badge(result)['versions'])
        value=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(value['text'],'已查看');self.assertEqual(value['unread'],0)
        self.assertEqual(value['tone'],'warning')
        self.assertIn('补段',value['tooltip']);self.assertIn('5/10',value['tooltip'])
        self.assertIn('已查看',value['tooltip'])

    def test_seen_handoff_survives_store_reopen_and_unchanged_cloud_refresh_in_both_views(self):
        self.handoff();result=self.receipt()
        mark_versions_seen(self.store,self.badge(result)['versions'])
        reopened=notice_snapshot(Store(self.store.root.path),announce=False)
        self.assertEqual(reopened['items'][0]['text'],'已查看')
        self.assertEqual(reopened['job_summaries'][self.job['id']]['text'],'已查看')
        refreshed=self.receipt()
        self.assertEqual(self.badge(refreshed)['text'],'已查看')
        self.assertEqual(self.fetch.call_count,5);self.assertEqual(self.api.writes,[])

    def test_ai_review_changes_color_not_human_seen_text(self):
        self.handoff();result=self.receipt()
        mark_versions_seen(self.store,self.badge(result)['versions'])
        self.api.rows[0][delivery.COLS['ai_review']]='AI预审不通过：第3段台词缺失'
        result=self.receipt();value=self.badge(result)
        self.assertEqual(value['text'],'已查看');self.assertEqual(value['tone'],'error')
        self.assertIn('第3段台词缺失',value['tooltip']);self.assertIn('5/10',value['tooltip'])

    def test_undo_seen_restores_handoff_count_without_redownloading(self):
        self.handoff();result=self.receipt();versions=self.badge(result)['versions']
        mark_versions_seen(self.store,versions);unmark_versions_seen(self.store,versions)
        result=self.receipt();value=self.badge(result)
        self.assertEqual(value['text'],'需用户补段 5/10');self.assertEqual(value['unread'],5)
        self.assertEqual(self.fetch.call_count,5);self.assertEqual(self.api.writes,[])

    def test_genuinely_new_segment_restores_unread_count_until_user_marks_it_seen(self):
        self.handoff();result=self.receipt()
        mark_versions_seen(self.store,self.badge(result)['versions'])
        self.items.append({'id':'video_6','name':'任务001_第06段.mp4','url':delivery.LINK})
        result=self.receipt();self.assertEqual(self.badge(result)['text'],'需用户补段 6/10')
        self.assertEqual(self.badge(result)['unread'],1)
        mark_versions_seen(self.store,self.badge(result)['versions'])
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['text'],'已查看')
        self.assertEqual(self.api.writes,[])

    def test_seen_handoff_does_not_hide_an_active_transfer_progress(self):
        self.handoff(18,{9});result=self.receipt()
        mark_versions_seen(self.store,self.badge(result)['versions'])
        snapshot=notice_snapshot(self.store,announce=False)
        self.assertEqual(snapshot['items'][0]['text'],'补段收取中 8/18')
        self.assertEqual(snapshot['job_summaries'][self.job['id']]['text'],'补段收取中 8/18')

    def test_normal_completion_after_user_fills_only_downloads_new_files_and_archives(self):
        self.handoff(6,{6});self.receipt()
        self.items.append({'id':'video_6','name':'任务001_第06段.mp4','url':delivery.LINK})
        self.api.rows[0][delivery.COLS['status']]='已完成'
        result=self.receipt()
        self.assertEqual(result['downloaded'],1);self.assertEqual(self.fetch.call_count,6)
        self.assertEqual(self.badge(result)['text'],'已收取');self.assertEqual(len(self.api.writes),1)

    def test_even_full_file_count_keeps_handoff_until_user_changes_cloud_status(self):
        self.handoff(2,set());result=self.receipt()
        self.assertEqual(self.badge(result)['text'],'需用户补段 2/2')
        self.assertEqual(self.api.writes,[])

    def test_unknown_total_is_not_inferred_as_a_complete_delivery(self):
        self.handoff(2,set());self.api.rows[0][delivery.COLS['spoken']]='Original without segment markers.'
        result=self.receipt();self.assertEqual(self.badge(result)['text'],'需用户补段 2/?')
        self.assertEqual(self.api.writes,[])

    def test_missing_result_link_is_not_disguised_as_successful_handoff_receipt(self):
        self.handoff();self.api.rows[0][delivery.COLS['result']]=''
        result=self.receipt();self.fetch.assert_not_called()
        self.assertEqual(self.badge(result)['text'],'等待补段成品链接 0/10')

    def test_transfer_failure_keeps_handoff_retry_and_expected_count(self):
        self.handoff();self.fetch.side_effect=TimeoutError('synthetic download timeout')
        result=self.receipt()
        self.assertEqual(self.badge(result)['text'],'补段下载待重试 0/10')
        self.assertIn('synthetic download timeout',self.badge(result)['tooltip'])
        self.assertEqual(self.api.writes,[])

    def test_handoff_transition_releases_old_discovery_backoff_once_not_repeatedly(self):
        self.handoff()
        self.store.save_delivery(self.job['id'],'@discovery',{'state':'retry','scope':'recursive-v1',
            'source':hashlib.sha256(delivery.LINK.encode()).hexdigest(),'retry_at':9999,'attempts':5})
        result=self.receipt();self.assertEqual(result['downloaded'],5)
        self.receipt(collect=PermissionError('synthetic permission error'))
        calls=self.fetch.call_count
        result=self.receipt()
        self.assertEqual(self.fetch.call_count,calls)
        self.assertEqual(self.badge(result)['text'],'补段下载待重试 5/10')

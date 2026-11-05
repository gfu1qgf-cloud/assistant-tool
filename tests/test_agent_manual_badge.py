"""Manual seen text outranks AI/poll observations without faking new receipts."""
import unittest
from pathlib import Path
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot,mark_versions_seen,unmark_versions_seen
import test_agent_badges as badge_fixture
import test_agent_delivery as receipt_fixture


class ManualBadgeTests(unittest.TestCase):
    setUp=badge_fixture.NoticeDataTests.setUp
    file=badge_fixture.NoticeDataTests.file
    done=badge_fixture.NoticeDataTests.done
    def test_explicit_seen_text_wins_over_ai_issue_and_persists_after_reload(self):
        version=self.file(); self.done(); mark_versions_seen(self.store,[version])
        self.store.save_delivery(self.key,'@ai_review',{'text':'AI预审不通过｜第3段8秒闭眼'})
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(item['text'],'已查看'); self.assertEqual(item['tone'],'error')
        self.assertIn('第3段8秒闭眼',item['tooltip']); self.assertIn('手动状态：已查看',item['tooltip'])
        unmark_versions_seen(self.store,[version])
        self.assertNotIn('已查看',notice_snapshot(self.store,announce=False)['items'][0]['text'])
    def test_seen_legacy_completed_set_is_not_replaced_by_later_cloud_failure(self):
        version=self.file(); self.done(); mark_versions_seen(self.store,[version])
        self.store.delivery_note(self.key,'收取待重试','云端成品目录暂时为空')
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(item['text'],'已查看'); self.assertEqual(item['tone'],'muted')
        self.assertIn('云端成品目录暂时为空',item['tooltip'])
    def test_actual_new_media_version_is_not_hidden_by_old_human_marker(self):
        old=self.file(); self.done(); mark_versions_seen(self.store,[old]); self.file(data=b'new-version')
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertEqual(item['unread'],1)
    def test_missing_file_is_still_reported_and_no_false_receipt_is_created(self):
        version=self.file(); self.done(); mark_versions_seen(self.store,[version])
        next((self.directory/'result').glob('*.mp4')).unlink()
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertIn('成品缺失',item['text']); self.assertEqual(item['received'],0)
    def test_new_unreceived_job_cannot_inherit_old_version_seen_marker(self):
        old=self.file(); self.done(); mark_versions_seen(self.store,[old])
        newer=self.store.stage([self.value],new_version=True)['imported'][0]
        self.store.delivery_note(newer,'收取待重试','新版本尚未下载')
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertNotIn('已查看',item['text']); self.assertIn('新版本尚未下载',item['tooltip'])


class ReceiptFactTests(unittest.TestCase):
    setUp=receipt_fixture.AgentReceiptTests.setUp
    stage=receipt_fixture.AgentReceiptTests.stage
    cloud=receipt_fixture.AgentReceiptTests.cloud
    download=receipt_fixture.AgentReceiptTests.download
    receipt=receipt_fixture.AgentReceiptTests.receipt
    def test_cloud_empty_after_verified_receipt_does_not_erase_completed_fact(self):
        self.receipt(); self.fetch.reset_mock()
        result=self.receipt(collect=ValueError('云端暂未列出视频'))
        item=result['task_badges']['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertIn('云端暂未列出视频',item['tooltip'])
        self.assertIn('未撤销收取事实',item['tooltip']); self.fetch.assert_not_called()
        self.assertTrue(self.store.delivery_files(self.job['id'])['@received']['archived'])
    def test_source_change_invalidates_old_receipt_fact(self):
        self.receipt(); self.api.rows[0][receipt_fixture.COLS['result']]=receipt_fixture.LINK.replace('synthetic_video','new_video')
        item=self.receipt(collect=ValueError('新版本尚不可用'))['task_badges']['items'][0]
        self.assertEqual(item['text'],'收取待重试 1/1')
    def test_new_link_cannot_inherit_old_media_seen_marker(self):
        self.receipt(); record=self.store.delivery_files(self.job['id'])['synthetic_video']
        mark_versions_seen(self.store,[[self.job['id'],'synthetic_video',record['sha256']]])
        self.api.rows[0][receipt_fixture.COLS['result']]=receipt_fixture.LINK.replace('synthetic_video','new_video')
        item=self.receipt(collect=ValueError('新交付链接尚不可用'))['task_badges']['items'][0]
        self.assertNotIn('已查看',item['text'])
    def test_disappeared_local_file_invalidates_receipt_fact(self):
        self.receipt(); record=self.store.delivery_files(self.job['id'])['synthetic_video']; Path(record['path']).unlink()
        item=self.receipt(collect=ValueError('云端也不可用'))['task_badges']['items'][0]
        self.assertIn('成品缺失',item['text'])
    def test_archive_failure_keeps_received_fact_but_not_fake_archive_success(self):
        self.api.fail_write=True; self.receipt()
        proof=self.store.delivery_files(self.job['id'])['@received']; self.assertFalse(proof['archived'])
        item=self.receipt(collect=ValueError('云端巡查失败'))['task_badges']['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertEqual(item['tone'],'warning')


class SeenRefreshRaceTests(unittest.TestCase):
    setUpClass=receipt_fixture.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=receipt_fixture.ReceiptMonitorTests.setUp
    tearDown=receipt_fixture.ReceiptMonitorTests.tearDown
    stage=receipt_fixture.ReceiptMonitorTests.stage
    def test_old_network_snapshot_cannot_replace_newer_manual_marker(self):
        from unittest.mock import patch
        self.plugin.notices.generation=7
        with patch.object(self.plugin.notices,'refresh') as refresh:
            self.plugin.receipts.completed({'message':'旧巡查已完成','downloaded':0,
                'task_badges':{'items':[],'notify_files':0,'ui_generation':6}})
        self.assertTrue(self.plugin.notices.pending); refresh.assert_called_once()
        self.assertEqual(self.plugin.notices.generation,7)

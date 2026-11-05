"""Read-only AI review warnings, no approval gates and no real accounts."""
from pathlib import Path
from unittest.mock import patch
import unittest

from app_plugins.builtin.ai_agent_assistant.review import review_state,review_tooltip,cache_review
from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS,headers,row_values,SUBMISSION_FIELDS
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot,mark_versions_seen,unmark_versions_seen
import test_agent_delivery as delivery_fixture
import test_agent_badges as notice_fixture
import test_ai_agent_assistant as ui_fixture
from test_agent_delivery import COLS


class ReviewParserTests(unittest.TestCase):
    def test_failure_partial_unavailable_and_unstructured_records_need_attention(self):
        for text in ('AI预审不通过｜审核员｜第3段5秒变脸','部分通过（注明未核项）｜未听音频',
                     '无法审核（注明原因）｜无法访问','第3段声音有问题'):
            with self.subTest(text=text): self.assertTrue(review_state(text)['problem'])
    def test_pass_pending_and_blank_do_not_claim_problem(self):
        for text in ('','待AI审核','AI预审通过｜实际已核全部'):
            with self.subTest(text=text): self.assertFalse(review_state(text)['problem'])
    def test_appended_status_supersedes_history_not_words_quoted_in_explanation(self):
        text='AI预审不通过｜第3段错误\nAI预审通过｜复查已修复此前的AI预审不通过问题'
        self.assertFalse(review_state(text)['problem'])
        self.assertTrue(review_state(text+'\n审核状态：部分通过｜音频未核')['problem'])
    def test_long_tooltip_respects_host_limit_and_discloses_truncation(self):
        text='AI预审不通过｜'+('历史记录'*1000)+'\n最新问题：第7段8秒漏读'
        tooltip=review_tooltip('base'*500,text)
        self.assertLessEqual(len(tooltip),3000); self.assertIn('完整记录见原表',tooltip)
        self.assertIn('第7段8秒漏读',tooltip)
    def test_review_column_is_readonly_and_header_order_is_not_fixed(self):
        from types import SimpleNamespace
        job={'id':'fixture','created':'now','payload':{'task_id':'01','title':'Fixture','language':'sk',
            'spoken':'【第1段｜约10秒】\nPreserved text.','chinese':'完整台词','chinese_confirmed':True,
            'reference':'','ai_review':'fake pass from client'}}
        self.assertNotIn('ai_review',SUBMISSION_FIELDS)
        for titles in ([title for _,title,_ in FIELDS], [title for _,title,_ in FIELDS][::-1]):
            _,mapping,count=headers([titles]); row=row_values(job,mapping,count)
            self.assertIsNone(row[mapping['ai_review']]); self.assertEqual(row[mapping['spoken']],job['payload']['spoken'])
        legacy=[title for key,title,_ in FIELDS if key!='ai_review']
        self.assertNotIn('ai_review',headers([legacy])[1])


class ReviewNoticeTests(unittest.TestCase):
    setUp=notice_fixture.NoticeDataTests.setUp
    file=notice_fixture.NoticeDataTests.file
    done=notice_fixture.NoticeDataTests.done
    def review(self,text):
        self.store.save_delivery(self.key,'@ai_review',{'state':'ai_review','available':True,'text':text})
    def test_received_issue_is_red_with_complete_detail_and_survives_seen_toggle(self):
        version=self.file(); self.done(); text='AI预审不通过｜审核员A｜第03段5.2秒漏读，请复核原句。'
        self.review(text)
        for action in (lambda:None,lambda:mark_versions_seen(self.store,[version]),lambda:unmark_versions_seen(self.store,[version])):
            action(); item=notice_snapshot(self.store,announce=False)['items'][0]
            self.assertEqual(item['tone'],'error'); self.assertEqual(item['text'],'已收取·AI需复核')
            self.assertIn(text,item['tooltip'])
    def test_partial_transfer_stays_visibly_partial_even_with_review_error(self):
        self.file(); self.store.save_delivery(self.key,'@discovery',{'state':'checked','total':3})
        self.store.delivery_note(self.key,'正在下载','待继续'); self.review('部分通过｜第2段未核')
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertIn('1/3',item['text']); self.assertEqual(item['tone'],'error')
    def test_clearing_or_passing_latest_review_restores_normal_badge_not_false_user_approval(self):
        self.file(); self.done(); self.review('AI预审不通过｜缺陷')
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['tone'],'error')
        self.review('AI预审通过｜复核完成，用户终审仍待进行')
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(item['tone'],'success'); self.assertIn('用户终审仍待进行',item['tooltip'])
        self.review(''); self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['tone'],'success')


class ReviewReceiptTests(unittest.TestCase):
    setUp=delivery_fixture.AgentReceiptTests.setUp
    stage=delivery_fixture.AgentReceiptTests.stage
    cloud=delivery_fixture.AgentReceiptTests.cloud
    download=delivery_fixture.AgentReceiptTests.download
    receipt=delivery_fixture.AgentReceiptTests.receipt
    def test_negative_review_does_not_block_download_or_get_written_back(self):
        text='AI预审不通过｜审核员｜第1段3秒脸部变形'
        self.api.rows[0][COLS['ai_review']]=text
        result=self.receipt(); self.assertEqual(result['downloaded'],1)
        self.assertEqual(result['task_badges']['items'][0]['tone'],'error')
        self.assertEqual(self.store.ai_reviews()[self.job['id']]['text'],text)
        self.assertEqual(len(self.api.writes),1); self.assertEqual(self.api.writes[0]['range'],"'口播表格'!Q2")
        self.assertEqual(self.api.rows[0][COLS['ai_review']],text)
    def test_review_added_after_download_refreshes_without_refetch_or_notification(self):
        self.receipt(); self.fetch.reset_mock()
        self.api.rows[0][COLS['ai_review']]='部分通过｜第3段待听审'
        result=self.receipt()
        self.fetch.assert_not_called(); self.assertEqual(len(self.api.writes),1)
        self.assertEqual(result['task_badges']['items'][0]['tone'],'error')
        self.assertEqual(result['task_badges']['notify_files'],0)
        self.api.rows[0][COLS['ai_review']]='AI预审通过｜已补核'
        self.assertEqual(self.receipt()['task_badges']['items'][0]['tone'],'success')
        self.api.rows[0][COLS['ai_review']]=''
        self.assertEqual(self.receipt()['task_badges']['items'][0]['tone'],'success')
        self.fetch.assert_not_called()
    def test_audit_changes_during_transfer_are_reflected_after_acknowledgement(self):
        def download(*args):
            self.api.rows[0][COLS['ai_review']]='AI预审不通过｜下载中发现的缺陷'
            return self.download(*args)
        self.fetch.side_effect=download
        result=self.receipt()
        self.assertIn('下载中发现的缺陷',result['task_badges']['items'][0]['tooltip'])
        self.assertEqual(result['task_badges']['items'][0]['tone'],'error')
    def test_other_task_identity_cannot_replace_cached_audit(self):
        self.receipt(); self.api.rows[0][COLS['task_id']]='OTHER'
        self.api.rows[0][COLS['ai_review']]='AI预审不通过｜其他任务的问题'
        self.receipt()
        self.assertEqual(self.store.ai_reviews()[self.job['id']]['text'],'')


class ReviewUiTests(unittest.TestCase):
    setUpClass=ui_fixture.AgentUiTests.__dict__['setUpClass']
    setUp=ui_fixture.AgentUiTests.setUp
    tearDown=ui_fixture.AgentUiTests.tearDown
    def test_assistant_status_cell_is_red_and_review_is_in_readonly_tooltip(self):
        dialog=self.plugin.open_dialog(); self.plugin.import_rows([0]); job=self.plugin.store.jobs()[0]
        text='AI预审不通过｜审核员｜第03段8秒持续闭眼'
        self.plugin.store.save_delivery(job['id'],'@ai_review',{'state':'ai_review','available':True,'text':text})
        self.plugin.store.delivery_note(job['id'],'已下载','示例已收取'); dialog.reload()
        self.assertEqual(dialog.table.item(0,6).foreground().color().name(),'#a22020')
        self.assertIn(text,dialog.table.item(0,6).toolTip()); self.assertIn(text,dialog.table.item(0,7).text())

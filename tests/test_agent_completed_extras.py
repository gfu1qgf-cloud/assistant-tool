"""Completed planned deliveries may contain an unrelated unnumbered clip."""
import hashlib
from pathlib import Path
import unittest
from app_plugins.builtin.ai_agent_assistant.delivery import DISCOVERY_SCOPE
from app_plugins.builtin.ai_agent_assistant.receipt_plan import check_parts
import test_agent_delivery as fixture
import test_agent_nested_receipts as nested


class CompletedExtrasTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download
    prepare=nested.NestedReceiptTests.prepare
    receive=nested.NestedReceiptTests.receive

    def prepared(self, count=19):
        self.prepare(count)
        for n in range(1,count+1): self.drive.video('result_root' if n%2 else 'deeper',n)
        self.drive.video('result_root',99,identifier='auxiliary_clip',name='guest_listening.mp4')

    def test_complete_nineteen_plus_auxiliary_downloads_in_three_rounds(self):
        self.prepared()
        first=self.receive()
        self.assertEqual(first['downloaded'],8)
        self.assertEqual(first['task_badges']['items'][0]['text'],'收取中 8/20')
        self.assertEqual(self.api.writes,[])
        second=self.receive(ids=first['continue_ids'])
        self.assertEqual(second['task_badges']['items'][0]['text'],'收取中 16/20')
        final=self.receive(ids=second['continue_ids'])
        self.assertEqual(self.fetch.call_count,20)
        self.assertEqual(final['task_badges']['items'][0]['text'],'已收取')
        self.assertIn('auxiliary_clip',{call.args[1]['id'] for call in self.fetch.call_args_list})
        auxiliary=self.store.delivery_files(self.job['id'])['auxiliary_clip']
        self.assertEqual(auxiliary['category'],'supplemental')
        self.assertEqual(Path(auxiliary['path']).parent.name,'Agent附加素材')
        self.assertNotEqual(Path(auxiliary['path']).parent.name,'result')
        self.assertEqual(len(self.api.writes),1)
        records=self.store.delivery_files(self.job['id'])
        self.assertEqual(records['@received']['total'],20)
        self.assertIn('guest_listening.mp4',records['@received']['selection_note'])
        self.assertIn('Agent附加素材',self.store.delivery_statuses()[self.job['id']]['detail'])
        self.receive();self.assertEqual(self.fetch.call_count,20)
        self.assertEqual(len(self.api.writes),1)

    def test_old_backoff_is_invalidated_once_and_real_network_error_still_backs_off(self):
        self.prepared(1)
        self.store.save_delivery(self.job['id'],'@discovery',{'state':'retry','scope':'recursive-v1','handoff':False,
            'source':hashlib.sha256(nested.FOLDER_LINK.encode()).hexdigest(),'retry_at':9999,'attempts':7})
        self.assertEqual(self.receive()['downloaded'],2)
        self.drive.fail=PermissionError('synthetic failure')
        self.receive();self.drive.listed.clear();self.receive()
        self.assertEqual(self.drive.listed,[])
        self.assertEqual(self.store.delivery_files(self.job['id'])['@discovery']['scope'],DISCOVERY_SCOPE)

    def test_duplicate_formal_segment_still_blocks_every_transfer(self):
        self.prepared(2);self.drive.video('deeper',1,identifier='duplicate_1')
        result=self.receive();self.fetch.assert_not_called()
        self.assertEqual(result['failed'],1);self.assertEqual(self.api.writes,[])

    def test_out_of_range_segment_is_not_treated_as_auxiliary(self):
        self.prepared(2);self.drive.video('deeper',3)
        result=self.receive();self.fetch.assert_not_called()
        self.assertEqual(result['failed'],1);self.assertEqual(self.api.writes,[])

    def test_other_task_segment_is_not_treated_as_auxiliary(self):
        self.prepared(2);self.drive.video('deeper',3,name='任务OTHER_第03段.mp4')
        result=self.receive();self.fetch.assert_not_called()
        self.assertEqual(result['failed'],1);self.assertEqual(self.api.writes,[])

    def test_unknown_clips_cannot_replace_missing_segments_or_archive(self):
        self.prepared(2)
        self.drive.children['deeper']=[v for v in self.drive.children['deeper'] if v['id']!='video_2']
        result=self.receive()
        self.assertEqual(self.api.writes,[])
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))

    def test_failed_auxiliary_keeps_counts_and_only_retries_it(self):
        self.prepared(2)
        def fetch(job,item,directory,drive,run):
            if item['id']=='auxiliary_clip': raise TimeoutError('synthetic extra download failure')
            return self.download(job,item,directory,drive,run)
        self.fetch.side_effect=fetch
        result=self.receive()
        self.assertEqual(result['downloaded'],2)
        self.assertEqual(result['task_badges']['items'][0]['text'],'收取待重试 2/3')
        self.assertEqual(self.api.writes,[])
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))
        self.store.retry_deliveries([self.job['id']]);self.fetch.side_effect=self.download
        result=self.receive()
        self.assertEqual(result['downloaded'],1)
        self.assertEqual(self.fetch.call_count,4)
        self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')
        self.assertEqual(len(self.api.writes),1)

    def test_missing_auxiliary_file_is_not_hidden_by_completed_receipt(self):
        from pathlib import Path
        self.prepared(2);self.receive()
        record=self.store.delivery_files(self.job['id'])['auxiliary_clip']
        Path(record['path']).unlink()
        from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot
        text=notice_snapshot(self.store)['items'][0]['text']
        self.assertNotIn(text,{'已收取','已查看'})
        self.assertIn('2/3',text)


class CompletedSelectionTests(unittest.TestCase):
    def test_image_and_unplanned_deliveries_are_unchanged(self):
        from app_plugins.builtin.ai_agent_assistant.receipt_plan import completed_files
        files=[{'id':'clip','name':'unnamed.mp4'}]
        for plan in (None,{'count':1,'kind':'image'}):
            self.assertEqual(completed_files(files,plan,'001'),(files,''))

    def test_cached_local_filename_and_legacy_s_names_are_supported(self):
        from app_plugins.builtin.ai_agent_assistant.receipt_plan import completed_files
        files=[{'id':'a'},{'id':'b','name':'02_S02.mp4'},{'id':'extra','name':'other.mp4'}]
        records={'a':{'path':'C:/result/Agent_abcdef123456_abcdef123456_01_S01.mp4'}}
        selected,note=completed_files(files,{'count':2},'001',records)
        self.assertEqual([f['id'] for f in selected],['a','b'])
        self.assertEqual(check_parts(selected,{'count':2},'001',records),'')
        self.assertIn('other.mp4',note)

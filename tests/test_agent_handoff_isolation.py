"""Reproduce a delivery folder containing formal videos and ASR candidates."""
from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch
from app_plugins.builtin.ai_agent_assistant.receipt_plan import handoff_files, HANDOFF_POLICY
from app_plugins.builtin.ai_agent_assistant.sheet_profiles import ORAL_USER_FILL
import test_agent_delivery as fixture
import test_agent_nested_receipts as nested


class HandoffIsolationTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download
    prepare=nested.NestedReceiptTests.prepare
    receive=nested.NestedReceiptTests.receive

    def handoff(self,total=22):
        self.prepare(total)
        self.api.rows[0][fixture.COLS['status']]=ORAL_USER_FILL

    def add(self,number,suffix='',identifier=None):
        self.drive.video('result_root',number,identifier=identifier,
            name=f'任务001_第{number:02}段{suffix}.mp4')

    def drain(self):
        results=[]
        for _ in range(6):
            result=self.receive();results.append(result)
            if not result['continue_ids']:return results
        self.fail('handoff continuation did not finish')

    def test_real_folder_shape_19_formal_plus_three_candidates_collects_19_not_zero_or_22(self):
        self.handoff()
        for number in range(1,23):
            if number not in {8,12,17}:self.add(number)
        for number,suffix in ((2,'_候选ASR'),(17,'_候选ASR'),(18,'_候选_ASR待验')):
            self.add(number,suffix,identifier=f'candidate_{number}')
        results=self.drain()
        self.assertEqual([r['downloaded'] for r in results],[8,8,3])
        self.assertEqual(results[-1]['task_badges']['items'][0]['text'],'需用户补段 19/22')
        self.assertEqual({c.args[1]['id'] for c in self.fetch.call_args_list},{f'video_{n}' for n in range(1,23) if n not in {8,12,17}})
        self.assertEqual(self.api.writes,[])
        self.assertIn('候选',results[-1]['task_badges']['items'][0]['tooltip'])
        self.assertNotIn('@received',self.store.delivery_files(self.job['id']))
        self.drain();self.assertEqual(self.fetch.call_count,19)

    def test_two_formal_versions_block_only_their_ordinal(self):
        self.handoff(3);self.add(1);self.add(2);self.add(3)
        self.add(2,'_另版',identifier='other_2')
        result=self.receive()
        self.assertEqual({c.args[1]['id'] for c in self.fetch.call_args_list},{'video_1','video_3'})
        self.assertEqual(result['task_badges']['items'][0]['text'],'需用户补段 2/3')
        self.assertIn('第2段存在重复版本',result['task_badges']['items'][0]['tooltip'])
        self.assertEqual(self.api.writes,[])

    def test_ambiguous_wrong_owner_and_out_of_range_never_download_but_good_part_does(self):
        self.handoff(3);self.add(1);self.add(9)
        self.drive.video('result_root',2,name='任务OTHER_第02段.mp4')
        self.drive.video('result_root',3,name='unidentified.mp4')
        result=self.receive()
        self.fetch.assert_called_once();self.assertEqual(self.fetch.call_args.args[1]['id'],'video_1')
        tooltip=result['task_badges']['items'][0]['tooltip']
        for text in ('其他任务','越界','未能确认段号'):self.assertIn(text,tooltip)
        self.assertEqual(self.api.writes,[])

    def test_candidate_without_formal_is_not_promoted_and_empty_selection_does_not_loop(self):
        self.handoff(2);self.add(1,'_候选ASR')
        result=self.receive();self.fetch.assert_not_called()
        self.assertEqual(result['continue_ids'],[])
        self.assertEqual(result['task_badges']['items'][0]['text'],'需用户补段 0/2')
        self.assertEqual(self.api.writes,[])

    def test_later_formal_replacement_only_downloads_the_new_part(self):
        self.handoff(2);self.add(1);self.add(2,'_候选ASR',identifier='candidate_2')
        self.receive();self.add(2)
        self.receive();self.assertEqual(self.fetch.call_count,2)
        self.api.rows[0][fixture.COLS['status']]='已完成'
        self.drive.children['result_root']=[v for v in self.drive.children['result_root'] if v['id']!='candidate_2']
        result=self.receive();self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')
        self.assertEqual(self.fetch.call_count,2);self.assertEqual(len(self.api.writes),1)

    def test_old_handoff_validation_backoff_is_invalidated_once_then_real_failure_backs_off(self):
        self.handoff(2);self.add(1)
        self.store.save_delivery(self.job['id'],'@discovery',{'state':'retry','scope':'recursive-v1','handoff':True,
            'source':hashlib.sha256(nested.FOLDER_LINK.encode()).hexdigest(),'retry_at':9999,'attempts':8})
        self.assertEqual(self.receive()['downloaded'],1)
        self.drive.fail=PermissionError('synthetic Drive denial')
        self.receive();self.drive.listed.clear();self.receive()
        self.assertEqual(self.drive.listed,[])
        self.assertEqual(self.store.delivery_files(self.job['id'])['@discovery']['handoff_policy'],HANDOFF_POLICY)

    def test_strict_completed_delivery_still_never_guesses_between_formal_versions(self):
        self.prepare(2);self.add(1);self.add(1,'_另版',identifier='other_1')
        result=self.receive();self.fetch.assert_not_called()
        self.assertEqual(result['failed'],1);self.assertEqual(self.api.writes,[])


class HandoffMonitorPipelineTests(unittest.TestCase):
    setUpClass=fixture.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=fixture.ReceiptMonitorTests.setUp
    tearDown=fixture.ReceiptMonitorTests.tearDown
    stage=fixture.ReceiptMonitorTests.stage
    download=fixture.AgentReceiptTests.download

    def test_real_monitor_submitted_worker_and_continuation_deliver_handoff_without_partial_permission(self):
        self.stage();job=self.plugin.store.jobs()[0]
        Path(job['payload']['target_dir']).mkdir(exist_ok=True)
        self.plugin.store.mark([job['id']],'submitted',fixture.spreadsheet_id(fixture.URL)+':口播表格')
        values={'job_id':job['id'],'task_id':job['payload']['task_id'],'status':ORAL_USER_FILL,
            'result':nested.FOLDER_LINK,'archive':'未下载',
            'spoken':'\n\n'.join(f'【第{i}段｜约10秒】\nExact {i}.' for i in range(1,11))}
        api=fixture.ReceiptSheets([[values.get(k,'') for k,_,_ in fixture.FIELDS]])
        drive=nested.FolderDrive()
        for number in range(1,10):drive.video('result_root',number,name=f'任务{values["task_id"]}_第{number:02}段.mp4')
        drive.video('result_root',1,identifier='candidate',name=f'任务{values["task_id"]}_第01段_候选ASR.mp4')
        self.plugin.receipts.start()
        downloaded=[]
        with patch.object(self.plugin.receipts,'download_authorization',return_value=None):
            self.plugin.receipts.check(manual=True)
            for batch in range(2):
                options=self.context.submit_background_task.call_args.kwargs
                self.assertFalse(options['payload']['allow_incomplete'])
                run=fixture.SimpleNamespace(check_canceled=fixture.Mock(),report=fixture.Mock())
                result=options['runner'](options['payload'],run,sheets=api,drive=drive,fetch=self.download,clock=lambda:1000)
                downloaded.append(result['downloaded'])
                self.handle.completed.emit(result);self.handle.finished.emit()
                if batch==0:self.plugin.receipts.tick()
        self.assertEqual(downloaded,[8,1]);self.assertEqual(api.writes,[])
        self.assertEqual(result['task_badges']['items'][0]['text'],'需用户补段 9/10')
        self.plugin.receipts.stop()

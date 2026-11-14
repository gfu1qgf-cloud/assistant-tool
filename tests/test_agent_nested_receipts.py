"""Use the actual Drive collector through the receipt worker, not a patched list."""
import copy
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from app_plugins.builtin.ai_agent_assistant.delivery import run_receipts
from app_plugins.builtin.ai_agent_assistant.delivery_drive import FOLDER
import test_agent_delivery as fixture

FOLDER_LINK='https://drive.google.com/drive/folders/result_root'


class FolderDrive:
    def __init__(self):
        self.metadata={'result_root':{'id':'result_root','mimeType':FOLDER,'name':'成品'}}
        self.children={'result_root':[]};self.listed=[];self.fail=None

    def files(self):return self

    def get(self,*,fileId,**_kwargs):
        return SimpleNamespace(execute=lambda **_:copy.deepcopy(self.metadata[fileId]))

    def list(self,*,q,pageToken=None,**_kwargs):
        folder=re.fullmatch(r"'([A-Za-z0-9_-]+)' in parents and trashed=false",q).group(1)
        self.listed.append(folder)
        def execute(**_):
            if self.fail:raise self.fail
            return {'files':copy.deepcopy(self.children[folder])}
        return SimpleNamespace(execute=execute)

    def folder(self,parent,identifier):
        value={'id':identifier,'mimeType':FOLDER,'name':identifier}
        self.metadata[identifier]=value;self.children[parent].append(value);self.children[identifier]=[]

    def video(self,parent,number,*,identifier=None,name=None):
        identifier=identifier or f'video_{number}'
        value={'id':identifier,'mimeType':'video/mp4','name':name or f'任务001_第{number:02}段.mp4'}
        self.metadata[identifier]=value;self.children[parent].append(value)


class NestedReceiptTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download

    def prepare(self,total):
        self.api.rows[0][fixture.COLS['result']]=FOLDER_LINK
        self.api.rows[0][fixture.COLS['spoken']]='\n\n'.join(
            f'【第{i}段｜约10秒】\nExact segment {i}.' for i in range(1,total+1))
        self.drive=FolderDrive();self.drive.folder('result_root','nested')
        self.drive.folder('nested','deeper')

    def receive(self,**options):
        return run_receipts({'root':str(self.store.root.path),**options},self.run,
            sheets=self.api,drive=self.drive,fetch=self.fetch,clock=lambda:1000)

    def test_videos_in_two_nested_levels_download_to_original_result_and_archive(self):
        self.prepare(2);self.drive.video('nested',1);self.drive.video('deeper',2)
        result=self.receive()
        self.assertEqual(result['downloaded'],2)
        self.assertEqual(self.drive.listed,['result_root','nested','deeper'])
        self.assertEqual(len(self.api.writes),1)
        for key in ('video_1','video_2'):
            record=self.store.delivery_files(self.job['id'])[key]
            self.assertEqual(Path(record['path']).parent,Path(self.job['payload']['target_dir'])/'result')
            self.assertTrue(Path(record['path']).is_file())
        self.receive();self.assertEqual(self.fetch.call_count,2);self.assertEqual(len(self.api.writes),1)

    def test_root_and_nested_videos_share_bounded_download_continuation(self):
        self.prepare(9);self.drive.video('result_root',1)
        for number in range(2,10):self.drive.video('deeper',number)
        first=self.receive()
        self.assertEqual(first['downloaded'],8);self.assertTrue(first['continue_ids'])
        self.assertEqual(self.api.writes,[])
        self.assertEqual(first['task_badges']['items'][0]['text'],'收取中 8/9')
        second=self.receive(ids=first['continue_ids'])
        self.assertEqual(second['downloaded'],1);self.assertEqual(self.fetch.call_count,9)
        self.assertEqual(len(self.api.writes),1)

    def test_missing_nested_segment_can_be_received_then_only_new_segment_downloads(self):
        self.prepare(2);self.drive.video('deeper',1)
        first=self.receive()
        self.assertEqual(first['downloaded'],1);self.assertEqual(first['partial'],1)
        self.assertEqual(self.api.writes,[])
        self.drive.video('nested',2)
        second=self.receive()
        self.assertEqual(second['downloaded'],1);self.assertEqual(self.fetch.call_count,2)
        self.assertEqual(len(self.api.writes),1)

    def test_duplicate_segment_in_nested_folder_is_not_guessed_or_archived(self):
        self.prepare(2);self.drive.video('result_root',1)
        self.drive.video('deeper',1,identifier='other_version')
        result=self.receive()
        self.assertEqual(result['downloaded'],2);self.assertEqual(result['failed'],0)
        self.assertEqual(self.fetch.call_count,2);self.assertEqual(self.api.writes,[])
        self.assertIn('重复',self.store.delivery_statuses()[self.job['id']]['detail'])

    def test_wrong_task_filename_is_kept_separate_without_blocking_transfer(self):
        self.prepare(2);self.drive.video('nested',1)
        self.drive.video('deeper',2,name='任务OTHER_第02段.mp4')
        self.receive();self.assertEqual(self.fetch.call_count,2);self.assertEqual(self.api.writes,[])
        self.assertEqual(self.store.delivery_files(self.job['id'])['video_2']['category'],'review')
        self.assertIn('其他任务',self.store.delivery_statuses()[self.job['id']]['detail'])

    def test_old_root_only_discovery_backoff_does_not_delay_fixed_enumeration(self):
        self.prepare(1);self.drive.video('deeper',1)
        self.store.save_delivery(self.job['id'],'@discovery',{
            'state':'retry','source':hashlib.sha256(FOLDER_LINK.encode()).hexdigest(),
            'retry_at':4600,'attempts':6,'detail':'root folder has no videos'})
        result=self.receive()
        self.assertEqual(result['downloaded'],1);self.assertEqual(len(self.api.writes),1)

    def test_new_recursive_discovery_failure_keeps_backoff(self):
        self.prepare(1);self.drive.fail=PermissionError('synthetic permission failure')
        first=self.receive();self.assertEqual(first['failed'],1)
        self.drive.fail=None;self.drive.video('nested',1);self.drive.listed.clear()
        second=self.receive()
        self.assertTrue(second['pending']);self.assertEqual(self.drive.listed,[])
        self.fetch.assert_not_called();self.assertEqual(self.api.writes,[])

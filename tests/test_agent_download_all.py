"""Business warnings never prevent receipt of a completed job's declared videos."""
import unittest
from pathlib import Path
from unittest.mock import patch
from app_plugins.builtin.ai_agent_assistant.delivery_drive import FOLDER
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot,mark_versions_seen
import test_agent_delivery as fixture
import test_agent_nested_receipts as nested


class DownloadAllTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download
    prepare=nested.NestedReceiptTests.prepare
    receive=nested.NestedReceiptTests.receive

    def versions(self):
        self.prepare(10)
        for n in range(1,11):self.drive.video('result_root',n)
        for n in (6,7,8):self.drive.video('deeper',n,identifier=f'v2_{n}',name=f'任务001_第{n:02}段-v2.mp4')

    def test_352_all_thirteen_download_without_choosing_v2(self):
        self.versions();first=self.receive()
        self.assertEqual(first['downloaded'],8)
        self.assertEqual(first['task_badges']['items'][0]['text'],'收取中 8/13')
        final=self.receive(ids=first['continue_ids'])
        self.assertEqual(final['downloaded'],5);self.assertEqual(self.fetch.call_count,13)
        records=self.store.delivery_files(self.job['id'])
        for n in range(1,11):
            parent=Path(records[f'video_{n}']['path']).parent.name
            self.assertEqual(parent,'Agent待选版本' if n in (6,7,8) else 'result')
        for n in (6,7,8):self.assertEqual(Path(records[f'v2_{n}']['path']).parent.name,'Agent待选版本')
        self.assertEqual(records['@received']['total'],13)
        self.assertTrue(records['@received']['review_required'])
        badge=final['task_badges']['items'][0]
        self.assertEqual(badge['text'],'已收取');self.assertEqual(badge['tone'],'warning')
        self.assertIn('未选择采用',badge['tooltip'])
        self.assertEqual(len(self.api.writes),1)
        self.receive();self.assertEqual(self.fetch.call_count,13)
        seen=notice_snapshot(self.store)['items'][0]['versions'];mark_versions_seen(self.store,seen)
        self.assertEqual(notice_snapshot(self.store)['items'][0]['text'],'已查看')

    def test_wrong_owner_out_of_range_and_unknown_names_are_all_retained_separately(self):
        self.prepare(2);self.drive.video('result_root',1);self.drive.video('deeper',2)
        self.drive.video('deeper',9,identifier='outside')
        self.drive.video('deeper',3,identifier='other',name='任务OTHER_第03段.mp4')
        self.drive.video('result_root',4,identifier='unknown',name='guest.mp4')
        result=self.receive();self.assertEqual(result['downloaded'],5)
        records=self.store.delivery_files(self.job['id'])
        self.assertEqual(Path(records['outside']['path']).parent.name,'Agent待选版本')
        self.assertEqual(Path(records['other']['path']).parent.name,'Agent待选版本')
        self.assertEqual(Path(records['unknown']['path']).parent.name,'Agent附加素材')
        self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')

    def test_one_invalid_video_does_not_prevent_other_transfers(self):
        self.prepare(3)
        for n in range(1,4):self.drive.video('result_root',n)
        def fetch(job,item,directory,drive,run):
            if item['id']=='video_2':raise ValueError('synthetic invalid video checksum')
            return self.download(job,item,directory,drive,run)
        self.fetch.side_effect=fetch
        result=self.receive();self.assertEqual(result['downloaded'],2)
        self.assertEqual(result['task_badges']['items'][0]['text'],'收取待重试 2/3')
        self.assertEqual(self.api.writes,[])

    def test_shortcut_or_unreadable_child_does_not_hide_other_videos(self):
        self.prepare(1);self.drive.video('result_root',1)
        self.drive.children['result_root'].append({'id':'shortcut','name':'unreadable','mimeType':'application/vnd.google-apps.shortcut'})
        result=self.receive();self.assertEqual(result['downloaded'],1)
        self.assertEqual(self.api.writes,[])
        self.assertIn('快捷方式',result['task_badges']['items'][0]['tooltip'])

    def test_invalid_child_id_does_not_hide_later_healthy_video(self):
        self.prepare(1)
        self.drive.children['result_root'].append({'id':'invalid id','name':'invalid','mimeType':'video/mp4'})
        self.drive.video('result_root',1)
        result=self.receive();self.assertEqual(result['downloaded'],1)
        self.assertEqual(self.api.writes,[])
        self.assertIn('无效成品ID',result['task_badges']['items'][0]['tooltip'])

    def test_one_malformed_google_link_does_not_hide_valid_result_folder(self):
        self.prepare(1);self.drive.video('result_root',1)
        self.api.rows[0][fixture.COLS['result']]+='\nhttps://drive.google.com/not-a-result'
        result=self.receive();self.assertEqual(result['downloaded'],1)
        self.assertEqual(self.api.writes,[])

    def test_incomplete_search_downloads_visible_files_without_false_archive(self):
        self.prepare(1);self.drive.video('result_root',1)
        original=self.drive.list
        def listing(**kwargs):
            request=original(**kwargs);execute=request.execute
            request.execute=lambda **options:{**execute(**options),'incompleteSearch':True}
            return request
        with patch.object(self.drive,'list',side_effect=listing):result=self.receive()
        self.assertEqual(result['downloaded'],1);self.assertEqual(self.api.writes,[])
        self.assertIn('检索结果不完整',result['task_badges']['items'][0]['tooltip'])

    def test_resource_limit_keeps_discovered_files_bounded_instead_of_discarding_them(self):
        self.prepare(1)
        for number in range(1,601):self.drive.video('result_root',number)
        from app_plugins.builtin.ai_agent_assistant.delivery_drive import collect_files
        files=collect_files([nested.FOLDER_LINK],self.drive,self.run,lenient=True)
        self.assertEqual(len(files),500);self.assertEqual(len(files.issues),1)
        self.assertIn('超过500',files.issues[0])
        with self.assertRaises(ValueError):collect_files([nested.FOLDER_LINK],self.drive,self.run)

    def test_bad_segment_plan_is_a_warning_not_a_download_gate(self):
        self.prepare(1);self.drive.video('result_root',1)
        self.api.rows[0][fixture.COLS['spoken']]='【第2段｜约10秒】\nMalformed ordering.'
        result=self.receive();self.assertEqual(result['downloaded'],1)
        self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')
        self.assertEqual(result['task_badges']['items'][0]['tone'],'warning')

    def test_unfinished_status_still_waits_without_downloading(self):
        self.prepare(1);self.drive.video('result_root',1)
        self.api.rows[0][fixture.COLS['status']]='执行中'
        result=self.receive();self.fetch.assert_not_called()
        self.assertIn('等待制作完成',result['task_badges']['items'][0]['text'])

    def test_missing_segments_still_receive_existing_files_without_false_completion(self):
        self.prepare(3);self.drive.video('result_root',1);self.drive.video('deeper',3)
        result=self.receive();self.assertEqual(result['downloaded'],2)
        self.assertEqual(result['task_badges']['items'][0]['text'],'待补齐 2/3')
        self.assertEqual(self.api.writes,[])

    def test_missing_archive_column_does_not_block_local_download(self):
        self.prepare(1);self.drive.video('result_root',1)
        from app_plugins.builtin.ai_agent_assistant.delivery import inspect as real_inspect
        def inspect(*args):
            info=real_inspect(*args);info['mapping'].pop('archive');return info
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.inspect',side_effect=inspect):result=self.receive()
        self.assertEqual(result['downloaded'],1);self.assertEqual(self.api.writes,[])
        self.assertEqual(result['task_badges']['items'][0]['text'],'已收取')
        self.assertIn('归档状态列',result['task_badges']['items'][0]['tooltip'])

    def test_unwritable_review_folder_does_not_block_primary_files(self):
        self.prepare(2);self.drive.video('result_root',1);self.drive.video('deeper',2)
        self.drive.video('deeper',1,identifier='other_1')
        from app_plugins.builtin.ai_agent_assistant.delivery_drive import output_directory
        def output(job,**kwargs):
            if kwargs.get('review'):raise PermissionError('synthetic review directory unavailable')
            return output_directory(job,**kwargs)
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.output_directory',side_effect=output):result=self.receive()
        self.assertEqual(result['downloaded'],1)
        self.assertEqual(self.fetch.call_count,1)
        self.assertEqual(result['task_badges']['items'][0]['text'],'收取待重试 1/3')

    def test_denied_file_is_isolated_without_bypassing_its_permission(self):
        self.prepare(2);self.drive.video('result_root',1);self.drive.video('deeper',2)
        self.drive.children['result_root'][-1]['capabilities']={'canDownload':False}
        from app_plugins.builtin.ai_agent_assistant.delivery_drive import download_file
        def fetch(job,item,directory,drive,run):
            if item.get('capabilities',{}).get('canDownload') is False:
                return download_file(job,item,directory,drive,run)
            return self.download(job,item,directory,drive,run)
        self.fetch.side_effect=fetch;result=self.receive()
        self.assertEqual(result['downloaded'],1);self.assertEqual(self.api.writes,[])
        self.assertEqual(result['task_badges']['items'][0]['text'],'收取待重试 1/2')

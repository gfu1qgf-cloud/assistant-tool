"""Users paste the video page; original generated-media ownership stays required."""
import copy
import json
import unittest

import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.setup_support import video_page, project_identity

PROJECT='https://flow.google.com/project/e81b03ed-2d80-450f-9cb7-c379de842c0d'
MEDIA='9d322db4-6599-408a-b4a7-36b3e36411cc'
VIDEO=PROJECT+'/edit/'+MEDIA


class VideoPageDownloadTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    bound_row=fixtures.TransferTests.bound_row

    def failed_video(self):
        service=self.service();row,client=self.bound_row(service)
        remote=copy.deepcopy(row['remote']);remote['page']=PROJECT
        binding=remote['bindings'][0];binding.update(mediaIds=[MEDIA],readyIds=[],urls={})
        service.store.update(row['id'],'download_parked',remote=remote)
        service.preferences.setdefault('profile_projects',{})[client['profile']]=PROJECT
        return service,service.store.get(row['id']),client

    def test_user_supplied_edit_url_is_parsed_and_belongs_to_same_project(self):
        self.assertEqual(video_page(VIDEO),{'project':PROJECT,'media_id':MEDIA})
        self.assertEqual(project_identity(VIDEO),project_identity(PROJECT))
        for bad in ('https://evil.test/project/p/edit/m','http://flow.google.com/project/p/edit/m',
                'https://user@flow.google.com/project/p/edit/m','https://flow.google.com/project/p/edit/m/other',PROJECT):
            self.assertIsNone(video_page(bad))

    def test_pasted_page_recovers_without_old_signed_url_or_regeneration(self):
        service,row,client=self.failed_video()
        service.execute({'action':'refresh_download_url','job':row['id'],'url':VIDEO})
        commands=[json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]
        request=next(command for command in commands if command.get('action')=='retry_download')
        self.assertTrue(request['params']['restore']['refresh_url'])
        self.assertEqual(request['params']['restore']['media_id'],MEDIA)
        self.assertEqual(request['params']['restore']['project'],PROJECT)
        self.assertEqual(request['params']['restore']['video_page'],VIDEO)
        self.assertEqual(service.store.get(row['id'])['status'],'downloading')
        self.assertFalse(any(command.get('action')=='run_tasks' for command in commands))
        self.assertEqual(service.store.get(row['id'])['attempt'],row['attempt'])

    def test_normal_retry_and_pasted_page_are_distinct_and_page_is_canonical(self):
        service,row,client=self.failed_video();service.retry_download(row)
        normal=next(json.loads(c.args[0]) for c in client['socket'].sendTextMessage.call_args_list if json.loads(c.args[0]).get('action')=='retry_download')
        self.assertNotIn('video_page',normal['params']['restore'])
        service.pending.clear();service.store.update(row['id'],'download_parked')
        service.execute({'action':'refresh_download_url','job':row['id'],'url':VIDEO+'?ignored=1#fragment'})
        commands=[json.loads(c.args[0]) for c in client['socket'].sendTextMessage.call_args_list]
        self.assertEqual(commands[-1]['params']['restore']['video_page'],VIDEO)
        self.assertNotEqual(normal['params'],commands[-1]['params'])

    def test_old_worker_waits_without_occupying_account_then_resumes_exact_page_on_update(self):
        service,row,client=self.failed_video();client['extension_version']='1.1.48'
        service.execute({'action':'refresh_download_url','job':row['id'],'url':VIDEO})
        self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        self.assertFalse(any(json.loads(c.args[0]).get('action')=='retry_download' for c in client['socket'].sendTextMessage.call_args_list))
        from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION
        client['extension_version']=EXTENSION_VERSION;service.recover_extension_downloads(client)
        retry=next(json.loads(c.args[0]) for c in client['socket'].sendTextMessage.call_args_list if json.loads(c.args[0]).get('action')=='retry_download')
        self.assertEqual(retry['params']['restore']['video_page'],VIDEO)
        self.assertEqual(service.store.get(row['id'])['status'],'downloading')
        self.assertFalse(any(json.loads(c.args[0]).get('action')=='run_tasks' for c in client['socket'].sendTextMessage.call_args_list))

    def test_another_project_or_media_cannot_replace_original_video(self):
        for link in (VIDEO.replace('e81b03ed','aaaaaaaa'),VIDEO.replace(MEDIA,'another-video')):
            with self.subTest(link=link):
                self.setUp();service,row,client=self.failed_video();before=copy.deepcopy(row);errors=[]
                service.failed.connect(errors.append)
                service.execute({'action':'refresh_download_url','job':row['id'],'url':link})
                self.assertTrue(errors);self.assertEqual(service.store.get(row['id']),before)
                self.assertFalse(service.pending)

    def test_unknown_generation_ownership_is_not_replaced_by_pasted_link(self):
        service,row,client=self.failed_video();remote=copy.deepcopy(row['remote'])
        remote['bindings'][0]['source']='dom_discovery'
        service.store.update(row['id'],row['status'],remote=remote)
        errors=[];service.failed.connect(errors.append)
        service.execute({'action':'refresh_download_url','job':row['id'],'url':VIDEO})
        self.assertTrue(errors);self.assertFalse(service.pending)


if __name__=='__main__':unittest.main()

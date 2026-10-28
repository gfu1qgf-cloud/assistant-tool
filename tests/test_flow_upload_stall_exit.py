"""Three-minute re-drag through the existing extension API; no paid replay."""
import time
import unittest
from unittest.mock import patch

import test_flow_stall_recovery as fixtures
from app_plugins.builtin.flow_assistant.credit_packs import begin
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info
from app_plugins.builtin.flow_assistant.ui_permission_recovery import recover as permit_retry
from app_plugins.builtin.flow_assistant.runtime_close import PAUSED


class UploadStallExitTests(unittest.TestCase):
    setUpClass=fixtures.StallRecoveryTests.__dict__['setUpClass']
    setUp=fixtures.StallRecoveryTests.setUp
    service=fixtures.StallRecoveryTests.service
    browser=fixtures.StallRecoveryTests.browser
    client=fixtures.StallRecoveryTests.client
    jobs=fixtures.StallRecoveryTests.jobs
    ready=fixtures.StallRecoveryTests.ready
    messages=fixtures.StallRecoveryTests.messages

    def uploading(self):
        service=self.service();service.timer.stop();service.paused=False
        service.preferences['browser_options']={'visual_upload_fallback':False}
        client=self.ready(service);service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'uploading',browser=client['id'],attempt='a'*32,remote={
            'profile':client['profile'],'account':client['account'],'cost':12})
        at=time.time()
        with patch('time.time',return_value=at):self.assertTrue(service.start_upload(service.store.get(row['id'])))
        return service,client,service.store.get(row['id']),at

    def uploads(self,client):return [m for m in self.messages(client) if m.get('action')=='upload_images']

    def test_exact_three_minute_boundary_cancels_old_wait_before_force_redrag(self):
        service,client,row,at=self.uploading();original=next(iter(service.pending))
        with patch('time.time',return_value=at+179):self.assertFalse(service.redrag_due_upload(row))
        self.assertEqual(len(self.uploads(client)),1)
        with patch('time.time',return_value=at+180):self.assertTrue(service.redrag_due_upload(service.store.get(row['id'])))
        self.assertTrue(service.pending[original]['retired']);self.assertEqual(len(self.uploads(client)),1)
        service._response(client,{'request_id':original,'success':False,'error':'cancelled old observation'})
        with patch('time.time',return_value=at+184):service.redrag_due_upload(service.store.get(row['id']))
        self.assertEqual(len(self.uploads(client)),1)
        with patch('time.time',return_value=at+185):self.assertTrue(service.redrag_due_upload(service.store.get(row['id'])))
        uploads=self.uploads(client);self.assertEqual(len(uploads),2)
        self.assertNotEqual(uploads[1]['params']['reference_reset'],uploads[0]['params']['reference_reset'])
        self.assertEqual(uploads[1]['params']['upload_id'],row['attempt'])
        self.assertEqual(service.store.get(row['id'])['browser'],row['browser'])
        self.assertFalse(any(m.get('action')=='run_tasks' for m in self.messages(client)))
        self.assertFalse(service.store.attempt_charged(row['attempt']))

    def test_exhausted_verification_waits_for_deadline_then_uses_fresh_upload_cycle(self):
        service,client,row,at=self.uploading();service.pending.clear()
        remote={**row['remote'],'transfer_retries':{'upload':{'attempt':row['attempt'],'count':5,'stopped':False}}}
        service.store.update(row['id'],'uploading',remote=remote)
        with patch('time.time',return_value=at+60):
            service.transfer_failed(service.store.get(row['id']),'upload','此图此前已发起上传，本次仅核对原素材，未重复拖放：尚未确认素材编号')
        waiting=service.store.get(row['id']);self.assertEqual(waiting['status'],'uploading')
        self.assertFalse(retry_info(waiting,'upload')['stopped']);self.assertEqual(retry_info(waiting,'upload')['next_at'],at+180)
        with patch('time.time',return_value=at+180):service.watch_transfer_retries()
        latest=service.store.get(row['id']);self.assertEqual(len(self.uploads(client)),2)
        self.assertEqual(retry_info(latest,'upload')['count'],0)
        self.assertEqual(latest['remote']['upload_cycle']['at'],at+180)
        with patch('time.time',return_value=at+181):self.assertFalse(service.redrag_due_upload(latest))

    def test_permit_and_pack_requeue_preserve_timer_and_retry_budget(self):
        service,client,row,at=self.uploading();service.pending.clear()
        service.store.update(row['id'],'uploading',remote={**row['remote'],'transfer_retries':{
            'upload':{'attempt':row['attempt'],'count':4,'stopped':False}}})
        self.assertTrue(permit_retry(service,client,service.store.get(row['id']),'upload_images',{
            'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'前台 UI 操作互斥申请未完成：rejected'}))
        queued=service.store.get(row['id']);self.assertEqual(retry_info(queued,'upload')['count'],4)
        begin(service,queued,client);prepared=service.store.get(row['id'])
        self.assertEqual(retry_info(prepared,'upload')['count'],4)
        self.assertEqual(prepared['remote']['upload_cycle']['at'],at)

    def test_confirmed_image_paid_work_manual_disable_and_permanent_error_never_force_upload(self):
        for kind in ('confirmed','paid','manual','permanent'):
            with self.subTest(kind=kind):
                self.setUp();service,client,row,at=self.uploading();service.pending.clear()
                remote=dict(row['remote'])
                if kind=='confirmed':
                    cycle=service.upload_cycle(row,client);cycle['confirmed']=True;remote['upload_cycle']=dict(cycle)
                elif kind=='paid':remote['generation_response']={'taskId':'original-paid-task'}
                elif kind=='manual':service.preferences['browser_blocks']={'profile:'+client['profile']:PAUSED}
                else:remote['transfer_retries']={'upload':{'permanent':True,'stopped':True}}
                service.store.update(row['id'],'uploading',remote=remote)
                with patch('time.time',return_value=at+181):self.assertFalse(service.redrag_due_upload(service.store.get(row['id'])))
                self.assertEqual(len(self.uploads(client)),1)

    def test_next_clip_of_same_image_keeps_forced_revision_and_does_not_redrag_early(self):
        service,client,row,at=self.uploading();service.pending.clear()
        with patch('time.time',return_value=at+180):service.redrag_due_upload(row)
        revision=self.uploads(client)[-1]['params']['reference_reset'];service.pending.clear()
        cycle=service.upload_cycle(service.store.get(row['id']),client);cycle['confirmed']=True
        service.store.update(row['id'],'completed')
        service.store.enqueue(self.jobs(duration=8,budget=0));next_row=service.store.rows()[-1]
        service.store.update(next_row['id'],'uploading',browser=client['id'],attempt='b'*32,remote={
            'account':client['account'],'profile':client['profile'],'cost':12})
        with patch('time.time',return_value=at+181):service.start_upload(service.store.get(next_row['id']))
        self.assertEqual(self.uploads(client)[-1]['params']['reference_reset'],revision)


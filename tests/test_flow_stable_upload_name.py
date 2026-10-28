"""Stable filenames, current-page negotiation and short unpaid reset exits."""
import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_flow_image_content_id as image_cases
import test_flow_task_reset as reset_cases
from app_plugins.builtin.flow_assistant.reference_image import freeze_reference, verified_receipt
from app_plugins.builtin.flow_assistant import ui_mutex


class StableNameTests(unittest.TestCase):
    setUpClass=image_cases.ImageContentIdTests.__dict__['setUpClass']
    setUp=image_cases.ImageContentIdTests.setUp
    service=image_cases.ImageContentIdTests.service
    jobs=image_cases.ImageContentIdTests.jobs
    upload=image_cases.ImageContentIdTests.upload
    request=image_cases.ImageContentIdTests.request
    client=image_cases.ImageContentIdTests.client
    actions=image_cases.ImageContentIdTests.actions

    def test_same_bytes_same_filename_with_separate_attempt_paths_and_receipts(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0]
        row['attempt']='a'*32;one=freeze_reference(row,service.downloads_root,stable_image_name=True)
        row['attempt']='b'*32;two=freeze_reference(row,service.downloads_root,stable_image_name=True)
        self.assertEqual(one['filename'],two['filename']);self.assertNotEqual(one['path'],two['path'])
        self.assertEqual(one['filename'],'IMG_'+one['sha256']+'.png')
        self.assertTrue(verified_receipt(two,{**two,'media_id':'media','verified':True}))
        self.assertFalse(verified_receipt(two,{**one,'media_id':'media','verified':True}))
        wrong={**one,'upload_id':'b'*32}
        self.assertFalse(verified_receipt(wrong,{**wrong,'media_id':'media','verified':True}))
        self.assertEqual(Path(one['path']).read_bytes(),Path(two['path']).read_bytes())

    def test_live_page_capability_enables_stable_names_and_missing_receipt_removes_it(self):
        service=self.service();client=self.client(service)
        for flag,expected in [(True,True),(None,False),('true',False)]:
            service._snapshot(client,{'url':client['saved_project'],'page_instance':'current',
                'reference_sha256_names':True,'reference_stable_sha256_names':flag})
            self.assertEqual(client['reference_stable_sha256_names'],expected)

    def test_retries_keep_old_filename_and_new_attempt_uses_stable_name(self):
        service=self.service();row,client=self.upload(service)
        before=row['remote']['upload_reference']
        client.update(reference_sha256_names=True,reference_stable_sha256_names=True)
        service.pending.clear();service.start_upload(service.store.get(row['id']))
        self.assertEqual(service.store.get(row['id'])['remote']['upload_reference'],before)
        service.pending.clear();service.store.update(row['id'],'uploading',attempt='c'*32)
        service.start_upload(service.store.get(row['id']))
        ref=service.store.get(row['id'])['remote']['upload_reference']
        self.assertEqual(ref['filename'],'IMG_'+ref['sha256']+'.png')

    def test_phase_receipt_is_attempt_scoped_and_publish_distinguishes_upload_from_mount(self):
        service=self.service();row,client=self.upload(service);key=self.request(service,'upload_images')
        snapshots=[];service.changed.connect(snapshots.append)
        event={'type':'reference_progress','request_id':key,'upload_id':row['attempt'],
            'phase':'mounting','url':client['saved_project']}
        service._message(client['socket'],json.dumps(event))
        self.assertEqual(service.store.get(row['id'])['remote']['upload_phase']['phase'],'mounting')
        self.assertEqual(snapshots[-1]['jobs'][0]['status_label'],'挂载参考图首帧')
        before=service.store.get(row['id'])
        service._message(client['socket'],json.dumps({**event,'upload_id':'d'*32,'phase':'uploading'}))
        self.assertEqual(service.store.get(row['id']),before)


class ShortResetTests(unittest.TestCase):
    setUpClass=reset_cases.TaskResetTests.__dict__['setUpClass']
    setUp=reset_cases.TaskResetTests.setUp
    service=reset_cases.TaskResetTests.service
    jobs=reset_cases.TaskResetTests.jobs
    client=reset_cases.TaskResetTests.client
    browser=reset_cases.TaskResetTests.browser
    orphan=reset_cases.TaskResetTests.orphan
    command=reset_cases.TaskResetTests.command
    actions=reset_cases.TaskResetTests.actions

    def active(self):
        service=self.service();row=self.orphan(service)
        client=self.browser(service,'Profile 23');client['id']=row['browser']
        service.preferences['browser_options']={'ui_mutex':True}
        client['capabilities']=['ui_mutex_v1']
        service.request(client,'upload_images',job=row['id'])
        key=next(k for k,p in service.pending.items() if p['action']=='upload_images')
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':'1'*32,'operation':'upload_images','url':client['saved_project']})
        return service,row,client,key

    def test_reset_sends_cancellation_and_releases_active_lock_within_five_seconds(self):
        service,row,client,key=self.active();now=time.time()
        self.assertEqual(ui_mutex.state(service)['owner']['client'],client['id'])
        service.execute(self.command(row))
        self.assertTrue(any(a.get('type')=='cancel_operation' and a['target_request_id']==key for a in self.actions(client)))
        self.assertLessEqual(service.store.reset_holds()[0]['until']-now,5.5)
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'2'*32,'profile':'Other'})
        with patch('time.time',return_value=now+6):
            ui_mutex.watch(service)
            self.assertEqual(ui_mutex.state(service)['owner']['client'],'@host')
            service.paused=True;service._tick()
            self.assertNotIn(key,service.pending);self.assertFalse(service.store.reset_holds())
        current=service.store.get(row['id'])
        self.assertEqual(current['status'],'preparing')
        self.assertNotEqual(current['attempt'],row['attempt'])

    def test_cancellation_reply_finishes_hold_early_and_cannot_submit_a_new_job(self):
        service,row,client,key=self.active();service.execute(self.command(row));before=service.store.get(row['id'])
        service._response(client,{'request_id':key,'success':False,'code':'FLOW_OPERATION_CANCELLED'})
        self.assertFalse(service.store.reset_holds());self.assertEqual(service.store.get(row['id']),before)
        self.assertNotIn('run_tasks',[a.get('action') for a in self.actions(client)])

    def test_new_request_of_same_kind_cannot_keep_the_retired_lease_alive(self):
        service,row,client,key=self.active();now=time.time()
        service.execute(self.command(row))
        replacement={k:v for k,v in service.pending[key].items() if k not in {'retired','cancel_deadline'}}
        service.pending['f'*32]={**replacement,'job':'other-job','attempt':'f'*32}
        with patch('time.time',return_value=now+6):ui_mutex.watch(service)
        self.assertIsNone(ui_mutex.state(service)['owner'])

    def test_charged_reset_does_not_cancel_paid_work_or_use_short_deadline(self):
        service,row,client,key=self.active();service.store.reserve(row['id'],row['attempt'],10,'rule')
        service.store.update(row['id'],'submitting');row=service.store.get(row['id'])
        service.execute(self.command(row,confirmed_submitted=True))
        self.assertNotIn('cancel_deadline',service.pending[key])
        self.assertGreater(service.store.reset_holds()[0]['until']-time.time(),60)
        self.assertTrue(service.store.attempt_charged(row['attempt']))


if __name__=='__main__':unittest.main()

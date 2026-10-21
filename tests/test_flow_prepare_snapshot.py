"""Reuse a real verified transition without weakening debit or reference checks."""
import copy
import json
import time
import unittest

import test_flow_image_affinity as fixtures
from flow_reference_fixtures import upload_receipt


class PrepareSnapshotTests(unittest.TestCase):
    setUpClass=fixtures.ImageAffinityTests.__dict__['setUpClass']
    setUp=fixtures.ImageAffinityTests.setUp
    service=fixtures.ImageAffinityTests.service
    jobs=fixtures.ImageAffinityTests.jobs
    client=fixtures.ImageAffinityTests.client
    browser=fixtures.ImageAffinityTests.browser

    def actions(self, client):
        return [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]

    def prepared(self):
        service=self.service();client=self.browser(service,'Profile 1',points=90)
        client['page_instance']='document-one';service.preferences['concurrency']=1
        service.store.enqueue(self.jobs(duration=8,count=2,budget=0));service.paused=False;service._tick()
        row=service.store.rows()[0]
        key=next(k for k,p in service.pending.items() if p['action']=='prepare')
        service._response(client,{'request_id':key,'success':True,'data':{
            'verified':True,'model':'omni_1_1_flash','email':client['account'],'page_instance':'document-one'}})
        self.assertIn('prepare_snapshot',client)
        self.assertEqual(service.store.get(row['id'])['status'],'uploading')
        # Finish the first simulated job and its upload request. No paid call.
        service.pending={k:p for k,p in service.pending.items() if p['job']!=row['id']}
        service.store.update(row['id'],'completed')
        return service,client,service.store.rows()[1]

    def test_same_page_settings_skip_second_prepare_and_keep_strict_upload_and_single_debit(self):
        service,client,row=self.prepared();observed=client['prepare_snapshot']['at'];service._tick()
        current=service.store.get(row['id'])
        self.assertEqual(current['status'],'uploading');self.assertTrue(current['remote']['prepare_snapshot']['reused'])
        self.assertEqual(sum(a['action']=='prepare' for a in self.actions(client)),1)
        self.assertEqual(client['prepare_snapshot']['at'],observed)
        upload=next(k for k,p in service.pending.items() if p['action']=='upload_images')
        receipt=upload_receipt(service,row['id'])
        service._response(client,{'request_id':upload,'success':True,'data':receipt})
        service._response(client,{'request_id':upload,'success':True,'data':receipt})
        self.assertEqual(sum(a['action']=='run_tasks' for a in self.actions(client)),1)
        self.assertEqual(client['credits'],90-current['remote']['cost'])
        self.assertEqual(self.actions(client)[-1]['params']['expected_account'],client['account'])

    def test_cache_misses_on_refresh_account_settings_project_extension_expiry_or_disabled(self):
        for mode in ('page','account','settings','project','extension','expired','disabled','stale_credits'):
            with self.subTest(mode=mode):
                self.setUp();service,client,row=self.prepared()
                if mode=='page':client['page_instance']='document-two'
                elif mode=='account':client['prepare_snapshot']['account']='other@example.test'
                elif mode=='settings':client['prepare_snapshot']['settings']=('omni_1_1_flash',10,'9:16','720p')
                elif mode=='project':client['prepare_snapshot']['project']=('flow.google.com','/project/other')
                elif mode=='extension':client['prepare_snapshot']['extension']='old-extension'
                elif mode=='expired':client['prepare_snapshot']['at']=time.time()-21601
                elif mode=='disabled':service.preferences['timing']={'prepare_snapshot_seconds':0}
                else:client['credits_at']=time.time()-21601
                service._tick()
                self.assertNotEqual(service.store.get(row['id'])['status'],'uploading')
                self.assertFalse(any(a['action']=='run_tasks' for a in self.actions(client)))

    def test_cached_prepare_cannot_skip_a_reduced_budget_or_missing_reference_receipt(self):
        for mode in ('budget','reference'):
            with self.subTest(mode=mode):
                self.setUp();service,client,row=self.prepared()
                if mode=='budget':service.store.set_payload(row['id'],{**row['payload'],'budget':1})
                service._tick()
                if mode=='reference':
                    key=next(k for k,p in service.pending.items() if p['action']=='upload_images')
                    service._response(client,{'request_id':key,'success':True,'data':{}})
                self.assertFalse(any(a['action']=='run_tasks' for a in self.actions(client)))
                self.assertFalse(service.store.batches()[row['payload']['batch_id']]['reserved'])

    def test_old_extension_without_page_proof_never_creates_a_cache(self):
        service=self.service();client=self.browser(service,points=90)
        service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False;service._tick()
        key=next(k for k,p in service.pending.items() if p['action']=='prepare')
        service._response(client,{'request_id':key,'success':True,'data':{
            'verified':True,'model':'omni_1_1_flash','email':client['account']}})
        self.assertNotIn('prepare_snapshot',client)

    def test_full_reset_invalidates_preflight_snapshot(self):
        service,client,row=self.prepared();service._tick();current=service.store.get(row['id'])
        service.execute({'action':'reset_tasks','ids':[row['id']], 'versions':{
            row['id']:{name:current[name] for name in ('attempt','browser','status')}}})
        self.assertNotIn('prepare_snapshot',client)


if __name__=='__main__':unittest.main()

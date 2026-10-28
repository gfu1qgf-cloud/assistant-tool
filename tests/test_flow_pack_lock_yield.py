"""Pack timeout hands over the lock, preserving members, attempts and charges."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_packs as fixtures
from app_plugins.builtin.flow_assistant import credit_packs as packs, ui_mutex, pack_mutex
from app_plugins.builtin.flow_assistant.timing import value


class PackLockYieldTests(unittest.TestCase):
    setUpClass=fixtures.PackTests.__dict__['setUpClass']
    setUp=fixtures.PackTests.setUp
    service=fixtures.PackTests.service
    jobs=fixtures.PackTests.jobs
    client=fixtures.PackTests.client
    actions=fixtures.PackTests.actions
    setup_pack=fixtures.PackTests.setup_pack
    dispatch=fixtures.PackTests.dispatch
    finish_submit=fixtures.PackTests.finish_submit

    def configured(self):
        service,client=self.setup_pack();service.timer.stop()
        service.preferences['browser_options']={'ui_mutex':True,'focus_before_action':False}
        service.preferences['timing']={'pack_ui_slice_seconds':10}
        self.dispatch(service)
        return service,client

    def acquire(self,service,client,token,operation,batch=''):
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':token,'operation':operation,
            'batch_id':batch,'url':client['saved_project']})

    def release(self,service,client,token):
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':token})

    def submitting(self,service,client):
        row=next(r for r in service.store.rows() if r['status']=='preparing')
        service.pending.clear()
        service.store.update(row['id'],'submitting',remote={**row['remote'],'pack_sent_at':time.time()})
        service.request(client,'run_tasks',job=row['id'])
        return service.store.get(row['id'])

    def test_default_is_120_and_initial_upload_is_outside_submit_clock(self):
        self.assertEqual(value({},'pack_ui_slice_seconds'),120)
        service,client=self.configured();now=time.time()
        row=service.store.rows()[0];service.pending.clear()
        service.store.update(row['id'],'uploading');service.request(client,'upload_images',job=row['id'])
        self.acquire(service,client,'a'*32,'upload_images')
        state=ui_mutex.state(service)
        with patch('time.time',return_value=now+150):ui_mutex.watch(service)
        self.assertEqual(state['owner']['token'],'a'*32)
        self.assertIsNone(state['burst']['started_at']);self.assertEqual(state['burst']['elapsed'],0)
        self.assertTrue(all(r['remote'].get('credit_pack') for r in service.store.rows()))

    def test_unclaimed_prepare_burst_cannot_block_other_windows_while_upload_request_is_pending(self):
        service,client=self.configured();now=time.time()
        self.acquire(service,client,'a'*32,'prepare')
        state=ui_mutex.state(service);self.release(service,client,'a'*32)
        self.assertIsNone(state['owner']);self.assertIn('burst',state)
        row=next(r for r in service.store.rows() if r['status']=='preparing')
        service.pending.clear();service.store.update(row['id'],'uploading')
        service.request(client,'upload_images',job=row['id'])
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'b'*32,'profile':'Other'})
        self.assertIsNone(state['owner'])
        before=service.store.rows()
        with patch('time.time',return_value=now+11):ui_mutex.watch(service)
        self.assertEqual(state['owner']['client'],'@host');self.assertNotIn('burst',state)
        self.assertEqual(service.store.rows(),before,'yielding a stale priority cannot alter the pack or replay submissions')

    def test_start_clock_on_submit_and_yield_only_after_current_phase_releases(self):
        service,client=self.configured();row=self.submitting(service,client);now=time.time()
        self.acquire(service,client,'a'*32,'submit',row['attempt'])
        state=ui_mutex.state(service);self.assertIsNotNone(state['burst']['started_at'])
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'b'*32,'profile':'Other'})
        before=service.store.rows()
        with patch('time.time',return_value=now+11):
            ui_mutex.watch(service)
            self.assertEqual(state['owner']['token'],'a'*32,'slice expiry must never revoke an active page phase')
            self.release(service,client,'a'*32)
        self.assertEqual(state['owner']['client'],'@host')
        self.assertNotIn('burst',state)
        # The granted submit timestamp may change, but yielding must not change
        # the membership, original browser, image, order or attempt.
        after=service.store.rows()
        for old,new in zip(before,after):
            self.assertEqual(old['attempt'],new['attempt'])
            self.assertEqual(old['status'],new['status'])
            self.assertEqual(old['remote']['credit_pack'],new['remote']['credit_pack'])
        self.assertEqual(client['pack_ui']['state'],'waiting')
        from app_plugins.builtin.flow_assistant.policy import waiting_reason
        queued=next(r for r in after if r['status']=='queued')
        reason=waiting_reason(queued,[client],after,service.preferences,{},service.store.batches())
        self.assertIn('整包保持原账号、图片和顺序',reason)

    def test_upload_after_first_submit_pauses_the_submission_budget(self):
        service,client=self.configured();row=self.submitting(service,client);now=time.time()
        self.acquire(service,client,'a'*32,'submit',row['attempt'])
        with patch('time.time',return_value=now+2):self.release(service,client,'a'*32)
        service.pending.clear();service.store.update(row['id'],'uploading')
        service.request(client,'upload_images',job=row['id'])
        with patch('time.time',return_value=now+3):self.acquire(service,client,'b'*32,'upload_images')
        state=ui_mutex.state(service);elapsed=state['burst']['elapsed']
        with patch('time.time',return_value=now+140):ui_mutex.watch(service)
        self.assertEqual(state['owner']['token'],'b'*32)
        self.assertAlmostEqual(state['burst']['elapsed'],elapsed,delta=.05)

    def test_reacquire_after_handoff_continues_original_pack_and_submits_once(self):
        service,client=self.configured();now=time.time()
        self.acquire(service,client,'a'*32,'prepare')
        first=self.finish_submit(service,client)
        self.release(service,client,'a'*32)
        state=ui_mutex.state(service)
        state['burst'].update(started_at=now,clock_at=now,elapsed=0)
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'b'*32,'profile':'Other'})
        with patch('time.time',return_value=now+11):
            ui_mutex.watch(service)
            self.assertEqual(state['owner']['client'],'@host')
            self.assertTrue(self.dispatch(service))
            next_row=next(r for r in service.store.rows() if r['status']=='preparing')
            self.acquire(service,client,'c'*32,'prepare')
            self.assertEqual(state['owner']['client'],'@host')
            ui_mutex.host_receive(service,{'action':'ui_native_release','token':'b'*32})
            self.assertEqual(state['owner']['token'],'c'*32)
            second=self.finish_submit(service,client)
        self.assertEqual(first['remote']['credit_pack']['id'],second['remote']['credit_pack']['id'])
        self.assertEqual(first['remote']['credit_pack']['browser'],second['remote']['credit_pack']['browser'])
        self.assertNotEqual(first['attempt'],second['attempt'])
        self.assertEqual(len([a for a in self.actions(client) if a.get('action')=='run_tasks']),2)
        self.assertEqual(client['credits'],20)

    def test_waiting_for_another_lock_does_not_trigger_ack_or_request_timeouts(self):
        service,client=self.configured();row=self.submitting(service,client);now=time.time()
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'a'*32,'profile':'Other'})
        self.acquire(service,client,'b'*32,'submit',row['attempt'])
        pack=copy.deepcopy(service.store.rows()[1]['remote']['credit_pack'])
        # Keep a real, separately bounded owner alive while this packed submit
        # waits. The queue wait must not masquerade as a missing website reply.
        with patch('time.time',return_value=now+25):
            ui_mutex.watch(service)
            self.assertFalse(self.dispatch(service))
            pending=next(p for p in service.pending.values() if p['action']=='run_tasks')
            self.assertAlmostEqual(pending['at'],now+25,delta=.05)
            self.assertEqual(service.store.rows()[1]['remote']['credit_pack'],pack)
            self.assertEqual(service.store.rows()[0]['status'],'submitting')
        with patch('time.time',return_value=now+26):
            ui_mutex.host_receive(service,{'action':'ui_native_release','token':'a'*32})
        self.assertAlmostEqual(service.store.get(row['id'])['remote']['pack_sent_at'],now+26,delta=.05)

    def test_waiter_keeps_exact_attempt_and_can_outlast_previous_queue_limit(self):
        service,client=self.configured();row=self.submitting(service,client);now=time.time()
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'a'*32,'profile':'Other'})
        self.acquire(service,client,'b'*32,'submit',row['attempt'])
        state=ui_mutex.state(service)
        # A bounded other phase is renewed here only as a fixture; the packed
        # waiter must not be rejected merely because its own wall clock is old.
        state['owner']['at']=now+170
        with patch('time.time',return_value=now+171):ui_mutex.watch(service)
        self.assertTrue(any(e['token']=='b'*32 for e in state['queue']))
        self.assertEqual(service.store.get(row['id'])['attempt'],row['attempt'])

    def test_slow_submit_ui_does_not_consume_the_website_receipt_deadline(self):
        service,client=self.configured();row=self.submitting(service,client);now=time.time()
        self.acquire(service,client,'a'*32,'submit',row['attempt'])
        before=service.store.rows()[1]['remote']['credit_pack']
        with patch('time.time',return_value=now+25):
            ui_mutex.watch(service)
            self.assertFalse(self.dispatch(service))
            self.assertEqual(service.store.rows()[1]['remote']['credit_pack'],before)
            self.release(service,client,'a'*32)
        self.assertAlmostEqual(service.store.get(row['id'])['remote']['pack_sent_at'],now+25,delta=.05)
        with patch('time.time',return_value=now+26):self.dispatch(service)
        self.assertEqual(service.store.rows()[1]['remote']['credit_pack'],before)

    def test_switch_off_or_disconnect_releases_retention_without_revoking_cloud(self):
        service,client=self.configured();self.acquire(service,client,'a'*32,'prepare')
        self.release(service,client,'a'*32)
        service.preferences['browser_options']['ui_mutex']=False
        ui_mutex.watch(service)
        self.assertNotIn('burst',ui_mutex.state(service))
        self.assertTrue(all(r['remote'].get('credit_pack') for r in service.store.rows()))


if __name__=='__main__':unittest.main()

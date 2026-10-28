"""Progress leases, race order and stop-before-handoff; no browser operations."""
import copy,json,time,unittest
from unittest.mock import patch
import test_flow_ui_mutex as fixtures
from app_plugins.builtin.flow_assistant import ui_mutex,ui_lease,mutex_monitor


class UiRenewalTests(unittest.TestCase):
    setUpClass=fixtures.UiMutexTests.__dict__['setUpClass']
    setUp=fixtures.UiMutexTests.setUp
    service=fixtures.UiMutexTests.service
    jobs=fixtures.UiMutexTests.jobs
    client=fixtures.UiMutexTests.client
    browser=fixtures.UiMutexTests.browser
    configured=fixtures.UiMutexTests.configured
    request=fixtures.UiMutexTests.request
    messages=fixtures.UiMutexTests.messages

    def locked(self):
        service,clients=self.configured();service.ui_lock_timer.stop()
        clients[0]['capabilities'].append(ui_lease.CAPABILITY)
        self.request(service,clients[0],'a'*32)
        self.request(service,clients[1],'b'*32)
        return service,clients,ui_mutex.state(service)['owner']

    def renew(self,service,client,**updates):
        metadata={'token':'a'*32,'seq':1,'step':'settings-confirmed','url':client['saved_project'],**updates}
        before=len(self.messages(client))
        service._message(client['socket'],json.dumps({'type':'reference_progress','ui_renew':metadata}))
        return next((m for m in self.messages(client)[before:] if 'renew_seq' in m),{'state':'ignored'})

    def test_success_extends_same_owner_and_deadline_only_and_duplicate_is_idempotent(self):
        service,clients,owner=self.locked();original=ui_lease.deadline(service,owner);at=owner['at']
        before=copy.deepcopy(service.store.rows())
        with patch('time.time',return_value=original-2):
            result=self.renew(service,clients[0]);new=owner['expires_at']
            self.assertEqual(result['state'],'granted');self.assertGreater(new,original)
            self.assertIs(ui_mutex.state(service)['owner'],owner)
            self.assertEqual(owner['at'],at)
            self.assertEqual(mutex_monitor.snapshot(service)['owner']['expires_at'],new)
        with patch('time.time',return_value=original+1):
            self.assertEqual(self.renew(service,clients[0])['expires_at'],new)
            ui_mutex.watch(service)
        self.assertIs(ui_mutex.state(service)['owner'],owner)
        self.assertEqual(service.store.rows(),before)
        self.assertTrue(any(e['client']==clients[1]['id'] for e in service.ui_mutex['queue']))

    def test_invalid_optional_requests_leave_normal_lease_and_queue_untouched(self):
        service,clients,owner=self.locked();before=copy.copy(owner);queue=list(service.ui_mutex['queue'])
        for fields in ({'token':'c'*32},{'seq':True},{'seq':0},{'seq':999},{'step':'waiting'},
                       {'step':'reference-mounted'},{'step':{}},{'url':'https://flow.google.com/project/other'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.renew(service,clients[0],**fields)['state'],'ignored')
                self.assertEqual(owner,before);self.assertEqual(service.ui_mutex['queue'],queue)
        self.assertEqual(self.renew(service,clients[1])['state'],'ignored')
        self.assertIs(service.ui_mutex['owner'],owner)
        self.assertFalse(service.safety_paused)

    def test_repeated_progress_step_cannot_keep_alive_even_with_new_sequence(self):
        service,clients,owner=self.locked();self.renew(service,clients[0])
        deadline=owner['expires_at']
        self.assertEqual(self.renew(service,clients[0],seq=2)['state'],'ignored')
        self.assertEqual(owner['expires_at'],deadline)

    def test_absent_or_broken_optional_metadata_does_not_block_the_normal_report(self):
        service,clients,owner=self.locked();before=copy.copy(owner)
        for field in ({},{'ui_renew':None},{'ui_renew':False}):
            service._message(clients[0]['socket'],json.dumps({'type':'reference_progress',**field}))
        self.assertEqual(owner,before)
        with patch.object(ui_lease,'renew',side_effect=RuntimeError('optional failure')):
            service._message(clients[0]['socket'],json.dumps({'type':'page_status','url':clients[0]['saved_project'],
                'note':'ordinary report accepted','controls_ready':False,'ui_renew':{'token':owner['token']}}))
        self.assertEqual(clients[0]['note'],'ordinary report accepted')
        self.assertIs(service.ui_mutex['owner'],owner)

    def test_expiry_retains_owner_until_exact_stop_receipt_and_rejects_late_renewal(self):
        service,clients,owner=self.locked();end=owner['expires_at']
        with patch('time.time',return_value=end):
            ui_mutex.watch(service)
            self.assertIs(service.ui_mutex['owner'],owner);self.assertTrue(owner['stopping'])
            self.assertIsNone(ui_mutex.foreground_pending(service,clients[0],owner['token']))
            self.assertEqual(self.renew(service,clients[0])['state'],'ignored')
            ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':owner['token']})
            self.assertIs(service.ui_mutex['owner'],owner,'unconfirmed release cannot hand off a stopping operation')
            ui_mutex.receive(service,clients[1],{'type':'ui_mutex_release','token':owner['token'],'stopped':True})
            self.assertIs(service.ui_mutex['owner'],owner)
            ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':owner['token'],'stopped':True})
        self.assertEqual(service.ui_mutex['owner']['client'],clients[1]['id'])
        self.assertEqual(self.renew(service,clients[0])['state'],'ignored')
        self.assertEqual(service.ui_mutex['owner']['client'],clients[1]['id'])

    def test_frozen_holder_disconnects_after_grace_before_next_grant(self):
        service,clients,owner=self.locked();end=owner['expires_at']
        service.ui_mutex['queue'][0]['queued_at']=end-1
        with patch('time.time',return_value=end):ui_mutex.watch(service)
        with patch('time.time',return_value=end+ui_lease.STOP_SECONDS-.1):ui_mutex.watch(service)
        clients[0]['socket'].abort.assert_not_called()
        with patch('time.time',return_value=end+ui_lease.STOP_SECONDS+.1):ui_mutex.watch(service)
        clients[0]['socket'].abort.assert_called_once()
        self.assertNotIn(clients[0]['socket'],service.clients)
        self.assertEqual(service.ui_mutex['owner']['client'],clients[1]['id'])

    def test_expiry_and_renewal_are_serialized_in_both_orders(self):
        service,clients,owner=self.locked();end=owner['expires_at']
        with patch('time.time',return_value=end-.01):self.renew(service,clients[0])
        with patch('time.time',return_value=end+.01):ui_mutex.watch(service)
        self.assertFalse(owner.get('stopping'))
        self.assertIs(service.ui_mutex['owner'],owner)
        with patch('time.time',return_value=owner['expires_at']):
            ui_mutex.watch(service);self.assertEqual(self.renew(service,clients[0],seq=2)['state'],'ignored')
        self.assertTrue(owner.get('stopping'))

    def test_cancel_operation_waits_for_stop_and_legacy_lease_is_not_opted_in(self):
        service,clients,owner=self.locked()
        ui_mutex.cancel_operations(service,clients[0],{'prepare'})
        self.assertTrue(owner.get('stopping'));self.assertIs(service.ui_mutex['owner'],owner)
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':owner['token'],'stopped':True})
        other=service.ui_mutex['owner'];self.assertFalse(other['renewable'])
        self.assertNotIn('renewal',self.messages(clients[1])[-1])


if __name__=='__main__':unittest.main()

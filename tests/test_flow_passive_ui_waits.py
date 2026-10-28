"""A passive upload/receipt releases both the lease and pack priority."""
import copy,unittest
import test_flow_pack_lock_yield as fixtures
from app_plugins.builtin.flow_assistant import ui_mutex

class PassiveUiWaitTests(unittest.TestCase):
    setUpClass=fixtures.PackLockYieldTests.__dict__['setUpClass']
    setUp=fixtures.PackLockYieldTests.setUp
    service=fixtures.PackLockYieldTests.service
    jobs=fixtures.PackLockYieldTests.jobs
    client=fixtures.PackLockYieldTests.client
    actions=fixtures.PackLockYieldTests.actions
    setup_pack=fixtures.PackLockYieldTests.setup_pack
    dispatch=fixtures.PackLockYieldTests.dispatch
    configured=fixtures.PackLockYieldTests.configured
    acquire=fixtures.PackLockYieldTests.acquire
    submitting=fixtures.PackLockYieldTests.submitting

    def setup_wait(self):
        service,client=self.configured();row=service.store.rows()[0]
        service.pending.clear();service.store.update(row['id'],'uploading')
        service.request(client,'upload_images',job=row['id'])
        self.acquire(service,client,'a'*32,'upload_images')
        other=self.client(service,'Other',50)
        other.update(capabilities=['ui_mutex_v1'],saved_project=client['saved_project'])
        service.request(other,'prepare');self.acquire(service,other,'b'*32,'prepare')
        return service,client,other

    def test_passive_release_hands_desktop_to_another_browser_without_splitting_pack(self):
        service,client,other=self.setup_wait();before=copy.deepcopy(service.store.rows())
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':'a'*32,'passive_wait':True})
        current=ui_mutex.state(service)
        self.assertEqual(current['owner']['client'],other['id']);self.assertNotIn('burst',current)
        self.assertEqual(service.store.rows(),before)
        self.assertEqual(client['pack_ui']['state'],'waiting')
        self.acquire(service,client,'c'*32,'upload_images')
        self.assertEqual(current['owner']['client'],other['id'],'resume waits its turn instead of stealing focus')
        ui_mutex.receive(service,other,{'type':'ui_mutex_release','token':'b'*32})
        self.assertEqual(current['owner']['token'],'c'*32)
        self.assertEqual(service.store.rows(),before)

    def test_regular_release_preserves_user_requested_continuous_pack(self):
        service,client,other=self.setup_wait()
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':'a'*32})
        current=ui_mutex.state(service)
        self.assertIsNone(current['owner']);self.assertEqual(current['burst']['client'],client['id'])

    def test_foreign_passive_release_does_not_revoke_the_current_owner(self):
        service,client,other=self.setup_wait()
        ui_mutex.receive(service,other,{'type':'ui_mutex_release','token':'a'*32,'passive_wait':True})
        current=ui_mutex.state(service)
        self.assertEqual(current['owner']['client'],client['id']);self.assertIn('burst',current)

    def test_unready_page_wait_yields_unowned_priority_but_not_an_active_operation(self):
        service,client,other=self.setup_wait();client['page_ready']=False
        ui_mutex.watch(service)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],client['id'])
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':'a'*32})
        self.assertEqual(ui_mutex.state(service)['owner']['client'],other['id'])
        self.assertNotIn('burst',ui_mutex.state(service))

    def test_pending_submit_receipt_yields_priority_and_preserves_original_attempt(self):
        service,client=self.configured();row=self.submitting(service,client)
        self.acquire(service,client,'a'*32,'submit',row['attempt'])
        current=ui_mutex.state(service)
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'b'*32,'profile':'Other'})
        before=copy.deepcopy(service.store.rows())
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':'a'*32,'passive_wait':True})
        self.assertEqual(current['owner']['client'],'@host');self.assertNotIn('burst',current)
        after=service.store.rows()
        for old,new in zip(before,after):
            self.assertEqual(new['attempt'],old['attempt']);self.assertEqual(new['status'],old['status'])
            self.assertEqual(new['remote'].get('credit_pack'),old['remote'].get('credit_pack'))

if __name__=='__main__':unittest.main()

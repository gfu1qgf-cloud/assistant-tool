"""Collection is persisted without admitting any new browser or paid work."""
import copy
import unittest
import test_flow_transfer_recovery as fixtures
import test_flow_setup as ui_fixtures
from app_plugins.builtin.flow_assistant.policy import admitted_queue
from app_plugins.builtin.flow_assistant.store import QueueStore, submission_risk


class CollectionTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    actions=fixtures.TransferTests.actions
    ui=ui_fixtures.SetupTests.ui

    def collecting(self):
        service=self.service()
        service.commit_preferences({k:v for k,v in service.preferences.items() if k!='collect_before_submit'})
        return service

    def add(self,service,n,duration=10,source='manual'):
        service.execute({'action':'enqueue','source':source,'request_id':'collect-'+str(n),
            'start_after_save':True,'items':[{**self.args,'prompt':'different prompt '+str(n),'budget':0,'duration':duration}]})

    def test_default_collects_manual_and_batch_even_when_queue_is_running(self):
        service=self.collecting();service.paused=False;client=self.client(service)
        launches=[];service.launch_requested.connect(launches.append)
        self.add(service,1);self.add(service,2,source='batch');service._tick()
        rows=service.store.rows()
        self.assertEqual([r['status'] for r in rows],['collected','collected'])
        self.assertFalse(admitted_queue(rows,service.store.batches(),{}))
        self.assertFalse(service.pending);self.assertFalse(launches)
        self.assertTrue(all(not r['attempt'] and not submission_risk(r) for r in rows))

    def test_start_releases_all_saved_same_image_jobs_before_planning_pack(self):
        service=self.collecting();client=self.client(service)
        client.update(capabilities=['credit_pack_submit_v1'],credit_pack_submit=True,page_ready=True,
                      page_at=__import__('time').time(),credits_local=True)
        for n,duration in enumerate((10,10,6,6)):self.add(service,n,duration)
        service.execute({'action':'start'});service._tick()
        rows=service.store.rows()
        self.assertEqual(rows[0]['status'],'preparing')
        self.assertEqual([r['status'] for r in rows[1:]],['queued']*3)
        self.assertEqual(len({r['remote']['credit_pack']['id'] for r in rows}),1)
        self.assertEqual(rows[0]['remote']['credit_pack']['total'],50)

    def test_turning_switch_off_does_not_release_already_collected_jobs(self):
        service=self.collecting();service.paused=False;self.add(service,1)
        service.execute({'action':'preferences','value':{'collect_before_submit':False}})
        self.add(service,2);rows=service.store.rows()
        self.assertEqual([r['status'] for r in rows],['collected','queued'])

    def test_selected_submission_and_replayed_addition_do_not_duplicate_records(self):
        service=self.collecting();self.add(service,1);self.add(service,2)
        rows=service.store.rows();service.execute({'action':'submit_selected','ids':[rows[1]['id']]})
        self.add(service,1)
        self.assertEqual([r['status'] for r in service.store.rows()],['collected','queued'])
        self.assertIn(rows[1]['id'],service.released)

    def test_restart_edit_cancel_reset_and_delete_keep_collection_safe(self):
        service=self.collecting();self.add(service,1);row=service.store.rows()[0]
        store=QueueStore(self.namespace);store.recover();self.assertEqual(store.rows()[0]['status'],'collected')
        service.execute({'action':'edit','ids':[row['id']],'value':{'prompt':'edited',
            **{key:row['payload'][key] for key in ('duration','aspect','resolution','budget')}}})
        row=service.store.get(row['id']);self.assertEqual(row['payload']['prompt'],'edited')
        store.reset_tasks([row['id']],versions={row['id']:{k:row[k] for k in ('status','attempt','browser')}})
        self.assertEqual(store.rows()[0]['status'],'collected')
        service.execute({'action':'cancel','ids':[row['id']]});self.assertEqual(store.rows()[0]['status'],'cancelled')
        self.add(service,2);other=store.rows()[-1]
        result=store.delete_records([other['id']]);self.assertEqual(result['deleted'],[other['id']])

    def test_collection_is_always_local_and_old_switch_is_not_shown(self):
        dialog,_,commands=self.ui();panel=dialog.manual
        self.assertTrue(panel.collect_only.isHidden());self.assertTrue(panel.collect_discard.isHidden())
        self.assertEqual(panel.submit.text(),'追加任务')
        panel.collect_only.setChecked(False)
        self.assertEqual(panel.submit.text(),'追加任务','a legacy preference cannot enable direct generation')

    def test_discard_keeps_records_drafts_files_and_does_not_release_new_tasks(self):
        service=self.collecting();self.add(service,1);first=service.store.rows()[0]
        service.store.save_draft('d'*64,{'prompt':'keep my draft'})
        image=self.image.read_bytes();service.released.add(first['id'])
        self.add(service,2);second=service.store.rows()[1]
        service.execute({'action':'discard_collected','ids':[first['id']]})
        self.assertEqual(service.store.get(first['id'])['status'],'cancelled')
        self.assertEqual(service.store.get(second['id'])['status'],'collected')
        self.assertNotIn(first['id'],service.released)
        self.assertEqual(service.store.drafts()['d'*64]['prompt'],'keep my draft')
        self.assertEqual(self.image.read_bytes(),image);self.assertFalse(service.pending)
        store=QueueStore(self.namespace);store.recover()
        self.assertEqual(store.get(first['id'])['status'],'cancelled')
        store.release_collected();self.assertEqual(store.get(first['id'])['status'],'cancelled')
        self.assertEqual(store.get(second['id'])['status'],'queued')

    def test_stale_discard_does_not_cancel_already_released_or_running_work(self):
        service=self.collecting()
        for n in range(5):self.add(service,n)
        rows=service.store.rows();states=['queued','preparing','generating','completed','collected']
        for row,status in zip(rows,states):service.store.update(row['id'],status)
        service.released.add(rows[0]['id'])
        service.execute({'action':'discard_collected','ids':[row['id'] for row in rows]})
        self.assertEqual([r['status'] for r in service.store.rows()],states[:-1]+['cancelled'])
        self.assertIn(rows[0]['id'],service.released)

    def test_discard_rejects_paid_or_bound_collection_and_empty_means_none(self):
        service=self.collecting()
        for n in range(4):self.add(service,n)
        rows=service.store.rows()
        service.store.reserve(rows[0]['id'],'a'*32,15,'duration_rule')
        service.store.update(rows[1]['id'],'collected',remote={'upstream_task':'old-paid-task'})
        service.store.update(rows[2]['id'],'collected',attempt='b'*32)
        service.execute({'action':'discard_collected','ids':[]})
        self.assertEqual([r['status'] for r in service.store.rows()],['collected']*4)
        service.execute({'action':'discard_collected','ids':[row['id'] for row in rows]})
        self.assertEqual([r['status'] for r in service.store.rows()],['collected']*3+['cancelled'])
        self.assertEqual(service.store.batches()[rows[0]['payload']['batch_id']]['reserved'],15)

    def test_submit_table_button_ignores_unrelated_saved_queue_records(self):
        dialog,_,commands=self.ui();panel=dialog.manual
        dialog.state={'jobs':[{'id':'unrelated','status':'collected'}]}
        panel.update_collect_controls()
        self.assertTrue(panel.collect_discard.isHidden());self.assertTrue(panel.collect_only.isHidden())
        self.assertEqual(panel.collect_submit.text(),'提交表格任务');self.assertFalse(panel.collect_submit.isEnabled())

    def test_explicit_final_collection_does_not_generate_even_if_global_mode_is_off(self):
        service=self.service();service.paused=False;self.client(service)
        service.execute({'action':'enqueue','source':'batch','request_id':'final-held','collect_only':True,
                         'start_after_save':True,'items':[{**self.args,'prompt':'held final','duration':10,'budget':0}]})
        service._tick();self.assertEqual(service.store.rows()[0]['status'],'collected');self.assertFalse(service.pending)

    def test_read_only_submission_lookup_never_adds_or_replays_work(self):
        service=self.collecting();self.add(service,1);receipts=[];failures=[]
        service.submitted.connect(receipts.append);service.submission_failed.connect(failures.append)
        service.execute({'action':'submission_lookup','source':'batch','request_id':'collect-1'})
        self.assertEqual(receipts[-1]['ids'],[service.store.rows()[0]['id']])
        service.execute({'action':'submission_lookup','source':'batch','request_id':'missing'})
        self.assertEqual(failures[-1]['code'],'FLOW_SUBMISSION_NOT_FOUND')
        self.assertEqual(len(service.store.rows()),1);self.assertFalse(service.pending)

    def test_expanded_jobs_have_exact_table_row_receipts_on_save_and_lookup(self):
        service=self.collecting();receipts=[];service.submitted.connect(receipts.append)
        service.execute({'action':'enqueue','source':'batch','request_id':'row-receipts','collect_only':True,
                         'compose_rows':['a'*32,'b'*32],
                         'items':[{**self.args,'prompt':'first','count':2},{**self.args,'prompt':'second','count':1}]})
        rows=service.store.rows();expected={'a'*32:[rows[0]['id'],rows[1]['id']],'b'*32:[rows[2]['id']]}
        self.assertEqual(receipts[-1]['row_receipts'],expected)
        service.execute({'action':'submission_lookup','source':'batch','request_id':'row-receipts'})
        self.assertEqual(receipts[-1]['row_receipts'],expected);self.assertEqual(len(service.store.rows()),3)


if __name__=='__main__':unittest.main()

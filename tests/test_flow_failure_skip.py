"""Failed downloads/temporary browsers cannot strand paid work or the FIFO."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
import test_flow_production as production
import test_flow_high_credit as high_fixtures
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner
from app_plugins.builtin.flow_assistant.high_credit import allowed, note_normal_spend, record
from app_plugins.builtin.flow_assistant.environment_recovery import watch as repair
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.store import QueueStore, fingerprint


class FailureSkipTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    bound_row = production.ProductionTests.bound_row

    def stopped(self, service, *, ready=False):
        row, client = self.bound_row(service)
        client.update(page_ready=True, batch_running=False, page_at=time.time(),
            saved_project=fixtures.PROJECT, project=fixtures.PROJECT, credits_local=True)
        service.preferences.setdefault('profile_projects', {})[client['profile']] = fixtures.PROJECT
        remote = copy.deepcopy(row['remote'])
        remote['cloud_finished'] = ready
        if not ready:
            remote['bindings'][0].update(readyIds=[], urls={})
        service.store.update(row['id'], 'download_failed', remote=remote)
        row = service.store.get(row['id'])
        service._save_transfer(row, 'download', {'count':0, 'probes':10, 'stopped':True,
            'stop_code':'binding_probe_limit', 'next_at':0, 'last_error':'原视频结果核对已达上限'})
        return service.store.get(row['id']), client

    def test_existing_exhausted_records_skip_without_claiming_refund_or_cloud_success(self):
        service = self.service(); row, client = self.stopped(service)
        service.store.reserve(row['id'], row['attempt'], 12, 'test')
        service.watch_transfer_retries()
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'download_parked')
        self.assertFalse(current['remote']['cloud_finished'])
        self.assertEqual(current['remote']['bindings'], row['remote']['bindings'])
        self.assertEqual((current['attempt'],current['browser']), (row['attempt'],client['id']))
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],12)
        self.assertEqual(QueueStore(self.namespace).get(row['id'])['status'],'download_parked')
        before = current['updated']; service.watch_transfer_retries()
        self.assertEqual(service.store.get(row['id'])['updated'],before)

    def test_skipped_old_image_does_not_hold_four_ready_accounts_or_reorder_fifo(self):
        service = self.service(); row, client = self.stopped(service)
        others = [self.browser(service, 'Spare '+str(n)) for n in range(3)]
        for browser in [client]+others: remember_owner(service,row,browser)
        other_image = self.image.with_name('different.png')
        other_image.write_bytes(self.image.read_bytes()+b'different image identity')
        jobs = self.jobs(count=4,duration=8,budget=0)
        for job in jobs:
            job['image'] = str(other_image); job['image_identity'] = fingerprint(other_image)
        service.store.enqueue(jobs); service.preferences['concurrency']=4
        service.paused=False; launches=[]; service.launch_requested.connect(launches.append)
        for _ in range(5): service._tick()
        rows = service.store.rows()
        self.assertEqual(rows[0]['status'],'download_parked')
        self.assertEqual([r['status'] for r in rows[1:]],['preparing']*4)
        self.assertEqual(len({r['browser'] for r in rows[1:]}),4)
        self.assertFalse(launches)
        self.assertFalse(service.safety_paused)

    def test_late_snapshots_interruption_and_unverified_download_cannot_reclaim_a_slot(self):
        service = self.service(); row, client = self.stopped(service)
        service.watch_transfer_retries(); row=service.store.get(row['id'])
        binding=copy.deepcopy(row['remote']['bindings'][0]); binding.update(readyIds=['media'], urls={'media':'https://flow-content.google/video/media'})
        service._snapshot(client, {'url':fixtures.PROJECT, 'batch_id':row['attempt'], 'running':False,
            'tasks':[{'taskId':'task','download_path':service.download_name(row),'mediaBindings':[binding]}]})
        self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'],[binding])
        for media in ('media','foreign'):
            service._download(client, {'event_id':'interrupt-'+media, 'batch_id':row['attempt'], 'data':{
                'state':'interrupted','taskId':'task','submissionId':'submission','mediaId':media,
                'download_path':service.download_name(row),'error':'NETWORK_FAILED'}})
            self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_manual_recovery_of_skipped_result_only_downloads_the_saved_original(self):
        service=self.service(); row,client=self.stopped(service,ready=True)
        service.watch_transfer_retries(); service.retry_download(service.store.get(row['id']))
        self.assertEqual(service.store.get(row['id'])['status'],'downloading')
        self.assertTrue(any(p['action']=='retry_download' for p in service.pending.values()))
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_release_local_wait_is_exact_once_and_does_not_clear_a_newer_runtime(self):
        service=self.service(); row,client=self.stopped(service)
        client.update(batch_running=True, active_batch='b'*32, capabilities=['skip_download_v1'])
        service.watch_transfer_retries()
        self.assertFalse(any(p['action']=='skip_download' for p in service.pending.values()))
        client['active_batch']=row['attempt']; service.watch_transfer_retries(); service.watch_transfer_retries()
        requests=[p for p in service.pending.values() if p['action']=='skip_download']
        self.assertEqual(len(requests),1); self.assertIsNone(requests[0]['job'])
        self.assertTrue(client['batch_running'],'only the page idle receipt may release runtime state')

    def test_busy_page_response_may_retry_local_release_but_has_a_finite_limit(self):
        service=self.service(); row,client=self.stopped(service)
        client.update(batch_running=True,active_batch=row['attempt'],capabilities=['skip_download_v1'])
        service.watch_transfer_retries()
        for n in range(3):
            key=next(k for k,p in service.pending.items() if p['action']=='skip_download')
            service._response(client,{'request_id':key,'success':False,'code':'FLOW_BATCH_BUSY'})
            when=client['skip_download_wait']['next_at']+.1
            with patch('time.time',return_value=when): service.watch_transfer_retries()
        self.assertFalse(any(p['action']=='skip_download' for p in service.pending.values()))
        self.assertEqual(client['skip_download_wait']['count'],3)
        self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        self.assertFalse(service.safety_paused)

    def preparation_fault(self, service):
        client=self.browser(service); client.update(capabilities=['runtime_controls_v1'],page_instance='old')
        service.store.enqueue(self.jobs(duration=8,budget=0)); row=service.store.rows()[0]
        service.quarantine(client,row,'无法确认页面已选中的模型',recovery_code='FLOW_SETTINGS_MISMATCH')
        service.paused=False
        return service.store.get(row['id']),client

    def test_typed_unpaid_failure_refreshes_and_reenters_full_preflight_after_new_page(self):
        service=self.service(); row,client=self.preparation_fault(service)
        key='profile:'+client['profile']; info=service.preferences['environment_recoveries'][key]
        with patch('time.time',return_value=info['next_at']+.1): self.assertTrue(repair(service))
        self.assertEqual(browser_status(client,service.preferences)['state'],'自动恢复中')
        request=next(p for p in service.pending.values() if p['action']=='repair_project')
        self.assertIsNone(request['job']); self.assertTrue(service.preferences['browser_blocks'])
        service.pending.clear(); client.update(page_ready=True,batch_running=False)
        self.assertFalse(repair(service),'old page readiness cannot clear the error')
        client['page_instance']='new'; self.assertTrue(repair(service))
        self.assertFalse(service.preferences['browser_blocks'])
        service._tick()
        self.assertEqual(service.store.get(row['id'])['status'],'preparing')
        self.assertTrue(any(p['action']=='prepare' for p in service.pending.values()))
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_recovery_limit_survives_restart_and_manual_resume_explicitly_resets_it(self):
        service=self.service(); row,client=self.preparation_fault(service)
        key='profile:'+client['profile']; entries=service.preferences['environment_recoveries']
        entries[key].update(count=3,next_at=0,state='refreshing',page_instance='old')
        service.commit_preferences({**service.preferences,'environment_recoveries':entries})
        self.assertTrue(repair(service)); self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'stopped')
        self.assertFalse(repair(service)); self.assertFalse(service.pending)
        self.assertEqual(QueueStore(self.namespace).settings()['environment_recoveries'][key]['count'],3)
        service.resume_browser({'keys':[key]})
        self.assertNotIn(key,service.preferences['environment_recoveries'])

    def test_paid_unknown_manual_blocks_and_pause_never_auto_refresh(self):
        for mode in ('paid','unknown','manual','paused','security'):
            with self.subTest(mode=mode):
                self.setUp()
                service=self.service(); row,client=self.preparation_fault(service)
                key='profile:'+client['profile']; service.preferences['environment_recoveries'][key]['next_at']=0
                if mode in {'paid','unknown'}:
                    service.store.update(row['id'],'generating' if mode=='paid' else 'uncertain',
                        browser=client['id'],attempt='a'*32,remote={'account':client['account'],'profile':client['profile']})
                    service.store.enqueue(self.jobs(duration=10,budget=0))
                elif mode=='manual': service.preferences['browser_blocks'][key]='手动暂停'
                elif mode=='paused': service.paused=True
                else: service.safety_paused=True
                self.assertFalse(repair(service)); self.assertFalse(service.pending)

    def test_existing_audited_pre_submit_block_is_admitted_for_bounded_automatic_repair(self):
        service=self.service(); row,client=self.preparation_fault(service)
        key='profile:'+client['profile']
        service.preferences.pop('environment_recoveries')
        service.preferences['browser_blocks'][key]='页面未确认 Omni 参数，未提交生成。'
        self.assertTrue(repair(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'waiting')
        self.assertFalse(service.pending)

    def test_shared_f5_limit_marks_the_account_skipped_instead_of_waiting_forever(self):
        service=self.service(); row,client=self.preparation_fault(service)
        key='profile:'+client['profile']
        service.preferences['environment_recoveries'][key]['next_at']=0
        client['f5_recovery']={'count':3,'next_at':0}
        self.assertTrue(repair(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'stopped')
        self.assertEqual(browser_status(client,service.preferences)['state'],'恢复失败／已跳过')
        self.assertFalse(service.pending)


class HighCreditFixTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    queue = high_fixtures.HighCreditTests.queue
    answer = high_fixtures.HighCreditTests.answer
    display = high_fixtures.HighCreditTests.display
    def test_yes_is_not_revoked_by_spend_cycles_or_small_account_updates(self):
        service=self.service(); rich=self.browser(service,points=417); self.queue(service)
        service._tick(); self.answer(service,True)
        small=self.browser(service,'Small',points=50); note_normal_spend(service,small)
        service._tick(); self.assertTrue(allowed(rich,service.preferences))
        self.assertEqual(record(rich,service.preferences)['permission'],'allow')
        self.assertIsNone(service.high_credit_pending)

    def test_timely_gui_click_is_preserved_when_worker_delivery_is_delayed(self):
        service=self.service(); rich=self.browser(service,points=417); self.queue(service)
        service._tick(); self.display(service); pending=service.high_credit_pending
        with patch('time.time',return_value=pending['expires_at']+.5):
            service._tick()
            self.answer(service,True,decided_at=pending['expires_at']-.1)
        self.assertTrue(allowed(rich,service.preferences))

    def test_unknown_unconnected_or_slow_ordinary_accounts_are_not_treated_as_spent(self):
        for mode in ('offline_attempted','online_slow'):
            with self.subTest(mode=mode):
                self.setUp()
                service=self.service(); rich=self.browser(service,points=417)
                service.preferences.update(profiles=['Ordinary'],profile_projects={'Ordinary':fixtures.PROJECT})
                if mode=='offline_attempted':
                    service.launched.add('Ordinary'); service.launch_attempts.add('Ordinary')
                else:
                    ordinary=self.browser(service,'Ordinary'); ordinary.update(page_ready=False,credits=None,
                        credits_local=False,connected=time.time()-1000)
                self.queue(service); service._tick()
                self.assertIsNone(service.high_credit_pending)
                self.assertFalse(allowed(rich,service.preferences))

    def test_legacy_explicit_yes_is_preserved_and_timeout_after_cycle_change_does_not_reprompt(self):
        from app_plugins.builtin.flow_assistant.high_credit import new_session_entries
        migrated=new_session_entries({'high_credit_accounts':{'rich@example.test':{
            'decision':'standby','decision_source':'user_approved'}}})
        self.assertEqual(migrated['rich@example.test']['permission'],'allow')
        service=self.service(); rich=self.browser(service,points=417); self.queue(service)
        service._tick(); self.display(service); pending=service.high_credit_pending
        service.preferences['normal_credit_spends']=1
        self.answer(service,False,source='timeout'); service._tick()
        self.assertIsNone(service.high_credit_pending)
        self.assertFalse(allowed(rich,service.preferences))
        self.assertEqual(record(rich,service.preferences)['asked_cycle'],1)


if __name__ == '__main__': unittest.main()

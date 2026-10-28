"""Four failed/pending results must not consume all account dispatch capacity."""
import copy
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import admission_profiles, can_start, summary, seen
from app_plugins.builtin.flow_assistant.store import choose_client, QueueStore
from app_plugins.builtin.flow_assistant.credit_policy import balance_ready
from app_plugins.builtin.flow_assistant.failure_release import watch, PRE_SUBMIT_ERRORS
from app_plugins.builtin.flow_assistant.connection_status import browser_status


class FailureCapacityTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    observe=fixtures.CreditCooldownTests.observe

    def held(self, service, count=4, status='uncertain', error='原提交结果暂未确认'):
        service.store.enqueue(self.jobs(count=count,duration=8,budget=0))
        rows=service.store.rows()[-count:]; clients=[]
        for index,row in enumerate(rows):
            client=self.browser(service,'Original '+str(index)); clients.append(client)
            service.store.update(row['id'],status,attempt=f'{index+1:032x}',browser=client['id'],
                remote={'profile':client['profile'],'account':client['account'],'cost':12},error=error)
            service.store.reserve(row['id'],f'{index+1:032x}',12,'duration_rule')
        return [service.store.get(row['id']) for row in rows],clients

    def test_four_unknown_results_leave_four_lanes_for_other_accounts(self):
        service=self.service(); held,originals=self.held(service)
        profiles=['Profile 83','Profile 84','Profile 85','Profile 86','Profile 87']
        service.preferences.update(concurrency=4,profiles=[c['profile'] for c in originals]+profiles,
            profile_projects={p:fixtures.PROJECT for p in [c['profile'] for c in originals]+profiles})
        service.store.enqueue(self.jobs(count=7,duration=8,budget=0)); service.paused=False
        self.assertEqual(admission_profiles(service),set())
        self.assertTrue(can_start(service,'Profile 83'))
        launches=[];service.launch_requested.connect(launches.append)
        service.fill_dispatch_slots()
        self.assertEqual(launches,profiles[:4])
        self.assertEqual(len(admission_profiles(service)),4)
        service.fill_dispatch_slots();self.assertEqual(len(launches),4)
        for profile in profiles[:4]:
            client=self.browser(service,profile);seen(service,client)
        service.fill_dispatch_slots()
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),4)
        self.assertEqual([service.store.get(r['id']) for r in held],held)
        self.assertEqual(sum(b['reserved'] for b in service.store.batches().values()),48)
        self.assertFalse(any(p['action'] in {'close_exhausted','run_tasks'} for p in service.pending.values()))

    def test_unknown_original_account_is_not_reused_through_an_alias(self):
        service=self.service(); held,clients=self.held(service)
        alias=self.browser(service,'Alias');alias['account']=clients[0]['account'].upper()
        spare=self.browser(service,'New Account')
        service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[-1]
        self.assertIs(choose_client([*clients,alias,spare],service.store.rows(),4,row['payload'],service.preferences,12),spare)

    def test_download_failures_keep_video_bindings_without_occupying_new_account_lanes(self):
        service=self.service(); held,clients=self.held(service,status='download_failed')
        for row in held:service.store.update(row['id'],'download_failed',remote={**row['remote'],'bindings':[{'mediaIds':['original-media']}]})
        before=service.store.rows()
        self.assertEqual(admission_profiles(service),set())
        # Task admission and physical window budgets are separate. Existing
        # four windows fill the default ceiling at concurrency=2, even though
        # failed downloads release every generation lane.
        from app_plugins.builtin.flow_assistant.browser_budget import maximum,profiles
        self.assertEqual(len(profiles(service)),maximum(service))
        self.assertFalse(can_start(service,'Another Account'))
        service.preferences['browser_window_limit']=8
        self.assertTrue(can_start(service,'Another Account'))
        self.assertEqual(service.store.rows(),before)
        self.assertEqual(summary(service)['unresolved'],4)
        self.assertIn('不占用其他账号启动名额',summary(service)['message'])

    def test_four_actually_running_tasks_still_prevent_a_fifth_start(self):
        service=self.service();held,clients=self.held(service,status='generating')
        service.preferences['concurrency']=4
        self.assertEqual(len(admission_profiles(service)),4)
        self.assertFalse(can_start(service,'Another Account'))
        service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False
        launches=[];service.launch_requested.connect(launches.append);service.fill_dispatch_slots()
        self.assertFalse(launches)

    def test_proven_settings_failure_releases_original_account_and_unused_local_reservation(self):
        service=self.service();held,clients=self.held(service,error=sorted(PRE_SUBMIT_ERRORS)[0])
        self.assertTrue(watch(service));self.assertFalse(watch(service))
        self.assertTrue(all(r['status']=='failed' for r in service.store.rows()))
        self.assertEqual(sum(b['reserved'] for b in service.store.batches().values()),0)
        self.assertTrue(all(not balance_ready(c,preferences=service.preferences) for c in clients))
        for client in clients:self.observe(service,client,50)
        service.store.enqueue(self.jobs(count=4,duration=8,budget=0));service.preferences['concurrency']=4;service.paused=False
        service.fill_dispatch_slots()
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),4)
        self.assertEqual({r['browser'] for r in service.store.rows() if r['status']=='preparing'},{c['id'] for c in clients})
        self.assertTrue(all(service.store.get(r['id'])['status']=='failed' for r in held))
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_live_failure_event_releases_without_waiting_for_application_restart(self):
        service=self.service();held,clients=self.held(service,count=1,status='generating')
        row=held[0]; client=clients[0]
        service._status(client,{'type':'hosted_task_status','batch_id':row['attempt'],'status':'failed',
            'message':sorted(PRE_SUBMIT_ERRORS)[0]})
        current=service.store.get(row['id'])
        self.assertEqual(current['status'],'failed')
        self.assertEqual(current['attempt'],row['attempt'])
        self.assertIn('释放账号',current['error'])
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],0)
        service._status(client,{'type':'hosted_task_status','batch_id':row['attempt'],'status':'failed','message':'late duplicate'})
        self.assertEqual(service.store.get(row['id']),current)

    def test_real_generation_evidence_prevents_unsubmitted_classification_and_credit_release(self):
        for key,value in [('bindings',[{'mediaIds':['paid-video']}]),('upstream_task','task'),('generation_response',{'accepted':True}),('cloud_finished',True)]:
            with self.subTest(key=key):
                self.setUp();service=self.service();held,_=self.held(service,count=1,error=sorted(PRE_SUBMIT_ERRORS)[0])
                row=held[0];service.store.update(row['id'],'uncertain',remote={**row['remote'],key:value},error=row['error'])
                before=service.store.get(row['id']);self.assertFalse(watch(service))
                self.assertEqual(service.store.get(row['id']),before)
                self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],12)

    def test_network_or_generic_failure_is_not_treated_as_proof_of_no_submission(self):
        service=self.service();held,_=self.held(service,count=1,error='提交失败: Network timeout')
        self.assertFalse(watch(service));self.assertEqual(service.store.get(held[0]['id']),held[0])
        self.assertTrue(can_start(service,'Other Account'))

    def test_release_and_balance_recheck_survive_restart_without_replaying_failed_jobs(self):
        service=self.service();held,clients=self.held(service,count=1,error=sorted(PRE_SUBMIT_ERRORS)[0])
        self.assertTrue(watch(service));saved=QueueStore(self.namespace)
        self.assertEqual(saved.get(held[0]['id'])['status'],'failed')
        self.assertGreater(saved.settings()['credit_balances'][clients[0]['account']]['recheck_after'],0)
        self.assertEqual(saved.batches()[held[0]['payload']['batch_id']]['reserved'],0)


if __name__=='__main__':unittest.main()

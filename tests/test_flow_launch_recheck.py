"""A stale pre-launch probe is not a permanent account exclusion."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import recover_legacy_probe_failures, request_start, launch_result, seen
from app_plugins.builtin.flow_assistant.timing import value


class LaunchRecheckTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs

    def configured(self):
        service=self.service(); service.preferences.update(concurrency=1,profiles=['Unknown'],
            profile_projects={'Unknown':fixtures.PROJECT})
        service.store.enqueue(self.jobs(duration=10,budget=0));service.paused=False
        return service

    def test_previous_v2_probe_failures_reenter_preflight_once_without_changing_accounts_or_jobs(self):
        prefs={'extension_probe_revision':2,'browser_launch_failures':{
            'Unknown':{'kind':'extension_probe_v2','message':'old inconclusive probe'},
            'Broken':{'kind':'launch','message':'executable missing'}},
            'credit_cooldowns':{'hold':{'until':time.time()+21600}},'account_blocks':{'blocked':{'kind':'manual'}}}
        before=copy.deepcopy(prefs);after=recover_legacy_probe_failures(prefs)
        self.assertNotIn('Unknown',after['browser_launch_failures'])
        self.assertIn('Broken',after['browser_launch_failures'])
        self.assertEqual(after['credit_cooldowns'],before['credit_cooldowns'])
        self.assertEqual(after['account_blocks'],before['account_blocks']);self.assertEqual(prefs,before)

    def test_transient_unknown_probe_retries_only_after_wait_without_opening_extra_windows(self):
        service=self.configured();launches=[];service.launch_requested.connect(launches.append)
        service._tick();self.assertEqual(launches,['Unknown'])
        launch_result(service,{'profile':'Unknown','opened':False,'kind':'extension_probe_v3',
            'retryable':True,'error':'temporary unreadable preferences'})
        for _ in range(4):service._tick()
        self.assertEqual(launches,['Unknown'])
        with patch('time.time',return_value=time.time()+61): service._tick()
        self.assertEqual(launches,['Unknown','Unknown'])
        self.assertTrue(all(row['status']=='queued' for row in service.store.rows()))

    def test_probe_retry_limit_persists_and_explicit_resume_releases_only_selected_profile(self):
        service=self.configured();now=time.time();launches=[];service.launch_requested.connect(launches.append)
        for index in range(3):
            with patch('time.time',return_value=now+index*61):
                service._tick();launch_result(service,{'profile':'Unknown','opened':False,'kind':'extension_probe_v3',
                    'retryable':True,'error':'temporary unreadable preferences'})
        with patch('time.time',return_value=now+9999):service._tick()
        self.assertEqual(len(launches),3)
        self.assertEqual(service.store.settings()['browser_probe_retries']['Unknown']['count'],3)
        service.execute({'action':'resume_browser','keys':['profile:Unknown']})
        self.assertNotIn('Unknown',service.preferences.get('browser_probe_retries',{}))

    def test_definite_disabled_probe_and_real_launch_failure_are_not_automatically_retried(self):
        for kind in ('extension_probe_v3','launch'):
            self.setUp();service=self.configured();launches=[];service.launch_requested.connect(launches.append)
            service._tick();launch_result(service,{'profile':'Unknown','opened':False,'kind':kind,
                'retryable':False,'error':'explicit disabled / process failure'})
            with patch('time.time',return_value=time.time()+9999):service._tick()
            self.assertEqual(launches,['Unknown'])

    def test_successful_handshake_clears_probe_retries(self):
        service=self.configured();service.preferences['browser_probe_retries']={'Unknown':{'count':2,'next_at':1}}
        client=self.browser(service,'Unknown');seen(service,client)
        self.assertNotIn('Unknown',service.preferences.get('browser_probe_retries',{}))

    def test_pending_small_account_recheck_does_not_prompt_for_high_credit_early(self):
        service=self.configured();service._tick()
        launch_result(service,{'profile':'Unknown','opened':False,'kind':'extension_probe_v3',
            'retryable':True,'error':'temporary unreadable preferences'})
        self.browser(service,'Rich',points=1038)
        service._tick();self.assertIsNone(service.high_credit_pending)

    def test_old_failure_recheck_keeps_twenty_seven_candidates_within_four_launch_slots(self):
        service=self.configured();profiles=['Unknown '+str(i) for i in range(27)]
        service.commit_preferences({**service.preferences,'concurrency':4,'profiles':profiles,
            'profile_projects':{p:fixtures.PROJECT for p in profiles},'extension_probe_revision':2,
            'browser_launch_failures':{p:{'kind':'extension_probe_v2','message':'old probe'} for p in profiles}})
        service.execute({'action':'stop'});restarted=self.service();restarted.paused=False
        launches=[];restarted.launch_requested.connect(launches.append)
        for _ in range(30):restarted._tick()
        self.assertEqual(len(launches),4);self.assertEqual(len(set(launches)),4)
        self.assertFalse(any(p['action']=='run_tasks' for p in restarted.pending.values()))

    def test_probe_limit_survives_worker_restart(self):
        service=self.configured();service._tick()
        service.preferences['browser_probe_retries']={'Unknown':{'count':2,'next_at':1}}
        launch_result(service,{'profile':'Unknown','opened':False,'kind':'extension_probe_v3',
            'retryable':True,'error':'temporary unreadable preferences'})
        service.execute({'action':'stop'});restarted=self.service();restarted.paused=False
        launches=[];restarted.launch_requested.connect(launches.append)
        with patch('time.time',return_value=time.time()+99999):restarted._tick()
        self.assertFalse(launches);self.assertEqual(restarted.preferences['browser_probe_retries']['Unknown']['count'],3)


if __name__=='__main__':unittest.main()

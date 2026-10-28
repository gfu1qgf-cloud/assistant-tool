"""Exit must reach every admission gate, using real and synthetic configs."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import can_start,admission_profiles
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle
from app_plugins.builtin.flow_assistant.project_startup import request_open
from app_plugins.builtin.flow_assistant.browser_reuse import abandon
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class StallFollowthroughTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs

    def queued(self,concurrency=4):
        service=self.service();service.timer.stop();service.paused=False
        service.preferences['concurrency']=concurrency
        service.store.enqueue(self.jobs(duration=8,count=10,budget=0))
        return service

    def test_stopped_clients_release_lanes_but_eight_windows_still_bound_new_starts(self):
        service=self.queued();old=[]
        for i in range(8):
            client=self.browser(service,'Old '+str(i))
            client.update(page_ready=False,connected=time.time()-3600,
                reuse_recovery={'state':'stopped','since':time.time()-3600,'credit_count':3,'reason':'timed out'},
                connection_recovery={'count':1,'state':'checking','next_at':time.time()-3600})
            service.connection_recoveries[client['profile']]=client['connection_recovery'];old.append(client)
        spares=['Spare '+str(i) for i in range(4)]
        service.preferences['profiles']=[c['profile'] for c in old]+spares
        service.preferences['profile_projects'].update({p:fixtures.PROJECT for p in spares})
        launches=[];service.launch_requested.connect(launches.append)
        service.fill_dispatch_slots()
        self.assertEqual(launches,[],'safe close must free a physical window before more backups open')
        self.assertFalse(admission_profiles(service))

    def test_expired_launch_records_release_capacity_without_reopening_same_profiles(self):
        service=self.queued();profiles=['Old '+str(i) for i in range(4)]
        service.preferences['browser_slots']={p:{'state':'opening','at':time.time()-1000} for p in profiles}
        before=copy.deepcopy(service.preferences)
        self.assertFalse(admission_profiles(service))
        self.assertTrue(can_start(service,'Other'))
        self.assertFalse(can_start(service,profiles[0]),'unknown old windows are still deduplicated')
        self.assertEqual(service.preferences,before)

    def test_repair_send_rejection_does_not_consume_an_attempt(self):
        service=self.queued();client=self.browser(service,'Repair')
        service.preferences['browser_options']={'ui_mutex':True}
        client.update(page_ready=False,connected=time.time()-3600,capabilities=[])
        self.assertFalse(repair_idle(service,client))
        self.assertEqual(service.connection_recoveries.get('Repair',{}).get('count',0),0)
        self.assertIn('互斥',client['connection_recovery']['message'])
        self.assertFalse(service.pending)

    def test_repair_busy_is_not_evidence_of_a_running_generation_or_failed_repair(self):
        service=self.queued();client=self.browser(service,'Repair')
        client.update(page_ready=False,connected=time.time()-3600,batch_running=False)
        self.assertTrue(repair_idle(service,client))
        key=next(k for k,p in service.pending.items() if p['action']=='repair_project')
        service._response(client,{'request_id':key,'success':False,'code':'FLOW_PAGE_REPAIR_BUSY',
            'error':'原项目正在加载或浏览器仍有下载，未执行 F5。'})
        self.assertFalse(client['batch_running'])
        self.assertEqual(service.connection_recoveries['Repair']['count'],0)
        self.assertIn('未执行',client['connection_recovery']['message'])

    def test_exhausted_repair_reports_last_actual_failure_instead_of_guessing_login(self):
        service=self.queued();client=self.browser(service,'Repair')
        client.update(page_ready=False,connected=time.time()-3600)
        service.connection_recoveries['Repair']={'count':3,'state':'failed','last_error':'无法定位项目输入框'}
        self.assertFalse(repair_idle(service,client))
        self.assertIn('无法定位项目输入框',client['connection_recovery']['message'])

    def test_stopped_account_does_not_start_another_project_recovery(self):
        service=self.queued();client=self.browser(service,'Repair')
        client.update(page_ready=False,project_missing=True)
        abandon(service,client,'prepare timeout')
        requests=[];service.project_open_requested.connect(requests.append)
        self.assertFalse(request_open(service,client));self.assertFalse(requests)

    def replay(self,duration):
        path=os.environ.get('LZX_FLOW_REPLAY_PREFERENCES')
        if not path:self.skipTest('optional replay of sanitized user preferences')
        data=json.loads(Path(path).read_text('utf-8'));prefs=copy.deepcopy(data['preferences']);now=data['at']
        service=self.service();service.timer.stop();service.paused=False;service.preferences=prefs
        service.store.enqueue(self.jobs(duration=duration,count=14,budget=0))
        from app_plugins.builtin.flow_assistant.image_affinity import cached_profile
        from app_plugins.builtin.flow_assistant.policy import block_for
        from app_plugins.builtin.flow_assistant.high_credit import allowed
        old=[]
        with patch('time.time',return_value=now):
            for profile,entry in prefs['browser_slots'].items():
                if entry.get('state')=='opening':entry['at']=now-1000
                if entry.get('state')!='online':continue
                cached=cached_profile(profile,prefs)
                if block_for(cached,prefs) or not allowed(cached,prefs):continue
                client=self.client(service,profile,50)
                client.update({k:v for k,v in cached.items() if k!='id'})
                client.update(project=cached['saved_project'],page_ready=False,page_loaded=False,page_at=now,
                    connected=now-3600,batch_running=False,native_busy=False,extension_version=EXTENSION_VERSION,
                    capabilities=['ui_mutex_v1','runtime_controls_v1'],
                    reuse_recovery={'state':'stopped','since':now-1000,'credit_count':3,'reason':'prepare timed out'},
                    connection_recovery={'state':'checking','count':1,'next_at':now-1000})
                service.connection_recoveries[profile]=client['connection_recovery'];old.append(client)
            self.assertGreaterEqual(len(old),8)
            launches=[];service.launch_requested.connect(launches.append)
            service.fill_dispatch_slots()
            self.assertTrue(launches,'real saved recovery/startup records cannot prevent all backup starts')
            first=service.store.rows()[0]
            profile=launches[0];cached=cached_profile(profile,prefs)
            client=self.client(service,profile,50)
            client.update(project=cached['saved_project'],saved_project=cached['saved_project'],page_loaded=True,
                page_ready=True,page_at=now,connected=now,batch_running=False,native_busy=False,
                extension_version=EXTENSION_VERSION,capabilities=['ui_mutex_v1','runtime_controls_v1'])
            service.request(client,'get_points')
            key=next(k for k,p in service.pending.items() if p['client']==client['id'] and p['action']=='get_points')
            service._response(client,{'request_id':key,'success':True,
                'data':{'email':cached.get('account') or 'new@example.test','remaining_points':50}})
            service.fill_dispatch_slots()
            self.assertEqual(service.store.get(first['id'])['status'],'preparing')
            self.assertEqual(service.store.get(first['id'])['browser'],client['id'])

    def test_real_preferences_replay_with_eight_second_fifo_head(self):self.replay(8)
    def test_real_preferences_replay_with_ten_second_fifo_head(self):self.replay(10)


if __name__=='__main__':unittest.main()

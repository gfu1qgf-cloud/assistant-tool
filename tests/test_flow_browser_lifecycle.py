"""Automatic closure is idle-only, acknowledged, and cannot loop across profiles."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_flow_runtime_close as fixtures
from app_plugins.builtin.flow_assistant import browser_lifecycle as lifecycle
from app_plugins.builtin.flow_assistant.runtime_close import CAPABILITY as CLOSE_CAPABILITY, PAUSED, response


class BrowserLifecycleTests(unittest.TestCase):
    setUpClass=fixtures.RuntimeCloseTests.__dict__['setUpClass']
    setUp=fixtures.RuntimeCloseTests.setUp
    service=fixtures.RuntimeCloseTests.service
    browser=fixtures.RuntimeCloseTests.browser
    client=fixtures.RuntimeCloseTests.client
    jobs=fixtures.RuntimeCloseTests.jobs

    def ready(self,service,profile='Automatic',points=50):
        client=self.browser(service,profile,points)
        client['capabilities']=[CLOSE_CAPABILITY,lifecycle.CAPABILITY,'window_inventory_v1']
        return client

    def messages(self,client):
        return [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]

    def confirm(self,service,client):
        key=next(key for key,pending in service.pending.items() if pending['action']=='close_browser')
        service._response(client,{'request_id':key,'success':True,'data':{'closed':True}})

    def test_below_ten_closes_idle_once_and_preserves_credit_cooldown(self):
        service=self.service();client=self.ready(service,points=5)
        before=copy.deepcopy(service.preferences.get('credit_cooldowns',{}))
        self.assertTrue(lifecycle.watch(service));self.assertFalse(lifecycle.watch(service))
        command=self.messages(client)[-1]
        self.assertEqual(command['params']['reason'],'auto_low_credit');self.assertEqual(command['params']['balance'],5)
        self.confirm(service,client)
        entry=service.preferences['browser_lifecycle'][client['profile']]
        self.assertEqual(entry['state'],'cooldown');self.assertGreater(entry['reopen_at'],time.time()+1000)
        self.assertEqual(service.preferences.get('credit_cooldowns',{}),before)
        self.assertNotEqual(service.preferences['browser_blocks']['profile:'+client['profile']],PAUSED)

    def test_unknown_or_ten_points_and_old_bridges_are_not_automatically_closed(self):
        service=self.service()
        for points in (None,10,50):
            client=self.ready(service,str(points),points)
            lifecycle.watch(service);self.assertFalse(self.messages(client))
        old=self.ready(service,'Old',5);old['capabilities']=['manual_browser_close_v1',lifecycle.CAPABILITY]
        self.assertFalse(lifecycle.watch(service));self.assertFalse(self.messages(old))
        unconfirmed=self.ready(service,'Unconfirmed',5);unconfirmed['identity_unconfirmed']=True
        self.assertFalse(lifecycle.watch(service));self.assertFalse(self.messages(unconfirmed))

    def test_inflight_and_unconfirmed_results_protect_original_browser(self):
        service=self.service();client=self.ready(service,points=1);service.store.enqueue(self.jobs(budget=0))
        row=service.store.rows()[0]
        for status in ('preparing','uploading','submitting','generating','downloading','uncertain','download_failed','archiving'):
            service.store.update(row['id'],status,browser=client['id'],remote={'profile':client['profile'],'account':client['account']})
            self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        self.assertNotIn('browser_lifecycle',service.preferences)

    def test_transient_failure_reenters_only_after_native_close_and_cooldown(self):
        service=self.service();client=self.ready(service);profile=client['profile']
        service.preferences.update(profiles=[profile],browser_blocks={'profile:'+profile:'No current window'})
        service.launched.add(profile);service.launch_attempts.add(profile)
        service.store.enqueue(self.jobs(budget=0));rows=copy.deepcopy(service.store.rows())
        self.assertTrue(lifecycle.watch(service))
        self.assertEqual(service.preferences['browser_lifecycle'][profile]['state'],'closing')
        with patch('time.time',return_value=time.time()+500):lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle'][profile]['state'],'closing','elapsed time cannot prove a window exited')
        self.confirm(service,client);until=service.preferences['browser_lifecycle'][profile]['reopen_at']
        service._disconnected(client['socket'])
        with patch('time.time',return_value=until-1):lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_blocks']['profile:'+profile],lifecycle.AUTO_CLOSING)
        with patch('time.time',return_value=until+1):lifecycle.watch(service)
        self.assertNotIn('profile:'+profile,service.preferences['browser_blocks'])
        self.assertNotIn(profile,service.launch_attempts);self.assertEqual(service.store.rows(),rows)
        next_client=self.ready(service,profile);service.preferences['browser_blocks']['profile:'+profile]='No current window'
        self.assertTrue(lifecycle.watch(service),'a recurring temporary fault may recycle only after confirmed closure and cooldown')
        self.confirm(service,next_client)
        entry=service.preferences['browser_lifecycle'][profile]
        self.assertEqual(entry['count'],2)
        self.assertGreaterEqual(entry['reopen_at']-time.time(),179)

    def test_manual_pause_login_security_and_other_profile_blocks_are_preserved(self):
        for reason in (PAUSED,'需要登录','网站提示安全限制，需要验证'):
            self.setUp();service=self.service();client=self.ready(service)
            service.preferences['browser_blocks']={'profile:'+client['profile']:reason,'profile:Other':'保留'}
            self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
            self.assertEqual(service.preferences['browser_blocks']['profile:Other'],'保留')

    def test_explicit_close_failure_restores_only_its_original_error_and_limits_retries(self):
        service=self.service();client=self.ready(service);key='profile:'+client['profile']
        service.preferences['browser_blocks']={key:'No current window','profile:Other':'保留'}
        self.assertTrue(lifecycle.watch(service))
        response(service,client,{'success':False,'error':'native close failed'})
        self.assertEqual(service.preferences['browser_blocks'],{key:'No current window','profile:Other':'保留'})
        self.assertEqual(service.preferences['browser_lifecycle'][client['profile']]['state'],'close_failed')
        self.assertFalse(lifecycle.watch(service))

    def test_both_options_can_disable_automatic_actions(self):
        service=self.service();client=self.ready(service,points=1)
        service.preferences['browser_options']={'auto_close_low_credit':False,'auto_reopen_page_faults':False}
        service.preferences['browser_blocks']={'profile:'+client['profile']:'No current window'}
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)

    def test_website_or_manual_restriction_takes_precedence_over_an_old_temporary_error(self):
        service=self.service();client=self.ready(service)
        service.preferences['browser_blocks']={'profile:'+client['profile']:'No current window',
            'browser:'+client['id']:'需要登录／验证码'}
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        service.preferences['browser_blocks'].pop('browser:'+client['id'])
        client['blocked']='Flow 网站提示安全限制'
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)

    def test_authenticated_zero_window_proof_recovers_a_lost_final_close_receipt(self):
        from app_plugins.builtin.flow_assistant.browser_slots import confirm_absence
        service=self.service();client=self.ready(service);profile=client['profile']
        service.preferences.update(profiles=[profile],browser_blocks={'profile:'+profile:'No current window'},
            browser_slots={profile:{'browser':client['id'],'state':'online'}})
        self.assertTrue(lifecycle.watch(service));service._disconnected(client['socket'])
        proof={'protocol':1,'token':service.pair['token'],'profile':profile,'browser_id':client['id'],
            'project':client['saved_project']}
        self.assertTrue(confirm_absence(service,proof))
        self.assertEqual(service.preferences['browser_lifecycle'][profile]['state'],'cooldown')
        self.assertNotIn(profile,service.preferences['browser_slots'])


if __name__=='__main__':unittest.main()

"""New sessions clear browser faults, retaining explicit choices and paid evidence."""
import copy,time,unittest
from unittest.mock import patch
import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.startup_state import reset_preferences
from app_plugins.builtin.flow_assistant.credit_policy import balance_ready
from app_plugins.builtin.flow_assistant.runtime_close import PAUSED

class StartupStateTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    browser=fixtures.CreditCooldownTests.browser
    observe=fixtures.CreditCooldownTests.observe
    client=fixtures.CreditCooldownTests.client

    def preferences(self):
        return {'profiles':['one','two'],'concurrency':8,'profile_projects':{'one':'https://flow.google.com/project/p'},
            'browser_options':{'ui_mutex':True,'focus_before_action':False},'timing':{'credit_cooldown_seconds':21600},
            'account_blocks':{'user':{'kind':'manual','reason':'disabled by user'},'auto':{'kind':'verification'}},
            'account_block_links':{'profile:one':'user','profile:two':'auto'},
            'browser_blocks':{'profile:one':PAUSED,'profile:two':'No current window'},
            'browser_launch_failures':{'two':{'kind':'extension_probe_v3'}},'browser_probe_retries':{'two':{'count':3}},
            'environment_recoveries':{'profile:two':{'state':'stopped'}},'bridge_recoveries':{'two':{'state':'stopped'}},
            'browser_lifecycle':{'two':{'state':'close_failed'}},'browser_slots':{'two':{'state':'disconnected'}},
            'parameter_blocks':{'profile:two':[{'duration':10}]},
            'credit_cooldowns':{'future':{'at':100,'until':300,'refresh_at':300},'expired':{'at':10,'until':150,'refresh_at':150},'unknown':{'at':100,'until':300}},
            'credit_cooldown_links':{'profile:a':'future','profile:b':'expired','profile:c':'unknown'},
            'exhausted_browser_disconnects':{'a':{'account':'future','refresh_at':300},'b':{'account':'expired','refresh_at':150},'c':{'account':'unknown','refresh_at':0}},
            'credit_balances':{'future':{'balance':5,'observed_at':100},'expired':{'balance':5,'observed_at':100},'healthy':{'balance':50,'observed_at':100}},
            'credit_refreshes':{'future':{'daily_time':'12:00','next_at':300}},
            'account_images':{'healthy':{'image':'same-material'}},'high_credit_accounts':{'big':{'permission':'skip'}}}

    def test_only_explicit_user_blocks_and_not_yet_due_refresh_holds_survive(self):
        prefs=self.preferences();before=copy.deepcopy(prefs);updated=reset_preferences(prefs,200)
        self.assertEqual(prefs,before,'pure transformation does not mutate the live preferences')
        self.assertEqual(set(updated['account_blocks']),{'user'})
        self.assertEqual(updated['account_block_links'],{'profile:one':'user'})
        self.assertEqual(updated['browser_blocks'],{'profile:one':PAUSED})
        self.assertEqual(set(updated['credit_cooldowns']),{'future'})
        self.assertEqual(set(updated['exhausted_browser_disconnects']),{'a'})
        for key in ('browser_launch_failures','browser_probe_retries','environment_recoveries','bridge_recoveries',
                    'browser_lifecycle','browser_slots','parameter_blocks'):self.assertFalse(updated[key],key)
        for key in ('profiles','profile_projects','concurrency','timing','browser_options','account_images','credit_refreshes','high_credit_accounts'):
            self.assertEqual(updated[key],before[key],key)

    def test_expired_and_healthy_cached_balances_need_new_observation(self):
        updated=reset_preferences(self.preferences(),200)
        for account in ('expired','healthy'):
            self.assertEqual(updated['credit_balances'][account]['recheck_after'],200)
            self.assertEqual(updated['credit_balances'][account]['observed_at'],100)
        self.assertNotIn('recheck_after',updated['credit_balances']['future'])

    def test_daily_schedule_can_preserve_old_marker_without_embedded_deadline(self):
        prefs=self.preferences();prefs['exhausted_browser_disconnects']['a']['refresh_at']=0
        prefs['credit_cooldowns']['future'].pop('refresh_at')
        updated=reset_preferences(prefs,200)
        self.assertEqual(updated['exhausted_browser_disconnects']['a']['refresh_at'],300)
        self.assertEqual(updated['credit_cooldowns']['future']['until'],300)
        expired=reset_preferences(prefs,301)
        self.assertFalse(expired['exhausted_browser_disconnects']);self.assertFalse(expired['credit_cooldowns'])

    def test_unknown_old_low_balance_expires_even_when_normal_balance_cache_is_unlimited(self):
        client={'account':'old@example.test','credits':5,'credits_local':True,'credits_at':100}
        self.assertFalse(balance_ready(client,100+3*86400,{'timing':{'credit_validity_seconds':0}}))

    def test_real_service_start_keeps_paid_jobs_and_receipts_and_remains_paused(self):
        service=self.service();service.timer.stop();service.store.enqueue(self.jobs(count=3,budget=0))
        rows=service.store.rows();row=rows[0]
        service.store.update(row['id'],'uncertain',attempt='a'*32,browser='paid-browser',
            remote={'generation_response':{'submissionId':'original'},'bindings':{'media':['saved-media']}})
        with service.store.connection() as db:
            db.execute('INSERT INTO charges VALUES (?,?,?,?,?,?)',('a'*32,row['payload']['batch_id'],row['id'],15,'actual',time.time()))
        prefs={**service.preferences,**self.preferences()};service.store.save_settings(prefs)
        original=copy.deepcopy(service.store.rows())
        with service.store.connection() as db:charges=list(db.execute('SELECT * FROM charges'))
        restarted=self.service();restarted.timer.stop()
        self.assertEqual(restarted.store.rows(),original)
        with restarted.store.connection() as db:self.assertEqual(list(db.execute('SELECT * FROM charges')),charges)
        self.assertEqual(set(restarted.preferences['account_blocks']),{'user'})
        self.assertFalse(restarted.preferences['environment_recoveries']);self.assertFalse(restarted.pending)
        self.assertTrue(restarted.paused);self.assertFalse(restarted.released)

    def test_force_reset_command_keeps_user_marks_and_inflight_upload_lease(self):
        from app_plugins.builtin.flow_assistant import ui_mutex
        from app_plugins.builtin.flow_assistant.account_policy import mark_account,account_hold
        from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
        from app_plugins.builtin.flow_assistant.startup_state import watch
        service=self.service();service.timer.stop()
        clients=[self.browser(service,label) for label in ('Idle','Manual','Future','Uploading')]
        idle,manual,future,busy=clients
        service.preferences['browser_options']={'ui_mutex':True,'focus_before_action':False}
        for client in clients:client['capabilities']=['ui_mutex_v1','runtime_controls_v1']
        service.preferences['credit_refreshes']={future['account']:{'daily_time':'23:59','next_at':time.time()+3600,'timezone':'Europe/Rome'}}
        self.observe(service,future,5)
        mark_account(service,manual,'user marked disabled',manual=True)
        service.store.enqueue(self.jobs(budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'uploading',attempt='a'*32,browser=busy['id'],remote={'profile':busy['profile'],'account':busy['account']})
        service.request(busy,'upload_images',job=row['id'])
        ui_mutex.receive(service,busy,{'type':'ui_mutex_request','token':'1'*32,'url':fixtures.PROJECT,'operation':'upload_images'})
        original=copy.deepcopy(service.store.rows());pending=copy.deepcopy({k:{x:v for x,v in p.items()} for k,p in service.pending.items()})
        owner=ui_mutex.state(service)['owner']['token']
        idle.update(reuse_recovery={'since':0,'state':'stopped'},setup_blocked='temporary error',blocked='No current window',page_ready=True)
        service.preferences['environment_recoveries']={'profile:'+idle['profile']:{'state':'stopped','count':3}}
        service.execute({'action':'reset_browser_errors'})
        self.assertEqual(account_hold(manual,service.preferences)['kind'],'manual')
        self.assertTrue(credit_hold(future,service.preferences))
        self.assertNotIn('setup_blocked',idle);self.assertEqual(idle['blocked'],'')
        self.assertEqual(service.store.rows(),original)
        self.assertEqual(ui_mutex.state(service)['owner']['token'],owner)
        self.assertTrue(all(service.pending.get(key)==value for key,value in pending.items()))
        probes=[p for p in service.pending.values() if p['action']=='get_points']
        self.assertEqual([p['client'] for p in probes],[idle['id']])
        self.assertTrue(busy['browser_recheck_pending']);self.assertTrue(service.paused)
        service.execute({'action':'reset_browser_errors'})
        self.assertEqual(len([p for p in service.pending.values() if p['action']=='get_points']),1,'double click does not duplicate reads')

    def test_reset_button_has_a_direct_worker_command_without_modal_confirmation(self):
        dialog,launcher,commands=fixtures.CreditCooldownTests.ui(self)
        dialog.tabs.setCurrentWidget(dialog.browser_page);commands.clear()
        self.assertTrue(dialog.reset_browser_errors_button.isEnabled())
        dialog.reset_browser_errors_button.click()
        self.assertEqual(commands,[{'action':'reset_browser_errors'}])

if __name__=='__main__':unittest.main()

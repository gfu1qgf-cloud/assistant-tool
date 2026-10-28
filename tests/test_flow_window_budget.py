"""Real coordinator simulations of bounded windows and safe idle recycling."""
import json,time,unittest
from unittest.mock import patch
import test_flow_browser_lifecycle as fixtures
from app_plugins.builtin.flow_assistant import browser_lifecycle as lifecycle,browser_slots as slots,browser_budget as budget
from app_plugins.builtin.flow_assistant.browser_reuse import abandon,before_new_browser
from app_plugins.builtin.flow_assistant.runtime_close import PAUSED,timeout,response


class WindowBudgetTests(fixtures.BrowserLifecycleTests):
    def pool(self,count=16):
        service=self.service();service.timer.stop()
        names=['Window '+str(i) for i in range(count)]
        service.preferences.update(concurrency=4,profiles=names,profile_projects={p:fixtures.fixtures.fixtures.PROJECT for p in names})
        return service,names

    def test_expired_launches_cannot_open_more_than_eight(self):
        service,names=self.pool();start=time.time()
        for round in range(10):
            with patch('time.time',return_value=start+round*100):
                for name in names:slots.request_start(service,name)
        self.assertEqual(len(service.preferences['browser_slots']),8)
        self.assertEqual(budget.maximum(service),8)
        self.assertFalse(slots.can_start(service,names[8]))
        self.assertIn('窗口上限',slots.summary(service)['message'])

    def test_healthy_four_do_not_open_new_accounts_when_existing_can_prepare(self):
        service,names=self.pool();service.store.enqueue(self.jobs(count=12,budget=0));service.paused=False
        for name in names[:4]:self.ready(service,name)
        launches=[];service.launch_requested.connect(launches.append)
        service.fill_dispatch_slots()
        self.assertFalse(launches)

    def test_failed_unpaid_preparation_is_safely_closed_and_reopened_after_cooldown(self):
        service,names=self.pool();client=self.ready(service,names[0]);slots.seen(service,client)
        abandon(service,client,'账号准备读取积分超时')
        self.assertTrue(lifecycle.watch(service));self.assertIn(names[0],budget.profiles(service))
        self.assertEqual(self.messages(client)[-1]['params']['reason'],'auto_page_fault')
        self.confirm(service,client);until=service.preferences['browser_lifecycle'][names[0]]['reopen_at']
        self.assertFalse(service.reuse_recoveries,'confirmed close must retire the failed page recovery epoch')
        service._disconnected(client['socket'])
        with patch('time.time',return_value=until-1):
            lifecycle.watch(service);self.assertFalse(slots.request_start(service,names[0],self.jobs(budget=0)[0]))
        with patch('time.time',return_value=until+1):lifecycle.watch(service)
        self.assertNotIn('profile:'+names[0],service.preferences['browser_blocks'])
        self.assertTrue(slots.request_start(service,names[0]))
        from app_plugins.builtin.flow_assistant.browser_reuse import recovery_state
        replacement=self.ready(service,names[0]);self.assertNotEqual(recovery_state(service,replacement).get('state'),'stopped')

    def test_surplus_idle_closes_one_at_a_time_down_to_four(self):
        service,names=self.pool();clients=[self.ready(service,n) for n in names[:8]]
        for client in clients:client['connected']=time.time()-60;slots.seen(service,client)
        for remaining in range(8,4,-1):
            self.assertTrue(lifecycle.watch(service));self.assertEqual(len(service.pending),1)
            self.assertFalse(lifecycle.watch(service))
            pending=next(iter(service.pending.values()));client=next(c for c in service.clients.values() if c['id']==pending['client'])
            self.assertEqual(client['runtime_close']['automatic_reason'],'idle_capacity')
            self.confirm(service,client);service._disconnected(client['socket'])
            self.assertEqual(len(budget.profiles(service)),remaining-1)
        self.assertFalse(lifecycle.watch(service))

    def test_paid_work_personal_tab_refusal_and_passive_chrome_are_protected(self):
        service,names=self.pool();clients=[self.ready(service,n) for n in names[:8]]
        for client in clients:client['connected']=time.time()-60;slots.seen(service,client)
        personal=clients[-1];personal['credit_observation_only']=True
        service.store.enqueue(self.jobs(count=7,budget=0))
        for row,client in zip(service.store.rows(),clients[:-1]):
            service.store.update(row['id'],'generating',browser=client['id'],attempt='a'*32,remote={'profile':client['profile']})
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        self.assertNotIn(personal['profile'],budget.profiles(service))
        service.store.update(service.store.rows()[0]['id'],'completed')
        self.assertTrue(lifecycle.watch(service))
        client=next(c for c in service.clients.values() if c['id']==next(iter(service.pending.values()))['client'])
        key=next(iter(service.pending));service._response(client,{'request_id':key,'success':False,'error':'此配置还有非本项目标签页，保留浏览器'})
        self.assertIn(client['profile'],budget.profiles(service));self.assertEqual(client['runtime_close']['state'],'failed')

    def test_manual_disabled_and_paid_uncertain_are_not_fault_recycled(self):
        service,names=self.pool();client=self.ready(service,names[0]);abandon(service,client,'准备超时')
        service.preferences['browser_blocks']={'profile:'+names[0]:PAUSED}
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        service.preferences['browser_blocks'].clear();service.store.enqueue(self.jobs(budget=0))
        row=service.store.rows()[0];service.store.update(row['id'],'uncertain',browser=client['id'],remote={'profile':names[0]})
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)

    def test_close_timeout_has_bounded_retry_but_does_not_release_physical_slot(self):
        service,names=self.pool();client=self.ready(service,names[0]);slots.seen(service,client)
        abandon(service,client,'准备超时');lifecycle.watch(service)
        service.pending.clear();timeout(client,service)
        self.assertEqual(service.preferences['browser_lifecycle'][names[0]]['state'],'close_failed')
        self.assertIn(names[0],budget.profiles(service));self.assertFalse(lifecycle.watch(service))
        with patch('time.time',return_value=time.time()+31):self.assertTrue(lifecycle.watch(service))

    def test_known_existing_overflow_never_opens_more(self):
        service,names=self.pool(40)
        for name in names[:28]:
            client=self.ready(service,name);slots.seen(service,client)
            client['reuse_recovery']={'state':'stopped','reason':'准备超时'}
        self.assertFalse(slots.request_start(service,names[28]))
        self.assertEqual(slots.summary(service)['physical_used'],28)

    def test_idle_replacements_are_kept_when_four_failed_windows_cannot_yet_close(self):
        service,names=self.pool();clients=[self.ready(service,n) for n in names[:8]]
        for client in clients:client['connected']=time.time()-60;slots.seen(service,client)
        for client in clients[:4]:
            abandon(service,client,'准备超时');client['native_busy']=True
        service.store.enqueue(self.jobs(count=12,budget=0))
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        self.assertFalse(slots.request_start(service,names[8]))
        self.assertTrue(all(not c.get('runtime_close') for c in clients[4:]))

    def test_third_fault_enters_long_cooldown_instead_of_rapid_reopen_loop(self):
        service,names=self.pool();client=self.ready(service,names[0]);slots.seen(service,client)
        service.preferences['browser_lifecycle']={names[0]:{'state':'eligible','count':2}}
        abandon(service,client,'准备超时');self.assertTrue(lifecycle.watch(service));self.confirm(service,client)
        entry=service.preferences['browser_lifecycle'][names[0]]
        self.assertEqual(entry['count'],3);self.assertGreaterEqual(entry['reopen_at']-time.time(),1799)
        service._disconnected(client['socket'])
        with patch('time.time',return_value=entry['reopen_at']-1):
            lifecycle.watch(service);self.assertEqual(service.preferences['browser_lifecycle'][names[0]]['state'],'cooldown')
        with patch('time.time',return_value=entry['reopen_at']+1):lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle'][names[0]]['count'],0)

for name in list(vars(fixtures.BrowserLifecycleTests)):
    if name.startswith('test_'):setattr(WindowBudgetTests,name,None)

if __name__=='__main__':unittest.main()

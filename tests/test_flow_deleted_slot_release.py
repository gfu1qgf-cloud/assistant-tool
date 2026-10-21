"""Deleted task journals may not permanently consume all browser windows."""
import json
import unittest
import time
from unittest.mock import MagicMock

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_cooldown import close_idle_exhausted
from app_plugins.builtin.flow_assistant.browser_slots import seen, occupied_profiles
from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
from app_plugins.builtin.flow_assistant.policy import capable
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class DeletedSlotReleaseTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    observe=fixtures.CreditCooldownTests.observe
    jobs=fixtures.CreditCooldownTests.jobs

    def retire_record(self,service,client,attempt):
        service.store.enqueue(self.jobs(duration=8,budget=0))
        row=service.store.rows()[-1]
        service.store.update(row['id'],'completed',attempt=attempt,browser=client['id'],
            remote={'account':client['account'],'profile':client['profile']})
        service.store.reserve(row['id'],attempt,12,'duration_rule')
        result=service.store.delete_records([row['id']])
        self.assertEqual(result['deleted'],[row['id']])

    def closure(self,client):
        return next(json.loads(call.args[0]) for call in reversed(client['socket'].sendTextMessage.call_args_list)
                    if json.loads(call.args[0]).get('action')=='close_exhausted')

    def test_confirmed_deleted_attempts_are_sent_only_to_their_original_browser(self):
        service=self.service();one=self.browser(service,'One',close=True);two=self.browser(service,'Two',close=True)
        one['capabilities'].append('close_released_batches_v1')
        self.retire_record(service,one,'a'*32);self.retire_record(service,two,'b'*32)
        self.observe(service,one,2);one['credit_check']['at']-=31
        self.assertTrue(close_idle_exhausted(service))
        sent=self.closure(one)
        self.assertEqual(sent['params'].get('released_batches'),['a'*32])
        self.assertNotIn('b'*32,sent['params']['released_batches'])
        self.assertEqual(service.store.batches()[next(iter(service.store.batches()))]['reserved'],12)

    def test_an_old_extension_cannot_consume_three_close_retries_on_unsupported_release_proof(self):
        service=self.service();client=self.browser(service,close=True)
        self.retire_record(service,client,'a'*32)
        self.observe(service,client,2);client['credit_check']['at']-=31
        for _ in range(5): self.assertFalse(close_idle_exhausted(service))
        self.assertFalse(any(p['action']=='close_exhausted' for p in service.pending.values()))
        self.assertEqual(client['credit_close'].get('count',0),0)

    def test_real_four_slot_shape_recovers_three_productive_accounts_and_keeps_unknown_result(self):
        service=self.service(); profiles=['Rich','Low1','Low2','Original','Next1','Next2','Next3']
        service.preferences.update(concurrency=4,profiles=profiles,
            profile_projects={p:fixtures.PROJECT for p in profiles})
        rich=self.browser(service,'Rich',points=814,close=True)
        low=[self.browser(service,p,close=True) for p in ('Low1','Low2')]
        held=self.browser(service,'Original',points=1)
        for i,client in enumerate([rich,*low]):
            client['capabilities']+=['close_idle_backup_v1','close_released_batches_v1']
            self.retire_record(service,client,str(i+1)*32)
        self.observe(service,rich,814)
        service.preferences['high_credit_accounts'][rich['account']].update(
            decision='skipped',decision_source='user_declined',asked_cycle=0)
        for client in low:
            self.observe(service,client,2);client['credit_check']['at']-=31
        for client in [rich,*low,held]:seen(service,client)
        service.store.enqueue(self.jobs(duration=8,count=7,budget=0))
        rows=service.store.rows();original=rows[0]
        service.store.update(original['id'],'uncertain',browser=held['id'],attempt='f'*32,
            remote={'profile':held['profile'],'account':held['account']})
        service.paused=False
        launches=[];service.launch_requested.connect(launches.append)
        for _ in range(20):
            service._tick()
            for key,pending in list(service.pending.items()):
                if pending['action']!='close_exhausted':continue
                client=next(c for c in service.clients.values() if c.get('id')==pending['client'])
                self.assertTrue(self.closure(client)['params'].get('released_batches'))
                service._response(client,{'request_id':key,'success':True,'data':{'closed':True}})
            for profile in launches:
                if not any(c.get('profile')==profile for c in service.clients.values()):
                    self.browser(service,profile,points=40)
            if len([r for r in service.store.rows() if r['status']=='preparing'])==3:break
        self.assertEqual(len({r['browser'] for r in service.store.rows() if r['status']=='preparing'}),3)
        self.assertEqual(service.store.get(original['id'])['status'],'uncertain')
        self.assertEqual(service.store.get(original['id'])['attempt'],'f'*32)
        self.assertLessEqual(len(occupied_profiles(service)),4)
        self.assertFalse(any(r['status']=='cancelled' for r in service.store.rows()))

    def test_reconnecting_cooldown_browser_restores_balance_for_idle_close_without_spending(self):
        service=self.service();old=self.browser(service,close=True)
        self.observe(service,old,2)
        socket=MagicMock()
        service.clients[socket]={'socket':socket,'authorized':False,'connected':time.time()}
        service._message(socket,json.dumps({'type':'hello','protocol':1,'token':service.pair['token'],
            'browser_id':old['id'],'profile':old['profile'],'project':old['saved_project'],
            'extension_version':EXTENSION_VERSION,'capabilities':['close_exhausted_v1','close_released_batches_v1']}))
        client=service.clients[socket]
        self.assertEqual(client['credits'],2)
        self.assertTrue(credit_hold(client,service.preferences))
        self.assertFalse(capable(client,self.jobs(duration=6,budget=0)[0],service.preferences,10))
        self.assertFalse(any(p['action']=='get_points' for p in service.pending.values()))
        for key,pending in list(service.pending.items()):
            service._response(client,{'request_id':key,'success':True,'data':{}})
        client['credit_check']['at']-=31
        self.assertTrue(close_idle_exhausted(service))


if __name__=='__main__':unittest.main()

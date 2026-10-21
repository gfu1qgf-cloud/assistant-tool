"""Rechecking an existing window is not a reason to open an offline backup."""
import json
import time
import unittest
from unittest.mock import MagicMock, patch
import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_cache import with_balance


class ExistingBalanceTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs

    def waiting(self, concurrency=1):
        service=self.service();client=self.browser(service,'Existing',points=50)
        client.update(credits_at=time.time()-21601, connected=time.time()-3600)
        service.preferences.update(concurrency=concurrency,profiles=['Existing','Offline'],
            profile_projects={'Existing':fixtures.PROJECT,'Offline':fixtures.PROJECT})
        service.store.enqueue(self.jobs(duration=8,count=3,budget=0));service.paused=False
        launches=[];service.launch_requested.connect(launches.append)
        return service,client,launches

    def test_pending_read_in_existing_window_reserves_its_lane_before_new_launch(self):
        service,client,launches=self.waiting()
        service._tick()
        self.assertTrue(any(p['action']=='get_points' and p['client']==client['id'] for p in service.pending.values()))
        self.assertFalse(launches)
        client['native_busy']=True  # The page is busy doing this very balance read.
        service._tick();self.assertFalse(launches)
        client['native_busy']=False
        request=next(k for k,p in service.pending.items() if p['action']=='get_points')
        service._response(client,{'request_id':request,'success':True,
            'data':{'email':client['account'],'remaining_points':50}})
        service._tick()
        self.assertEqual(service.store.rows()[0]['browser'],client['id'])
        self.assertFalse(launches)

    def test_one_pending_read_does_not_serialize_other_parallel_lanes(self):
        service,client,launches=self.waiting(concurrency=2)
        service._tick();self.assertEqual(launches,['Offline'])

    def test_failed_read_releases_reservation_and_starts_backup(self):
        service,client,launches=self.waiting();service._tick()
        request=next(k for k,p in service.pending.items() if p['action']=='get_points')
        service._response(client,{'request_id':request,'success':False,'error':'temporary read failure'})
        service._tick();self.assertEqual(launches,['Offline'])

    def test_expired_pending_read_does_not_reserve_forever(self):
        service,client,launches=self.waiting();service._tick()
        for request in service.pending.values():request['at']-=3600
        service._tick();self.assertEqual(launches,['Offline'])

    def test_blocked_window_does_not_reserve_backup_lane(self):
        service,client,launches=self.waiting();service.request(client,'get_points')
        service.preferences['browser_blocks']={'profile:Existing':'login needs manual attention'}
        service._tick();self.assertEqual(launches,['Offline'])

    def test_reconnect_reuses_valid_balance_without_get_points(self):
        service=self.service();old=self.browser(service,'Existing',points=35)
        service.commit_preferences(with_balance(service.preferences,old,service.clients.values()))
        socket=MagicMock();service.clients[socket]={'socket':socket,'authorized':False,'connected':time.time()}
        service._message(socket,json.dumps({'type':'hello','protocol':1,'token':service.pair['token'],
            'browser_id':'restored','profile':'Existing','project':fixtures.PROJECT,'extension_version':'1.1.31'}))
        self.assertFalse(any(p['client']=='restored' and p['action']=='get_points' for p in service.pending.values()))
        self.assertEqual(service.clients[socket]['credits'],35)


if __name__=='__main__':unittest.main()

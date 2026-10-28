"""Other-image owners must never reserve this FIFO head's spare account lanes."""
import copy
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_cache import refresh_needed
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner
from app_plugins.builtin.flow_assistant.connection_status import browser_status


class ImageCapacityTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser

    def queue(self, cold=True):
        service=self.service();service.timer.stop();service.preferences['concurrency']=8
        service.store.enqueue(self.jobs(count=4,duration=8,budget=0));original=service.store.rows()
        for index,row in enumerate(original):
            client=self.browser(service,'Running '+str(index),points=50)
            remember_owner(service,row,client)
            service.store.update(row['id'],'generating',attempt=f'{index+1:032x}',browser=client['id'],
                remote={'account':client['account'],'profile':client['profile'],'activity_at':time.time()})
        other=self.target/'new-image.png';other.write_bytes(b'different queued reference')
        service.store.enqueue(self.jobs(images=[other],count=6,duration=8,budget=0))
        owners=[self.browser(service,'Other image '+str(index),points=50) for index in range(8)]
        for client in owners:
            remember_owner(service,original[0],client)
            client.update(page_ready=not cold,connected=time.time()-20)
        spares=['New spare '+str(index) for index in range(5)]
        service.preferences['profiles']=[c['profile'] for c in service.clients.values()]+spares
        service.preferences['profile_projects'].update({p:fixtures.PROJECT for p in spares})
        launches=[];service.launch_requested.connect(launches.append);service.paused=False
        return service,owners,spares,launches

    def test_four_of_eight_running_with_many_cold_other_image_owners_launches_four_backups(self):
        service,owners,spares,launches=self.queue()
        original=[copy.deepcopy(r) for r in service.store.rows()[:4]]
        claims=copy.deepcopy(service.preferences['account_images'])
        service.fill_dispatch_slots()
        self.assertEqual(launches,spares[:4],'other-image page recovery cannot consume the four spare lanes')
        service.fill_dispatch_slots();self.assertEqual(launches,spares[:4])
        for profile in launches:self.browser(service,profile,points=50)
        service.fill_dispatch_slots()
        rows=service.store.rows()
        self.assertEqual([r['status'] for r in rows[4:]],['preparing']*4+['queued']*2)
        self.assertEqual(rows[:4],original)
        for client in owners:
            self.assertEqual(service.preferences['account_images'][client['account']],claims[client['account']])

    def test_ready_existing_accounts_for_head_are_used_before_opening_backups(self):
        service,owners,spares,launches=self.queue()
        ready=[self.browser(service,'Ready for head '+str(index),points=50) for index in range(4)]
        service.fill_dispatch_slots()
        self.assertFalse(launches)
        self.assertEqual({r['browser'] for r in service.store.rows()[4:8]},{c['id'] for c in ready})

    def test_connected_other_image_startup_leases_do_not_block_uncovered_head_slots(self):
        service,owners,spares,launches=self.queue()
        service.launching={client['profile']:time.time() for client in owners[:4]}
        service.launched.update(service.launching)
        service.fill_dispatch_slots()
        self.assertEqual(launches,spares[:4])
        self.assertFalse(set(c['profile'] for c in owners) & set(service.launching))

    def test_existing_other_image_credit_reads_do_not_reserve_useful_probe_capacity(self):
        service,owners,spares,launches=self.queue(cold=False)
        for client in owners:
            client['credits_at']-=21601
            service.request(client,'get_points')
        useful=self.browser(service,'Useful existing',points=50);useful['credits_at']-=21601
        self.assertTrue(refresh_needed(service,[service.store.rows()[4]],capacity=1))
        self.assertTrue(any(p['action']=='get_points' and p['client']==useful['id'] for p in service.pending.values()))

    def test_existing_recovery_for_current_image_still_reserves_only_its_free_lanes(self):
        service,owners,spares,launches=self.queue()
        head=service.store.rows()[4]
        for client in owners[:4]:remember_owner(service,head,client)
        service.fill_dispatch_slots()
        self.assertFalse(launches,'the four current-image recovery accounts already cover the four vacancies')

    def test_credit_refresh_skips_other_image_owners_and_probes_useful_existing_account(self):
        service,owners,spares,launches=self.queue(cold=False)
        for client in owners:client['credits_at']-=21601
        useful=self.browser(service,'Useful existing',points=50);useful['credits_at']-=21601
        self.assertTrue(refresh_needed(service,[service.store.rows()[4]],capacity=1))
        reads=[p['client'] for p in service.pending.values() if p['action']=='get_points']
        self.assertEqual(reads,[useful['id']])

    def test_idle_other_image_owner_is_not_displayed_as_ready_for_the_current_head(self):
        service,owners,spares,launches=self.queue(cold=False)
        status=browser_status(owners[0],service.preferences,service.store.rows())
        self.assertEqual(status['state'],'其他图片独占')
        self.assertIn('其他账号',status['reason'])


if __name__=='__main__':unittest.main()

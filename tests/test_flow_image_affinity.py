"""Drain an image's current account; different source images still parallelize."""
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.image_affinity import image_key, preferred_owner
from app_plugins.builtin.flow_assistant.store import QueueStore


class ImageAffinityTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser

    def queue(self, service, count=4):
        service.store.enqueue(self.jobs(duration=8, count=count, budget=0)); service.paused=False
        service.preferences['concurrency']=4
        return service.store.rows()

    def test_same_image_copies_do_not_run_on_four_accounts_at_once(self):
        service=self.service(); clients=[self.browser(service,'Profile '+str(i),points=points) for i,points in enumerate((26,38,90,1300))]
        self.queue(service)
        for _ in range(5): service._tick()
        active=[row for row in service.store.rows() if row['status']=='preparing']
        self.assertEqual(len(active),1); self.assertEqual(active[0]['browser'],clients[0]['id'])
        published=[]; service.changed.connect(published.append); service.publish()
        self.assertTrue(all('同一参考图' in row['wait_reason'] for row in published[-1]['jobs'] if row['status']=='queued'))

    def test_keep_funded_owner_even_when_another_smaller_account_becomes_ready(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=50); rows=self.queue(service, count=3)
        service._tick(); service.store.update(rows[0]['id'],'completed')
        self.browser(service,'Profile 2',points=26); service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],owner['id'])
        service.store.update(rows[1]['id'],'completed'); owner['credits']=9
        service._tick(); self.assertEqual(service.store.get(rows[2]['id'])['browser'],'Profile 2-id')

    def test_last_paid_job_finishes_before_the_same_image_moves_to_next_account(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); rows=self.queue(service,count=2)
        self.browser(service,'Profile 2',points=38); service._tick()
        owner['credits']=2; service.store.update(rows[0]['id'],'generating')
        service._tick(); self.assertEqual(service.store.get(rows[1]['id'])['status'],'queued')
        service.store.update(rows[0]['id'],'completed'); service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],'Profile 2-id')

    def test_different_images_keep_four_account_parallelism(self):
        service=self.service(); service.preferences['concurrency']=4; service.paused=False
        for i in range(4):
            image=self.target/f'image-{i}.png';image.write_bytes(('distinct reference '+str(i)).encode())
            service.store.enqueue(self.jobs(images=[image],duration=8,budget=0))
            self.browser(service,'Profile '+str(i),points=40+i*20)
        for _ in range(5): service._tick()
        active=[row for row in service.store.rows() if row['status']=='preparing']
        self.assertEqual(len(active),4); self.assertEqual(len({row['browser'] for row in active}),4)

    def test_reset_clears_persistent_image_owner_and_allows_other_browser(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); row=self.queue(service,count=1)[0]
        service._tick(); current=service.store.get(row['id']); key=image_key(current['payload'])
        self.assertIn(key,QueueStore(self.namespace).settings()['image_affinities'])
        service.execute({'action':'reset_tasks','ids':[row['id']], 'versions':{row['id']:{name:current[name] for name in ('attempt','browser','status')}}})
        self.assertNotIn(key,QueueStore(self.namespace).settings()['image_affinities'])
        self.browser(service,'Profile 2',points=38); service._tick()
        self.assertEqual(service.store.get(row['id'])['browser'],'Profile 2-id')

    def test_offline_owner_recovery_is_bounded(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); row=self.queue(service,count=1)[0]
        service._tick(); payload=service.store.get(row['id'])['payload']; service.clients.clear()
        self.assertTrue(preferred_owner(payload,[],service.preferences,12))
        service.preferences['image_affinities'][image_key(payload)]['at']=time.time()-100
        self.assertIsNone(preferred_owner(payload,[],service.preferences,12))

    def test_unsupported_parameter_does_not_lock_an_image_to_an_incapable_owner(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); row=self.queue(service,count=1)[0]
        service._tick(); payload=service.store.get(row['id'])['payload']
        service.preferences['parameter_blocks']={'profile:'+owner['profile']:[{
            **{key:payload[key] for key in ('model','duration','aspect','resolution')}, 'reason':'unsupported'}]}
        self.assertIsNone(preferred_owner(payload,[owner],service.preferences,12))


if __name__=='__main__':unittest.main()

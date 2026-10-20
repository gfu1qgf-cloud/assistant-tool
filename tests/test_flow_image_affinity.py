"""Same-image parallelism with account/image exclusion and a funded account pool."""
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.image_affinity import image_key, allows_image, account_images, retained
from app_plugins.builtin.flow_assistant.credit_cache import with_balance
from app_plugins.builtin.flow_assistant.store import QueueStore


class ImageAffinityTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser

    def queue(self, service, count=8):
        service.store.enqueue(self.jobs(duration=8, count=count, budget=0)); service.paused=False
        service.preferences['concurrency']=4
        return service.store.rows()

    def test_same_image_really_runs_on_four_distinct_accounts(self):
        service=self.service(); clients=[self.browser(service,'Profile '+str(i),points=points) for i,points in enumerate((26,38,90,1300))]
        self.queue(service)
        for _ in range(5): service._tick()
        active=[row for row in service.store.rows() if row['status']=='preparing']
        self.assertEqual(len(active),4); self.assertEqual({row['browser'] for row in active},{c['id'] for c in clients})
        saved=QueueStore(self.namespace).settings()['account_images']
        self.assertEqual(set(saved),{c['account'] for c in clients})
        self.assertEqual({entry['image'] for entry in saved.values()},{image_key(active[0]['payload'])})

    def test_keep_funded_owner_even_when_another_smaller_account_becomes_ready(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=50); rows=self.queue(service, count=3)
        service.preferences['concurrency']=1
        service._tick(); service.store.update(rows[0]['id'],'completed')
        self.browser(service,'Profile 2',points=26); service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],owner['id'])
        service.store.update(rows[1]['id'],'completed'); owner['credits']=9
        service._tick(); self.assertEqual(service.store.get(rows[2]['id'])['browser'],'Profile 2-id')

    def test_same_image_can_use_another_account_while_first_account_finishes_paid_job(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); rows=self.queue(service,count=2)
        self.browser(service,'Profile 2',points=38); service._tick()
        owner['credits']=2; service.store.update(rows[0]['id'],'generating')
        service._tick()
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
        self.assertEqual(QueueStore(self.namespace).settings()['account_images'][owner['account']]['image'],key)
        service.execute({'action':'reset_tasks','ids':[row['id']], 'versions':{row['id']:{name:current[name] for name in ('attempt','browser','status')}}})
        self.assertNotIn(owner['account'],QueueStore(self.namespace).settings()['account_images'])
        self.browser(service,'Profile 2',points=38); service._tick()
        self.assertEqual(service.store.get(row['id'])['browser'],'Profile 2-id')

    def test_offline_selected_account_is_tried_once_before_starting_a_new_account(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=50); rows=self.queue(service,count=2)
        service.preferences.update(concurrency=1, profiles=['Profile 1','Profile 2'])
        service._tick(); service.store.update(rows[0]['id'],'completed')
        service.preferences=with_balance(service.preferences,owner,list(service.clients.values()))
        service.clients.clear(); backup=self.browser(service,'Profile 2',points=26)
        launched=[];service.launch_requested.connect(launched.append);service._tick();service._tick()
        self.assertEqual(launched,['Profile 1'])
        deadline=service.small_balance_probes['Profile 1'];backup['page_at']=deadline+1
        with patch('time.time',return_value=deadline+.1):service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],backup['id'])
        self.assertEqual(launched,['Profile 1'])

    def test_unsupported_remaining_image_variant_does_not_trap_account(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=26); rows=self.queue(service,count=2)
        service._tick(); service.store.update(rows[0]['id'],'completed'); payload=rows[1]['payload']
        service.preferences['parameter_blocks']={'profile:'+owner['profile']:[{
            **{key:payload[key] for key in ('model','duration','aspect','resolution')}, 'reason':'unsupported'}]}
        other=self.target/'other.png';other.write_bytes(b'other reference')
        other_job=self.jobs(images=[other],duration=6,budget=0)[0]
        self.assertTrue(allows_image(owner,other_job,service.store.rows(),service.preferences))

    def test_account_finishes_its_image_before_taking_another_even_at_higher_priority(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=50); rows=self.queue(service,count=2)
        service.preferences['concurrency']=1;service._tick();service.store.update(rows[0]['id'],'completed')
        other=self.target/'other.png';other.write_bytes(b'other reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0)); other_row=service.store.rows()[-1]
        payload={**other_row['payload'],'priority':100};service.store.set_payload(other_row['id'],payload)
        service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],owner['id'])
        self.assertEqual(service.store.get(other_row['id'])['status'],'queued')

    def test_free_member_waits_for_its_image_last_result_instead_of_starting_fifth_account(self):
        service=self.service(); clients=[self.browser(service,'Profile '+str(i),points=50+i*10) for i in range(4)]
        rows=self.queue(service,count=4)
        for _ in range(4):service._tick()
        service.store.update(rows[0]['id'],'completed')
        self.browser(service,'Fresh',points=15)
        other=self.target/'other.png';other.write_bytes(b'other reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0)); target=service.store.rows()[-1]
        service._tick();self.assertEqual(service.store.get(target['id'])['status'],'queued')
        for row in rows[1:]:service.store.update(row['id'],'completed')
        service._tick();self.assertEqual(service.store.get(target['id'])['browser'],clients[0]['id'])

    def test_four_selected_accounts_continue_instead_of_rotating_into_ten_new_ones(self):
        service=self.service(); clients=[self.browser(service,'Profile '+str(i),points=50+i*10) for i in range(4)]
        rows=self.queue(service,count=8)
        for _ in range(4):service._tick()
        for row in rows[:4]:service.store.update(row['id'],'completed')
        for i in range(10):self.browser(service,'Fresh '+str(i),points=15+i)
        for _ in range(4):service._tick()
        self.assertEqual({service.store.get(row['id'])['browser'] for row in rows[4:]},{c['id'] for c in clients})
        self.assertEqual(len(service.preferences['account_images']),4)

    def test_four_same_image_uploads_have_independent_attempts_and_exact_reference_receipts(self):
        from flow_reference_fixtures import upload_receipt
        import json
        service=self.service();clients=[self.browser(service,'Profile '+str(i),points=50+i*10) for i in range(4)]
        self.queue(service,count=4)
        for _ in range(4):service._tick()
        for client in clients:
            request=next(k for k,p in service.pending.items() if p['client']==client['id'] and p['action']=='prepare')
            service._response(client,{'request_id':request,'success':True,'data':{'verified':True,'model':'omni_1_1_flash'}})
        references=[]
        for client in clients:
            request=next(k for k,p in service.pending.items() if p['client']==client['id'] and p['action']=='upload_images')
            row=service.store.get(service.pending[request]['job']);receipt=upload_receipt(service,row['id'])
            references.append(receipt['reference'])
            service._response(client,{'request_id':request,'success':True,'data':receipt})
        self.assertEqual(len({ref['upload_id'] for ref in references}),4)
        self.assertEqual(len({ref['path'] for ref in references}),4)
        self.assertEqual(len({ref['sha256'] for ref in references}),1)
        for client in clients:
            submissions=[json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list
                         if json.loads(call.args[0]).get('action')=='run_tasks']
            self.assertEqual(len(submissions),1)
            self.assertEqual(submissions[0]['params']['expected_account'],client['account'])

    def test_finished_image_releases_accounts_but_the_same_funded_pool_is_reused(self):
        service=self.service(); clients=[self.browser(service,'Profile '+str(i),points=50+i*10) for i in range(4)]
        rows=self.queue(service,count=4)
        for _ in range(4):service._tick()
        for row in rows:service.store.update(row['id'],'completed')
        other=self.target/'other.png';other.write_bytes(b'other reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,count=4,budget=0))
        self.browser(service,'Fresh',points=15)
        for _ in range(4):service._tick()
        current=[row for row in service.store.rows() if row['status']=='preparing']
        self.assertEqual({row['browser'] for row in current},{c['id'] for c in clients})
        self.assertEqual(len({image_key(row['payload']) for row in current}),1)
        self.assertNotEqual(image_key(current[0]['payload']),image_key(rows[0]['payload']))

    def test_funded_pool_survives_normal_service_restart(self):
        service=self.service(); owner=self.browser(service,'Profile 1',points=50); rows=self.queue(service,count=2)
        service.preferences['concurrency']=1;service._tick();service.store.update(rows[0]['id'],'completed')
        restarted=self.service();restarted.paused=False;restarted.preferences['concurrency']=1
        self.browser(restarted,'Profile 1',points=50);self.browser(restarted,'Fresh',points=15);restarted._tick()
        self.assertEqual(restarted.store.get(rows[1]['id'])['browser'],owner['id'])

    def test_exhausted_account_is_retired_and_does_not_steal_back_after_refill(self):
        service=self.service(); one=self.browser(service,'Profile 1',points=50); rows=self.queue(service,count=3)
        service.preferences['concurrency']=1;service._tick();service.store.update(rows[0]['id'],'completed');one['credits']=9
        two=self.browser(service,'Profile 2',points=26);service._tick()
        self.assertFalse(service.preferences['account_images'][one['account']]['active'])
        service.store.update(rows[1]['id'],'completed');one['credits']=15;service._tick()
        self.assertEqual(service.store.get(rows[2]['id'])['browser'],two['id'])

    def test_legacy_image_owner_is_migrated_without_serializing_same_image(self):
        service=self.service(); one=self.browser(service,'Profile 1',points=50); two=self.browser(service,'Profile 2',points=26)
        rows=self.queue(service,count=2); key=image_key(rows[0]['payload'])
        service.preferences['image_affinities']={key:{'account':one['account'],'profile':one['profile'],'browser':one['id'],'at':time.time()}}
        service._tick();service._tick()
        self.assertEqual({service.store.get(row['id'])['browser'] for row in rows},{one['id'],two['id']})
        self.assertEqual(set(account_images(service.preferences)),{one['account'],two['account']})
        self.assertFalse(service.preferences['image_affinities'])

    def test_offline_profile_of_the_same_busy_account_does_not_open_an_extra_browser(self):
        service=self.service();one=self.browser(service,'Profile 1',points=50)
        duplicate=self.browser(service,'Profile 2',points=50);duplicate['account']=one['account']
        service.preferences.update(profiles=['Profile 1','Profile 2'])
        service.preferences=with_balance(service.preferences,one,list(service.clients.values()))
        service.clients.pop(duplicate['socket']);self.queue(service,count=8)
        launched=[];service.launch_requested.connect(launched.append)
        for _ in range(4):service._tick()
        self.assertEqual(len([r for r in service.store.rows() if r['status']=='preparing']),1)
        self.assertFalse(launched)

    def test_legacy_duplicate_account_uses_most_recent_image_not_dictionary_order(self):
        preferences={'image_affinities':{
            'newer-image':{'account':'Same@Example.Test','at':200},
            'older-image':{'account':'same@example.test','at':100}}}
        entries=account_images(preferences)
        self.assertEqual(entries['same@example.test']['image'],'newer-image')
        self.assertNotIn('account_images',preferences,'reading old preferences must not write a migration')

    def test_expired_selected_balance_read_does_not_start_an_unneeded_replacement(self):
        service=self.service();owner=self.browser(service,'Profile 1',points=50);rows=self.queue(service,count=2)
        service.preferences['concurrency']=1;service._tick();service.store.update(rows[0]['id'],'completed')
        owner.update(credits_at=time.time()-21601,connected=time.time()-4000)
        service.preferences.update(profiles=['Profile 1','Backup'],profile_projects={'Profile 1':fixtures.PROJECT,'Backup':fixtures.PROJECT})
        launched=[];service.launch_requested.connect(launched.append);service._tick()
        self.assertTrue(any(p['action']=='get_points' for p in service.pending.values()))
        self.assertFalse(launched)

    def test_temporary_page_remount_reserves_selected_account_instead_of_rotating_it(self):
        service=self.service();owner=self.browser(service,'Profile 1',points=50);rows=self.queue(service,count=2)
        service.preferences['concurrency']=1;service._tick();service.store.update(rows[0]['id'],'completed')
        owner['connected']=time.time()-4000
        service._snapshot(owner,{'url':owner['project'],'controls_ready':False,'running':False,'tasks':[]})
        self.browser(service,'Fresh',points=15);service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['status'],'queued')
        owner['page_ready']=True;service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],owner['id'])

    def test_unreleased_or_unaffordable_image_rows_do_not_deadlock_explicit_other_work(self):
        for mode in ('paused','budget'):
            with self.subTest(mode=mode):
                self.setUp();service=self.service();owner=self.browser(service,'Profile 1',points=50)
                rows=self.queue(service,count=2);service.preferences['concurrency']=1
                service._tick();service.store.update(rows[0]['id'],'completed')
                other=self.target/'next.png';other.write_bytes(b'next reference')
                service.store.enqueue(self.jobs(images=[other],duration=8,budget=0));next_row=service.store.rows()[-1]
                if mode=='paused':service.paused=True;service.released={next_row['id']}
                else:service.store.set_payload(rows[1]['id'],{**rows[1]['payload'],'budget':1})
                service._tick()
                self.assertEqual(service.store.get(next_row['id'])['browser'],owner['id'])
                self.assertEqual(service.store.get(rows[1]['id'])['status'],'queued')


if __name__=='__main__':unittest.main()

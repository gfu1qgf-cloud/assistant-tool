"""Retained accounts get first refusal; waiting cannot strand ready open accounts."""
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner, retained_pool


class WaitingParallelTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs

    def fixture(self, kind='balance', extra=0):
        service = self.service(); service.preferences['concurrency'] = 4
        busy = self.browser(service, 'Profile 135', points=26)
        owners = [self.browser(service, 'Owner '+str(n), points=11 if kind in {'balance','upload'} else 50) for n in range(3)]
        spares = [self.browser(service, 'Open spare '+str(n), points=38) for n in range(3)]
        for n in range(extra): self.browser(service, 'Unfunded '+str(n), points=2)
        service.preferences['profiles'] = [c['profile'] for c in service.clients.values()]
        for duration in (10,8,8,8,10,10,6): service.store.enqueue(self.jobs(duration=duration,budget=0))
        rows = service.store.rows()
        for client in [busy]+owners: remember_owner(service, rows[0], client)
        service.store.update(rows[0]['id'], 'preparing', browser=busy['id'], attempt='a'*32,
            remote={'profile':busy['profile'],'account':busy['account'],'activity_at':time.time()})
        service.request(busy, 'prepare', job=rows[0]['id'])
        if kind == 'upload':
            service.pending.clear()
            service.store.update(rows[0]['id'], 'uploading', remote={**service.store.get(rows[0]['id'])['remote'],
                'cost':15,'transfer_retries':{'upload':{'count':1,'next_at':time.time()+300}}})
        elif kind == 'native':
            for client in owners: client['native_busy'] = True
        elif kind == 'credits':
            for client in owners:
                client['credits_at'] -= 21601
                service.request(client,'get_points')
        elif kind == 'lane':
            service.preferences['profile_lane_modes'] = {c['profile']:'fixed' for c in owners}
            service.preferences['profile_lanes'] = {c['profile']:6 for c in owners}
        elif kind == 'retry':
            for client in owners: client['prepare_wait'] = {'next_at':time.time()+300}
        service.paused = False; launches = []
        service.launch_requested.connect(launches.append)
        return service,busy,owners,spares,rows,launches

    def assert_parallel(self, kind, extra=0):
        service,busy,owners,spares,rows,launches = self.fixture(kind,extra)
        self.assertEqual(len(retained_pool(list(service.clients.values()),service.store.rows(),service.preferences)),4)
        original = service.store.get(rows[0]['id'])
        for _ in range(5): service._tick()
        current = service.store.rows()
        self.assertEqual(sum(r['status'] in {'preparing','uploading'} for r in current),4)
        self.assertEqual([r['browser'] for r in current[1:4]],[c['id'] for c in spares])
        self.assertEqual([r['status'] for r in current[4:]],['queued']*3)
        self.assertEqual(service.store.get(rows[0]['id']),original)
        self.assertFalse(launches)
        self.assertEqual(len({r['attempt'] for r in current[:4]}),4)
        self.assertEqual(len({r['payload']['image_identity']['sha256'] for r in current[:4]}),1)

    def test_twenty_two_open_browsers_with_short_job_credit_reservations_fill_four_lanes(self):
        self.assert_parallel('balance',extra=15)

    def test_native_busy_retained_accounts_do_not_block_ready_connected_accounts(self):
        self.assert_parallel('native')

    def test_upload_retry_keeps_original_attempt_while_other_ready_accounts_fill_spare_lanes(self):
        self.assert_parallel('upload')

    def test_expired_balance_reads_only_wait_their_own_accounts(self):
        self.assert_parallel('credits')

    def test_six_second_retained_accounts_do_not_reserve_eight_second_slots(self):
        self.assert_parallel('lane')

    def test_account_parameter_wait_only_waits_that_account(self):
        self.assert_parallel('retry')

    def test_ready_retained_account_still_runs_before_an_earlier_connected_spare(self):
        service,busy,owners,spares,rows,launches = self.fixture()
        owners[0]['credits'] = 50
        service.preferences['profiles'] = [c['profile'] for c in spares+owners+[busy]]
        service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],owners[0]['id'])
        self.assertFalse(launches)

    def test_idle_image_history_can_be_reused_but_duplicate_busy_account_stays_locked(self):
        service,busy,owners,spares,rows,launches = self.fixture()
        other = self.target/'other.png'; other.write_bytes(b'different reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0))
        other_row = service.store.rows()[-1]
        remember_owner(service,other_row,spares[0])
        spares[1]['account'] = busy['account'].upper()
        service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['browser'],spares[0]['id'])
        self.assertNotIn(spares[1]['id'],[r['browser'] for r in service.store.rows() if r['status']=='preparing'])
        self.assertEqual(service.store.get(other_row['id'])['status'],'queued')
        self.assertFalse(launches)

    def test_no_ready_existing_account_admits_only_three_backups_for_three_free_lanes(self):
        service,busy,owners,spares,rows,launches = self.fixture()
        for spare in spares: service.clients.pop(spare['socket'])
        service.preferences['profiles'].append('Offline backup')
        service.preferences['profile_projects']['Offline backup'] = fixtures.PROJECT
        for _ in range(8): service._tick()
        self.assertEqual(launches,[c['profile'] for c in spares])
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),1)


if __name__=='__main__': unittest.main()

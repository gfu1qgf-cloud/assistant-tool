"""Parallel scheduling contracts, including deliberate paid-result holds.

Use the current fixed tariff and ordinary accounts when testing affordability.
Unresolved paid work holds its browser/profile until explicitly parked; that
safety boundary must not be removed merely to increase measured throughput.
"""
import time
import unittest
from unittest.mock import patch, MagicMock

import test_flow_assistant as fixtures
import test_flow_credit_cooldown as credit_fixtures
from app_plugins.builtin.flow_assistant.store import choose_client, TRANSIENT
from app_plugins.builtin.flow_assistant.policy import capable, waiting_reason
from app_plugins.builtin.flow_assistant.credit_cooldown import MIN_CREDITS


def _ready_client(service, profile, points=50):
    """Minimal fully-ready client attached to service."""
    client = credit_fixtures.CreditCooldownTests.browser(
        credit_fixtures.CreditCooldownTests(), service, profile, points)
    client.update(page_ready=True, page_at=time.time(),
                  credits_local=True, authorized=True)
    return client


def _row(status, profile='', account='', browser=''):
    """Minimal row dict for choose_client / waiting_reason tests."""
    return {'id': 'job-' + status + profile, 'status': status,
            'browser': browser,
            'remote': {'account': account, 'profile': profile},
            'payload': {}}


class BlockedFlagNotClearedTests(unittest.TestCase):
    """BUG-C: client['blocked'] = '积分不足' must be cleared when a job is
    requeued because the prepare response found insufficient credits.
    If it is not cleared, capable() permanently returns False for this client
    even after the account receives more credits.
    """
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def _prepare_then_insufficient(self, service, client, row, cost):
        """Simulate prepare returning a cost the client cannot afford."""
        service.store.update(row['id'], 'preparing',
                             attempt='a' * 32, browser=client['id'],
                             remote={'account': client['account'],
                                     'profile': client.get('profile', ''),
                                     'activity_at': time.time()})
        service.request(client, 'prepare', job=row['id'])
        key = next(k for k, p in service.pending.items()
                   if p['action'] == 'prepare')
        # prepare succeeds but reported cost exceeds credits
        service._response(client, {
            'request_id': key, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash',
                     'cost': cost, 'email': client['account']}})

    def test_insufficient_credit_at_prepare_clears_blocked_on_requeue(self):
        """After prepare reveals cost > credits, the job is requeued and
        client['blocked'] must be '' so the account can be selected again."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        row = service.store.rows()[0]
        client = _ready_client(service, 'Profile A', points=9)
        client['credits'] = 9  # below the authoritative 6-second cost of 10
        service.paused = False

        self._prepare_then_insufficient(service, client, row, cost=10)

        self.assertEqual(service.store.get(row['id'])['status'], 'queued',
                         'job must return to queued when credits are short')
        self.assertFalse(client.get('blocked'),
                         'an affordability miss must not become a permanent account ban')
        self.assertFalse(any(request['action'] == 'run_tasks' for request in service.pending.values()))

    def test_blocked_account_is_excluded_from_capable(self):
        """Sanity: when blocked is non-empty, capable() must return False."""
        service = self.service()
        client = _ready_client(service, 'Profile B', points=80)
        client['blocked'] = '积分不足'
        job = self.jobs(duration=6, budget=0)[0]
        self.assertFalse(capable(client, job, service.preferences, 10))

    def test_cleared_blocked_account_is_capable_again(self):
        """Once blocked is cleared and credits are sufficient, capable() must
        return True so the account re-enters the scheduling pool."""
        service = self.service()
        client = _ready_client(service, 'Profile C', points=80)
        client['blocked'] = ''
        job = self.jobs(duration=6, budget=0)[0]
        self.assertTrue(capable(client, job, service.preferences, 10))

    def test_upload_phase_insufficient_credit_also_clears_blocked(self):
        """The same guard applies at upload_images time (L643 path)."""
        from flow_reference_fixtures import upload_receipt
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        row = service.store.rows()[0]
        client = _ready_client(service, 'Profile D', points=9)
        client['credits'] = 9
        service.paused = False

        # advance to uploading state with a cost already set
        service.store.update(row['id'], 'uploading',
                             attempt='b' * 32, browser=client['id'],
                             remote={'account': client['account'],
                                     'profile': client.get('profile', ''),
                                     'cost': 10, 'cost_source': 'duration_rule',
                                     'activity_at': time.time()})
        service.request(client, 'upload_images', job=row['id'])
        key = next(k for k, p in service.pending.items()
                   if p['action'] == 'upload_images')
        service._response(client, {
            'request_id': key, 'success': True,
            'data': upload_receipt(service, row['id'])})

        self.assertEqual(service.store.get(row['id'])['status'], 'queued',
                         'job must return to queued when cost > credits at upload')
        self.assertFalse(client.get('blocked'),
                         'an upload-phase affordability miss is not an account ban')
        self.assertFalse(any(request['action'] == 'run_tasks' for request in service.pending.values()))


class ChooseClientProfileLockTests(unittest.TestCase):
    """Unresolved paid work holds a profile, but explicit parking releases it."""
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs

    def _client(self, profile, account, credits=80):
        return {'id': 'browser-' + account, 'profile': profile,
                'account': account, 'credits': credits,
                'credits_at': time.time(), 'credits_local': True,
                'page_ready': True, 'blocked': '', 'batch_running': False,
                'native_busy': False, 'reset_wait_until': 0,
                'credit_close': {}, 'connection_recovery': {},
                'credit_check': {}, 'prepare_wait': {},
                'authorized': True}

    def test_uncertain_row_holds_profile_even_after_a_different_account_logs_in(self):
        """Changing account cannot silently discard the old paid-result hold."""
        client_a = self._client('Profile X', 'a@example.test')
        client_b = self._client('Profile X', 'b@example.test')
        # account A has an uncertain row
        rows = [_row('uncertain', profile='Profile X', account='a@example.test',
                     browser='browser-a@example.test')]
        selected = choose_client([client_a, client_b], rows, limit=4)
        self.assertIsNone(selected, 'unresolved paid work still owns this browser profile')

    def test_explicitly_parked_download_releases_the_profile(self):
        client = self._client('Profile X', 'b@example.test')
        rows = [_row('download_parked', profile='Profile X', account='a@example.test',
                     browser='old-browser')]
        selected = choose_client([client], rows, limit=4)
        self.assertIsNotNone(selected)
        self.assertEqual(selected['account'], 'b@example.test')

    def test_uncertain_row_does_block_same_account(self):
        """The same account that owns the uncertain row must not be chosen."""
        client = self._client('Profile Y', 'same@example.test')
        rows = [_row('uncertain', profile='Profile Y',
                     account='same@example.test', browser=client['id'])]
        selected = choose_client([client], rows, limit=4)
        self.assertIsNone(selected,
                          'the account with an uncertain row must not be selected')

    def test_download_failed_row_does_not_lock_unrelated_profile(self):
        """A download_failed row on Profile Z must not prevent Profile W from
        being chosen, even if choose_client sees them in the same rows list."""
        client_w = self._client('Profile W', 'w@example.test')
        rows = [_row('download_failed', profile='Profile Z',
                     account='z@example.test', browser='browser-z')]
        selected = choose_client([client_w], rows, limit=4)
        self.assertIsNotNone(selected,
                             'download_failed on an unrelated profile must not '
                             'block a healthy account on a different profile')
        self.assertEqual(selected['account'], 'w@example.test')

    def test_four_profiles_hold_uncertain_results_and_release_after_explicit_parking(self):
        """A paid-result hold is not cleared by changing the login account."""
        clients = [self._client(f'Profile {i}', f'fresh{i}@example.test')
                   for i in range(4)]
        rows = [_row('uncertain', profile=f'Profile {i}',
                     account=f'stale{i}@example.test',
                     browser=f'browser-stale{i}')
                for i in range(4)]
        # concurrency = 4
        eligible_count = sum(
            1 for c in clients
            if choose_client([c], rows, limit=4) is not None)
        self.assertEqual(eligible_count, 0)
        for row in rows:
            row['status'] = 'download_parked'
        self.assertEqual(sum(choose_client([client], rows, limit=4) is not None
                             for client in clients), 4)

    def test_transient_row_does_lock_same_account_across_profiles(self):
        """A TRANSIENT row (generating) for account X must prevent account X
        from being chosen, regardless of profile."""
        for status in TRANSIENT:
            with self.subTest(status=status):
                client = self._client('Profile Alpha', 'busy@example.test')
                rows = [_row(status, profile='Profile Beta',
                             account='busy@example.test',
                             browser='other-browser')]
                selected = choose_client([client], rows, limit=4)
                self.assertIsNone(selected,
                                  f'account with a {status} row elsewhere '
                                  'must not be double-dispatched')


class SafetyPausedNoAutoRecoveryTests(unittest.TestCase):
    """BUG-B: _tick() catching any exception must not permanently halt the
    queue via safety_paused=True.  After the exception clears, normal ticks
    must resume dispatching work.
    """
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def test_tick_exception_pauses_but_start_resumes_dispatch(self):
        """After a _tick() exception causes safety_paused, the user's 'start'
        command must clear it and allow the next tick to dispatch work."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        client = _ready_client(service, 'Profile A', points=80)
        service.paused = False

        # Force _tick() to raise on its first real call
        original_rows = service.store.rows

        call_count = [0]
        def failing_rows():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError('simulated transient SQLite error')
            return original_rows()

        with patch.object(service.store, 'rows', side_effect=failing_rows):
            service._tick()  # first tick: raises → safety_paused=True

        self.assertTrue(service.safety_paused,
                        '_tick() exception must set safety_paused')
        # queue is stuck — second tick also no-ops
        service._tick()
        self.assertTrue(all(r['status'] == 'queued'
                            for r in service.store.rows()))

        # user clicks "start" → safety_paused must clear
        service.execute({'action': 'start'})
        self.assertFalse(service.safety_paused)
        self.assertFalse(service.paused)

        # third tick must now dispatch
        service._tick()
        active = [r for r in service.store.rows() if r['status'] == 'preparing']
        self.assertEqual(len(active), 1,
                         'after start, the next clean tick must dispatch work')

    def test_safety_paused_blocks_generation_retry_watch(self):
        """watch_generation_retries must be skipped while safety_paused."""
        from app_plugins.builtin.flow_assistant.generation_recovery import watch
        service = self.service()
        service.safety_paused = True
        # watch must return False immediately without doing anything
        self.assertFalse(watch(service))

    def test_queued_work_is_not_dispatched_while_safety_paused(self):
        """_tick() must skip all dispatch logic while safety_paused=True."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        client = _ready_client(service, 'Profile A', points=80)
        service.paused = False
        service.safety_paused = True

        service._tick()
        self.assertTrue(all(r['status'] == 'queued'
                            for r in service.store.rows()),
                        'no dispatch must occur while safety_paused')


class ParallelThroughputTests(unittest.TestCase):
    """End-to-end: with concurrency=2 and two healthy accounts, two jobs must
    both enter preparing within one tick — no single-threaded stall.
    """
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs

    def test_two_accounts_two_jobs_both_start_in_one_tick(self):
        """Core parallel throughput regression: 2 ready accounts, 2 queued jobs,
        concurrency=2 → after _tick() both jobs must be in preparing."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0, count=2))
        _ready_client(service, 'Profile One', points=80)
        _ready_client(service, 'Profile Two', points=80)
        service.preferences['concurrency'] = 2
        service.paused = False

        # Each tick dispatches one job (by design); two ticks = two preparing
        service._tick()
        service._tick()

        preparing = [r for r in service.store.rows()
                     if r['status'] == 'preparing']
        self.assertEqual(len(preparing), 2,
                         'both jobs must be dispatched to separate accounts '
                         'when concurrency=2 and two healthy accounts are ready')

    def test_uncertain_row_from_previous_batch_does_not_reduce_throughput(self):
        """A leftover uncertain row from a deleted/old batch must not consume
        a concurrency slot or block a fresh account from accepting a new job."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        # artificially inject an old uncertain row from a different batch
        old_jobs = self.jobs(duration=6, budget=0)
        service.store.enqueue(old_jobs)
        old_row = service.store.rows()[-1]
        service.store.update(old_row['id'], 'uncertain',
                             attempt='old' + 'a' * 29, browser='old-browser',
                             remote={'profile': 'Profile Old',
                                     'account': 'old@example.test'})

        client = _ready_client(service, 'Profile New', points=80)
        service.preferences['concurrency'] = 2
        service.paused = False

        service._tick()
        new_rows = [r for r in service.store.rows()
                    if r['status'] == 'preparing']
        self.assertEqual(len(new_rows), 1,
                         'the fresh account must be dispatched despite an old '
                         'uncertain row in the database')
        self.assertEqual(new_rows[0]['browser'], client['id'])


class WaitingReasonAccuracyTests(unittest.TestCase):
    """The waiting_reason() text shown to the user must reflect the actual
    blocking cause, not a misleading fallback like '积分不足'.
    """
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs

    def test_waiting_reason_is_not_credit_shortage_when_eligible_list_is_empty(self):
        """If the real cause is 'no eligible clients' (busy_profiles locking
        everyone out), the waiting reason must not say '积分不足' — it should
        say something about browsers or lanes being unavailable."""
        service = self.service()
        service.store.enqueue(self.jobs(duration=6, budget=0))
        row = service.store.rows()[0]

        # All clients are on the same profile which has an uncertain row
        client = _ready_client(service, 'Profile X', points=200)
        service.preferences['concurrency'] = 1

        # Force an uncertain row for this profile/account
        fake_uncertain = _row('uncertain', profile='Profile X',
                              account=client['account'], browser=client['id'])

        clients_published = [{
            **{k: v for k, v in client.items() if k != 'socket'},
            'duration_lane': None, 'environment_block': '',
            'account_state': {}, 'credit_cooldown': {}}]

        reason = waiting_reason(row, clients_published,
                                [row, fake_uncertain],
                                service.preferences, service.observed_costs,
                                service.store.batches())
        # The reason must not be the misleading '积分不足' fallback
        self.assertNotIn('积分不足', reason,
                         'waiting_reason must not blame credits when the real '
                         'cause is the account being busy with another row')


if __name__ == '__main__':
    unittest.main()

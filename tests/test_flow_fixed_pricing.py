"""The user's three tariffs apply end-to-end, without live Flow billing."""
import unittest
from types import SimpleNamespace
from flow_reference_fixtures import upload_receipt

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.pricing import fixed_cost
from app_plugins.builtin.flow_assistant.policy import estimated_cost, cost_key
from app_plugins.builtin.flow_assistant.manual_panel import estimate_text
from app_plugins.builtin.flow_assistant.store import QueueStore, choose_client
from model.AppDataStorage import ApplicationStorage


class FixedPricingTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    def client(self, service, *args, **kwargs):
        value = fixtures.FlowTests.client(self, service, *args, **kwargs)
        value['account'] = value['account'].casefold()
        return value

    def prepared(self, service, client, page_cost=None):
        request = next(key for key, value in service.pending.items() if value['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': True, 'data': {
            'verified': True, 'model': 'omni_1_1_flash', 'cost': page_cost, 'email': client['account']}})

    def test_three_explicit_tariffs_override_stale_observations(self):
        for duration, expected in ((6, 10), (8, 12), (10, 15)):
            job = self.jobs(duration=duration)[0]
            self.assertEqual(fixed_cost(job), expected)
            self.assertEqual(estimated_cost(job, {cost_key(job): 2}), expected)
        for duration in (None, 4, 5, True, '8'):
            self.assertIsNone(fixed_cost({'model': 'omni_1_1_flash', 'duration': duration}))
        self.assertIsNone(fixed_cost({'model': 'other', 'duration': 8}))

    def test_prepare_works_without_page_cost_and_keeps_correct_cost_source(self):
        for duration, expected in ((6, 10), (8, 12), (10, 15)):
            with self.subTest(duration=duration):
                self.namespace = ApplicationStorage(self.root / f'private-{duration}').for_plugin('flow_assistant')
                service = self.service(); service.store.enqueue(self.jobs(duration=duration, budget=0))
                client = self.client(service, 'Profile ' + str(duration)); service.paused = False; service._tick()
                self.prepared(service, client)
                row = service.store.rows()[-1]
                self.assertEqual(row['status'], 'uploading', row['error'])
                self.assertEqual(row['remote']['cost'], expected)
                self.assertEqual(row['remote']['cost_source'], 'duration_rule')
                self.assertFalse(service.preferences.get('browser_blocks'))

    def test_user_fixed_rule_is_authoritative_even_when_page_reports_another_value(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0)); client = self.client(service)
        service.paused = False; service._tick(); self.prepared(service, client, page_cost=99)
        row = service.store.rows()[0]
        self.assertEqual(row['remote']['cost'], 15); self.assertEqual(row['remote']['cost_source'], 'duration_rule')
        self.assertFalse(service.observed_costs)

    def test_affordability_uses_fixed_cost_before_preparing_or_launching(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=8, budget=0)); client = self.client(service, credits=11)
        payload = service.store.rows()[0]['payload']
        self.assertIsNone(choose_client([client], [], 2, payload, {}, estimated_cost(payload, {})))
        service.paused = False; service._tick()
        self.assertFalse(any(request['action'] == 'prepare' for request in service.pending.values()))
        client['credits'] = 12; service._tick()
        self.assertTrue(any(request['action'] == 'prepare' for request in service.pending.values()))

    def test_batch_cap_and_balance_reservation_use_fixed_tariff(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2, duration=10, budget=0), limit_total=20)
        client = self.client(service); service.paused = False; service._tick(); self.prepared(service, client)
        request = next(key for key, value in service.pending.items() if value['action'] == 'upload_images')
        service._response(client, {'request_id': request, 'success': True, 'data': upload_receipt(service, service.pending[request]['job'])})
        row = service.store.rows()[0]; batch = service.store.batches()[row['payload']['batch_id']]
        self.assertEqual(batch['reserved'], 15); self.assertEqual(batch['fixed'], 1); self.assertEqual(batch['estimated'], 0)
        request = next(key for key, value in service.pending.items() if value['action'] == 'run_tasks')
        service._response(client, {'request_id': request, 'success': True})
        self.assertEqual(client['credits'], 35)
        service.store.update(row['id'], 'completed'); service._tick()
        self.assertEqual(service.store.rows()[1]['status'], 'queued')

    def test_underbudget_is_waiting_not_silently_increased(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=8, budget=11)); self.client(service)
        service.paused = False; service._tick(); service.publish()
        self.assertEqual(service.store.rows()[0]['status'], 'queued'); self.assertFalse(service.pending)

    def test_large_batch_estimate_uses_rule_without_observed_costs(self):
        text = estimate_text([{'images': ['image'] * 100, 'count': 3, 'duration': 8, 'aspect': '9:16', 'resolution': '720p'}], {})
        self.assertIn('300 条', text); self.assertIn('3600', text); self.assertIn('固定时长规则', text)
        self.assertNotIn('费用待页面确认', text)

    def test_old_missing_cost_block_is_retired_only_for_covered_durations(self):
        store = QueueStore(self.namespace); store.enqueue(self.jobs(duration=10, budget=0) + self.jobs(duration=4, budget=0))
        first, second = store.rows()
        for row, profile in ((first, 'Ten'), (second, 'Four')):
            store.update(row['id'], 'queued', remote={'environment_error': True, 'profile': profile})
        store.save_settings({'browser_blocks': {'profile:Ten': '页面费用无法读取，请填写单条积分预算或修正网页后恢复。',
            'profile:Four': '页面费用无法读取，请填写单条积分预算或修正网页后恢复。', 'profile:Other': '模型核对失败'}})
        # Exercise the price migration itself, before the separate new-session
        # browser reset. Startup intentionally rechecks all automatic faults.
        from app_plugins.builtin.flow_assistant.operations import FlowOperations
        service = SimpleNamespace(store=store, preferences=store.settings())
        FlowOperations.recover_fixed_price_blocks(service)
        self.assertNotIn('profile:Ten', service.preferences['browser_blocks'])
        self.assertIn('profile:Four', service.preferences['browser_blocks'])
        self.assertIn('profile:Other', service.preferences['browser_blocks'])
        self.assertFalse(service.store.get(first['id'])['remote']['environment_error'])
        self.assertTrue(service.store.get(second['id'])['remote']['environment_error'])


if __name__ == '__main__': unittest.main()

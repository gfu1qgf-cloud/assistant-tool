"""Manual submissions must fill distinct accounts despite preflight failures."""
import time
import unittest
from unittest.mock import patch
from flow_reference_fixtures import upload_receipt, distinct_jobs

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.store import choose_client
from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare


class ManualParallelismTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def browser(self, service, index, version='1.1.17', credits=70):
        client = self.client(service, 'Profile ' + str(index), credits)
        url = 'https://labs.google/fx/tools/flow/project/fixture-' + str(index)
        client.update(account=f'fixture{index}@example.test', project=url, saved_project=url,
            extension_version=version, page_ready=True, page_at=time.time(), credits_local=True,
            batch_running=False)
        service.preferences.setdefault('profile_projects', {})[client['profile']] = url
        return client

    def snapshot(self, service, client):
        service._snapshot(client, {'url': client['saved_project'], 'running': False,
            'controls_ready': True, 'tasks': []})

    def submit(self, service, duration=10, count=8):
        for index, job in enumerate(distinct_jobs(self, count, duration=duration, budget=0)):
            service.execute({'action': 'enqueue', 'source': 'manual', 'request_id': 'manual-' + str(index),
                'items': [{**self.args, 'images':[job['image']], 'prompt': f'manual fixture {index}', 'duration': duration, 'budget': 0}]})

    def acknowledge(self, service, client):
        for action in ('prepare', 'upload_images', 'run_tasks'):
            request = next(key for key, item in service.pending.items()
                if item['client'] == client['id'] and item['action'] == action)
            service._response(client, {'request_id': request, 'success': True,
                'data': {'verified': True, 'model': 'omni_1_1_flash', 'email': client['account'], **(upload_receipt(service, service.pending[request]['job']) if action == 'upload_images' else {})}})

    def test_manual_submissions_fill_four_slots_after_compatible_settings_fix(self):
        service = self.service()
        service.preferences.update(concurrency=4, auto_lane=True)
        clients = [self.browser(service, i, version='1.1.16', credits=credits)
                   for i, credits in enumerate((35, 55, 75, 90))]
        service.preferences['profile_lanes'] = {c['profile']: 8 for c in clients}
        service.preferences['browser_blocks'] = {'profile:' + c['profile']:
            '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。' for c in clients[:3]}
        for client in clients:
            self.snapshot(service, client)
        self.submit(service)
        self.assertTrue(service.paused, 'manual releases must work while the global queue remains paused')
        for _ in range(5):
            service._tick()
        active = [row for row in service.store.rows() if row['status'] == 'preparing']
        self.assertEqual(len(active), 4)
        self.assertEqual(len({row['browser'] for row in active}), 4)
        order = [row['browser'] for row in active]
        self.assertEqual(order, [c['id'] for c in clients], 'small balances lead, rich account fills the fourth slot')
        for client in clients:
            self.acknowledge(service, client)
        self.assertEqual(sum(r['status'] == 'generating' for r in service.store.rows()), 4)
        self.assertEqual(sum(r['status'] == 'queued' for r in service.store.rows()), 4)
        for _ in range(3):
            service._tick()
        self.assertFalse(any(item['action'] == 'prepare' for item in service.pending.values()))

    def test_preflight_wait_applies_to_browser_before_next_queued_item(self):
        service = self.service(); first = self.browser(service, 1); second = self.browser(service, 2)
        self.submit(service, count=4); service._tick()
        request = next(key for key, item in service.pending.items() if item['action'] == 'prepare')
        owner = next(c for c in (first, second) if c['id'] == service.pending[request]['client'])
        service._response(owner, {'request_id': request, 'success': False,
            'code': 'FLOW_SETTINGS_NOT_READY', 'error': '设置面板正在重建'})
        self.snapshot(service, owner)
        for _ in range(3):
            service._tick()
        preparations = [item for item in service.pending.values() if item['action'] == 'prepare']
        self.assertEqual(len(preparations),1,'the same FIFO head can use another ready account during this browser backoff')
        self.assertEqual(preparations[0]['job'],service.store.rows()[0]['id'])
        self.assertNotEqual(preparations[0]['client'], owner['id'])
        self.assertFalse(service.preferences.get('browser_blocks'))

    def test_retry_count_belongs_to_browser_not_the_task_that_moves(self):
        service = self.service(); first = self.browser(service, 1); second = self.browser(service, 2)
        self.submit(service, count=1); service._tick()
        row = service.store.rows()[0]
        defer_prepare(service, first, row, '面板加载中', 'FLOW_SETTINGS_NOT_READY')
        defer_prepare(service, first, service.store.get(row['id']), '面板加载中', 'FLOW_SETTINGS_NOT_READY')
        defer_prepare(service, second, service.store.get(row['id']), '面板加载中', 'FLOW_SETTINGS_NOT_READY')
        self.assertEqual(service.store.get(row['id'])['remote']['prepare_retry']['count'], 1)
        self.assertFalse(service.preferences.get('browser_blocks'))

    def test_one_unsupported_duration_does_not_disable_other_work(self):
        service = self.service(); client = self.browser(service, 1, version='1.1.18')
        self.submit(service, count=1); service._tick()
        request = next(key for key, item in service.pending.items() if item['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': False,
            'code': 'FLOW_DURATION_UNSUPPORTED', 'error': '当前模型不支持 10 秒',
            'data': {'duration': 10, 'available_durations': [6, 8]}})
        self.assertFalse(service.preferences.get('browser_blocks'), 'a duration limit is not a broken browser')
        service.execute({'action': 'enqueue', 'source': 'manual', 'items': [{**self.args,
            'prompt': 'supported duration', 'duration': 8, 'budget': 0}]})
        for _ in range(3):
            service._tick()
        rows = service.store.rows()
        self.assertEqual([r['status'] for r in rows], ['queued', 'queued'])
        service.execute({'action':'cancel','ids':[rows[0]['id']]});service._tick()
        self.acknowledge(service, client)
        self.assertEqual(service.store.rows()[1]['status'], 'generating')
        self.assertEqual(service.store.batches()[rows[0]['payload']['batch_id']]['reserved'], 0)

    def test_startup_credit_timeout_recovers_after_real_page_readiness(self):
        service = self.service(); client = self.browser(service, 1)
        client.update(account='', credits=None, credits_local=False)
        service.request(client, 'get_points')
        key = next(key for key, item in service.pending.items() if item['action'] == 'get_points')
        service.pending[key]['at'] = time.time() - 61
        service._tick()
        client['credits_retry_at'] = 0
        self.snapshot(service, client)
        pending = [(key, item) for key, item in service.pending.items() if item['action'] == 'get_points']
        self.assertEqual(len(pending), 1, 'timeout during four-window startup must rearm the initial probe')
        service._response(client, {'request_id': pending[0][0], 'success': True,
            'data': {'email': 'fixture1@example.test', 'remaining_points': 70}})
        self.submit(service, count=1); service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_default_duration_can_use_idle_automatic_lanes(self):
        service = self.service(); clients = [self.browser(service, i) for i in range(4)]
        service.preferences.update(profile_lanes={c['profile']: 8 for c in clients}, auto_lane=True)
        selected = choose_client(clients, [], 4, {'duration': None}, service.preferences)
        self.assertIsNotNone(selected)
        service.preferences['profile_lane_modes'] = {c['profile']: 'fixed' for c in clients}
        self.assertIsNone(choose_client(clients, [], 4, {'duration': None}, service.preferences))

    def test_new_duration_panel_fix_rechecks_only_exact_unpaid_legacy_error(self):
        service = self.service(); client = self.browser(service, 1, version='1.1.18')
        key = 'profile:' + client['profile']
        error = '找不到视频时长选项，无法确认 10 秒；请检查当前模型的设置面板'
        service.preferences['browser_blocks'] = {key: error}
        self.snapshot(service, client)
        self.assertNotIn(key, service.preferences['browser_blocks'])
        service.preferences['browser_blocks'][key] = '当前模型不支持 10 秒'
        self.snapshot(service, client)
        self.assertEqual(service.preferences['browser_blocks'][key], '当前模型不支持 10 秒')


if __name__ == '__main__':
    unittest.main()

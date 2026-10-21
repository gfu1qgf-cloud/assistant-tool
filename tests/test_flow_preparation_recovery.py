"""Stale pre-submit failures must not silently reduce account concurrency."""
import time
import unittest
from unittest.mock import patch
from flow_reference_fixtures import upload_receipt, distinct_jobs

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION

LEGACY = '无法确认页面已选中 Omni 1.1 Flash、比例、分辨率和 x1，未提交生成。'
TIMEOUT = '浏览器响应超时，请检查页面后恢复；未发送新的生成请求。'


class PreparationRecoveryTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def browser(self, service, profile='Profile 104'):
        client = self.client(service, profile, credits=90)
        url = 'https://labs.google/fx/tools/flow/project/' + profile.replace(' ', '-')
        client.update(project=url, saved_project=url, extension_version=EXTENSION_VERSION,
                      page_ready=True, page_at=time.time(), credits_local=True,
                      account=profile.replace(' ', '').lower() + '@example.test')
        service.preferences.setdefault('profile_projects', {})[profile] = url
        return client

    def page(self, service, client, **updates):
        service._snapshot(client, {'url': client['saved_project'], 'tasks': [],
                                  'running': False, 'controls_ready': True, **updates})

    def test_four_accounts_recheck_orphan_blocks_and_fill_four_slots(self):
        service = self.service()
        service.preferences['concurrency'] = 4
        clients = [self.browser(service, 'Profile ' + str(i)) for i in range(4)]
        service.preferences['browser_blocks'] = {
            ('browser:' + c['id']): LEGACY if i < 2 else TIMEOUT for i, c in enumerate(clients)}
        service.store.enqueue(distinct_jobs(self, 6, duration=8, budget=0))
        for client in clients:
            self.page(service, client)
        self.assertFalse(service.preferences['browser_blocks'])
        self.assertTrue(service.paused, 'read-only recovery must not start a paused queue')
        self.assertFalse(service.pending)
        service.execute({'action': 'start'})
        for _ in range(5):
            service._tick()
        preparing = [row for row in service.store.rows() if row['status'] == 'preparing']
        self.assertEqual(len(preparing), 4)
        self.assertEqual(len({r['browser'] for r in preparing}), 4)
        self.assertEqual({p['action'] for p in service.pending.values()}, {'prepare'})
        self.assertEqual(sum(row['status'] == 'queued' for row in service.store.rows()), 2)
        # Drive four separate mocked browser protocols all the way to the
        # generation acknowledgements, with distinct account debits and batches.
        for client in clients:
            key = next(k for k, p in service.pending.items() if p['client'] == client['id'] and p['action'] == 'prepare')
            service._response(client, {'request_id': key, 'success': True,
                'data': {'verified': True, 'model': 'omni_1_1_flash', 'email': client['account']}})
            key = next(k for k, p in service.pending.items() if p['client'] == client['id'] and p['action'] == 'upload_images')
            service._response(client, {'request_id': key, 'success': True, 'data': upload_receipt(service, service.pending[key]['job'])})
            key = next(k for k, p in service.pending.items() if p['client'] == client['id'] and p['action'] == 'run_tasks')
            service._response(client, {'request_id': key, 'success': True, 'data': {}})
        self.assertEqual(sum(r['status'] == 'generating' for r in service.store.rows()), 4)
        self.assertEqual(len({r['attempt'] for r in service.store.rows() if r['status'] == 'generating'}), 4)
        for _ in range(3):
            service._tick()
        self.assertEqual(sum(r['status'] == 'generating' for r in service.store.rows()), 4)
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))

    def test_recovery_still_requires_actual_parameter_verification(self):
        service = self.service(); client = self.browser(service)
        service.preferences['browser_blocks'] = {'profile:' + client['profile']: LEGACY}
        service.store.enqueue(self.jobs(duration=8, budget=0))
        self.page(service, client)
        service.paused = False; service._tick()
        request = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': False,
            'code': 'FLOW_SETTINGS_MISMATCH', 'error': '实际已选中的输出数量为 x2'})
        self.assertTrue(service.preferences['browser_blocks'])
        self.assertFalse(any(p['action'] in {'run_tasks', 'upload_images'} for p in service.pending.values()))
        self.assertEqual(next(iter(service.store.batches().values()))['reserved'], 0)

    def test_only_exact_old_pre_submit_blocks_recover_once_per_connection(self):
        service = self.service(); client = self.browser(service)
        key = 'browser:' + client['id']
        for error in ('需要登录', 'captcha', '当前模型不支持8秒', '实际参数不一致', TIMEOUT + 'captcha'):
            service.preferences['browser_blocks'] = {key: error}
            self.page(service, client)
            self.assertEqual(service.preferences['browser_blocks'][key], error)
        service.preferences['browser_blocks'] = {key: LEGACY}
        self.page(service, client)
        self.assertNotIn(key, service.preferences['browser_blocks'])
        self.assertEqual(service.preferences['preparation_block_history'][-1]['error'], LEGACY)
        service.preferences['browser_blocks'] = {key: LEGACY}
        self.page(service, client)
        self.assertEqual(service.preferences['browser_blocks'][key], LEGACY)

    def test_busy_wrong_project_old_extension_and_unknown_results_stay_protected(self):
        service = self.service(); client = self.browser(service)
        key = 'profile:' + client['profile']
        service.preferences['browser_blocks'] = {key: LEGACY}
        self.page(service, client, running=None)
        self.assertIn(key, service.preferences['browser_blocks'], 'missing busy state is not explicit idle proof')
        for updates in ({'running': True}, {'controls_ready': False},
                        {'url': 'https://labs.google/fx/tools/flow/project/other'}):
            self.page(service, client, **updates)
            self.assertIn(key, service.preferences['browser_blocks'])
        client['extension_version'] = '1.1.13'
        self.page(service, client)
        self.assertIn(key, service.preferences['browser_blocks'])
        client['extension_version'] = EXTENSION_VERSION
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        service.store.update(row['id'], 'uncertain', attempt='a'*32, browser=client['id'],
                             remote={'account': client['account']})
        self.page(service, client)
        self.assertIn(key, service.preferences['browser_blocks'])
        self.assertEqual(service.store.get(row['id'])['attempt'], 'a'*32)

    def test_slow_prepare_gets_full_deadline_then_bounded_pre_submit_retry(self):
        service = self.service(); client = self.browser(service)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        service.paused = False; service._tick()
        request = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        service.pending[request]['at'] = time.time() - 65
        service._tick()
        self.assertIn(request, service.pending, '45-second readiness plus settings may exceed 60 seconds')
        service.pending[request]['at'] = time.time() - 111
        service._tick()
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(row['remote']['prepare_retry']['count'], 1)
        self.assertFalse(service.preferences.get('browser_blocks'))

    def test_missing_settings_retry_is_bounded_and_does_not_affect_other_accounts(self):
        service = self.service(); one = self.browser(service, 'Profile 1')
        two = self.browser(service, 'Profile 2')
        service.store.enqueue(distinct_jobs(self, 2, duration=8, budget=0))
        service.paused = False; service._tick()
        key = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        owner = next(c for c in (one, two) if c['id'] == service.pending[key]['client'])
        service._response(owner, {'request_id': key, 'success': False,
            'code': 'FLOW_SETTINGS_NOT_READY', 'error': '设置面板正在重建'})
        row = next(r for r in service.store.rows() if r['remote'].get('prepare_retry'))
        self.assertEqual(row['remote']['prepare_retry']['count'], 1)
        self.assertFalse(service.preferences.get('browser_blocks'))
        service._tick()
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))
        with patch('time.time',return_value=row['remote']['prepare_retry']['next_at']+.1):service._tick()
        self.assertTrue(any(p['action'] == 'prepare' and p['client'] != owner['id'] for p in service.pending.values()))
        from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare
        for _ in range(3):
            defer_prepare(service, owner, service.store.get(row['id']), '设置面板正在重建', 'FLOW_SETTINGS_NOT_READY')
        self.assertTrue(service.preferences['browser_blocks'])

    def test_late_prepare_reply_or_timeout_cannot_requeue_same_attempt_after_submission(self):
        service = self.service(); client = self.browser(service)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        service.paused = False; service._tick()
        key = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        row = service.store.rows()[0]
        service.store.update(row['id'], 'generating')
        before = service.store.get(row['id'])
        service._response(client, {'request_id': key, 'success': False, 'code': 'FLOW_SETTINGS_NOT_READY'})
        self.assertEqual(service.store.get(row['id']), before)
        service.request(client, 'prepare', {}, row['id'])
        key = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        service.pending[key]['at'] = time.time() - 111
        service._tick()
        self.assertEqual(service.store.get(row['id']), before)


if __name__ == '__main__':
    unittest.main()

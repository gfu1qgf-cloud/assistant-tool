import time
import unittest

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare, watch_preparation, missing_window

PROJECT = 'https://flow.google.com/project/confirmed-project'


class PageReadinessTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def setup_job(self):
        service = self.service(); client = self.client(service, 'Profile 23')
        client.update(project=PROJECT, saved_project=PROJECT, page_ready=True)
        service.preferences['profile_projects'] = {'Profile 23': PROJECT}
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        service.store.update(row['id'], 'preparing', attempt='a'*32, browser=client['id'],
            remote={'profile': client['profile'], 'account': client['account']})
        return service, client, service.store.get(row['id'])

    def test_page_loaded_is_not_controls_ready(self):
        service, client, row = self.setup_job()
        service.store.update(row['id'], 'queued', attempt='', browser='')
        service._snapshot(client, {'url': PROJECT, 'tasks': [], 'controls_ready': False,
            'ready_note': '正在等待控件稳定'})
        self.assertTrue(client['page_loaded']); self.assertFalse(client['page_ready'])
        service.paused = False; service._tick()
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertFalse(any(r['action'] == 'prepare' for r in service.pending.values()))

    def test_account_probe_recovers_boundedly_without_paid_submission(self):
        service, client, row = self.setup_job()
        client.update(account='previous@example.test', credits=50, credits_local=True)
        service.request(client, 'get_points')
        for attempt in range(3):
            key = next(k for k, r in service.pending.items() if r['action'] == 'get_points')
            service._response(client, {'request_id': key, 'success': False,
                'code': 'FLOW_ACCOUNT_NOT_READY', 'data': {'remaining_points': 50, 'email': ''}})
            self.assertEqual(client['account'], 'previous@example.test')
            self.assertEqual(client['credits'], 50, 'the last confirmed number remains visible')
            self.assertTrue(client['identity_unconfirmed'], 'an unreadable fresh account must not authorize the previous login')
            service._snapshot(client, {'url': PROJECT, 'controls_ready': True, 'tasks': []})
            self.assertFalse(service.pending, 'wait for cooldown before reopening account menu')
            client['credits_retry_at'] = 0
            for _ in range(4):
                service._snapshot(client, {'url': PROJECT, 'controls_ready': True, 'tasks': []})
            self.assertEqual(len(service.pending), 1 if attempt < 2 else 0)
        self.assertIn('3次', client['note'])
        self.assertEqual(service.store.get(row['id'])['status'], 'preparing')

    def test_successful_account_probe_stops_recovery(self):
        service, client, row = self.setup_job()
        client.update(account='', credits=None, credits_ready_failures=1,
            credits_waiting_ready=True, credits_retry_at=0)
        service._snapshot(client, {'url': PROJECT, 'controls_ready': True, 'tasks': []})
        key = next(k for k, r in service.pending.items() if r['action'] == 'get_points')
        service._response(client, {'request_id': key, 'success': True,
            'data': {'remaining_points': 50, 'email': 'actual@example.test'}})
        for _ in range(10):
            service._snapshot(client, {'url': PROJECT, 'controls_ready': True, 'tasks': []})
        self.assertEqual(client['account'], 'actual@example.test')
        self.assertEqual(client['credits'], 50)
        self.assertFalse(service.pending)

    def test_account_retry_does_not_wait_for_generation_editor(self):
        service, client, row = self.setup_job()
        service.request(client, 'get_points')
        key = next(k for k, r in service.pending.items() if r['action'] == 'get_points')
        service._response(client, {'request_id': key, 'success': False, 'code': 'FLOW_ACCOUNT_NOT_READY'})
        client['credits_retry_at'] = 0
        service._snapshot(client, {'url': PROJECT, 'controls_ready': False, 'tasks': []})
        self.assertFalse(client['page_ready'])
        self.assertEqual([r['action'] for r in service.pending.values()], ['get_points'])

    def test_commands_request_readiness_without_repeated_credit_queries(self):
        service, client, row = self.setup_job()
        service.request(client, 'prepare', {}, row['id'])
        import json
        message = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertTrue(message['params']['wait_for_ready'])
        service.request(client, 'get_points')
        message = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertNotIn('wait_for_ready', message['params'], 'account header works before generation controls load')

    def test_compatible_patches_do_not_block_working_browsers(self):
        from app_plugins.builtin.flow_assistant.setup_support import project_block, compatible_extension
        service, client, row = self.setup_job()
        for version in ('1.1.8', '1.1.9', '1.1.10', '1.1.11'):
            client['extension_version'] = version
            self.assertTrue(compatible_extension(version))
            self.assertEqual(project_block(client, service.preferences), '')
        for version in ('1.1.4', '1.1.7', '2.0.0', 'broken'):
            client['extension_version'] = version
            self.assertTrue(project_block(client, service.preferences))

    def test_no_current_window_before_submission_waits_and_reopens_once(self):
        service, client, row = self.setup_job(); launches = []
        service.launch_requested.connect(launches.append)
        service.request(client, 'prepare', {}, row['id'])
        key = next(k for k, v in service.pending.items() if v['action'] == 'prepare')
        service._response(client, {'request_id': key, 'success': False, 'error': 'No current window'})
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'queued'); self.assertEqual(current['attempt'], '')
        self.assertFalse(service.preferences.get('browser_blocks'))
        self.assertEqual(current['remote']['prepare_retry']['count'], 1)
        self.assertFalse(watch_preparation(service)); self.assertEqual(launches, [])
        recovery = {**current['remote']['prepare_retry'], 'next_at': 0}
        service.store.update(row['id'], 'queued', remote={**current['remote'], 'prepare_retry': recovery})
        self.assertTrue(watch_preparation(service)); self.assertFalse(watch_preparation(service))
        self.assertEqual(launches, ['Profile 23'])
        self.assertFalse(any(v['action'] == 'run_tasks' for v in service.pending.values()))

    def test_prepare_wait_is_bounded_and_count_survives_next_attempt(self):
        service, client, row = self.setup_job()
        for count in range(1, 4):
            self.assertTrue(defer_prepare(service, client, row, 'waiting', 'FLOW_PAGE_NOT_READY'))
            row = service.store.get(row['id'])
            self.assertEqual(row['remote']['prepare_retry']['count'], count)
            recovery = {**row['remote']['prepare_retry'], 'next_at': 0}
            client['prepare_wait'] = recovery
            service.store.update(row['id'], 'queued', remote={**row['remote'], 'prepare_retry': recovery})
            service._snapshot(client, {'url': PROJECT, 'tasks': [], 'controls_ready': True})
            service.paused = False; service._tick(); row = service.store.get(row['id'])
            self.assertEqual(row['status'], 'preparing')
            self.assertEqual(row['remote']['prepare_retry']['count'], count)
        self.assertTrue(defer_prepare(service, client, row, 'waiting', 'FLOW_PAGE_NOT_READY'))
        self.assertTrue(service.preferences.get('browser_blocks'))
        self.assertIn('3次', service.store.get(row['id'])['error'])

    def test_post_submit_window_error_never_returns_task_to_queue(self):
        service, client, row = self.setup_job()
        service.store.update(row['id'], 'submitting')
        service.request(client, 'run_tasks', {}, row['id'])
        key = next(k for k, v in service.pending.items() if v['action'] == 'run_tasks')
        service._response(client, {'request_id': key, 'success': False, 'error': 'No current window'})
        row = service.store.get(row['id'])
        self.assertEqual(row['status'], 'uncertain')
        self.assertNotIn('prepare_retry', row['remote'])

    def test_old_window_block_recovers_only_its_pre_submit_job(self):
        service, client, row = self.setup_job()
        service.quarantine(client, row, 'No current window')
        service.preferences['browser_blocks']['profile:other'] = 'No current window'
        service.preferences['browser_blocks']['browser:'+client['id']] = '需要登录'
        service._snapshot(client, {'url': PROJECT, 'tasks': [], 'controls_ready': False})
        self.assertIn('profile:Profile 23', service.preferences['browser_blocks'])
        service._snapshot(client, {'url': PROJECT, 'tasks': [], 'controls_ready': True})
        self.assertNotIn('profile:Profile 23', service.preferences['browser_blocks'])
        self.assertEqual(service.preferences['browser_blocks']['profile:other'], 'No current window')
        self.assertEqual(service.preferences['browser_blocks']['browser:'+client['id']], '需要登录')
        row = service.store.get(row['id'])
        self.assertEqual(row['status'], 'queued'); self.assertTrue(row['remote']['window_block_recovered'])

    def test_real_config_error_is_not_misclassified_as_temporary(self):
        service, client, row = self.setup_job()
        self.assertFalse(defer_prepare(service, client, row, '当前模型不支持10秒'))
        self.assertFalse(missing_window('captcha required; No current window'))

    def test_initial_credit_probe_retries_only_after_controls_recover(self):
        service, client, row = self.setup_job()
        client['credits_local'] = True
        service.request(client, 'get_points')
        key = next(k for k, r in service.pending.items() if r['action'] == 'get_points')
        before = client['credits']
        service._response(client, {'request_id': key, 'success': False,
            'error': 'No current window', 'code': 'FLOW_PAGE_NOT_READY'})
        self.assertEqual(client['credits'], before, 'a temporary window failure must not erase the local balance')
        service._snapshot(client, {'url': PROJECT, 'controls_ready': False, 'tasks': []})
        self.assertFalse(any(r['action'] == 'get_points' for r in service.pending.values()))
        for _ in range(10):
            service._snapshot(client, {'url': PROJECT, 'controls_ready': True, 'tasks': []})
        self.assertEqual(sum(r['action'] == 'get_points' for r in service.pending.values()), 1,
                         'readiness snapshots must not repeatedly open the credits menu')

    def test_site_warning_pauses_queue_without_touching_paid_generation(self):
        import json
        service, client, row = self.setup_job()
        service.store.update(row['id'], 'generating')
        service.paused = False
        service._message(client['socket'], json.dumps({'type': 'site_pause'}))
        self.assertTrue(service.paused); self.assertTrue(service.safety_paused)
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'generating'); self.assertEqual(current['attempt'], row['attempt'])
        self.assertFalse(any(r['action'] in {'refresh_project', 'run_tasks'} for r in service.pending.values()))


if __name__ == '__main__': unittest.main()

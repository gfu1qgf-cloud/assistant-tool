"""Covered-window flags and delayed original-result recovery, with no live browser."""
import copy
import time
import unittest
from unittest.mock import patch

from model.ChromeFlowRuntime import FLOW_BACKGROUND_FLAGS, background_flags
from PYUI.chrome_runner_pyui import ChromeRunnerDialog
import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info, retry_detail


class FlowBackgroundLaunchTests(unittest.TestCase):
    def test_real_launcher_carries_flags_on_flow_queue_and_preset_paths(self):
        launcher = type('Launcher', (), {'chrome_path': 'chrome.exe',
            'get_web_list': lambda _: ['https://labs.google/fx/tools/flow/project/original']})()
        for kwargs in ({}, {'urls': ['https://flow.google.com/project/original#lzx_setup=ticket'], 'new_window': False}):
            with self.subTest(kwargs=kwargs), patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
                self.assertEqual(ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'}, **kwargs), (True, None))
                command = popen.call_args.args[0]
                self.assertTrue(all(command.count(flag) == 1 for flag in FLOW_BACKGROUND_FLAGS))
                if kwargs: self.assertNotIn('--new-window', command)
                self.assertIn('--profile-directory=Profile 23', command)

    def test_unrelated_and_lookalike_urls_do_not_change_browser_runtime(self):
        for url in ['https://example.test/', 'https://flow.google.com.evil.test/',
                    'https://labs.google/fx/tools/flow-other/', 'http://flow.google.com/',
                    'https://flow.google.com:123/', 'https://user:pass@flow.google.com/', 'https://flow.google.com:invalid/']:
            with self.subTest(url=url): self.assertEqual(background_flags([url]), [])
        self.assertEqual(background_flags(['https://labs.google/fx/en-US/tools/flow/project/test']), list(FLOW_BACKGROUND_FLAGS))


class FlowLateResultTests(unittest.TestCase):
    setUpClass = fixtures.TransferTests.__dict__['setUpClass']
    setUp = fixtures.TransferTests.setUp
    service = fixtures.TransferTests.service
    jobs = fixtures.TransferTests.jobs
    client = fixtures.TransferTests.client
    bound_row = fixtures.TransferTests.bound_row
    actions = fixtures.TransferTests.actions

    def waiting(self):
        service = self.service(); row, client = self.bound_row(service)
        original = copy.deepcopy(row['remote']['bindings'])
        pending = [{**b, 'readyIds': [], 'urls': {}} for b in original]
        service.store.update(row['id'], 'download_failed', remote={**row['remote'], 'bindings': pending})
        return service, service.store.get(row['id']), client, original

    def snapshot(self, service, client, row, bindings):
        service._snapshot(client, {'batch_id': row['attempt'], 'running': False, 'tasks': [{
            'taskId': row['remote']['upstream_task'], 'download_path': service.download_name(row),
            'mediaBindings': bindings, 'status': ['failed'], 'downloadIds': []}]})

    def test_pending_snapshot_does_not_consume_more_probes(self):
        service, row, client, _ = self.waiting()
        service.retry_download(row, automatic=True)
        current = service.store.get(row['id'])
        self.assertIn('视频结果核对 1/10', retry_detail(current, service.preferences))
        for _ in range(12):
            service._save_transfer(current, 'download', {**retry_info(current, 'download'), 'probe_at': 0})
            service.retry_download(service.store.get(row['id']), automatic=True)
        self.assertEqual(sum(a['action'] == 'snapshot' for a in self.actions(client)), 1)
        self.assertEqual(retry_info(service.store.get(row['id']), 'download')['probes'], 1)

    def test_late_verified_result_is_retained_without_reoccupying_skipped_download(self):
        service, row, client, original = self.waiting()
        service.preferences['timing'] = {'download_probe_attempts': 1}
        service.retry_download(row, automatic=True)
        current = service.store.get(row['id'])
        self.snapshot(service, client, current, current['remote']['bindings'])
        service._save_transfer(current, 'download', {**retry_info(current, 'download'), 'probe_at': 0})
        service.retry_download(service.store.get(row['id']), automatic=True)
        self.assertTrue(retry_info(service.store.get(row['id']), 'download')['stopped'])
        self.snapshot(service, client, service.store.get(row['id']), original)
        current = service.store.get(row['id']); info = retry_info(current, 'download')
        self.assertTrue(info['stopped']); self.assertEqual(info['count'], 0)
        self.assertEqual(current['status'], 'download_parked')
        self.assertEqual(current['remote']['bindings'], original)
        service.watch_transfer_retries()
        actions = self.actions(client)
        self.assertEqual(sum(a['action'] == 'retry_download' for a in actions), 0)
        self.assertNotIn('run_tasks', [a['action'] for a in actions])

    def test_download_limits_disabled_retries_and_foreign_results_do_not_resume(self):
        for mode in ('exhausted', 'disabled', 'foreign', 'other_error'):
            with self.subTest(mode=mode):
                service, row, client, original = self.waiting()
                info = {'count': 5 if mode == 'exhausted' else 0, 'stopped': True,
                        'stop_code': 'other' if mode == 'other_error' else 'binding_probe_limit', 'next_at': 0}
                service._save_transfer(row, 'download', info)
                if mode == 'disabled': service.preferences['download_retry_limit'] = 0
                if mode == 'foreign': original[0]['mediaIds'] = original[0]['readyIds'] = ['foreign-media']
                self.snapshot(service, client, service.store.get(row['id']), original)
                self.assertTrue(retry_info(service.store.get(row['id']), 'download')['stopped'])
                service.watch_transfer_retries()
                self.assertFalse(any(a['action'] in {'retry_download', 'run_tasks'} for a in self.actions(client)))


if __name__ == '__main__': unittest.main()

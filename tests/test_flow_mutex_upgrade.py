"""A compatible protocol cannot hide a missing required UI capability."""
import time
import json
import unittest
from unittest.mock import MagicMock

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_reuse import before_new_browser
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class MutexUpgradeTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs
    ui = fixtures.CreditCooldownTests.ui

    def test_connection_guide_does_not_finish_before_new_capability_handshake(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        client = {'id':'guide', 'profile':'Profile 23', 'saved_project':fixtures.PROJECT,
            'project':fixtures.PROJECT, 'page_ready':True, 'extension_version':'1.1.37', 'capabilities':[]}
        state = {'clients':[client], 'preferences':{'browser_options':{'ui_mutex':True},
            'profile_projects':{'Profile 23':fixtures.PROJECT}}, 'jobs':[]}
        setup.phase = 'auto_pair'; setup.connection_timer.start(10000)
        setup.render_connection(state)
        self.assertEqual(setup.phase, 'auto_pair')
        self.assertTrue(setup.connection_timer.isActive())
        client['capabilities'] = ['ui_mutex_v1']; client['extension_version'] = EXTENSION_VERSION
        setup.render_connection(state)
        self.assertEqual(setup.phase, '')
        self.assertFalse(setup.connection_timer.isActive())

    def configured(self):
        service = self.service(); service.timer.stop(); service.paused = False
        service.preferences['browser_options'] = {'ui_mutex': True}
        client = self.browser(service, 'Old')
        client.update(extension_version='1.1.37', credits_at=time.time()-100000)
        service.preferences.update(profiles=['Old', 'Backup'], concurrency=2)
        service.preferences['profile_projects']['Backup'] = fixtures.PROJECT
        service.store.enqueue(self.jobs(duration=8, count=3, budget=0))
        return service, client

    def connect(self, service, client):
        results = []; service.setup_ready.connect(results.append)
        service.execute({'action':'setup_profile', 'request_id':'connect',
            'profile':client['profile'], 'project':client['saved_project']})
        return results[-1]

    def test_compatible_old_bridge_connection_gets_idle_reload_ticket(self):
        service, client = self.configured()
        result = self.connect(service, client)
        self.assertTrue(result['success'])
        self.assertFalse(result.get('connected'), 'must not falsely report usable connection')
        self.assertEqual(service.setup_requests['Old']['mode'], 'reload_only')
        self.assertIn('lzx_action=options', result['project_claim_url'])

    def test_same_version_missing_capability_also_gets_reload_ticket(self):
        service, client = self.configured(); client['extension_version'] = EXTENSION_VERSION
        result = self.connect(service, client)
        self.assertFalse(result.get('connected'))
        self.assertEqual(service.setup_requests['Old']['mode'], 'reload_only')

    def test_required_upgrade_keeps_queued_jobs_and_stops_backup_window_growth(self):
        service, client = self.configured(); before = service.store.rows()
        launches = []; service.launch_requested.connect(launches.append)
        for _ in range(3): service.fill_dispatch_slots()
        self.assertFalse(launches)
        self.assertNotEqual(client.get('reuse_recovery', {}).get('state'), 'stopped')
        self.assertEqual(service.store.rows(), before)
        self.assertIn('扩展', service.browser_reuse_wait['message'])
        self.assertIn(EXTENSION_VERSION, service.browser_reuse_wait['message'])

    def test_browser_status_identifies_upgrade_before_generic_credit_wait(self):
        service, client = self.configured()
        status = browser_status(client, service.preferences)
        self.assertEqual(status['state'], '扩展需更新')
        self.assertIn(EXTENSION_VERSION, status['reason'])
        self.assertIn('连接', status['reason'])

    def test_mutex_disabled_preserves_compatible_old_bridge(self):
        service, client = self.configured(); service.preferences['browser_options']['ui_mutex'] = False
        self.assertTrue(self.connect(service, client).get('connected'))

    def test_successful_reload_handshake_releases_old_stopped_recovery(self):
        from app_plugins.builtin.flow_assistant.browser_reuse import abandon, stopped
        service, client = self.configured(); abandon(service, client, 'old unsupported bridge')
        self.connect(service, client)
        socket = MagicMock()
        service.clients[socket] = {'socket':socket, 'authorized':False, 'connected':time.time()}
        service._message(socket, json.dumps({'type':'hello','protocol':1,'token':'x'*40,
            'browser_id':client['id'], 'profile':client['profile'], 'project':client['saved_project'],
            'extension_version':EXTENSION_VERSION, 'capabilities':['ui_mutex_v1']}))
        current = service.clients[socket]
        self.assertFalse(stopped(service, current))
        self.assertTrue(any(p['client']==current['id'] and p['action']=='get_points'
                            for p in service.pending.values()))

    def test_capable_connected_browser_still_receives_fifo_head(self):
        service, client = self.configured()
        ready = self.browser(service, 'Ready')
        ready.update(capabilities=['ui_mutex_v1'], extension_version=EXTENSION_VERSION)
        head = service.store.rows()[0]
        service.fill_dispatch_slots()
        self.assertEqual(service.store.get(head['id'])['status'], 'preparing')
        self.assertEqual(service.store.get(head['id'])['browser'], ready['id'])

    def test_upgrade_refuses_every_unfinished_paid_or_unpaid_phase(self):
        service, client = self.configured(); row = service.store.rows()[0]
        for status in ('preparing','uploading','submitting','generating','downloading','uncertain','archiving','download_failed'):
            with self.subTest(status=status):
                service.store.update(row['id'], status, browser=client['id'],
                    remote={'profile':client['profile'], 'account':client['account']})
                before = service.store.rows()
                result = self.connect(service, client)
                self.assertFalse(result['success'])
                self.assertNotIn('Old', service.setup_requests)
                self.assertEqual(service.store.rows(), before)


if __name__ == '__main__': unittest.main()

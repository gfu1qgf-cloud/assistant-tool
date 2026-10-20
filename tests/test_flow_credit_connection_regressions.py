"""Credit/readiness combinations from the reported 52-credit browser state."""
import time
import unittest

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.policy import block_for, waiting_reason
from app_plugins.builtin.flow_assistant.setup_pairing import confirm_saved_connection
from app_plugins.builtin.flow_assistant.setup_support import project_block

PROJECT = 'https://flow.google.com/project/confirmed-project'
OTHER = 'https://flow.google.com/project/other-project'


class CreditConnectionRegressionTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def connected(self, service, profile='Profile 104', credits=52):
        client = self.client(service, profile, credits)
        client.update(saved_project=PROJECT, project=PROJECT, page_loaded=True, page_ready=True,
                      page_at=time.time(), credits_local=True, setup_pending=True, setup_id='b' * 32)
        service.preferences.setdefault('profile_projects', {})[profile] = PROJECT
        client['setup_blocked'] = project_block(client, service.preferences)
        return client

    def test_published_pending_browser_is_confirming_not_an_environment_error(self):
        service = self.service()
        client = self.connected(service)
        service.setup_requests[client['profile']] = {'id': 'a' * 32, 'project': PROJECT,
            'expires': time.time() + 120, 'claimed': True}
        published = {**client, 'environment_block': block_for(client, service.preferences)}
        self.assertEqual(browser_status(published, service.preferences)['state'], '自动确认中')

    def test_funded_pending_browser_and_empty_backup_are_not_credit_shortage(self):
        service = self.service()
        funded = self.connected(service)
        backup = self.client(service, 'Profile 105', credits=0)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        reason = waiting_reason(row, [funded, backup], [row], service.preferences, {}, {})
        self.assertNotIn('积分不足', reason)
        self.assertIn('确认', reason)
        self.assertIn('Profile 104', reason)

    def test_same_saved_page_retires_orphan_pending_and_starts_parameter_check(self):
        service = self.service()
        client = self.connected(service)
        self.assertNotIn(client['profile'], service.setup_requests)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        service.paused = False
        service._tick()
        self.assertFalse(client['setup_pending'])
        self.assertFalse(client['setup_blocked'])
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertEqual([r['action'] for r in service.pending.values()], ['prepare'])

    def test_own_saved_ticket_completes_claim_without_another_reconnect(self):
        service = self.service()
        client = self.connected(service)
        service.setup_requests[client['profile']] = {'id': client['setup_id'], 'project': PROJECT,
            'expires': time.time() + 120, 'claimed': True, 'force_pair': True}
        self.assertTrue(confirm_saved_connection(service, client))
        self.assertFalse(client['setup_pending'])
        self.assertNotIn(client['profile'], service.setup_requests)

    def test_different_saved_project_shows_project_problem_not_confirmation(self):
        service = self.service()
        client = self.connected(service)
        client['saved_project'] = OTHER
        reason = project_block(client, service.preferences)
        self.assertNotIn('后台自动确认', reason)
        self.assertIn('项目地址', reason)
        self.assertFalse(confirm_saved_connection(service, client))
        self.assertTrue(client['setup_pending'])

    def test_true_credit_shortage_still_reports_shortage(self):
        service = self.service()
        client = self.client(service, credits=5)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        self.assertIn('积分不足', waiting_reason(row, [client], [row], {}, {}, {}))

    def test_live_other_claim_and_stale_page_are_not_silently_confirmed(self):
        service = self.service()
        client = self.connected(service)
        service.setup_requests[client['profile']] = {'id': 'a' * 32, 'project': PROJECT,
            'expires': time.time() + 120, 'claimed': True, 'force_pair': True}
        self.assertFalse(confirm_saved_connection(service, client))
        self.assertTrue(client['setup_pending'])
        service.setup_requests.clear()
        client.update(page_at=time.time() - 60, page_ready=False)
        self.assertFalse(confirm_saved_connection(service, client))
        self.assertTrue(client['setup_pending'])

    def test_non_pending_client_does_not_keep_stale_confirmation_block(self):
        service = self.service()
        client = self.connected(service)
        client['setup_pending'] = False
        confirm_saved_connection(service, client)
        self.assertFalse(client['setup_blocked'])
        self.assertEqual(browser_status(client, service.preferences)['state'], '可接单')


if __name__ == '__main__':
    unittest.main()

"""Real coordinator message ordering; no browser/account or paid generation."""
import json
import time
import unittest
from unittest.mock import MagicMock

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION
from app_plugins.builtin.flow_assistant.setup_pairing import claim_setup

PROJECT = 'https://flow.google.com/project/confirmed-project'
OTHER = 'https://flow.google.com/project/another-project'


class PairingStateTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service

    def prepare(self, service, request='first', **updates):
        service.execute({'action': 'setup_profile', 'request_id': request,
            'profile': 'Profile 23', 'project': PROJECT, **updates})
        return service.setup_requests['Profile 23']['id']

    def hello(self, service, *, project=PROJECT, setup='b'*32, version=EXTENSION_VERSION):
        socket = MagicMock()
        service.clients[socket] = {'socket': socket, 'authorized': False, 'connected': time.time()}
        service._message(socket, json.dumps({'type': 'hello', 'protocol': 1,
            'token': service.pair['token'], 'browser_id': 'saved-browser', 'profile': 'Profile 23',
            'project': project, 'setup_id': setup, 'extension_version': version}))
        return socket, service.clients[socket]

    def page(self, service, socket, project=PROJECT):
        service._message(socket, json.dumps({'type': 'page_status', 'url': project}))

    def test_repeated_connect_keeps_ticket_even_after_claim_before_hello(self):
        service = self.service(); first = self.prepare(service)
        claimant = {'socket': MagicMock(), 'authorized': False}
        self.assertTrue(claim_setup(service, claimant, {'ticket': first}))
        self.assertEqual(self.prepare(service, 'second'), first)
        socket, client = self.hello(service, setup=first)
        self.assertFalse(client['setup_pending'])
        self.assertNotIn('Profile 23', service.setup_requests)

    def test_saved_authenticated_connection_confirms_itself_after_real_page_response(self):
        service = self.service(); ticket = self.prepare(service)
        socket, client = self.hello(service)
        self.assertTrue(client['setup_pending'], 'URL alone must not be page-ready proof')
        self.page(service, socket)
        self.assertFalse(client['setup_pending'])
        self.assertFalse(client['setup_blocked'])
        self.assertNotIn('Profile 23', service.setup_requests)
        self.assertEqual(client['setup_id'], 'b'*32, 'do not rewrite browser credentials or reconnect it')
        self.assertFalse(any(r['action'] == 'run_tasks' for r in service.pending.values()))
        late = {'socket': MagicMock(), 'authorized': False}
        self.assertFalse(claim_setup(service, late, {'ticket': ticket}), 'retired ticket cannot issue new credentials later')

    def test_snapshot_also_confirms_without_manual_reopening(self):
        service = self.service(); self.prepare(service)
        socket, client = self.hello(service)
        service._message(socket, json.dumps({'type': 'snapshot', 'data': {'url': PROJECT, 'tasks': []}}))
        self.assertFalse(client['setup_pending'])

    def test_saved_other_project_cannot_confirm_target_even_if_page_url_matches(self):
        service = self.service(); self.prepare(service)
        socket, client = self.hello(service, project=OTHER)
        self.page(service, socket)
        self.assertTrue(client['setup_pending'])
        self.assertIn('Profile 23', service.setup_requests)

    def test_current_other_project_or_old_extension_cannot_confirm(self):
        for project, version in ((OTHER, EXTENSION_VERSION), (PROJECT, '1.1.4')):
            service = self.service(); self.prepare(service)
            socket, client = self.hello(service, version=version)
            self.page(service, socket, project)
            self.assertTrue(client['setup_pending'])

    def test_unfinished_claim_and_explicit_repair_are_not_bypassed(self):
        for claim, force in ((True, False), (False, True)):
            service = self.service(); ticket = self.prepare(service, force_pair=force)
            if claim:
                self.assertTrue(claim_setup(service, {'socket': MagicMock(), 'authorized': False}, {'ticket': ticket}))
            socket, client = self.hello(service)
            self.page(service, socket)
            self.assertTrue(client['setup_pending'])

    def test_expired_claim_does_not_leave_valid_saved_browser_blocked_forever(self):
        service = self.service(); self.prepare(service)
        service.setup_requests['Profile 23'].update(claimed=True, expires=time.time()-1)
        socket, client = self.hello(service)
        self.page(service, socket)
        self.assertFalse(client['setup_pending'])

    def test_no_token_cannot_confirm_anything(self):
        service = self.service(); self.prepare(service)
        socket = MagicMock(); service.clients[socket] = {'socket': socket, 'authorized': False}
        service._message(socket, json.dumps({'type': 'page_status', 'url': PROJECT}))
        self.assertFalse(service.clients[socket]['authorized'])
        self.assertIn('Profile 23', service.setup_requests)


if __name__ == '__main__': unittest.main()

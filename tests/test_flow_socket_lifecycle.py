"""Exercise real Qt wrapper destruction without opening a network connection."""
import json
import gc
import time
import unittest
import weakref
from unittest.mock import MagicMock, patch

from PyQt6 import sip
from PyQt6.QtWebSockets import QWebSocket
import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.service import EXTENSION_ID, PROTOCOL, FlowService


class SocketLifecycleTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def stale(self, service):
        socket = QWebSocket()
        client = {'socket': socket, 'id': 'stable-browser', 'profile': 'Profile 19',
            'authorized': True, 'connected': time.time(), 'account': 'fixture@example.test',
            'credits': 70, 'credits_local': True, 'page_ready': True, 'page_at': time.time()}
        service.clients[socket] = client
        sip.delete(socket)
        return socket, client

    def test_deleted_old_wrapper_does_not_interrupt_replacement_handshake(self):
        service = self.service(); stale, old = self.stale(service)
        fresh = MagicMock(); service.clients[fresh] = {'socket': fresh, 'authorized': False, 'connected': time.time()}
        errors = []; service.failed.connect(errors.append)
        service._message(fresh, json.dumps({'type': 'hello', 'protocol': PROTOCOL,
            'token': service.pair['token'], 'browser_id': old['id'], 'profile': 'Profile 19',
            'project': 'https://labs.google/fx/tools/flow/project/fixture', 'extension_version': '1.1.18'}))
        self.assertFalse(errors)
        self.assertNotIn(stale, service.clients)
        messages = [json.loads(call.args[0]) for call in fresh.sendTextMessage.call_args_list]
        self.assertTrue(any(message.get('type') == 'welcome' for message in messages))
        self.assertEqual({item['action'] for item in service.pending.values()}, {'get_points', 'snapshot'})

    def test_deleted_send_retains_possible_paid_submission_and_retires_request(self):
        service = self.service(); socket, client = self.stale(service)
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'submitting', attempt='a'*32, browser=client['id'],
            remote={'account': client['account'], 'cost': 12})
        service.store.reserve(row['id'], 'a'*32, 12, 'duration_rule')
        service.request(client, 'run_tasks', {}, row['id'])
        self.assertNotIn(socket, service.clients)
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.get(row['id'])['attempt'], 'a'*32)
        self.assertFalse(service.pending)
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 12)

    def test_deleted_client_cannot_pause_other_accounts_during_tick(self):
        service = self.service(); socket, _ = self.stale(service)
        service.clients[socket]['authorized'] = False
        service.clients[socket]['connected'] = time.time() - 10
        self.client(service, 'Other')
        service.store.enqueue(self.jobs(duration=8, budget=0)); service.paused = False
        errors = []; service.failed.connect(errors.append)
        service._tick()
        self.assertFalse(errors)
        self.assertFalse(service.safety_paused)
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_destroyed_signal_removes_socket_and_disconnect_is_idempotent(self):
        service = self.service()
        socket = QWebSocket(origin=f'chrome-extension://{EXTENSION_ID}')
        with patch.object(service.server, 'nextPendingConnection', return_value=socket):
            service._accept()
        self.assertIs(socket.parent(), service)
        self.assertIn(socket, service.clients)
        sip.delete(socket)
        self.assertNotIn(socket, service.clients)
        service._disconnected(socket)
        service.execute({'action': 'stop'})

    def test_late_socket_signals_do_not_retain_or_call_a_collected_coordinator(self):
        service = FlowService(self.namespace, str(self.root / 'downloads'))
        service.server = MagicMock()
        socket = QWebSocket(origin=f'chrome-extension://{EXTENSION_ID}')
        service.server.nextPendingConnection.return_value = socket
        service._accept()
        socket.setParent(None)
        owner = weakref.ref(service)
        del service
        gc.collect()
        self.assertIsNone(owner(), 'socket handlers must not keep a retired coordinator alive')
        socket.textMessageReceived.emit('{}')
        socket.disconnected.emit()
        sip.delete(socket)


if __name__ == '__main__':
    unittest.main()

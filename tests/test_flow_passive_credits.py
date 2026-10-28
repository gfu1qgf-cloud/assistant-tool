"""Manual observation must not become foreground work or auto-close consent."""
import copy
import json
import time
import unittest
from unittest.mock import MagicMock,patch

import test_flow_browser_lifecycle as fixtures
from app_plugins.builtin.flow_assistant.passive_credits import CAPABILITY, receive
from app_plugins.builtin.flow_assistant.browser_lifecycle import watch
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class PassiveCreditTests(unittest.TestCase):
    setUpClass=fixtures.BrowserLifecycleTests.__dict__['setUpClass']
    setUp=fixtures.BrowserLifecycleTests.setUp
    service=fixtures.BrowserLifecycleTests.service
    client=fixtures.BrowserLifecycleTests.client
    browser=fixtures.BrowserLifecycleTests.browser
    jobs=fixtures.BrowserLifecycleTests.jobs
    ready=fixtures.BrowserLifecycleTests.ready

    def manual(self):
        service=self.service();service.timer.stop();client=self.ready(service,'Manual')
        client.update(page_instance='manual-page',credit_observation_only=True)
        client['capabilities'].append(CAPABILITY)
        return service,client

    def observation(self,client,**changes):
        return {'type':'credit_observation','url':client['saved_project'],'page_instance':client['page_instance'],
            'account':client['account'],'balance':5,'observed_at':time.time()+.01,**changes}

    def test_manual_hello_only_requests_snapshot_even_on_old_bridge(self):
        for version in ('1.1.40',EXTENSION_VERSION):
            service=self.service();service.timer.stop();socket=MagicMock()
            service.clients[socket]={'socket':socket,'authorized':False,'connected':time.time()}
            service._message(socket,json.dumps({'type':'hello','protocol':1,'token':service.pair['token'],
                'browser_id':'manual-'+version,'profile':'Manual','project':'https://flow.google.com/project/manual',
                'extension_version':version,'capabilities':[CAPABILITY]}))
            client=service.clients[socket];self.assertTrue(client['credit_observation_only'])
            actions=[json.loads(c.args[0]).get('action') for c in socket.sendTextMessage.call_args_list]
            self.assertNotIn('get_points',actions);self.assertIn('snapshot',actions)
            self.assertFalse(any('foreground_token' in json.loads(c.args[0]) for c in socket.sendTextMessage.call_args_list))

    def test_observation_updates_saved_balance_without_requests_dispatch_or_auto_close(self):
        service,client=self.manual();before=copy.deepcopy(service.store.rows())
        with patch.object(service,'wake_dispatch') as dispatch:
            service._message(client['socket'],json.dumps(self.observation(client,balance=0)))
        dispatch.assert_not_called();self.assertEqual(client['credits'],0)
        self.assertEqual(service.preferences['credit_balances'][client['account']]['balance'],0)
        self.assertEqual(service.store.rows(),before);self.assertFalse(service.pending)
        self.assertFalse(watch(service));self.assertFalse(repair_idle(service,client))
        client['socket'].sendTextMessage.assert_not_called()

    def test_closed_manual_window_never_gets_automatic_points_probe_but_explicit_button_can(self):
        service,client=self.manual()
        self.assertFalse(service.request(client,'get_points'));self.assertFalse(service.pending)
        self.assertFalse(repair_idle(service,client));client['socket'].sendTextMessage.assert_not_called()
        self.assertTrue(service.request(client,'get_points',manual_credit=True))

    def test_original_flow_startup_can_still_read_and_use_account(self):
        service,client=self.manual();service.launching[client['profile']]=time.time()
        self.assertTrue(service.request(client,'get_points'));self.assertFalse(client['credit_observation_only'])

    def test_bad_or_wrong_document_or_unresolved_result_cannot_replace_balance(self):
        changes=[{'url':'https://flow.google.com/project/other'},{'page_instance':'old'},
            {'balance':True},{'balance':-1},{'observed_at':time.time()-100},{'account':'not-email'}]
        for delta in changes:
            service,client=self.manual();before=copy.deepcopy(service.preferences)
            self.assertFalse(receive(service,client,self.observation(client,**delta)))
            self.assertEqual(service.preferences,before);self.assertFalse(service.pending)
        service,client=self.manual();service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.store.update(row['id'],'uncertain',browser=client['id'],remote={'account':client['account']})
        self.assertFalse(receive(service,client,self.observation(client)))

    def test_duplicate_or_probe_race_does_not_restore_spent_credits(self):
        service,client=self.manual();message=self.observation(client)
        self.assertTrue(receive(service,client,message));self.assertFalse(receive(service,client,message))
        service.request(client,'get_points',manual_credit=True)
        self.assertFalse(receive(service,client,self.observation(client,balance=100)))


if __name__=='__main__':unittest.main()

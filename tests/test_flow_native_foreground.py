"""Only an authenticated, one-time operation may focus its exact native window."""
import copy
import ctypes
import json
import unittest
from unittest.mock import MagicMock, patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.foreground import native_activate, receive, description

MODULE = 'app_plugins.builtin.flow_assistant.foreground'


class NativeForegroundTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs

    def request(self):
        service=self.service(); client=self.browser(service)
        client['capabilities']=['native_foreground_v1']
        service.request(client,'prepare')
        key=next(iter(service.pending)); pending=service.pending[key]
        message={'type':'foreground_request','request_id':key,'token':pending['foreground_token']}
        return service,client,message

    def test_windows_result_is_verified_and_returned_without_consuming_paid_request(self):
        service,client,message=self.request(); before=copy.deepcopy(service.store.rows())
        with patch(MODULE+'.native_activate',return_value='focused') as activate:
            service._message(client['socket'],json.dumps(message))
            activate.assert_called_once_with('[AssistantFlowFocus '+message['token']+']')
            service._message(client['socket'],json.dumps(message))
            self.assertEqual(activate.call_count,1)
        self.assertIn(message['request_id'],service.pending)
        self.assertEqual(service.store.rows(),before)
        self.assertEqual(client['foreground']['state'],'focused')
        self.assertIn('Windows 已确认',description(client,service.preferences))

    def test_wrong_client_token_or_finished_request_never_focuses(self):
        service,client,message=self.request(); other=self.browser(service,'Other')
        with patch(MODULE+'.native_activate') as activate:
            receive(service,other,message)
            receive(service,client,{**message,'token':'0'*32})
            service.pending.clear(); receive(service,client,message)
            activate.assert_not_called()

    def test_extension_window_id_is_bound_to_verified_native_identity(self):
        service,client,message=self.request()
        def activate(marker,*,identity):
            identity.update(hwnd=101,pid=9001);return 'focused'
        with patch(MODULE+'.native_activate',side_effect=activate):
            receive(service,client,{**message,'window_id':42,'tab_id':7})
        self.assertEqual(client['native_window'],{'chrome_window_id':42,'hwnd':101,'pid':9001})

    def test_disabled_during_handshake_cancels_activation(self):
        service,client,message=self.request()
        service.preferences['browser_options']={'focus_before_action':False}
        with patch(MODULE+'.native_activate') as activate:
            receive(service,client,message); activate.assert_not_called()
        self.assertEqual(client['foreground']['state'],'disabled')

    def test_denial_is_visible_and_does_not_cancel_or_repeat_submission(self):
        service,client,message=self.request()
        with patch(MODULE+'.native_activate',return_value='denied'):
            receive(service,client,message)
        self.assertIn('未允许',description(client,service.preferences))
        self.assertEqual(len(service.pending),1)

    def test_caption_lag_retries_are_bounded_and_stop_when_request_finishes(self):
        service,client,message=self.request(); timers=[]
        with patch(MODULE+'.native_activate',return_value='not_found') as activate, \
             patch(MODULE+'.QtCore.QTimer.singleShot',side_effect=lambda ms,callback:timers.append(callback)):
            receive(service,client,message)
            while timers: timers.pop(0)()
            self.assertEqual(activate.call_count,5)
        self.assertEqual(client['foreground']['state'],'not_found')
        service,client,message=self.request(); timers=[]
        with patch(MODULE+'.native_activate',return_value='not_found') as activate, \
             patch(MODULE+'.QtCore.QTimer.singleShot',side_effect=lambda ms,callback:timers.append(callback)):
            receive(service,client,message); service.pending.clear(); timers.pop(0)()
            self.assertEqual(activate.call_count,1)

    def test_extension_timeout_cancels_pending_native_activation(self):
        service,client,message=self.request(); timers=[]
        with patch(MODULE+'.native_activate',return_value='not_found') as activate, \
             patch(MODULE+'.QtCore.QTimer.singleShot',side_effect=lambda ms,callback:timers.append(callback)):
            receive(service,client,message)
            receive(service,client,{**message,'type':'foreground_cancel'})
            timers.pop(0)()
            self.assertEqual(activate.call_count,1)
        self.assertEqual(client['foreground']['state'],'timeout')

    def test_retired_request_never_activates_old_window(self):
        service,client,message=self.request(); service.pending[message['request_id']]['retired']=True
        with patch(MODULE+'.native_activate') as activate:
            receive(service,client,message); activate.assert_not_called()

    def test_old_bridge_and_passive_queries_receive_no_native_ticket(self):
        service=self.service(); client=self.browser(service)
        service.request(client,'prepare'); self.assertNotIn('foreground_token',next(iter(service.pending.values())))
        client['capabilities']=['native_foreground_v1']; service.pending.clear()
        service.request(client,'snapshot'); self.assertNotIn('foreground_token',next(iter(service.pending.values())))

    def win_api(self, *, titles=None, executable='chrome.exe', foreground=101, iconic=False):
        titles=titles if titles is not None else {101:'[AssistantFlowFocus abc] - Google Chrome'}
        user=MagicMock(); kernel=MagicMock()
        def enum(callback,_):
            for hwnd in titles: callback(hwnd,0)
            return True
        def text(hwnd,buffer,size): buffer.value=titles[hwnd]; return len(buffer.value)
        def classname(hwnd,buffer,size): buffer.value='Chrome_WidgetWin_1'; return len(buffer.value)
        def image(handle,flags,buffer,size): buffer.value='C:\\Chrome\\'+executable; return True
        user.EnumWindows.side_effect=enum; user.GetWindowTextW.side_effect=text
        user.GetClassNameW.side_effect=classname; user.IsWindowVisible.return_value=True
        user.IsIconic.return_value=iconic; user.GetForegroundWindow.return_value=foreground
        kernel.QueryFullProcessImageNameW.side_effect=image
        return user,kernel

    def test_native_api_selects_exact_caption_and_checks_foreground_hwnd(self):
        for foreground,state in ((101,'focused'),(202,'denied')):
            user,kernel=self.win_api(foreground=foreground,iconic=True)
            with patch('ctypes.WinDLL',side_effect=[user,kernel]):
                self.assertEqual(native_activate('[AssistantFlowFocus abc]'),state)
            user.SetForegroundWindow.assert_called_once_with(101)
            user.ShowWindowAsync.assert_called_once_with(101,9)
            self.assertFalse(user.SetWindowPos.called,'no permanent or temporary topmost trick')

    def test_shared_pid_wrong_executable_or_ambiguous_titles_are_not_guessed(self):
        for options,state in (({'titles':{101:'other profile'}},'not_found'),
                ({'executable':'other.exe'},'not_found'),
                ({'titles':{101:'[AssistantFlowFocus abc]',102:'[AssistantFlowFocus abc]'}},'ambiguous')):
            user,kernel=self.win_api(**options)
            with patch('ctypes.WinDLL',side_effect=[user,kernel]):
                self.assertEqual(native_activate('[AssistantFlowFocus abc]'),state)
            user.SetForegroundWindow.assert_not_called()


if __name__ == '__main__': unittest.main()

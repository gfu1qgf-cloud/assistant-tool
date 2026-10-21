"""One timer after a successful launch, with no delay before idle launches."""
import copy
import unittest
from unittest.mock import patch

from qt_compat import QtTest
import test_flow_setup as setups


class LaunchSpacingTests(unittest.TestCase):
    setUpClass = setups.SetupTests.__dict__['setUpClass']
    setUp = setups.SetupTests.setUp
    ui = setups.SetupTests.ui

    def fixture(self):
        dialog,launcher,commands=self.ui();plugin=dialog.plugin
        for i in (25,26):launcher.profiles.append({'directory':f'Profile {i}','name':str(i)})
        plugin.snapshot={'paused':False,'preferences':{'profile_projects':{
            p['directory']:setups.PROJECT for p in launcher.profiles}}}
        return plugin,launcher,commands

    def expire(self, plugin):
        plugin.launch_timer.stop();plugin._next_browser_launch()

    def test_first_request_is_immediate_and_only_then_starts_three_to_five_second_timer(self):
        plugin,launcher,_=self.fixture()
        with patch('app_plugins.builtin.flow_assistant.plugin.random.uniform',return_value=4) as gap:
            self.assertTrue(plugin.launch_browser('Profile 23',[setups.PROJECT])[0])
        self.assertEqual(launcher.launch_profile.call_count,1)
        gap.assert_called_once_with(3,5)
        self.assertTrue(plugin.launch_timer.isActive())
        self.assertEqual(plugin.launch_timer.interval(),4000)

    def test_four_continuous_requests_launch_one_per_timer_expiry(self):
        plugin,launcher,_=self.fixture()
        with patch('app_plugins.builtin.flow_assistant.plugin.random.uniform',return_value=4):
            for i in (23,24,25,26):plugin.launch_browser(f'Profile {i}',[setups.PROJECT])
            self.assertEqual(launcher.launch_profile.call_count,1)
            for expected in (2,3,4):
                self.expire(plugin)
                self.assertEqual(launcher.launch_profile.call_count,expected)
                self.assertEqual(plugin.launch_timer.interval(),4000)
        self.assertFalse(plugin.pending_browser_launches)
        self.assertTrue(all(call.kwargs['new_window'] is False for call in launcher.launch_profile.call_args_list))

    def test_after_idle_timer_expiry_a_new_request_has_no_fresh_wait(self):
        plugin,launcher,_=self.fixture();plugin.launch_browser('Profile 23',[setups.PROJECT])
        self.expire(plugin)
        self.assertFalse(plugin.launch_timer.isActive())
        plugin.launch_browser('Profile 24',[setups.PROJECT])
        self.assertEqual(launcher.launch_profile.call_count,2)
        self.assertFalse(plugin.pending_browser_launches)

    def test_duplicate_pending_profile_coalesces_and_acknowledges_only_after_actual_start(self):
        plugin,launcher,_=self.fixture();plugin.launch_browser('Profile 23',[setups.PROJECT])
        receipts=[]
        for _ in range(3):plugin.launch_browser('Profile 24',[setups.PROJECT],completed=lambda *r:receipts.append(r))
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertEqual(len(plugin.pending_browser_launches),1);self.assertFalse(receipts)
        self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count,2)
        self.assertEqual(receipts,[(True,None)]*3)

    def test_real_qt_timer_releases_pending_request_without_another_submit(self):
        plugin,launcher,_=self.fixture()
        with patch('app_plugins.builtin.flow_assistant.plugin.random.uniform',return_value=.01):
            plugin.launch_browser('Profile 23',[setups.PROJECT]);plugin.launch_browser('Profile 24',[setups.PROJECT])
            self.assertEqual(launcher.launch_profile.call_count,1)
            QtTest.QTest.qWait(35)
            self.assertEqual(launcher.launch_profile.call_count,2)

    def test_failed_start_does_not_impose_another_gap_before_first_real_browser(self):
        plugin,launcher,_=self.fixture();launcher.launch_profile.side_effect=[(False,'fixture failed'),(True,None)]
        self.assertFalse(plugin.launch_browser('Profile 23',[setups.PROJECT])[0])
        self.assertFalse(plugin.launch_timer.isActive())
        self.assertTrue(plugin.launch_browser('Profile 24',[setups.PROJECT])[0])
        self.assertEqual(launcher.launch_profile.call_count,2)

    def test_stopping_cancels_queued_start_and_no_late_chrome_is_opened(self):
        plugin,launcher,_=self.fixture();plugin.launch_browser('Profile 23',[setups.PROJECT])
        receipts=[];plugin.launch_browser('Profile 24',[setups.PROJECT],completed=lambda *r:receipts.append(r))
        plugin.stop();self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertFalse(plugin.launch_timer.isActive());self.assertFalse(plugin.pending_browser_launches)
        self.assertEqual(receipts[0][0],False)

    def test_queue_pause_is_rechecked_when_timer_expires(self):
        plugin,launcher,commands=self.fixture()
        plugin.snapshot['preferences']['browser_slots']={p:{'state':'opening'} for p in ('Profile 23','Profile 24')}
        for p in ('Profile 23','Profile 24'):
            plugin._launch_checked({'profile':p,'status':'enabled','version':setups.EXTENSION_VERSION})
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertFalse(any(c.get('profile')=='Profile 24' and 'opened' in c for c in commands))
        plugin.snapshot['paused']=True;self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertTrue(commands[-1]['cancelled'])

    def test_missing_project_restore_and_normal_launch_use_the_same_timer(self):
        plugin,launcher,commands=self.fixture()
        plugin.launch_browser('Profile 23',[setups.PROJECT])
        command={'action':'project_open_result','request_id':'restore','profile':'Profile 24',
            'browser':'background','project':setups.PROJECT}
        plugin.snapshot['preferences']['browser_slots']={'Profile 24':{'project_startup':{**command,'state':'opening'}}}
        plugin.snapshot['clients']=[{'id':'background','profile':'Profile 24','saved_project':setups.PROJECT,
            'page_ready':False,'project_missing':True}]
        plugin._open_missing_project(command)
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertFalse(any(c.get('request_id')=='restore' for c in commands))
        self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count,2)
        self.assertTrue(commands[-1]['opened'])


if __name__=='__main__':unittest.main()

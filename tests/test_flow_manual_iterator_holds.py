"""Manual next shares durable account holds; no Chrome or live user data."""
import copy
import time
import unittest
from unittest.mock import MagicMock, patch

import test_chrome_flow_merge as fixtures
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.browser_manager import skip_reason


class ManualIteratorHoldTests(unittest.TestCase):
    setUpClass=fixtures.ChromeFlowMergeTests.__dict__['setUpClass']
    setUp=fixtures.ChromeFlowMergeTests.setUp
    create_dialog=fixtures.ChromeFlowMergeTests.create_dialog
    preferences=fixtures.ChromeFlowMergeTests.preferences

    def mark_profile(self,prefs,profile):
        key='profile:'+profile
        prefs.setdefault('account_blocks',{})[key]={'kind':'manual','reason':'用户手动标记账号不可用','at':time.time()}
        prefs.setdefault('account_block_links',{})[key]=key

    def test_manual_unavailable_without_email_or_balance_is_skipped(self):
        prefs={};self.mark_profile(prefs,'Profile 1');before=copy.deepcopy(prefs)
        self.assertIn('用户',skip_reason('Profile 1',prefs,[]))
        self.assertEqual(prefs,before)
        self.assertFalse(skip_reason('Profile 2',prefs,[]))

    def test_account_hold_does_not_expire_with_balance_or_daily_refill(self):
        prefs=self.preferences(500);account='confirmed@example.test'
        prefs['account_blocks']={account:{'kind':'manual','reason':'用户手动标记账号不可用'}}
        prefs['account_block_links']={'profile:Profile 1':account}
        prefs['credit_refreshes']={account:{'daily_time':'21:24','next_at':time.time()-10}}
        for record in prefs['credit_balances'].values():record['observed_at']-=100
        with patch('time.time',return_value=time.time()+21601):
            self.assertIn('用户',skip_reason('Profile 1',prefs,[]))
        prefs['account_blocks'].clear();prefs['account_block_links'].clear()
        self.assertFalse(skip_reason('Profile 1',prefs,[]))

    def test_disabling_only_credit_skip_still_filters_manually_unavailable_profiles(self):
        plugin=FlowAssistantPlugin();self.addCleanup(plugin.stop)
        prefs=self.preferences(5);self.mark_profile(prefs,'Profile 2')
        plugin.snapshot={'preferences':prefs,'clients':[]};plugin.skip_exhausted_iterator=False
        predicate=plugin.iterator_skip_filter()
        self.assertTrue(callable(predicate))
        self.assertFalse(predicate({'directory':'Profile 1'}),'the low-credit toggle keeps its meaning')
        self.assertIn('用户',predicate({'directory':'Profile 2'}))

    def test_manual_next_skips_marked_profiles_and_preserves_submission_order(self):
        plugin=FlowAssistantPlugin();self.addCleanup(plugin.stop)
        prefs={};self.mark_profile(prefs,'Profile 1');self.mark_profile(prefs,'Profile 2')
        plugin.snapshot={'preferences':prefs,'clients':[]}
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2','Profile 3']
        dialog.profile_skip_factory=plugin.iterator_skip_filter
        dialog.launch_profile=MagicMock(return_value=(True,None))
        self.assertEqual(dialog.launch_next_profile()['directory'],'Profile 3')
        self.assertEqual(dialog.next_profile_index,0)
        dialog.launch_profile.assert_called_once()
        self.assertIn('不可用',dialog.status_label.text())
        self.assertIn('用户',dialog.status_label.toolTip())

    def test_all_marked_accounts_never_launch_or_advance_or_show_a_modal(self):
        plugin=FlowAssistantPlugin();self.addCleanup(plugin.stop)
        prefs={}
        for name in ('Profile 1','Profile 2'):self.mark_profile(prefs,name)
        plugin.snapshot={'preferences':prefs,'clients':[]}
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2'];dialog.next_profile_index=1
        dialog.profile_skip_factory=plugin.iterator_skip_filter;dialog.launch_profile=MagicMock(return_value=(True,None))
        with patch('PYUI.chrome_runner_pyui.QMessageBox.warning') as warning:
            self.assertIsNone(dialog.launch_next_profile());warning.assert_not_called()
        dialog.launch_profile.assert_not_called();self.assertEqual(dialog.next_profile_index,1)


if __name__=='__main__':unittest.main()

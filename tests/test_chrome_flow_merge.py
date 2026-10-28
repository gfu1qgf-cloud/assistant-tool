"""One host owner and iterator; no real Chrome, global hotkeys or user stores."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_chrome_profile_groups as groups
from qt_compat import QtCore, QtTest, QtWidgets
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from model.AppDataStorage import ApplicationStorage


class ChromeFlowMergeTests(unittest.TestCase):
    setUpClass=groups.ChromeProfileGroupTests.__dict__['setUpClass']
    create_dialog=groups.ChromeProfileGroupTests.create_dialog

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def test_next_skips_exhausted_profiles_preserves_order_and_persists_next_candidate(self):
        path=self.root/'config.json';dialog=self.create_dialog(path);self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2','Profile 3'];dialog.next_profile_index=0
        checked=[]
        def skip(profile):
            checked.append(profile['directory']);return '积分5，尚未刷新' if profile['directory']=='Profile 1' else ''
        dialog.profile_skip_reason=skip;dialog.launch_profile=MagicMock(return_value=(True,None))
        launched=dialog.launch_next_profile()
        self.assertEqual(launched['directory'],'Profile 2');self.assertEqual(checked,['Profile 1','Profile 2'])
        self.assertEqual(dialog.next_profile_index,2)
        self.assertIn('跳过1',dialog.status_label.text())
        restored=self.create_dialog(path);self.addCleanup(restored.close)
        self.assertEqual(restored.next_profile_index,2)

    def test_all_exhausted_scans_once_without_launching_or_modal_prompt(self):
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2','Profile 3'];dialog.next_profile_index=1
        checks=[];dialog.profile_skip_reason=lambda p:checks.append(p['directory']) or '积分不足'
        dialog.launch_profile=MagicMock(return_value=(True,None))
        with patch('PYUI.chrome_runner_pyui.QMessageBox.warning') as warning:
            self.assertIsNone(dialog.launch_next_profile());warning.assert_not_called()
        self.assertEqual(checks,['Profile 2','Profile 3','Profile 1']);dialog.launch_profile.assert_not_called()
        self.assertEqual(dialog.next_profile_index,1)

    def test_failure_after_skips_keeps_the_failed_candidate_for_retry(self):
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2','Profile 3']
        dialog.profile_skip_reason=lambda p:'积分不足' if p['directory']=='Profile 1' else ''
        dialog.launch_profile=MagicMock(return_value=(False,'cannot open'))
        with patch('PYUI.chrome_runner_pyui.QMessageBox.critical'):
            self.assertIsNone(dialog.launch_next_profile())
        self.assertEqual(dialog.next_profile_index,1)

    def test_one_registration_owns_commands_settings_and_chrome_storage_without_moving_files(self):
        storage=ApplicationStorage(self.root/'private')
        old=storage.for_plugin('chrome_launcher').file('bookmarks/library.json');old.write_json({'keep':True})
        parent=QtWidgets.QWidget();self.addCleanup(parent.close)
        context=SimpleNamespace(storage=storage.for_plugin('flow_assistant'),parent_widget=parent,
            register_command=MagicMock(),register_settings_page=MagicMock(),register_main_widget=MagicMock(),
            load_config=lambda:{},log=MagicMock())
        plugin=FlowAssistantPlugin();plugin.register(context);self.addCleanup(plugin.stop)
        self.assertEqual(plugin.plugin_id,'flow_assistant')
        self.assertEqual(plugin.display_name,'Chrome 与 Flow 助手')
        self.assertIs(plugin.browser_launcher.context.parent_widget,parent)
        self.assertEqual(plugin.browser_launcher.context.storage.path,storage.for_plugin('chrome_launcher').path)
        self.assertEqual(plugin.context.storage.path,storage.for_plugin('flow_assistant').path)
        self.assertEqual(old.read_json(),{'keep':True})
        self.assertEqual(context.register_main_widget.call_count,1)
        self.assertEqual({c.args[0].page_id for c in context.register_settings_page.call_args_list},{'chrome','timing','browser'})
        self.assertEqual(context.register_command.call_count,2)

    def preferences(self,balance=0):
        account='confirmed@example.test';project='https://flow.google.com/project/original';now=time.time()
        return {'profile_projects':{'Profile 1':project},
            'credit_balances':{account:{'account':account,'balance':balance,'observed_at':now,'balance_at':now}},
            'credit_balance_links':{'profile:Profile 1':{'account':account,'project':['flow.google.com','/project/original']}},
            'credit_cooldowns':{account:{'account':account,'balance':balance,'at':now,'until':now+21600}},
            'credit_cooldown_links':{'profile:Profile 1':account}}

    def test_balance_policy_uses_existing_cooldown_but_unknown_and_refilled_accounts_remain_candidates(self):
        from app_plugins.builtin.flow_assistant.browser_manager import skip_reason
        prefs=self.preferences();original=copy.deepcopy(prefs)
        self.assertTrue(skip_reason('Profile 1',prefs,[]))
        self.assertFalse(skip_reason('Unknown',prefs,[]))
        self.assertEqual(prefs,original)
        with patch('time.time',return_value=time.time()+21601):self.assertFalse(skip_reason('Profile 1',prefs,[]))
        prefs['credit_refreshes']={'confirmed@example.test':{'daily_time':'21:24','next_at':time.time()-1}}
        for entry in prefs['credit_balances'].values():entry['observed_at']-=10
        self.assertFalse(skip_reason('Profile 1',prefs,[]),'passed daily refill must permit a fresh browser check')

    def test_current_login_replaces_old_cached_account_and_ten_points_are_not_exhausted(self):
        from app_plugins.builtin.flow_assistant.browser_manager import skip_reason
        prefs=self.preferences();live={'id':'online','profile':'Profile 1','account':'new@example.test',
            'credits':50,'credits_at':time.time(),'credits_local':True}
        self.assertFalse(skip_reason('Profile 1',prefs,[live]))
        self.assertFalse(skip_reason('Profile 1',self.preferences(10),[]))

    def test_unified_window_uses_two_sidebar_sections_and_routes_both_entries(self):
        parent=QtWidgets.QWidget();self.addCleanup(parent.close)
        storage=ApplicationStorage(self.root/'private')
        context=SimpleNamespace(storage=storage.for_plugin('flow_assistant'),parent_widget=parent,
            register_command=MagicMock(),register_settings_page=MagicMock(),register_main_widget=MagicMock(),
            load_config=lambda:{},log=MagicMock(),current_task_targets=lambda:[])
        plugin=FlowAssistantPlugin();plugin.register(context);self.addCleanup(plugin.stop)
        manager=self.create_dialog(self.root/'config.json');plugin.browser_launcher.dialog=manager
        dialog=plugin.open_browser_manager();self.addCleanup(dialog.close)
        self.assertEqual([dialog.sidebar.item(i).text() for i in range(dialog.sidebar.count())],['Chrome','FLOW 插件'])
        self.assertIs(dialog.sections.currentWidget(),dialog.chrome_section)
        self.assertEqual(dialog.tabs.indexOf(manager),-1,'Chrome must not be a tab inside FLOW')
        self.assertFalse(manager.isWindow(),'the manager is a page in the shared window')
        self.assertIs(plugin.browser_launcher.open_launcher(),dialog)
        self.assertIs(plugin.open_flow_queue(),dialog);self.assertEqual(dialog.tabs.currentIndex(),0)
        self.assertIs(dialog.sections.currentWidget(),dialog.flow_page)
        self.assertEqual(dialog.tabs.count(),4)
        dialog.manual.prompt.setPlainText('切换时保留的提示词');manager.next_profile_index=2
        self.app.processEvents()
        for _ in range(2):
            for row, page in ((0,dialog.chrome_section),(1,dialog.flow_page)):
                QtTest.QTest.mouseClick(dialog.sidebar.viewport(),QtCore.Qt.MouseButton.LeftButton,
                    pos=dialog.sidebar.visualItemRect(dialog.sidebar.item(row)).center())
                self.assertIs(dialog.sections.currentWidget(),page)
        self.assertEqual(dialog.manual.prompt.toPlainText(),'切换时保留的提示词')
        self.assertEqual(manager.next_profile_index,2)
        dialog.show_chrome();dialog.show_flow_page(dialog.manual)
        self.assertIs(dialog.sections.currentWidget(),dialog.flow_page)
        dialog.show_chrome();dialog.select_jobs([])
        self.assertIs(dialog.sections.currentWidget(),dialog.flow_page)
        self.assertEqual(dialog.tabs.currentIndex(),0)
        capture=os.environ.get('LZX_FLOW_SIDEBAR_QA')
        if capture:
            from model.AppTheme import apply_ui_theme
            destination=Path(capture);destination.mkdir(parents=True,exist_ok=True)
            for theme in ('dark','light'):
                apply_ui_theme(self.app,theme)
                dialog.show_chrome();self.app.processEvents();dialog.grab().save(str(destination/f'chrome-{theme}.png'))
                dialog.show_flow_page(0);self.app.processEvents();dialog.grab().save(str(destination/f'flow-{theme}.png'))
        dialog.close();self.assertIs(plugin.open_browser_manager(),dialog)
        self.assertIs(plugin.browser_launcher.dialog,manager,'opening entries cannot recreate the iterator or its cursor')

    def test_iterator_snapshot_is_captured_once_and_disabled_credit_option_keeps_account_filter(self):
        plugin=FlowAssistantPlugin();self.addCleanup(plugin.stop)
        plugin.snapshot={'preferences':self.preferences(),'clients':[]}
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2','Profile 3']
        factory=MagicMock(side_effect=plugin.iterator_skip_filter);dialog.profile_skip_factory=factory
        dialog.launch_profile=MagicMock(return_value=(True,None))
        self.assertEqual(dialog.launch_next_profile()['directory'],'Profile 2');factory.assert_called_once()
        plugin.skip_exhausted_iterator=False;plugin.snapshot={}
        with patch('app_plugins.builtin.flow_assistant.browser_manager.saved_preferences',return_value={}) as read:
            predicate=plugin.iterator_skip_filter();read.assert_called_once()
            self.assertTrue(callable(predicate));self.assertFalse(predicate({'directory':'Profile 1'}))

    def test_read_error_preserves_the_iterator_and_never_launches(self):
        dialog=self.create_dialog(self.root/'config.json');self.addCleanup(dialog.close)
        dialog.iterator_profile_directories=['Profile 1','Profile 2'];dialog.next_profile_index=1
        dialog.profile_skip_factory=MagicMock(side_effect=ValueError('damaged credit record'))
        dialog.launch_profile=MagicMock(return_value=(True,None))
        self.assertIsNone(dialog.launch_next_profile());self.assertEqual(dialog.next_profile_index,1)
        dialog.launch_profile.assert_not_called();self.assertIn('damaged credit record',dialog.status_label.text())

    def test_saved_preferences_are_read_only_and_never_create_a_missing_queue(self):
        import sqlite3
        from app_plugins.builtin.flow_assistant.browser_manager import saved_preferences
        storage=ApplicationStorage(self.root/'private').for_plugin('flow_assistant')
        context=SimpleNamespace(storage=storage)
        saved=storage.file('queue.sqlite3')
        self.assertEqual(saved_preferences(context),{});self.assertFalse(saved.exists())
        saved.path.parent.mkdir(parents=True,exist_ok=True)
        preferences=self.preferences()
        with sqlite3.connect(saved.path) as database:
            database.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)')
            database.execute('INSERT INTO settings VALUES (?,?)',('preferences',json.dumps(preferences)))
        database.close()
        before=saved.path.read_bytes()
        self.assertEqual(saved_preferences(context),preferences)
        self.assertEqual(saved.path.read_bytes(),before)


if __name__=='__main__':unittest.main()

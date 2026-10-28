"""Saved Chrome groups update an open FLOW window without launching browsers."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_chrome_flow_merge as fixtures
from qt_compat import QtWidgets
from model.AppDataStorage import ApplicationStorage
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.scheduling_groups import GROUP_ROLE, SAVED_LIST, resolve


class GroupSyncTests(unittest.TestCase):
    setUpClass = fixtures.ChromeFlowMergeTests.__dict__['setUpClass']
    setUp = fixtures.ChromeFlowMergeTests.setUp
    create_dialog = fixtures.ChromeFlowMergeTests.create_dialog

    def window(self, preferences=None):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.close)
        storage = ApplicationStorage(self.root/'private')
        context = SimpleNamespace(storage=storage.for_plugin('flow_assistant'), parent_widget=parent,
            register_command=MagicMock(), register_settings_page=MagicMock(), register_main_widget=MagicMock(),
            load_config=lambda: {}, log=MagicMock(), current_task_targets=lambda: [])
        plugin = FlowAssistantPlugin(); plugin.register(context); self.addCleanup(plugin.stop)
        manager = self.create_dialog(self.root/'config.json')
        manager.profile_groups = [
            {'id': 'parent', 'name': 'Flow', 'profile_directories': ['Profile 1'], 'child_group_ids': ['child']},
            {'id': 'child', 'name': 'More', 'profile_directories': ['Profile 2'], 'child_group_ids': []},
            {'id': 'other', 'name': 'Other', 'profile_directories': ['Profile 3'], 'child_group_ids': []}]
        plugin.browser_launcher.dialog = manager
        dialog = plugin.open_browser_manager(); self.addCleanup(dialog.close)
        commands = []; plugin.command.connect(commands.append)
        state = {'preferences': copy.deepcopy(preferences if preferences is not None else {
            'profile_group_id': 'parent', 'profiles': ['Profile 1', 'Profile 2']}),
            'jobs': [], 'clients': [], 'paused': True}
        dialog.update_state(state); commands.clear()
        manager.launch_profile = MagicMock()
        return dialog, manager, commands

    def test_saved_child_changes_update_expanded_members_and_keep_drafts_and_cursor(self):
        dialog, manager, commands = self.window()
        dialog.manual.prompt.setPlainText('draft'); dialog.concurrent.setValue(7)
        manager.next_profile_index = 2
        dialog.setup.phase = 'auto_pair'; dialog.setup.connection_timer.start()
        manager.profile_groups[1]['profile_directories'] = ['Profile 2', 'Profile 3']
        manager.profile_groups[0]['name'] = 'Renamed'
        self.assertTrue(manager.save_profile_groups())
        self.assertEqual(dialog.group.currentText(), 'Renamed')
        self.assertEqual(dialog.group.currentData(), ['Profile 1', 'Profile 2', 'Profile 3'])
        self.assertEqual(commands, [{'action': 'preferences', 'value': {
            'profile_group_id': 'parent', 'profiles': ['Profile 1', 'Profile 2', 'Profile 3']}}])
        self.assertEqual(dialog.manual.prompt.toPlainText(), 'draft')
        self.assertEqual(dialog.concurrent.value(), 7)
        self.assertEqual(manager.next_profile_index, 2)
        self.assertEqual(dialog.setup.phase, 'auto_pair')
        self.assertTrue(dialog.setup.connection_timer.isActive())
        self.assertEqual(dialog.browsers.rowCount(), 3)
        manager.save_profile_groups(); self.assertEqual(len(commands), 1)
        manager.launch_profile.assert_not_called()

    def test_unsaved_combo_choice_does_not_replace_the_saved_group_on_catalog_change(self):
        dialog, manager, commands = self.window()
        dialog.group.setCurrentIndex(dialog.group.findData('other', GROUP_ROLE))
        manager.profile_groups[1]['profile_directories'] = ['Profile 3']
        manager.save_profile_groups()
        self.assertEqual(dialog.group.currentData(GROUP_ROLE), 'other')
        self.assertEqual(commands[-1]['value'], {'profile_group_id': 'parent', 'profiles': ['Profile 1', 'Profile 3']})
        dialog.save_preferences()
        self.assertEqual(commands[-1]['value']['profile_group_id'], 'other')
        commands.clear(); manager.profile_groups[2]['profile_directories'] = ['Profile 2']
        manager.save_profile_groups()
        self.assertEqual(commands[-1]['value']['profiles'], ['Profile 2'])
        self.assertEqual(commands[-1]['value']['profile_group_id'], 'other')

    def test_deleted_saved_group_stops_new_auto_launches_without_task_commands(self):
        dialog, manager, commands = self.window()
        manager.profile_groups = manager.profile_groups[1:]
        manager.save_profile_groups()
        self.assertEqual(commands, [{'action': 'preferences', 'value': {'profiles': [], 'profile_group_id': ''}}])
        self.assertEqual(dialog.group.currentIndex(), 0)
        manager.launch_profile.assert_not_called()

    def test_legacy_superset_migration_and_ambiguous_snapshot_are_preserved(self):
        groups = [('all', 'All', ['P1', 'P2']), ('new', 'New', ['P2'])]
        self.assertEqual(resolve({'profiles': ['P1']}, groups), ('all', ['P1', 'P2']))
        self.assertEqual(resolve({'profiles': ['P2']}, groups), ('new', ['P2']))
        self.assertEqual(resolve({'profiles': ['P1']}, groups+[('also', 'Also', ['P1', 'P3'])]), (SAVED_LIST, ['P1']))
        dialog, manager, commands = self.window({'profiles': ['Missing']})
        self.assertEqual(dialog.group.currentData(), ['Missing'])
        self.assertEqual(dialog.group.currentData(GROUP_ROLE), SAVED_LIST)
        manager.save_profile_groups(); self.assertEqual(commands, [])
        self.assertEqual(dialog.group.currentData(), ['Missing'])

    def test_profile_reload_updates_members_and_guide_without_interrupting_connection(self):
        dialog, manager, commands = self.window()
        manager.profile_groups[1]['profile_directories'].append('Profile 4')
        manager.save_profile_groups(); commands.clear()
        dialog.setup.project.setText('https://flow.google.com/project/unsaved')
        dialog.setup.phase = 'auto_pair'; dialog.setup.connection_timer.start()
        directory = self.root/'chrome'; directory.mkdir()
        profiles = {'Profile '+str(i): {'name': 'Browser '+str(i)} for i in range(1, 5)}
        for profile in profiles: (directory/profile).mkdir()
        (directory/'Local State').write_text(json.dumps({'profile': {'info_cache': profiles}}), 'utf-8')
        manager.user_data_dir = str(directory)
        manager.load_profiles()
        self.assertEqual(dialog.group.currentData(), ['Profile 1', 'Profile 2', 'Profile 4'])
        self.assertEqual(commands[-1]['value']['profiles'], ['Profile 1', 'Profile 2', 'Profile 4'])
        self.assertGreaterEqual(dialog.setup.profile.findData('Profile 4'), 0)
        self.assertEqual(dialog.setup.project.text(), 'https://flow.google.com/project/unsaved')
        self.assertEqual(dialog.setup.phase, 'auto_pair')
        self.assertTrue(dialog.setup.connection_timer.isActive())
        manager.launch_profile.assert_not_called()

    def test_failed_save_and_iterator_position_updates_do_not_notify_flow(self):
        dialog, manager, commands = self.window()
        manager.profile_groups[1]['profile_directories'] = ['Profile 3']
        with patch.object(manager, 'write_config', return_value=False):
            self.assertFalse(manager.save_profile_groups(show_error=False))
        self.assertFalse(commands)
        manager.save_iterator_state()
        self.assertFalse(commands)

    def test_observed_legacy_preferences_resolve_to_one_current_group(self):
        snapshot = os.environ.get('LZX_FLOW_REPLAY_GROUPS')
        if not snapshot: self.skipTest('optional read-only observed group snapshot')
        saved = json.loads(Path(snapshot).read_text('utf-8'))
        key, members = resolve(saved['preferences'], saved['groups'])
        self.assertEqual(key, saved['expected_group_id'])
        self.assertGreater(len(members), len(saved['preferences']['profiles']))
        self.assertEqual(set(saved['preferences']['profiles'])-set(members), set())


class GroupAdmissionTests(unittest.TestCase):
    import test_flow_credit_cooldown as flow_fixtures
    setUpClass = flow_fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = flow_fixtures.CreditCooldownTests.setUp
    service = flow_fixtures.CreditCooldownTests.service
    jobs = flow_fixtures.CreditCooldownTests.jobs

    def test_synced_members_reach_worker_candidates_without_overwriting_jobs_or_projects(self):
        from app_plugins.builtin.flow_assistant.connection_status import configured_profiles
        service = self.service(); service.timer.stop(); service.paused = True
        service.store.enqueue(self.jobs(duration=8, budget=0))
        original = service.store.rows()
        service.preferences.update(profiles=['Old'], concurrency=8, profile_projects={
            'Old': 'https://flow.google.com/project/old', 'New': 'https://flow.google.com/project/new'})
        service.execute({'action': 'preferences', 'value': {
            'profile_group_id': 'flow', 'profiles': ['Old', 'New', 'No project']}})
        self.assertEqual(configured_profiles(service.preferences), ['Old', 'New'])
        self.assertEqual(service.store.rows(), original)
        self.assertEqual(service.preferences['concurrency'], 8)
        self.assertEqual(service.preferences['profile_projects']['New'], 'https://flow.google.com/project/new')
        self.assertEqual(service.store.settings()['profile_group_id'], 'flow')


if __name__ == '__main__': unittest.main()

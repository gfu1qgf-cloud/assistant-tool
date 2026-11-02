"""Account routing regressions. Synthetic OAuth files only; network is mocked."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage
from model.GoogleAccounts import GoogleAccountManager, DISABLED, load_managed_service
from test_google_accounts import CLIENT, IDENTITY, token


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = GoogleAccountManager(ApplicationStorage(self.root))

    def login(self, sub, features=('sheets', 'drive')):
        client = self.manager.add_client(CLIENT, 'Fixture')
        return self.manager.save_login(client, {**IDENTITY, 'sub': sub}, token(features))

    def legacy(self, prefix, features):
        client = copy.deepcopy(CLIENT)
        client['installed']['client_id'] = prefix + '-client'
        value = {**token(features), 'client_id': prefix + '-client'}
        files = [self.root/(prefix+'Credentials.json'), self.root/(prefix+'Token.json')]
        for path, data in zip(files, (client, value)): path.write_text(json.dumps(data), encoding='utf-8')
        return files

    def test_global_binding_never_applies_to_plugin(self):
        key = self.login('global'); self.manager.bind_many({'sheets': key, 'drive': key, 'docs': DISABLED})
        with patch('googleapiclient.discovery.build') as build:
            for feature, profile in [('drive', 'task_delivery'), ('sheets', 'review_sheet'),
                                     ('sheets', 'sheet_monitor'), ('docs', 'document_formatter')]:
                self.assertIsNone(load_managed_service(feature, profile=profile, manager=self.manager))
        build.assert_not_called()

    def test_plugin_bindings_and_disabled_state_are_independent(self):
        upload = self.login('upload'); agent = self.login('agent', ('sheets',))
        self.manager.bind_many({'task_delivery.drive': upload, 'review_sheet.sheets': upload,
                                'ai_agent_assistant.sheets': agent, 'sheet_monitor.sheets': DISABLED})
        with patch('model.GoogleAccounts._credentials', return_value=Mock()) as creds, patch('googleapiclient.discovery.build'):
            load_managed_service('drive', profile='task_delivery', manager=self.manager)
            self.assertEqual(creds.call_args.args[1], upload)
            load_managed_service('sheets', profile='ai_agent_assistant', manager=self.manager)
            self.assertEqual(creds.call_args.args[1], agent)
        with self.assertRaisesRegex(ValueError, '停用'):
            load_managed_service('sheets', profile='sheet_monitor', manager=self.manager)
        self.assertEqual(self.manager.bound_account('review_sheet.sheets')[0], upload)

    def test_scoped_relogin_still_checks_permissions(self):
        key = self.login('same'); self.manager.bind('task_delivery.drive', key)
        with self.assertRaisesRegex(ValueError, '已绑定'): self.login('same', ('sheets',))

    def test_original_two_authorizations_import_without_mutation_or_binding(self):
        files = self.legacy('GoogleDrive', ('drive',)) + self.legacy('GoogleSheets', ('sheets',))
        before = {path: path.read_bytes() for path in files}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            self.assertEqual(self.manager.integrate_legacy({}), [])
            self.assertEqual(self.manager.integrate_legacy({}), [])
        state = self.manager.snapshot()
        self.assertEqual(len(state['accounts']), 2); self.assertEqual(len(state['clients']), 2)
        self.assertEqual(state['bindings'], {})
        self.assertTrue(all(not item['email'] and '未核验' in item['label'] for item in state['accounts']))
        self.assertEqual(before, {path: path.read_bytes() for path in files})
        raw = self.manager.file.path.read_bytes()
        self.assertNotIn(b'fixture-secret', raw); self.assertNotIn(b'fixture-refresh', raw)

    def test_distinct_per_prefix_paths_import_separately(self):
        self.legacy('GoogleSheets', ('sheets',)); special = self.legacy('Special', ('sheets',))
        self.manager.integrate_legacy({'sheet_monitor_credentials_file': str(special[0]),
                                       'sheet_monitor_token_file': str(special[1])})
        self.assertEqual(len(self.manager.snapshot()['accounts']), 2)

    def test_removed_import_is_not_silently_resurrected(self):
        self.legacy('GoogleDrive', ('drive',)); self.manager.integrate_legacy({})
        key = self.manager.snapshot()['accounts'][0]['id']
        self.manager.bind('task_delivery.drive', key); self.manager.remove(key)
        self.manager.integrate_legacy({})
        self.assertEqual(self.manager.snapshot()['accounts'], [])
        self.assertEqual(self.manager.snapshot()['bindings']['task_delivery.drive'], DISABLED)

    def test_mismatched_pair_is_reported_and_not_deleted(self):
        files = self.legacy('GoogleDrive', ('drive',))
        data = json.loads(files[1].read_text()); data['client_id'] = 'different'
        files[1].write_text(json.dumps(data)); before = files[1].read_bytes()
        self.assertEqual(len(self.manager.integrate_legacy({})), 1)
        self.assertFalse(self.manager.file.exists()); self.assertEqual(files[1].read_bytes(), before)

    def test_empty_registry_stays_absent(self):
        self.assertEqual(self.manager.integrate_legacy({}), [])
        self.assertFalse(self.manager.file.exists())

    def test_old_global_agent_selection_migrates_only_once(self):
        from test_google_authorization_registry import fixture_registry
        self.manager.registry = fixture_registry()
        key = self.login('agent'); self.manager.bind_many({'sheets': key, 'drive': key})
        self.manager.integrate_legacy({}); state = self.manager.snapshot()
        self.assertEqual(state['bindings']['ai_agent_assistant.sheets'], key)
        self.assertNotIn('task_delivery.drive', state['bindings'])
        self.assertNotIn('review_sheet.sheets', state['bindings'])
        self.manager.bind('ai_agent_assistant.sheets', '')
        self.manager.integrate_legacy({})
        self.assertNotIn('ai_agent_assistant.sheets', self.manager.snapshot()['bindings'])

    def test_new_explicit_agent_selection_survives_migration(self):
        old = self.login('old'); new = self.login('new')
        self.manager.bind_many({'sheets': old, 'ai_agent_assistant.sheets': new})
        self.manager.integrate_legacy({})
        self.assertEqual(self.manager.bound_account('ai_agent_assistant.sheets')[0], new)

    def test_helper_entry_points_pass_distinct_profiles(self):
        from model.GoogleDriveHelper import load_drive_service
        from model.GoogleSheetsHelper import load_sheets_service
        from app_plugins.builtin.document_formatter.google_auth import load_service
        with patch('model.GoogleAccounts.load_managed_service', return_value='service') as load:
            load_drive_service()
            self.assertEqual(load.call_args.kwargs['profile'], 'task_delivery')
            for prefix in ('review_sheet', 'task_submission_sheet', 'sheet_monitor'):
                load_sheets_service({}, prefix)
                self.assertEqual(load.call_args.kwargs['profile'], prefix)
            load_service(self.root)
            self.assertEqual(load.call_args.kwargs['profile'], 'document_formatter')

    def test_drive_legacy_loader_uses_original_file_despite_other_global_account(self):
        from model import GoogleDriveHelper as drive
        key = self.login('wrong-global'); self.manager.bind('drive', key)
        files = self.legacy('GoogleDrive', ('drive',))
        credentials = Mock(valid=True)
        with (patch('model.GoogleAccounts.GoogleAccountManager', return_value=self.manager),
              patch.object(drive, 'TOKEN_FILE', files[1]), patch.object(drive, 'CLIENT_SECRET_FILE', files[0]),
              patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=credentials) as read,
              patch('googleapiclient.discovery.build', return_value='legacy') as build):
            self.assertEqual(drive.load_drive_service(), 'legacy')
        self.assertEqual(read.call_args.args[0], str(files[1]))
        self.assertIs(build.call_args.kwargs['credentials'], credentials)

    def test_sheets_legacy_loader_keeps_config_prefix_paths(self):
        from model.GoogleSheetsHelper import load_sheets_service
        key = self.login('wrong-global'); self.manager.bind('sheets', key)
        files = self.legacy('Special', ('sheets',))
        credentials = Mock(valid=True)
        with (patch('model.GoogleAccounts.GoogleAccountManager', return_value=self.manager),
              patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=credentials) as read,
              patch('googleapiclient.discovery.build', return_value='legacy')):
            self.assertEqual(load_sheets_service({'sheet_monitor_credentials_file': str(files[0]),
                                                  'sheet_monitor_token_file': str(files[1])}, 'sheet_monitor'), 'legacy')
        self.assertEqual(read.call_args.args[0], str(files[1]))

    def test_scoped_expected_account_rejects_other_plugins_account(self):
        key = self.login('one'); self.manager.bind('review_sheet.sheets', key)
        with self.assertRaisesRegex(ValueError, '发生变化'):
            load_managed_service('sheets', profile='ai_agent_assistant', manager=self.manager, expected_account_id=key)

    def test_folder_permission_failure_is_clear_and_not_retried(self):
        import httplib2
        from googleapiclient.errors import HttpError
        from model.GoogleDriveHelper import get_or_create_remote_folder
        service = Mock(); service.files().list().execute.return_value = {'files': []}
        service.files().create().execute.side_effect = HttpError(httplib2.Response({'status': '403'}),
            json.dumps({'error': {'message': 'No permission', 'errors': [{'reason': 'insufficientParentPermissions'}]}}).encode())
        with patch('model.GoogleDriveHelper.time.sleep') as sleep:
            with self.assertRaisesRegex(PermissionError, '目标文件夹的写入权限'):
                get_or_create_remote_folder(service, 'fake-parent', 'fake-folder')
        sleep.assert_not_called(); self.assertEqual(service.files().create().execute.call_count, 1)


if __name__ == '__main__': unittest.main()

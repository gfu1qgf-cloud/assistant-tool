"""Dynamic OAuth declarations: synthetic credentials, mocked APIs, real host/UI."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.api import GoogleAuthorization
from app_plugins.host import PluginHost
from model.AppDataStorage import ApplicationStorage
from model.GoogleAccounts import (GoogleAccountManager, DISABLED, load_authorization_service,
    prepare_google_account, load_google_service, load_managed_service)
from model.GoogleAuthorizationRegistry import GoogleAuthorizationRegistry
from test_google_accounts import CLIENT, IDENTITY, token

SCOPE = 'https://www.googleapis.com/auth/tasks'
DEFINITION = GoogleAuthorization('jobs', '新插件 · Google Tasks', 'tasks', 'v1', (SCOPE,))


def fixture_registry():
    """Built-in-compatible UI fixture, not a production plugin whitelist."""
    registry = GoogleAuthorizationRegistry()
    registry.register('sheet_monitor', GoogleAuthorization('sheets', '表格监视器', 'sheets', 'v4',
        ('https://www.googleapis.com/auth/spreadsheets',), allow_legacy=True, legacy_bindings=('core.sheet_monitor',)))
    registry.register('task_delivery', GoogleAuthorization('drive', '上传', 'drive', 'v3',
        ('https://www.googleapis.com/auth/drive',), allow_legacy=True))
    registry.register('task_delivery', GoogleAuthorization('review_sheet', '审核', 'sheets', 'v4',
        ('https://www.googleapis.com/auth/spreadsheets',), allow_legacy=True, legacy_bindings=('review_sheet.sheets',)))
    registry.register('task_delivery', GoogleAuthorization('task_submission_sheet', '提交', 'sheets', 'v4',
        ('https://www.googleapis.com/auth/spreadsheets',), allow_legacy=True, legacy_bindings=('task_submission_sheet.sheets',)))
    registry.register('document_formatter', GoogleAuthorization('docs', '文档', 'docs', 'v1',
        ('https://www.googleapis.com/auth/documents',), allow_legacy=True))
    registry.register('ai_agent_assistant', GoogleAuthorization('sheets', 'Agent', 'sheets', 'v4',
        ('https://www.googleapis.com/auth/spreadsheets',), legacy_bindings=('sheets',)))
    return registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.storage = ApplicationStorage(self.temp.name)
        self.registry = GoogleAuthorizationRegistry()
        self.registry.register('example', DEFINITION)
        self.manager = GoogleAccountManager(self.storage, registry=self.registry)
        self.client = self.manager.add_client(CLIENT, 'Fixture')

    def login(self, scopes=(SCOPE,), subject='one'):
        return self.manager.save_login(self.client, {**IDENTITY, 'sub': subject}, {**token(), 'scopes': list(scopes)})

    def test_new_api_and_scope_work_without_changing_core_lists(self):
        account = self.login(); self.manager.bind('example.jobs', account)
        request = prepare_google_account('example', 'jobs', manager=self.manager)
        with patch('googleapiclient.discovery.build', return_value='tasks-service') as build:
            self.assertEqual(load_google_service(request, manager=self.manager), 'tasks-service')
        self.assertEqual(build.call_args.args, ('tasks', 'v1'))
        self.assertEqual(build.call_args.kwargs['http'].http.timeout, 30)
        self.assertNotIn('refresh_token', json.dumps(request))
        self.assertNotIn('fixture-secret', json.dumps(request))

    def test_two_plugins_same_purpose_are_isolated(self):
        self.registry.register('other', DEFINITION)
        one, two = self.login(subject='one'), self.login(subject='two')
        self.manager.bind_many({'example.jobs': one, 'other.jobs': two})
        self.assertEqual(prepare_google_account('example', 'jobs', manager=self.manager)['account_id'], one)
        self.assertEqual(prepare_google_account('other', 'jobs', manager=self.manager)['account_id'], two)

    def test_prepare_cannot_use_other_owner_or_unregistered_purpose(self):
        for owner, purpose in [('other', 'jobs'), ('example', 'missing')]:
            with self.assertRaises(ValueError): prepare_google_account(owner, purpose, manager=self.manager)
        with self.assertRaises(ValueError): self.manager.bind('unknown.jobs', 'anything')

    def test_same_owner_duplicate_or_alias_collision_is_atomic(self):
        before = self.registry.entries()
        for owner, definition in [('example', DEFINITION), ('other', replace(DEFINITION, legacy_bindings=('example.jobs',)))]:
            with self.assertRaises(ValueError): self.registry.register(owner, definition)
        self.assertEqual(self.registry.entries(), before)

    def test_invalid_declarations_reject_non_google_scopes_and_paths(self):
        for definition in [replace(DEFINITION, purpose_id='../x'), replace(DEFINITION, api='https://evil.test'),
                           replace(DEFINITION, scopes=('https://evil.test/auth/tasks',)),
                           replace(DEFINITION, scopes=[]), replace(DEFINITION, scopes=()),
                           replace(DEFINITION, legacy_bindings=('path/file.json',))]:
            with self.assertRaises(ValueError): self.registry.register('new', definition)

    def test_google_api_identifiers_are_not_restricted_to_old_lowercase_names(self):
        self.registry.register('analytics', replace(DEFINITION, api='youtubeAnalytics', version='v1.1'))
        self.registry.register('mail', replace(DEFINITION, api='gmail', scopes=('https://mail.google.com/',)))
        self.assertEqual(len(self.registry.entries()), 3)

    def test_scope_requirements_persist_when_plugin_is_uninstalled(self):
        account = self.login(); self.manager.bind('example.jobs', account)
        self.registry.unregister_owner('example')
        with self.assertRaisesRegex(ValueError, '已绑定'):
            self.login(scopes=('https://www.googleapis.com/auth/spreadsheets',))
        self.assertEqual(self.manager.bound_account('example.jobs')[0], account)
        with self.assertRaisesRegex(ValueError, '未注册'):
            load_authorization_service('example.jobs', manager=self.manager)

    def test_disabled_plugin_cannot_prepare_or_execute_but_keeps_binding(self):
        account = self.login(); self.manager.bind('example.jobs', account)
        request = prepare_google_account('example', 'jobs', manager=self.manager)
        self.registry.owner_enabled = lambda _owner: False
        self.assertEqual(self.registry.entries(active_only=True), ())
        with patch('googleapiclient.discovery.build') as build:
            with self.assertRaisesRegex(ValueError, '禁用'): load_google_service(request, manager=self.manager)
            with self.assertRaisesRegex(ValueError, '禁用'): prepare_google_account('example', 'jobs', manager=self.manager)
        build.assert_not_called()
        self.assertEqual(self.manager.bound_account('example.jobs')[0], account)
        self.registry.owner_enabled = lambda _owner: True
        self.assertEqual(len(self.registry.entries(active_only=True)), 1)

    def test_queue_rejects_account_or_declaration_changes(self):
        account = self.login(); self.manager.bind('example.jobs', account)
        request = prepare_google_account('example', 'jobs', manager=self.manager)
        two = self.login(subject='two'); self.manager.bind('example.jobs', two)
        with self.assertRaisesRegex(ValueError, '发生变化'): load_google_service(request, manager=self.manager)
        self.registry.unregister_owner('example')
        self.registry.register('example', replace(DEFINITION, version='v2'))
        with self.assertRaisesRegex(ValueError, '提交后改变'): load_google_service(request, manager=self.manager)

    def test_missing_new_permissions_prevents_queueing(self):
        account = self.login(); self.manager.bind('example.jobs', account)
        self.registry.unregister_owner('example')
        self.registry.register('example', replace(DEFINITION, scopes=(SCOPE, 'https://www.googleapis.com/auth/calendar.readonly')))
        with self.assertRaisesRegex(ValueError, '补充授权'): prepare_google_account('example', 'jobs', manager=self.manager)

    def test_existing_alias_preserved_and_unbind_does_not_restore_it(self):
        self.registry = fixture_registry(); self.manager.registry = self.registry
        key = self.login(scopes=('https://www.googleapis.com/auth/spreadsheets',))
        # Simulate an actual old encrypted file rather than new bind() normalization.
        with self.manager._transaction():
            state = self.manager._read(); state['bindings']['review_sheet.sheets'] = key; self.manager._write(state)
        before = self.manager.file.read_bytes()
        self.assertEqual(self.manager.snapshot()['bindings']['task_delivery.review_sheet'], key)
        with patch('googleapiclient.discovery.build', return_value='sheets'):
            self.assertEqual(load_managed_service('sheets', profile='review_sheet', manager=self.manager), 'sheets')
        self.assertEqual(before, self.manager.file.read_bytes())
        self.manager.bind('task_delivery.review_sheet', '')
        self.assertIsNone(self.manager.bound_account('review_sheet.sheets'))
        self.assertNotIn('review_sheet.sheets', self.manager.snapshot()['bindings'])

    def test_global_old_sheets_only_aliases_agent_not_other_plugins(self):
        self.manager.registry = fixture_registry()
        key = self.login(scopes=('https://www.googleapis.com/auth/spreadsheets',))
        with self.manager._transaction():
            state = self.manager._read(); state['bindings']['sheets'] = key; self.manager._write(state)
        self.assertEqual(self.manager.bound_account('ai_agent_assistant.sheets')[0], key)
        self.assertIsNone(self.manager.bound_account('task_delivery.review_sheet'))
        self.manager.bind('ai_agent_assistant.sheets', '')
        self.assertIsNone(self.manager.bound_account('ai_agent_assistant.sheets'))

    def test_no_registered_feature_falls_back_to_old_account_after_removal(self):
        account = self.login(); self.manager.bind('example.jobs', account); self.manager.remove(account)
        with self.assertRaisesRegex(ValueError, '停用'): load_authorization_service('example.jobs', manager=self.manager)


class MainWindow(QtWidgets.QWidget):
    def __init__(self): super().__init__(); self.config = {}
    def load_config(self): return dict(self.config)
    def appendLog(self, *_args, **_kwargs): pass


class HostRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.window = MainWindow(); self.host = PluginHost(self.window)
    def tearDown(self):
        self.window.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def test_declaration_registered_in_own_namespace_and_rolled_back_on_failure(self):
        class Broken:
            plugin_id = 'broken'
            def register(self, context):
                context.register_google_authorization(DEFINITION)
                raise RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError): self.host.install(Broken())
        self.assertIsNone(self.host.google_authorizations.resolve('broken.jobs'))
        self.assertEqual(self.host.google_authorizations.entries(), ())

    def test_real_plugins_declare_their_own_features(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        from app_plugins.builtin.document_formatter.plugin import DocumentFormatterPlugin
        from app_plugins.builtin.ai_agent_assistant.plugin import AIAgentAssistantPlugin
        from app_plugins.builtin.sheet_monitor import SheetMonitorPlugin
        for cls in (TaskDeliveryPlugin, DocumentFormatterPlugin, AIAgentAssistantPlugin, SheetMonitorPlugin): self.host.install(cls())
        keys = {entry.key for entry in self.host.google_authorizations.entries()}
        self.assertEqual(keys, {'sheet_monitor.sheets', 'task_delivery.drive', 'task_delivery.review_sheet',
            'task_delivery.task_submission_sheet', 'document_formatter.docs', 'ai_agent_assistant.sheets'})

    def test_context_cannot_prepare_unregistered_purpose(self):
        from app_plugins.host import PluginContext
        with self.assertRaises(ValueError): PluginContext(self.host, 'missing').prepare_google_account('jobs')

    def test_ui_is_generated_from_declarations_including_new_api(self):
        from PYUI.google_accounts_pyui import GoogleAccountsPage
        with tempfile.TemporaryDirectory() as directory:
            registry = GoogleAuthorizationRegistry(); registry.register('new_plugin', DEFINITION)
            manager = GoogleAccountManager(ApplicationStorage(directory), registry=registry)
            client = manager.add_client(CLIENT, 'Fixture')
            account = manager.save_login(client, IDENTITY, {**token(), 'scopes': [SCOPE]})
            manager.bind('new_plugin.jobs', account)
            page = GoogleAccountsPage(manager=manager); page.reload()
            self.assertEqual(set(page.permissions), {'new_plugin.jobs'})
            self.assertEqual(set(page.bindings), {'new_plugin.jobs'})
            registry.owner_enabled = lambda _owner: False; page.reload()
            self.assertEqual(page.bindings, {})
            page.save_bindings()  # Must not clear hidden plugin bindings.
            self.assertEqual(manager.bound_account('new_plugin.jobs')[0], account)
            registry.owner_enabled = lambda _owner: True; page.reload()
            self.assertEqual(page.bindings['new_plugin.jobs'].currentData(), account)
            page.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_new_oauth_scope_is_requested_by_ui_not_fixed_service_list(self):
        from PYUI.google_accounts_pyui import GoogleAccountsPage
        with tempfile.TemporaryDirectory() as directory:
            registry = GoogleAuthorizationRegistry(); registry.register('new_plugin', DEFINITION)
            manager = GoogleAccountManager(ApplicationStorage(directory), registry=registry)
            manager.add_client(CLIENT, 'Fixture')
            tasks = Mock(); tasks.records.return_value = []
            page = GoogleAccountsPage(manager=manager, tasks=tasks); page.reload()
            page.permissions['new_plugin.jobs'].setChecked(True); page.login()
            self.assertEqual(tasks.submit.call_args.kwargs['payload']['scopes'], [SCOPE])
            page.handle = None
            page.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_changed_scope_does_not_disable_login_or_clear_old_binding(self):
        from PYUI.google_accounts_pyui import GoogleAccountsPage
        with tempfile.TemporaryDirectory() as directory:
            registry = GoogleAuthorizationRegistry(); registry.register('example', DEFINITION)
            manager = GoogleAccountManager(ApplicationStorage(directory), registry=registry)
            client = manager.add_client(CLIENT, 'Fixture')
            account = manager.save_login(client, IDENTITY, {**token(), 'scopes': [SCOPE]})
            manager.bind('example.jobs', account)
            registry.unregister_owner('example')
            registry.register('example', replace(DEFINITION, scopes=(SCOPE, 'https://www.googleapis.com/auth/calendar.readonly')))
            page = GoogleAccountsPage(manager=manager, tasks=Mock()); page.reload()
            self.assertTrue(page.login_button.isEnabled())
            self.assertEqual(page.bindings['example.jobs'].currentData(), account)
            self.assertIn('补充授权', page.status.text())
            page.save_bindings()  # Fails safely, not an implicit unbind.
            self.assertEqual(manager.bound_account('example.jobs')[0], account)
            page.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__': unittest.main()

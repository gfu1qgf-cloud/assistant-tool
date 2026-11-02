import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.ai_agent_assistant import AIAgentAssistantPlugin
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot, spreadsheet_id, validate_payload
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS, headers, row_values, run_export, clipboard_tsv

URL = 'https://docs.google.com/spreadsheets/d/' + 'synthetic_sheet_1234567890' + '/edit'


def target(root, identifier='001', chinese='完整中文台词：保留所有内容和标点。'):
    task = SimpleNamespace(task_id=identifier, task_name='被截断标题', _full_task_name='完整中文台词：保留所有内容和标点。',
        task_type='FL口播', task_audio_text='Pane, prosím, požehnaj tento deň.', task_reference_link='',
        task_requirements='9:16，按原文制作', task_video_prompt='画面提示词', task_notes='',
        raw={'中文台词': chinese} if chinese is not None else {})
    return {'task': task, 'target_dir': str(Path(root)/identifier), 'row': 0}


class FakeSheets:
    def __init__(self, ids=(), header=None):
        self.ids = list(ids); self.header = header or [title for _key, title, _aliases in FIELDS]
        self.writes = []; self.fail = False; self.partial = False; self.validation = {}
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kwargs):
        if 'fields' in kwargs and 'ranges' not in kwargs:
            value = {'spreadsheetUrl': URL, 'sheets': [{'properties': {'sheetId': 0, 'title': '口播表格', 'gridProperties': {'rowCount': 1000, 'columnCount': 26}}}]}
        elif 'ranges' in kwargs:
            cells = [{} for _ in self.header]
            if self.validation: cells[self.header.index('执行状态')] = {'dataValidation': self.validation}
            value = {'sheets': [{'data': [{'rowData': [{'values': cells}]}]}]}
        elif kwargs['valueRenderOption'] == 'FORMULA': value = {'values': [self.header]}
        else: value = {'values': [[identifier] for identifier in self.ids]}
        return SimpleNamespace(execute=lambda **_opts: value)
    def append(self, **kwargs):
        def execute(**opts):
            self.writes.append((copy.deepcopy(kwargs), opts))
            index = self.header.index('作业ID')
            self.ids.extend(row[index] for row in kwargs['body']['values'])
            if self.fail: raise TimeoutError('ambiguous network timeout')
            return {'updates': {'updatedRows': 0 if self.partial else len(kwargs['body']['values'])}}
        return SimpleNamespace(execute=execute)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'private')
        self.payload = snapshot(target(self.root), 'sk')
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock(), is_canceled=Mock(return_value=False))
    def stage(self, payload=None):
        result = self.store.stage([payload or self.payload]); return (result['imported'] + result['unchanged'])[0]
    def export(self, api=None, ids=None, operation='submit'):
        return run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
            'ids': ids or [self.stage()], 'operation': operation}, self.run, service=api or FakeSheets())

    def test_snapshot_preserves_full_chinese_not_sanitized_truncated_name(self):
        value = snapshot(target(self.root, chinese=None), 'sk')
        self.assertEqual(value['chinese'], '完整中文台词：保留所有内容和标点。')
        self.assertFalse(value['chinese_confirmed'])
        with self.assertRaisesRegex(ValueError, '核对'): validate_payload(value)
        self.assertEqual(self.payload['task_id'], '001')
    def test_explicit_chinese_column_wins(self):
        self.assertEqual(snapshot(target(self.root, chinese='另一段中文台词'), 'sk')['chinese'], '另一段中文台词')
    def test_conflicting_chinese_columns_block_instead_of_guessing(self):
        value = target(self.root, chinese='甲'); value['task'].raw['中文文案'] = '乙'
        with self.assertRaises(ValueError): snapshot(value, 'sk')
    def test_no_language_guess_or_fake_translation(self):
        value = snapshot(target(self.root), 'unknown')
        with self.assertRaisesRegex(ValueError, '口播语言'): validate_payload(value)
    def test_missing_script_and_local_reference_block(self):
        for change in ({'spoken': ''}, {'chinese': ''}, {'reference': 'C:/private/file.jpg'}):
            with self.assertRaises(ValueError): validate_payload({**self.payload, **change})
    def test_private_or_unrelated_url_rejected(self):
        for value in ('http://docs.google.com/spreadsheets/d/abc', 'https://evil.test/spreadsheets/d/' + 'a'*30, 'file:///C:/secret'):
            with self.assertRaises(ValueError): spreadsheet_id(value)
    def test_job_ids_stable_after_restart_and_no_duplicate_import(self):
        key = self.stage(); other = Store(self.store.root.path)
        result = other.stage([self.payload])
        self.assertEqual(result['unchanged'], [key]); self.assertEqual(len(other.jobs()), 1)
    def test_distinct_project_directories_do_not_merge_same_task_number(self):
        self.stage(); self.store.stage([snapshot(target(self.root/'other'), 'sk')])
        self.assertEqual(len(self.store.jobs()), 2)
    def test_explicit_new_version_has_new_id(self):
        first = self.stage(); second = self.store.stage([self.payload], new_version=True)['imported'][0]
        self.assertNotEqual(first, second)
    def test_changed_draft_updates_but_submitted_never_overwritten(self):
        key = self.stage(); changed = {**self.payload, 'spoken': 'new'}
        self.store.stage([changed]); self.assertEqual(self.store.jobs([key])[0]['payload']['spoken'], 'new')
        self.store.mark([key], 'submitted', 'destination')
        result = self.store.stage([{**changed, 'spoken': 'different'}])
        self.assertEqual(result['blocked'], ['001'])
        self.assertEqual(self.store.jobs([key])[0]['payload']['spoken'], 'new')
    def test_headers_reordered_and_extra_columns_preserved(self):
        titles = [title for _key, title, _aliases in FIELDS][::-1] + ['客户额外字段']
        _row, mapping, count = headers([titles]); job = self.store.jobs([self.stage()])[0]
        row = row_values(job, mapping, count)
        self.assertTrue(row[mapping['spoken']].endswith(self.payload['spoken'])); self.assertIsNone(row[-1])
    def test_missing_duplicate_or_multiple_headers_block(self):
        titles = [title for _key, title, _aliases in FIELDS]
        for rows in ([titles + ['作业ID']], [titles, titles], [['任务编号']]):
            with self.assertRaises(ValueError): headers(rows)
    def test_api_append_raw_no_existing_row_updates_and_no_secret_path(self):
        api = FakeSheets(); result = self.export(api)
        self.assertEqual(result['added'], 1)
        request, options = api.writes[0]
        self.assertEqual(request['valueInputOption'], 'RAW'); self.assertEqual(request['insertDataOption'], 'OVERWRITE')
        self.assertEqual(options['num_retries'], 0)
        serialized = json.dumps(request, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertIn(self.payload['spoken'], serialized); self.assertIn(self.payload['chinese'], serialized)
    def test_second_click_and_restart_skip_cloud_job(self):
        api = FakeSheets(); self.export(api); result = self.export(api)
        self.assertEqual(len(api.writes), 1); self.assertEqual(result['confirmed'], 1)
    def test_timeout_after_remote_success_check_recovers_without_reappend(self):
        api = FakeSheets(); api.fail = True
        with self.assertRaisesRegex(RuntimeError, '核对'): self.export(api)
        self.assertEqual(self.store.jobs()[0]['state'], 'uncertain')
        result = self.export(api, operation='check')
        self.assertEqual(result['confirmed'], 1); self.assertEqual(len(api.writes), 1)
        self.assertEqual(self.store.jobs()[0]['state'], 'submitted')
    def test_crash_sending_record_not_replayed_when_remote_absent(self):
        key = self.stage(); job = self.store.jobs([key])[0]
        self.store.begin([job], spreadsheet_id(URL) + ':口播表格')
        api = FakeSheets(); result = self.export(api, [key])
        self.assertEqual(api.writes, []); self.assertEqual(result['blocked'], ['001'])
    def test_one_uncertain_job_does_not_block_other_tasks(self):
        first = self.stage(); self.store.begin(self.store.jobs([first]), spreadsheet_id(URL) + ':口播表格')
        second = self.stage(snapshot(target(self.root, '002'), 'sk'))
        api = FakeSheets(); result = self.export(api, [first, second])
        self.assertEqual(result['added'], 1); self.assertEqual(result['blocked'], ['001'])
    def test_invalid_dropdown_stops_before_any_write(self):
        api = FakeSheets(); api.validation = {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': '已完成'}]}}
        with self.assertRaisesRegex(ValueError, '下拉'): self.export(api)
        self.assertEqual(api.writes, [])
    def test_wrong_destination_does_not_silently_resubmit(self):
        key = self.stage(); self.store.mark([key], 'submitted', 'another:sheet')
        api = FakeSheets(); result = self.export(api, [key])
        self.assertEqual(result['blocked'], ['001']); self.assertEqual(api.writes, [])
    def test_current_payload_cannot_change_between_preview_and_begin(self):
        key = self.stage(); old = self.store.jobs([key])
        self.store.edit(key, 'spoken', 'changed')
        with self.assertRaises(ValueError): self.store.begin(old, 'destination')
    def test_cannot_delete_or_edit_submitted_and_uncertain_records(self):
        key = self.stage(); self.store.mark([key], 'uncertain', 'destination')
        self.store.remove_drafts([key]); self.assertEqual(len(self.store.jobs()), 1)
        with self.assertRaises(ValueError): self.store.edit(key, 'chinese', 'changed')
    def test_no_managed_account_fails_without_browser_or_legacy_token(self):
        self.stage()
        with patch('app_plugins.builtin.ai_agent_assistant.sheets.load_managed_service', return_value=None), patch('webbrowser.open') as browser:
            with self.assertRaisesRegex(ValueError, 'API授权'):
                run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
                            'ids': [self.store.jobs()[0]['id']], 'operation': 'submit'}, self.run)
        browser.assert_not_called(); self.assertEqual(self.store.jobs()[0]['state'], 'draft')
    def test_clipboard_formula_injection_guard_and_multiline(self):
        key = self.stage(); self.store.edit(key, 'spoken', '=HYPERLINK("bad")\nsecond line')
        self.store.edit(key, 'chinese', '=不应作为公式')
        text = clipboard_tsv(self.store.jobs([key]))
        self.assertIn("'=不应作为公式", text); self.assertIn('second line', text)
    def test_queue_connect_closes_native_database_handle(self):
        self.stage(); self.store.file.path.rename(self.store.file.path.with_suffix('.moved'))
    def test_invalid_job_does_not_block_valid_job(self):
        first = self.stage(); second = self.stage({**snapshot(target(self.root, '002'), 'sk'), 'spoken': ''})
        result = self.export(FakeSheets(), [first, second])
        self.assertEqual(result['added'], 1); self.assertEqual(result['invalid'], ['002'])
    def test_pending_journal_not_deleted_by_cancellation_before_network(self):
        from model.BackgroundTasks import BackgroundTaskCanceled
        self.stage(); self.run.check_canceled.side_effect = BackgroundTaskCanceled()
        api = FakeSheets()
        with self.assertRaises(BackgroundTaskCanceled): self.export(api)
        self.assertEqual(api.writes, []); self.assertEqual(self.store.jobs()[0]['state'], 'draft')
    def test_partial_write_confirmation_remains_uncertain(self):
        api = FakeSheets(); api.partial = True
        with self.assertRaises(RuntimeError): self.export(api)
        self.assertEqual(self.store.jobs()[0]['state'], 'uncertain')
    def test_queued_operation_cannot_silently_switch_google_account(self):
        from model.GoogleAccounts import load_managed_service
        manager = Mock(); manager.bound_account.return_value = ('different-account', {})
        with patch('googleapiclient.discovery.build') as build:
            with self.assertRaisesRegex(ValueError, '未自动换号'):
                load_managed_service('sheets', manager=manager, expected_account_id='submitted-account')
        build.assert_not_called()


class AgentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        from model.AppDataStorage import data_directory
        self.main = QtWidgets.QWidget(); self.plugin = AIAgentAssistantPlugin()
        self.context = SimpleNamespace(storage=data_directory(self.temp.name), parent_widget=self.main,
            register_command=Mock(), register_main_widget=Mock(), register_google_authorization=Mock(),
            prepare_google_account=Mock(return_value={'authorization_id': 'ai_agent_assistant.sheets', 'account_id': 'fixture-account'}),
            task_language=lambda _task: 'sk',
            task_targets=lambda _rows: [target(self.temp.name)], log=Mock(),
            available_nodes=lambda: SimpleNamespace(definitions={'script_segmenter.split': object()}))
        self.plugin.register(self.context)
    def tearDown(self):
        self.main.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def test_register_does_not_create_files_or_network(self):
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(self.context.register_command.call_count, 2)
        self.assertEqual(self.context.register_main_widget.call_count, 1)
    def test_corrupt_queue_is_visible_error_not_unhandled_qt_callback(self):
        self.plugin.store.file.write_bytes(b'not a database')
        with patch.object(QtWidgets.QMessageBox, 'warning') as warning:
            self.assertIsNone(self.plugin.open_ui())
        warning.assert_called_once()
    def test_storage_failure_during_refresh_is_inline_not_qt_exception(self):
        dialog = self.plugin.open_dialog()
        with patch.object(self.plugin.store, 'jobs', side_effect=OSError('数据卷不可用')):
            dialog.reload()
        self.assertIn('数据卷不可用', dialog.status.text())
    def test_dialog_single_oral_tab_and_import_uses_full_scripts(self):
        dialog = self.plugin.open_dialog(); self.plugin.import_rows([0])
        tabs = dialog.findChild(QtWidgets.QTabWidget)
        self.assertEqual(tabs.count(), 1); self.assertEqual(tabs.tabText(0), '口播')
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 4).text(), '完整中文台词：保留所有内容和标点。')
        self.assertIs(self.plugin.open_dialog(), dialog)
    def test_import_does_not_share_current_task_object_with_worker(self):
        self.plugin.import_rows([0]); jobs = self.plugin.store.jobs()
        self.assertNotIn('task', jobs[0]['payload']); self.assertNotIn('credentials', jobs[0]['payload'])
    def test_host_google_account_window_reuses_core_registry(self):
        from PYUI.google_accounts_pyui import open_google_accounts
        dialog = open_google_accounts(self.main, None)
        self.assertIs(open_google_accounts(self.main, None), dialog)
        self.assertEqual(dialog.account_page.objectName(), 'google_accounts_page')
    def test_fallback_chinese_requires_confirmation_before_export(self):
        self.context.task_targets = lambda _rows: [target(self.temp.name, chinese=None)]
        dialog = self.plugin.open_dialog(); self.plugin.import_rows([0])
        self.assertEqual(len(dialog.table.selectionModel().selectedRows()), 1)
        self.assertFalse(self.plugin.store.jobs()[0]['payload']['chinese_confirmed'])
        dialog.chinese_source.setChecked(True); self.plugin.import_rows([0])
        self.assertEqual(len(dialog.table.selectionModel().selectedRows()), 1)
    def test_real_host_worker_survives_dialog_close_and_finishes_cleanly(self):
        import threading
        import time
        from model.BackgroundTasks import BackgroundTaskManager
        from app_plugins.builtin.ai_agent_assistant import plugin as plugin_module
        manager = BackgroundTaskManager(logger=lambda *_args: None)
        self.context.submit_background_task = lambda **kwargs: manager.submit(owner='ai_agent_assistant', **kwargs)
        dialog = self.plugin.open_dialog(); self.plugin.import_rows([0])
        release = threading.Event(); entered = threading.Event(); ids = []
        def runner(data, run):
            ids.append(threading.get_ident()); entered.set()
            run.report('模拟提交中', 50); release.wait(3)
            return {'message': '模拟提交完成'}
        try:
            with patch.object(plugin_module, 'run_export', new=runner):
                self.plugin.submit({'url': URL, 'sheet': '口播表格'}, [self.plugin.store.jobs()[0]['id']], 'submit')
                with self.assertRaises(ValueError): self.plugin.submit({'url': URL, 'sheet': '口播表格'}, [], 'submit')
                deadline = time.monotonic() + 5
                while not entered.is_set() and time.monotonic() < deadline:
                    self.app.processEvents(); time.sleep(.005)
                self.assertTrue(entered.is_set()); self.assertNotEqual(ids[0], threading.get_ident())
                dialog.close(); self.assertFalse(dialog.isVisible()); release.set()
                while self.plugin.handle is not None and time.monotonic() < deadline:
                    self.app.processEvents(); time.sleep(.005)
                self.assertIsNone(self.plugin.handle); self.assertTrue(self.plugin.can_close()[0])
        finally:
            release.set(); manager.cancel_all()
            deadline = time.monotonic() + 5
            while manager.is_busy() and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.005)
            manager.shutdown(); manager.deleteLater(); self.app.processEvents()


if __name__ == '__main__': unittest.main()

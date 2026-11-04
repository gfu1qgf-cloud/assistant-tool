"""Synthetic receipt queues and mock Google services; never use real accounts."""
import copy
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskCanceled
from app_plugins.builtin.ai_agent_assistant.delivery import run_receipts
from app_plugins.builtin.ai_agent_assistant.delivery_drive import (
    collect_files, download_file, result_links, output_directory, cached_file, error_message, FOLDER)
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot, spreadsheet_id
from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS
from app_plugins.builtin.ai_agent_assistant.plugin import AIAgentAssistantPlugin
from test_ai_agent_assistant import target, URL

LINK = 'https://drive.google.com/file/d/synthetic_video/view'
COLS = {key: index for index, (key, _title, _aliases) in enumerate(FIELDS)}


class ReceiptSheets:
    def __init__(self, rows):
        self.rows = rows; self.writes = []; self.inspected = []; self.fail_write = False
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kwargs):
        if 'ranges' in kwargs:
            result = {'sheets': [{'data': [{'rowData': [{'values': [{'dataValidation': {
                'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': '未下载'}, {'userEnteredValue': '已下载'}]}}}]}]}]}]}
        elif 'fields' in kwargs:
            self.inspected.append(kwargs['spreadsheetId'])
            result = {'spreadsheetUrl': URL, 'sheets': [{'properties': {'sheetId': 0, 'title': '口播表格',
                'gridProperties': {'rowCount': 1000, 'columnCount': len(FIELDS)}}}]}
        elif re.search(r'!A1:', kwargs['range']): result = {'values': [[title for _key, title, _aliases in FIELDS]]}
        else: result = {'values': copy.deepcopy(self.rows)}
        return SimpleNamespace(execute=lambda **_options: result)
    def update(self, **kwargs):
        def execute(**options):
            self.writes.append(copy.deepcopy(kwargs))
            if self.fail_write: raise TimeoutError('simulated acknowledgement timeout')
            row = int(re.search(r'(\d+)$', kwargs['range']).group(1)) - 2
            self.rows[row][COLS['archive']] = kwargs['body']['values'][0][0]
            return {'updatedCells': 1}
        return SimpleNamespace(execute=execute)


class AgentReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'private')
        self.store.save_settings({'url': URL, 'sheet': '口播表格'})
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock())
        self.job = self.stage('001')
        self.row = self.cloud(self.job)
        self.api = ReceiptSheets([self.row]); self.fetch = Mock(side_effect=self.download)
        self.items = [{'id': 'synthetic_video', 'url': LINK}]
    def stage(self, identifier):
        value = snapshot(target(self.root, identifier), 'sk'); Path(value['target_dir']).mkdir()
        key = self.store.stage([value])['imported'][0]
        self.store.mark([key], 'submitted', spreadsheet_id(URL) + ':口播表格')
        return self.store.jobs([key])[0]
    def cloud(self, job, **changes):
        values = {'job_id': job['id'], 'task_id': job['payload']['task_id'], 'status': '已完成',
                  'result': LINK, 'archive': '未下载', **changes}
        return [values.get(key, '') for key, _title, _aliases in FIELDS]
    def download(self, job, item, directory, drive, run):
        path = directory/(job['id'] + '-' + item['id'] + '.mp4'); path.write_bytes(b'synthetic-video')
        return {'state': 'downloaded', 'path': str(path), 'size': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'retry_at': 0}
    def receipt(self, *, collect=None, clock=1000, **options):
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files', return_value=self.items) as gather:
            if collect: gather.side_effect = collect
            return run_receipts({'root': str(self.store.root.path), **options}, self.run, sheets=self.api,
                drive=object(), fetch=self.fetch, clock=lambda: clock)

    def test_completed_job_downloads_to_original_task_and_only_updates_archive_cell(self):
        result = self.receipt()
        self.assertEqual(result['downloaded'], 1)
        record = self.store.delivery_files(self.job['id'])['synthetic_video']
        self.assertEqual(Path(record['path']).parent, Path(self.job['payload']['target_dir'])/'result')
        self.assertEqual(self.api.writes[0]['range'], "'口播表格'!Q2")
        self.assertEqual(self.api.writes[0]['body'], {'values': [['已下载']]})
        self.assertEqual(self.api.writes[0]['valueInputOption'], 'RAW')
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'], '已下载')

    def test_repeated_poll_does_not_redownload_or_rewrite_completed_row(self):
        self.receipt(); self.receipt()
        self.fetch.assert_called_once(); self.assertEqual(len(self.api.writes), 1)

    def test_forced_recollection_downloads_cached_file_without_changing_submission(self):
        self.receipt(); old = self.store.delivery_files(self.job['id'])['synthetic_video']
        self.receipt(force=True, ids=[self.job['id']])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertTrue(Path(old['path']).is_file())
        self.assertEqual(self.store.jobs()[0]['state'], 'submitted')
        self.receipt(); self.assertEqual(self.fetch.call_count, 2)
        self.assertNotIn('force_download', self.store.delivery_files(self.job['id'])['synthetic_video'])

    def test_force_requires_explicit_selection_before_any_mutation(self):
        self.receipt()
        with self.assertRaisesRegex(ValueError, '明确选择'): self.receipt(force=True)
        self.fetch.assert_called_once()

    def test_force_directory_selection_only_uses_latest_submitted_job(self):
        self.receipt()
        payload = self.job['payload'].copy(); payload['spoken'] += ' new version'
        newest = self.store.stage([payload], new_version=True)['imported'][0]
        self.store.mark([newest], 'submitted', self.job['destination'])
        newjob = self.store.jobs([newest])[0]; self.api.rows.append(self.cloud(newjob))
        unrelated = self.stage('002'); self.api.rows.append(self.cloud(unrelated))
        self.fetch.reset_mock()
        self.receipt(force=True, directories=[self.job['payload']['target_dir']])
        self.fetch.assert_called_once(); self.assertEqual(self.fetch.call_args.args[0]['id'], newest)
        self.assertEqual(self.store.delivery_files(unrelated['id']), {})

    def test_forced_folder_larger_than_budget_continues_next_poll_without_repeating_success(self):
        self.items = [{'id': 'video_' + str(index), 'url': LINK} for index in range(10)]
        self.receipt(); self.receipt(); self.assertEqual(self.fetch.call_count, 10)
        self.receipt(force=True, ids=[self.job['id']]); self.assertEqual(self.fetch.call_count, 18)
        records = self.store.delivery_files(self.job['id'])
        self.assertEqual(sum(bool(value.get('force_download')) for value in records.values()), 2)
        self.receipt(); self.assertEqual(self.fetch.call_count, 20)
        self.receipt(); self.assertEqual(self.fetch.call_count, 20)

    def test_failed_forced_recollection_preserves_old_file_and_retries_after_restart(self):
        self.receipt(); old = self.store.delivery_files(self.job['id'])['synthetic_video']
        self.fetch.side_effect = TimeoutError('temporary')
        self.receipt(force=True, ids=[self.job['id']])
        self.assertTrue(Path(old['path']).is_file())
        self.assertTrue(Store(self.store.root.path).delivery_files(self.job['id'])['synthetic_video']['force_download'])
        self.fetch.side_effect = self.download; self.receipt(clock=1061)
        self.assertEqual(self.fetch.call_count, 3)

    def test_forced_recollection_keeps_cloud_identity_and_completion_guards(self):
        self.receipt(); self.api.rows[0][COLS['status']] = '执行中'
        self.receipt(force=True, ids=[self.job['id']]); self.fetch.assert_called_once()
        self.api.rows[0][COLS['status']] = '已完成'; self.api.rows[0][COLS['task_id']] = 'other'
        self.receipt(); self.fetch.assert_called_once()

    def test_missing_local_file_is_downloaded_again_even_if_cloud_says_downloaded(self):
        self.receipt()
        Path(self.store.delivery_files(self.job['id'])['synthetic_video']['path']).unlink()
        self.receipt(); self.assertEqual(self.fetch.call_count, 2)

    def test_status_not_completed_never_downloads_even_with_link(self):
        self.api.rows[0][COLS['status']] = '执行中'; self.receipt()
        self.fetch.assert_not_called(); self.assertFalse(self.api.writes)

    def test_completed_without_link_remains_waiting(self):
        self.api.rows[0][COLS['result']] = ''; self.receipt()
        self.fetch.assert_not_called()
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'], '等待成品链接')

    def test_duplicate_cloud_ids_never_guess_a_row(self):
        self.api.rows.append(copy.deepcopy(self.row)); self.receipt()
        self.fetch.assert_not_called(); self.assertFalse(self.api.writes)

    def test_task_number_mismatch_is_not_downloaded(self):
        self.api.rows[0][COLS['task_id']] = 'other'; self.receipt(); self.fetch.assert_not_called()

    def test_missing_original_directory_does_not_fall_back_to_current_project(self):
        Path(self.job['payload']['target_dir']).rmdir(); self.receipt()
        self.fetch.assert_not_called(); self.assertFalse(Path(self.job['payload']['target_dir']).exists())

    def test_retry_backoff_persists_and_manual_retry_clears_due_time(self):
        self.fetch.side_effect = TimeoutError('temporary')
        self.receipt(); self.receipt(clock=1010)
        self.assertEqual(self.fetch.call_count, 1)
        entry = self.store.delivery_files(self.job['id'])['synthetic_video']
        self.assertEqual(entry['attempts'], 1); self.assertEqual(entry['retry_at'], 1060)
        self.store.retry_deliveries([self.job['id']]); self.fetch.side_effect = self.download
        self.receipt(clock=1010); self.assertEqual(self.fetch.call_count, 2)

    def test_failed_job_does_not_prevent_other_task_from_downloading(self):
        second = self.stage('002'); self.api.rows.append(self.cloud(second))
        def fetch(job, *args):
            if job['id'] == self.job['id']: raise TimeoutError('temporary')
            return self.download(job, *args)
        self.fetch.side_effect = fetch; result = self.receipt()
        self.assertEqual(result['downloaded'], 1); self.assertEqual(result['failed'], 1)

    def test_partial_folder_only_retries_missing_file_and_acknowledges_when_complete(self):
        self.items.append({'id': 'second_video', 'url': LINK})
        def fetch(job, item, *args):
            if item['id'] == 'second_video': raise OSError('retry second')
            return self.download(job, item, *args)
        self.fetch.side_effect = fetch; self.receipt(); self.assertFalse(self.api.writes)
        self.fetch.side_effect = self.download; self.receipt(clock=1061)
        self.assertEqual(self.fetch.call_count, 3); self.assertEqual(len(self.api.writes), 1)

    def test_row_moved_during_download_is_relocated_by_job_id(self):
        unrelated = self.cloud({'id': 'another', 'payload': {'task_id': 'unrelated'}})
        self.api.rows.append(unrelated)
        def fetch(*args): self.api.rows.reverse(); return self.download(*args)
        self.fetch.side_effect = fetch; self.receipt()
        self.assertEqual(self.api.writes[0]['range'], "'口播表格'!Q3")

    def test_link_changed_during_download_is_not_marked_downloaded(self):
        def fetch(*args):
            self.api.rows[0][COLS['result']] = 'https://drive.google.com/file/d/new_result/view'
            return self.download(*args)
        self.fetch.side_effect = fetch; self.receipt(); self.assertFalse(self.api.writes)
        self.assertEqual(self.store.delivery_statuses()[self.job['id']]['status'], '已下载·待回写')

    def test_ack_timeout_keeps_local_file_and_retries_only_ack(self):
        self.api.fail_write = True; self.receipt(); self.api.fail_write = False; self.receipt()
        self.fetch.assert_called_once(); self.assertEqual(len(self.api.writes), 2)

    def test_current_target_setting_does_not_redirect_previous_submissions(self):
        self.store.save_settings({'url': URL.replace('synthetic_sheet_', 'new_sheet_'), 'sheet': '别的分页'})
        self.receipt(); self.assertEqual(self.api.inspected, [spreadsheet_id(URL)])

    def test_copy_pasted_job_can_be_found_by_exact_id_without_resubmitting(self):
        self.store.mark([self.job['id']], 'draft', '')
        self.receipt(); self.assertEqual(self.store.jobs()[0]['state'], 'submitted')
        self.assertEqual(len(self.api.writes), 1)
    def test_manual_selected_retry_does_not_collect_unselected_task(self):
        second = self.stage('002'); self.api.rows.append(self.cloud(second))
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files', return_value=self.items):
            result = run_receipts({'root': str(self.store.root.path), 'ids': [self.job['id']]}, self.run,
                sheets=self.api, drive=object(), fetch=self.fetch)
        self.assertEqual(result['downloaded'], 1)
        self.assertEqual(self.fetch.call_args.args[0]['id'], self.job['id'])
        self.assertEqual(self.store.delivery_files(second['id']), {})

    def test_empty_folder_or_bad_link_is_not_success(self):
        self.receipt(collect=ValueError('folder empty'))
        self.fetch.assert_not_called(); self.assertFalse(self.api.writes)
    def test_discovery_failures_back_off_but_changed_link_is_checked_immediately(self):
        self.receipt(collect=ValueError('folder inaccessible'))
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files', return_value=self.items) as gather:
            run_receipts({'root': str(self.store.root.path)}, self.run, sheets=self.api, drive=object(), fetch=self.fetch, clock=lambda: 1010)
            gather.assert_not_called()
        self.api.rows[0][COLS['result']] = LINK + '?new_source=1'
        self.receipt(clock=1010); self.fetch.assert_called_once()
    def test_manual_retry_also_releases_folder_discovery_backoff(self):
        self.receipt(collect=ValueError('folder inaccessible'))
        self.store.retry_deliveries([self.job['id']]); self.receipt(clock=1010)
        self.fetch.assert_called_once()

    def test_cancel_releases_run_lease_without_ack(self):
        self.fetch.side_effect = BackgroundTaskCanceled('stop')
        self.run.check_canceled.side_effect = lambda: None
        # Cancellation signaled by a worker remains visible to the outer handler.
        def canceled(*args):
            self.run.check_canceled.side_effect = BackgroundTaskCanceled('stop')
            raise BackgroundTaskCanceled('stop')
        self.fetch.side_effect = canceled
        with self.assertRaises(BackgroundTaskCanceled): self.receipt()
        self.assertFalse(self.api.writes); self.assertTrue(self.store.claim_delivery_run('next'))

    def test_second_process_cannot_claim_active_lease_or_release_another_owner(self):
        self.assertTrue(self.store.claim_delivery_run('first', clock=1000))
        self.assertFalse(self.store.claim_delivery_run('second', clock=1001))
        self.store.release_delivery_run('second')
        self.assertFalse(self.store.claim_delivery_run('second', clock=1002))
        self.assertTrue(self.store.claim_delivery_run('second', clock=1181))
        with self.assertRaises(RuntimeError): self.store.renew_delivery_run('first', clock=1182)


class DriveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.job = {'id': 'a'*32, 'payload': {'target_dir': str(self.directory)}}
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock())
        self.bytes = b'synthetic-video-content'
        self.item = {'id': 'synthetic_video', 'url': LINK, 'name': 'video.mp4', 'size': str(len(self.bytes)),
                     'md5Checksum': hashlib.md5(self.bytes).hexdigest()}
        self.drive = Mock(); self.drive.files.return_value.get_media.return_value = SimpleNamespace(headers={})
    def transfer(self, data=None, verify=None):
        data = self.bytes if data is None else data
        class Downloader:
            def __init__(self, stream, request, chunksize): self.stream = stream
            def next_chunk(self, num_retries): self.stream.write(data); return None, True
        with patch('googleapiclient.http.MediaIoBaseDownload', Downloader):
            return download_file(self.job, self.item, self.directory, self.drive, self.run, verify=verify or Mock())
    def test_verified_file_is_atomically_published_and_cached(self):
        record = self.transfer()
        self.assertEqual(Path(record['path']).read_bytes(), self.bytes)
        self.assertTrue(cached_file(record, self.directory))
        self.assertFalse(list(self.directory.glob('*.pending*')))
    def test_same_filename_from_different_drive_id_has_distinct_destination(self):
        first = self.transfer(); self.item['id'] = 'different_video'; self.item['url'] = LINK.replace('synthetic_video', 'different_video')
        second = self.transfer(); self.assertNotEqual(first['path'], second['path'])
    def test_existing_different_content_is_never_overwritten(self):
        first = self.transfer(); path = Path(first['path']); path.write_bytes(b'existing unrelated bytes')
        with self.assertRaisesRegex(ValueError, '未覆盖'): self.transfer()
        self.assertEqual(path.read_bytes(), b'existing unrelated bytes')
    def test_updated_drive_file_preserves_previous_version_under_distinct_name(self):
        first = self.transfer(); new = b'x' * len(self.bytes); self.item['md5Checksum'] = hashlib.md5(new).hexdigest()
        second = self.transfer(new)
        self.assertNotEqual(first['path'], second['path']); self.assertEqual(Path(first['path']).read_bytes(), self.bytes)
    def test_existing_equal_content_after_crash_can_be_recovered(self):
        first = self.transfer(); second = self.transfer(); self.assertEqual(first['path'], second['path'])
    def test_same_id_without_remote_checksum_preserves_old_bytes_and_deduplicates_equal_bytes(self):
        del self.item['md5Checksum']
        first = self.transfer(); repeated = self.transfer()
        self.assertEqual(first['path'], repeated['path'])
        new = b'x' * len(self.bytes); second = self.transfer(new)
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(Path(first['path']).read_bytes(), self.bytes)
        self.assertEqual(Path(second['path']).read_bytes(), new)
        self.assertEqual(len(list(self.directory.glob('*.mp4'))), 2)
    def test_legacy_public_filename_is_reused_when_contents_are_equal(self):
        del self.item['md5Checksum']
        identity = self.item['id'] + '\0'
        prefix = f"Agent_{self.job['id'][:12]}_{hashlib.sha256(identity.encode()).hexdigest()[:12]}_"
        legacy = self.directory/(prefix + self.item['name']); legacy.write_bytes(self.bytes)
        result = self.transfer()
        self.assertEqual(Path(result['path']), legacy)
        self.assertEqual(len(list(self.directory.glob('*.mp4'))), 1)
    def test_truncated_or_wrong_checksum_file_is_not_published(self):
        with self.assertRaisesRegex(ValueError, '长度'): self.transfer(b'truncated')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.item['md5Checksum'] = '0'*32
        with self.assertRaisesRegex(ValueError, '校验'): self.transfer()
    def test_unplayable_file_is_not_published(self):
        with self.assertRaises(ValueError): self.transfer(verify=Mock(side_effect=ValueError('no video track')))
        self.assertEqual(list(self.directory.iterdir()), [])
    def test_result_links_only_accept_drive_https_and_limit_link_count(self):
        self.assertEqual(result_links('成品：' + LINK), [LINK])
        for text in ('http://drive.google.com/file/d/test/view', 'https://example.test/video.mp4', '本地 C:/secret.mp4'):
            with self.assertRaises(ValueError): result_links(text)
    def test_public_folder_requires_explicit_drive_account(self):
        with self.assertRaisesRegex(ValueError, 'Drive 读取授权'):
            collect_files(['https://drive.google.com/drive/folders/synthetic_folder'], None, self.run)
    def test_folder_enumeration_finds_nested_videos_and_deduplicates_ids(self):
        api = Mock()
        api.files.return_value.get.return_value.execute.return_value = {'id': 'root_folder', 'name': 'root', 'mimeType': FOLDER}
        pages = [ {'files': [{'id': 'nested', 'mimeType': FOLDER}, {'id': 'img', 'mimeType': 'image/png', 'name': 'image.png'}]},
                  {'files': [{'id': 'video', 'mimeType': 'video/mp4', 'name': 'part.mp4', 'size': '25'}]} ]
        api.files.return_value.list.return_value.execute.side_effect = pages
        result = collect_files(['https://drive.google.com/drive/folders/root_folder'], api, self.run)
        self.assertEqual([item['id'] for item in result], ['video'])
    def test_repeated_folder_pagination_is_bounded(self):
        api = Mock(); api.files.return_value.get.return_value.execute.return_value = {'id': 'root_folder', 'mimeType': FOLDER}
        api.files.return_value.list.return_value.execute.return_value = {'nextPageToken': 'same', 'files': []}
        with self.assertRaisesRegex(ValueError, '重复分页'):
            collect_files(['https://drive.google.com/drive/folders/root_folder'], api, self.run)
    def test_empty_folder_does_not_count_as_delivered(self):
        api = Mock(); api.files.return_value.get.return_value.execute.return_value = {'id': 'root_folder', 'mimeType': FOLDER}
        api.files.return_value.list.return_value.execute.return_value = {'files': []}
        with self.assertRaisesRegex(ValueError, '暂时没有'):
            collect_files(['https://drive.google.com/drive/folders/root_folder'], api, self.run)
    def test_incomplete_drive_search_does_not_mark_partial_folder_complete(self):
        api = Mock(); api.files.return_value.get.return_value.execute.return_value = {'id': 'root_folder', 'mimeType': FOLDER}
        api.files.return_value.list.return_value.execute.return_value = {'incompleteSearch': True, 'files': []}
        with self.assertRaisesRegex(ValueError, '不完整'):
            collect_files(['https://drive.google.com/drive/folders/root_folder'], api, self.run)
    def test_drive_404_reports_nonexistence_or_permission_not_success(self):
        error = RuntimeError('SDK diagnostic')
        error.resp = SimpleNamespace(status=404); error.uri = 'https://www.googleapis.com/drive/v3/files/private'
        text = error_message(error)
        self.assertIn('404', text); self.assertIn('无权访问', text); self.assertNotIn('private', text)
    def test_real_video_bytes_pass_real_ffprobe_before_publication(self):
        import subprocess
        from app_plugins.builtin.ai_agent_assistant.delivery_drive import verify_video
        from model.node_workflow.render import resolve_tools
        ffmpeg, _ffprobe = resolve_tools()
        source = self.directory/'synthetic-source.mp4'
        result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x64:d=0.3', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0)
        data = source.read_bytes(); source.unlink()
        self.item['size'] = str(len(data)); self.item['md5Checksum'] = hashlib.md5(data).hexdigest()
        record = self.transfer(data, verify=verify_video)
        self.assertTrue(cached_file(record, self.directory))
        self.assertGreater(verify_video(Path(record['path']))['duration'], 0)


class Handle(QtCore.QObject):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    progressChanged = QtCore.pyqtSignal(str, int)
    finished = QtCore.pyqtSignal()
    def requestInterruption(self): self.canceled = True


class ReceiptMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        from model.AppDataStorage import data_directory
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.window = QtWidgets.QWidget(); self.plugin = AIAgentAssistantPlugin()
        self.handle = Handle(); self.handle.canceled = False
        self.context = SimpleNamespace(parent_widget=self.window, storage=data_directory(self.temp.name),
            register_command=Mock(), register_main_widget=Mock(), register_google_authorization=Mock(),
            prepare_google_account=Mock(return_value={'authorization_id': 'synthetic', 'account_id': 'synthetic'}),
            submit_background_task=Mock(return_value=self.handle), log=Mock())
        self.plugin.register(self.context)
    def tearDown(self): self.window.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def stage(self):
        self.plugin.store.save_settings({'url': URL, 'sheet': '口播表格'})
        self.plugin.store.stage([snapshot(target(self.temp.name), 'sk')])
    def test_register_has_independent_drive_purpose_and_no_network(self):
        purposes = [call.args[0].purpose_id for call in self.context.register_google_authorization.call_args_list]
        self.assertEqual(purposes, ['sheets', 'downloads'])
        self.context.submit_background_task.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
    def test_no_jobs_or_disabled_auto_mode_does_not_submit_background_work(self):
        self.plugin.receipts.check(); self.context.submit_background_task.assert_not_called()
        self.stage(); self.plugin.store.save_settings({'url': URL, 'sheet': '口播表格', 'auto_download': False})
        self.plugin.receipts.check(); self.context.submit_background_task.assert_not_called()
    def test_active_receipt_is_not_duplicated_and_stop_requests_cancel(self):
        self.stage()
        with patch.object(self.plugin.receipts, 'download_authorization', return_value=None):
            self.plugin.receipts.check(); self.plugin.receipts.check(manual=True)
        self.context.submit_background_task.assert_called_once()
        self.assertFalse(self.plugin.stop()); self.assertTrue(self.handle.canceled)
        self.handle.finished.emit(); self.assertIsNone(self.plugin.receipts.handle)
    def test_force_scope_is_carried_to_existing_worker_and_busy_click_does_not_duplicate(self):
        self.stage()
        with patch.object(self.plugin.receipts, 'download_authorization', return_value=None):
            self.plugin.receipts.check(manual=True, directories=['C:/synthetic/task'], force=True)
            self.plugin.receipts.check(manual=True, directories=['C:/other/task'], force=True)
        self.context.submit_background_task.assert_called_once()
        payload = self.context.submit_background_task.call_args.kwargs['payload']
        self.assertEqual(payload['directories'], ['C:/synthetic/task']); self.assertTrue(payload['force'])
        self.handle.finished.emit()
    def test_manual_result_is_visible_without_opening_dialog_even_when_no_new_files(self):
        self.context.notify = Mock(); self.plugin.receipts.manual_notice = True
        self.plugin.receipts.completed({'message': '本次下载0个视频', 'downloaded': 0})
        self.context.notify.assert_called_once_with('AI Agent 成品', '本次下载0个视频')
        self.assertIsNone(self.plugin.dialog)
    def test_assistant_recollect_passes_exact_selected_jobs_after_confirmation(self):
        self.stage(); self.context.task_language = lambda _task: 'sk'
        dialog = self.plugin.open_dialog(); dialog.table.selectAll()
        ids = dialog.selected_ids()
        with patch.object(self.plugin, 'request_recollect') as recollect:
            dialog.actions['recollect_download'].trigger()
        recollect.assert_called_once_with(ids=ids)
    def test_same_plugin_capable_account_can_seed_download_binding(self):
        manager = Mock()
        manager.bound_account.side_effect = [None, ('chosen', {'credentials': {'scope': 'https://www.googleapis.com/auth/drive'}})]
        with patch('model.GoogleAccounts.GoogleAccountManager', return_value=manager), patch('model.GoogleAccounts.token_scopes', return_value={'https://www.googleapis.com/auth/drive'}):
            self.plugin.receipts.download_authorization()
        manager.bind.assert_called_once_with('ai_agent_assistant.downloads', 'chosen')
    def test_explicit_download_account_is_never_replaced(self):
        manager = Mock(); manager.bound_account.return_value = ('other', {})
        with patch('model.GoogleAccounts.GoogleAccountManager', return_value=manager): self.plugin.receipts.download_authorization()
        manager.bind.assert_not_called(); manager.bound_account.assert_called_once_with('ai_agent_assistant.downloads')
    def test_closing_dialog_keeps_receipt_owned_by_plugin(self):
        self.stage()
        self.context.task_language = lambda _task: 'sk'
        dialog = self.plugin.open_dialog()
        with patch.object(self.plugin.receipts, 'download_authorization', return_value=None): self.plugin.receipts.check(manual=True)
        dialog.close()
        self.assertFalse(dialog.isVisible()); self.assertIs(self.plugin.receipts.handle, self.handle)
        self.assertFalse(self.handle.canceled)
        self.handle.finished.emit()
    def test_disable_auto_receipt_does_not_require_valid_edited_sheet_url(self):
        self.stage(); self.context.task_language = lambda _task: 'sk'
        dialog = self.plugin.open_dialog(); dialog.url.setText('incomplete URL')
        dialog.auto_download.setChecked(False)
        self.assertFalse(self.plugin.store.settings()['auto_download'])


if __name__ == '__main__': unittest.main()

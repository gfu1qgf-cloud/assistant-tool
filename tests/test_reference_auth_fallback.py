"""Public-first references; all saved Drive credentials, never rebind/login."""
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtWidgets
from model.GoogleDriveDownloader import AuthenticationRequired, DownloadError, parse_drive_link, open_download_response
from model.ReferenceDriveAuth import ReferenceAccountPool, ReferenceAccessError, download_reference
from model.TaskReferenceDownloader import TaskReferenceDownloadThread, TaskReferenceJob

DRIVE = 'https://www.googleapis.com/auth/drive'
LINK = parse_drive_link('https://drive.google.com/file/d/synthetic-reference/view')


def manager():
    value = Mock()
    value.snapshot.return_value = {'accounts': [{'id': key, 'scopes': [DRIVE]} for key in ('first', 'second', 'third')]}
    value.account.side_effect = lambda key: {'credentials': {'client_id': key, 'refresh_token': 'synthetic-' + key, 'scopes': [DRIVE]}}
    return value


class CredentialPoolTests(unittest.TestCase):
    def test_permission_failure_tries_next_saved_plugin_account_then_stops(self):
        auth = manager()
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=lambda key, **kw: key), patch(
                'model.ReferenceDriveAuth.download_reference', side_effect=[ReferenceAccessError('403'), ('downloaded', Path('done'))]) as download:
            pool = ReferenceAccountPool(manager=auth, legacy_files=[])
            self.assertEqual(pool.download(LINK, Path('synthetic'))[0], 'downloaded')
            self.assertEqual([call.args[0] for call in download.call_args_list], ['first', 'second'])
        auth.bind.assert_not_called(); auth.bind_many.assert_not_called()
    def test_every_credential_is_tried_once_when_all_lack_access(self):
        auth = manager()
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=lambda key, **kw: key), patch(
                'model.ReferenceDriveAuth.download_reference', side_effect=ReferenceAccessError('404')) as download:
            pool = ReferenceAccountPool(manager=auth, legacy_files=[])
            with self.assertRaisesRegex(ReferenceAccessError, '3 个'): pool.download(LINK, Path('synthetic'))
            self.assertEqual(download.call_count, 3)
    def test_transient_network_failure_does_not_rotate_accounts(self):
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=lambda key, **kw: key), patch(
                'model.ReferenceDriveAuth.download_reference', side_effect=TimeoutError('network')) as download:
            pool = ReferenceAccountPool(manager=manager(), legacy_files=[])
            with self.assertRaises(TimeoutError): pool.download(LINK, Path('synthetic'))
            self.assertEqual(download.call_count, 1)
    def test_last_successful_account_is_prioritized_only_in_memory(self):
        auth = manager()
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=lambda key, **kw: key), patch(
                'model.ReferenceDriveAuth.download_reference', side_effect=[ReferenceAccessError('403'), ('downloaded', Path('done')), ('downloaded', Path('next'))]) as download:
            pool = ReferenceAccountPool(manager=auth, legacy_files=[])
            pool.download(LINK, Path('synthetic')); pool.download(LINK, Path('synthetic'))
            self.assertEqual([call.args[0] for call in download.call_args_list], ['first', 'second', 'second'])
        auth.bind_many.assert_not_called()
    def test_duplicate_credentials_and_non_drive_accounts_are_not_retried(self):
        auth = manager()
        auth.snapshot.return_value['accounts'].append({'id': 'sheets-only', 'scopes': ['https://www.googleapis.com/auth/spreadsheets']})
        auth.account.side_effect = lambda key: {'credentials': {'client_id': 'same', 'refresh_token': 'same', 'scopes': [DRIVE]}}
        pool = ReferenceAccountPool(manager=auth, legacy_files=[])
        self.assertEqual(len(pool.candidates), 1)
    def test_bad_account_does_not_block_other_saved_accounts(self):
        auth = manager()
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=[ValueError('bad first'), 'second-service']), patch(
                'model.ReferenceDriveAuth.download_reference', return_value=('downloaded', Path('done'))):
            pool = ReferenceAccountPool(manager=auth, legacy_files=[])
            self.assertEqual(pool.download(LINK, Path('synthetic'))[0], 'downloaded')
    def test_cancel_does_not_attempt_any_account(self):
        pool = ReferenceAccountPool(manager=manager(), legacy_files=[])
        with self.assertRaises(InterruptedError): pool.download(LINK, Path('synthetic'), canceled=lambda: True)
    def test_retry_does_not_revisit_account_already_denied_for_same_file(self):
        with patch('model.GoogleAccounts.load_drive_account_service', side_effect=lambda key, **kw: key), patch(
                'model.ReferenceDriveAuth.download_reference', side_effect=[ReferenceAccessError('403'), TimeoutError('network'), ('downloaded', Path('done'))]) as download:
            pool = ReferenceAccountPool(manager=manager(), legacy_files=[])
            with self.assertRaises(TimeoutError): pool.download(LINK, Path('synthetic'))
            pool.download(LINK, Path('synthetic'))
            self.assertEqual([call.args[0] for call in download.call_args_list], ['first', 'second', 'second'])
    def test_explicit_saved_account_service_never_changes_plugin_binding(self):
        from model.GoogleAccounts import load_drive_account_service
        auth = manager(); auth.file.path = Path('synthetic-registry')
        with patch('model.GoogleAccounts._credentials', return_value=Mock()) as credentials, patch('googleapiclient.discovery.build') as build:
            load_drive_account_service('second', manager=auth)
        credentials.assert_called_once_with(auth, 'second', required_scopes=(DRIVE,))
        self.assertEqual(build.call_args.args, ('drive', 'v3'))
        auth.bind.assert_not_called(); auth.bind_many.assert_not_called()
    def test_default_pool_uses_manager_legacy_import_and_respects_removed_accounts(self):
        auth = manager(); auth.snapshot.return_value = {'accounts': []}
        pool = ReferenceAccountPool(manager=auth)
        auth.integrate_legacy.assert_called_once_with()
        self.assertEqual(pool.candidates, [])
        auth.bind.assert_not_called(); auth.bind_many.assert_not_called()


class ReferenceThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_public_success_never_loads_credentials(self):
        worker = TaskReferenceDownloadThread([])
        with patch('model.TaskReferenceDownloader.download_one', return_value=('downloaded', Path('public'))) as public, patch(
                'model.ReferenceDriveAuth.ReferenceAccountPool') as pool:
            worker.download_reference_file(LINK, Path('synthetic'))
        public.assert_called_once(); pool.assert_not_called()
    def test_login_page_triggers_saved_pool_once_and_private_retries_skip_public(self):
        worker = TaskReferenceDownloadThread([]); pool = Mock(); pool.download.return_value = ('downloaded', Path('authorized'))
        with patch('model.TaskReferenceDownloader.download_one', side_effect=AuthenticationRequired('login')) as public, patch(
                'model.ReferenceDriveAuth.ReferenceAccountPool', return_value=pool) as factory:
            worker.download_reference_file(LINK, Path('synthetic')); worker.download_reference_file(LINK, Path('synthetic'))
        public.assert_called_once(); factory.assert_called_once(); self.assertEqual(pool.download.call_count, 2)
    def test_permission_failure_is_not_retried_three_times_or_block_next_reference(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); jobs = [TaskReferenceJob(str(index), f'https://drive.google.com/file/d/file-{index}/view', root/str(index)) for index in (1, 2)]
            worker = TaskReferenceDownloadThread(jobs, retries=3, cache_path=root/'cache.json')
            logs = []; worker.log.connect(logs.append)
            with patch.object(worker, 'download_reference_file', side_effect=ReferenceAccessError('all denied')) as download, patch('model.TaskReferenceDownloader.time.sleep') as sleep:
                worker._run()
            self.assertEqual(download.call_count, 2); sleep.assert_not_called()
            self.assertFalse(any('Sign-in Loading' in text for text in logs))
    def test_google_login_redirect_is_classified_before_reading_page_text(self):
        response = Mock(); response.url = 'https://accounts.google.com/v3/signin/identifier'
        opener = Mock(); opener.open.return_value = response
        with self.assertRaises(AuthenticationRequired): open_download_response(opener, 'https://drive.usercontent.google.com/download?id=test', 10)
        response.close.assert_called_once(); response.read.assert_not_called()
    def test_access_request_html_also_allows_credential_fallback(self):
        from email.message import Message
        response = io.BytesIO(b'<html><body>You need <span>access</span></body></html>')
        response.url = 'https://drive.google.com/uc?id=synthetic'
        response.headers = Message(); response.headers['Content-Type'] = 'text/html'
        opener = Mock(); opener.open.return_value = response
        with self.assertRaises(AuthenticationRequired): open_download_response(opener, 'https://drive.usercontent.google.com/download?id=synthetic', 10)
    def test_anonymous_404_can_be_permission_masking_so_saved_credentials_are_tried(self):
        from urllib.error import HTTPError
        worker = TaskReferenceDownloadThread([]); pool = Mock(); pool.download.return_value = ('downloaded', Path('authorized'))
        with patch('model.TaskReferenceDownloader.download_one', side_effect=HTTPError('https://drive.google.com/uc', 404, 'missing', {}, None)), patch(
                'model.ReferenceDriveAuth.ReferenceAccountPool', return_value=pool):
            worker.download_reference_file(LINK, Path('synthetic'))
        pool.download.assert_called_once()


class AuthenticatedBytesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.data = b'synthetic-picture-bytes'
        self.api = Mock(); self.request = SimpleNamespace(headers={})
        self.api.files.return_value.get.return_value.execute.return_value = {'id': LINK.file_id, 'name': 'reference.png',
            'mimeType': 'image/png', 'size': str(len(self.data)), 'md5Checksum': hashlib.md5(self.data).hexdigest()}
        self.api.files.return_value.get_media.return_value = self.request
    def download(self):
        data = self.data
        class Downloader:
            def __init__(self, stream, request, chunksize): self.stream = stream
            def next_chunk(self, num_retries): self.stream.write(data); return None, True
        with patch('googleapiclient.http.MediaIoBaseDownload', Downloader): return download_reference(self.api, LINK, self.root)
    def test_image_download_is_verified_and_original_name_preserved(self):
        state, path = self.download(); self.assertEqual(state, 'downloaded'); self.assertEqual(path.name, 'reference.png')
        self.assertEqual(path.read_bytes(), self.data)
    def test_existing_different_file_is_not_overwritten(self):
        (self.root/'reference.png').write_bytes(b'unrelated')
        state, path = self.download(); self.assertNotEqual(path.name, 'reference.png')
        self.assertEqual((self.root/'reference.png').read_bytes(), b'unrelated')
    def test_wrong_length_is_not_archived(self):
        self.api.files.return_value.get.return_value.execute.return_value['size'] = '999'
        with self.assertRaises(DownloadError): self.download()
        self.assertEqual(list(self.root.iterdir()), [])
    def test_revoked_runtime_credential_is_not_a_generic_network_retry(self):
        from google.auth.exceptions import RefreshError
        self.api.files.return_value.get.return_value.execute.side_effect = RefreshError('invalid_grant')
        with self.assertRaises(ReferenceAccessError): self.download()


if __name__ == '__main__': unittest.main()

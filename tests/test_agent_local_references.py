"""Synthetic images and fake Drive only; never read user credentials or upload real media."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import data_directory
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot, fingerprint, validate_payload
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.references import reference_sources, folder_id, run_reference_plan, prepare_job_references
from app_plugins.builtin.ai_agent_assistant.sheets import run_export, FIELDS
from app_plugins.builtin.ai_agent_assistant.plugin import AIAgentAssistantPlugin
from test_ai_agent_assistant import target, FakeSheets, URL

LINK = 'https://drive.google.com/file/d/synthetic_remote/view'
PARENT = 'synthetic_folder'


class MissingFile(Exception):
    resp = SimpleNamespace(status=404)


class Media:
    def __init__(self, filename, **kwargs): self._fd = open(filename, 'rb')
    def stream(self): return self._fd


class Drive:
    def __init__(self):
        self.items = {PARENT: {'id': PARENT, 'mimeType': 'application/vnd.google-apps.folder', 'capabilities': {'canAddChildren': True}}}
        self.creates = []; self.generated = 0; self.timeout_after_create = False; self.on_create = None
    def files(self): return self
    def generateIds(self, **kwargs):
        self.generated += 1
        return SimpleNamespace(execute=lambda **_kw: {'ids': ['generated_' + str(self.generated)]})
    def get(self, fileId, **kwargs):
        def execute(**_kw):
            if fileId not in self.items: raise MissingFile()
            return copy.deepcopy(self.items[fileId])
        return SimpleNamespace(execute=execute)
    def create(self, body, media_body, **kwargs):
        def next_chunk(**_kw):
            media_body.stream().seek(0)
            data = media_body.stream().read(); self.creates.append(copy.deepcopy(body))
            self.items[body['id']] = {**body, 'size': str(len(data)), 'md5Checksum': hashlib.md5(data).hexdigest()}
            if self.on_create: self.on_create()
            if self.timeout_after_create: raise TimeoutError('upload response lost')
            return None, {'id': body['id']}
        return SimpleNamespace(next_chunk=next_chunk)


class LocalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'private')
        self.image = self.root/'example.png'; Image.new('RGB', (64, 96), 'blue').save(self.image)
        self.value = snapshot(target(self.root), 'sk'); self.value['reference'] = str(self.image)
        self.key = self.store.stage([self.value])['imported'][0]
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock(), is_canceled=Mock(return_value=False))
        self.api = Drive()
    def plan(self):
        return run_reference_plan({'root': str(self.store.root.path), 'ids': [self.key], 'next': {'url': URL, 'sheet': '口播表格'}}, self.run)
    def approval(self):
        return {'confirmed': True, 'plan': self.plan()['upload_plan'], 'folder': PARENT,
            'authorization': {'account_id': 'synthetic-account', 'authorization_id': 'ai_agent_assistant.uploads'}}
    def prepare(self, approval=None):
        with patch('googleapiclient.http.MediaFileUpload', Media):
            return prepare_job_references(self.store.jobs([self.key])[0], approval or self.approval(), self.store, self.run, api=self.api)
    def test_existing_links_are_unchanged_and_never_need_auth(self):
        job = self.store.jobs([self.key])[0]; job['payload']['reference'] = LINK
        with patch('app_plugins.builtin.ai_agent_assistant.references.load_google_service') as load:
            self.assertEqual(prepare_job_references(job, None, self.store, self.run), job)
        load.assert_not_called(); self.assertFalse(self.api.creates)
    def test_strict_sheet_validation_still_rejects_local_paths(self):
        validate_payload(self.value, allow_local_references=True)
        with self.assertRaises(ValueError): validate_payload(self.value)
    def test_plan_has_thumbnail_digest_task_identity_and_no_upload(self):
        before = self.store.jobs(); plan = self.plan()
        self.assertEqual(self.store.jobs(), before); self.assertFalse(self.api.creates)
        item = plan['upload_plan'][0]
        self.assertEqual(item['id'], self.key); self.assertEqual(item['task_id'], self.value['task_id'])
        self.assertTrue(item['files'][0]['thumbnail']); self.assertEqual(item['digest'], fingerprint(self.value))
    def test_missing_invalid_or_oversized_local_image_blocks_plan(self):
        for reference in (str(self.root/'missing.png'), str(self.root/'bad.png')):
            (self.root/'bad.png').write_bytes(b'not an image')
            self.store.edit(self.key, 'reference', reference)
            with self.assertRaises(Exception): self.plan()
        self.assertFalse(self.api.creates)
    def test_relative_filename_resolves_only_inside_original_task_directory(self):
        value = dict(self.value, target_dir=str(self.root), reference='example.png')
        self.assertEqual(Path(reference_sources(value)[0]['value']), self.image)
        for text in ('../secret.png', r'\\server\secret.png', 'https://evil.test/image.png', 'folder', 'video.mp4'):
            with self.subTest(text=text), self.assertRaises(ValueError): reference_sources(dict(value, reference=text))
    def test_no_upload_without_explicit_approval(self):
        with self.assertRaisesRegex(ValueError, '确认'):
            prepare_job_references(self.store.jobs([self.key])[0], None, self.store, self.run, api=self.api)
        self.assertEqual(self.api.generated, 0); self.assertFalse(self.api.creates)
    def test_confirmed_image_uploaded_once_and_only_link_replaces_private_path(self):
        approval = self.approval(); job = self.prepare(approval)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(job['payload']['reference'], 'https://drive.google.com/file/d/generated_1/view')
        self.assertTrue(self.image.exists()); self.assertFalse(list(self.store.root.path.glob('reference-*')))
        self.assertNotIn('permissions', self.api.creates[0]); self.assertEqual(self.api.creates[0]['parents'], [PARENT])
    def test_real_sdk_media_stream_works_with_fake_drive_without_network(self):
        job = prepare_job_references(self.store.jobs([self.key])[0], self.approval(), self.store, self.run, api=self.api)
        self.assertEqual(job['payload']['reference'], 'https://drive.google.com/file/d/generated_1/view')
        self.assertFalse(list(self.store.root.path.glob('reference-*')))
    def test_same_bytes_reuse_remote_upload_after_reimport_without_duplicate_create(self):
        self.prepare()
        new_key = self.store.stage([self.value], new_version=True)['imported'][0]
        self.key = new_key; self.prepare()
        self.assertEqual(len(self.api.creates), 1)
    def test_confirmed_file_changed_before_upload_is_not_uploaded(self):
        approval = self.approval(); Image.new('RGB', (64, 96), 'red').save(self.image)
        with self.assertRaisesRegex(ValueError, '确认后改变'): self.prepare(approval)
        self.assertFalse(self.api.creates); self.assertFalse(list(self.store.root.path.glob('reference-*')))
    def test_confirmed_task_changed_is_not_uploaded(self):
        approval = self.approval(); self.store.edit(self.key, 'spoken', 'new script')
        with self.assertRaisesRegex(ValueError, '确认后改变'): self.prepare(approval)
        self.assertFalse(self.api.creates)
    def test_mixed_drive_links_and_local_images_preserve_order(self):
        self.store.edit(self.key, 'reference', LINK + '\n' + str(self.image))
        job = self.prepare()
        self.assertEqual(job['payload']['reference'].splitlines()[0], LINK)
        self.assertEqual(len(job['payload']['reference'].splitlines()), 2)
    def test_uncertain_upload_uses_same_recorded_id_and_no_duplicate_on_retry(self):
        approval = self.approval(); self.api.timeout_after_create = True
        with self.assertRaises(TimeoutError): self.prepare(approval)
        self.api.timeout_after_create = False; self.prepare(approval)
        self.assertEqual(len(self.api.creates), 1); self.assertEqual(self.api.generated, 1)
    def test_folder_and_cached_metadata_must_match_before_reuse(self):
        self.prepare(); self.store.edit(self.key, 'reference', str(self.image))
        self.api.items['generated_1']['md5Checksum'] = '0' * 32
        with self.assertRaisesRegex(ValueError, '未覆盖'): self.prepare()
        self.assertEqual(len(self.api.creates), 1)
    def test_run_export_uploads_then_submits_only_drive_link(self):
        api = FakeSheets(); approval = self.approval()
        with patch('googleapiclient.http.MediaFileUpload', Media):
            result = run_export({'root': str(self.store.root.path), 'ids': [self.key], 'url': URL, 'sheet': '口播表格',
                'operation': 'submit', 'reference_approval': approval}, self.run, service=api, upload_service=self.api)
        self.assertEqual(result['added'], 1)
        row = api.writes[0][0]['body']['values'][0]; index = next(i for i, item in enumerate(FIELDS) if item[0] == 'reference')
        self.assertEqual(row[index], 'https://drive.google.com/file/d/generated_1/view')
        self.assertNotIn(str(self.image), str(row))
    def test_upload_failure_does_not_append_incomplete_job(self):
        api = FakeSheets(); approval = self.approval(); self.api.timeout_after_create = True
        with patch('googleapiclient.http.MediaFileUpload', Media):
            result = run_export({'root': str(self.store.root.path), 'ids': [self.key], 'url': URL, 'sheet': '口播表格',
                'operation': 'submit', 'reference_approval': approval}, self.run, service=api, upload_service=self.api)
        self.assertEqual(result['added'], 0); self.assertFalse(api.writes)
        self.assertEqual(self.store.jobs()[0]['state'], 'failed')
    def test_concurrent_edit_during_upload_is_never_overwritten_or_marked_failed(self):
        api = FakeSheets(); approval = self.approval()
        self.api.on_create = lambda: self.store.edit(self.key, 'spoken', 'concurrent new script')
        with patch('googleapiclient.http.MediaFileUpload', Media):
            run_export({'root': str(self.store.root.path), 'ids': [self.key], 'url': URL, 'sheet': '口播表格',
                'operation': 'submit', 'reference_approval': approval}, self.run, service=api, upload_service=self.api)
        job = self.store.jobs()[0]; self.assertEqual(job['payload']['spoken'], 'concurrent new script')
        self.assertEqual(job['state'], 'draft'); self.assertFalse(api.writes)
    def test_folder_parser_rejects_non_drive_destinations(self):
        self.assertEqual(folder_id('https://drive.google.com/drive/folders/' + PARENT), PARENT)
        for text in ('', 'https://evil.test/' + PARENT, 'https://drive.google.com.evil.test/drive/folders/' + PARENT):
            with self.assertRaises(ValueError): folder_id(text)


class LocalRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.main = QtWidgets.QWidget(); self.plugin = AIAgentAssistantPlugin()
        self.context = SimpleNamespace(parent_widget=self.main, storage=data_directory(self.temp.name),
            register_command=Mock(), register_main_widget=Mock(), register_google_authorization=Mock(), log=Mock(),
            task_language=lambda _task: 'sk', task_targets=lambda _rows: [], available_nodes=lambda: SimpleNamespace(definitions={'script_segmenter.split': object()}))
        self.plugin.register(self.context)
    def tearDown(self):
        self.main.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def test_task_list_follows_source_import_order(self):
        values = [snapshot(target(self.temp.name, identifier), 'sk') for identifier in ('05', '02', '11')]
        self.plugin.store.stage(values); dialog = self.plugin.open_dialog()
        self.assertEqual([dialog.table.item(row, 0).text() for row in range(3)], ['05', '02', '11'])
    def test_force_remove_submitted_records_preserves_files_and_reimport_gets_new_id(self):
        value = snapshot(target(self.temp.name), 'sk'); key = self.plugin.store.stage([value])['imported'][0]
        self.plugin.store.mark([key], 'submitted', 'synthetic:sheet')
        path = Path(self.temp.name)/'keep.mp4'; path.write_bytes(b'keep')
        self.plugin.store.save_delivery(key, 'video', {'path': str(path)})
        dialog = self.plugin.open_dialog(); dialog.table.selectAll()
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No): dialog.remove_drafts()
        self.assertEqual(len(self.plugin.store.jobs()), 1)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes): dialog.remove_drafts()
        self.assertEqual(self.plugin.store.jobs(), []); self.assertTrue(path.exists())
        self.assertNotEqual(self.plugin.store.stage([value])['imported'][0], key)
    def test_active_cross_process_receipt_blocks_force_remove_without_partial_delete(self):
        value = snapshot(target(self.temp.name), 'sk'); key = self.plugin.store.stage([value])['imported'][0]
        self.plugin.store.claim_delivery_run('other')
        with self.assertRaisesRegex(ValueError, '收取仍在运行'):
            self.plugin.store.remove_records({key: ('draft', fingerprint(value))})
        self.assertEqual(len(self.plugin.store.jobs()), 1)
    def test_cloning_submitted_record_creates_editable_new_draft_without_cloud_write(self):
        value = snapshot(target(self.temp.name), 'sk'); key = self.plugin.store.stage([value])['imported'][0]
        self.plugin.store.mark([key], 'submitted', 'synthetic:sheet')
        dialog = self.plugin.open_dialog(); dialog.table.selectAll()
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes): dialog.clone_drafts()
        jobs = self.plugin.store.jobs(); self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]['state'], 'submitted'); self.assertEqual(jobs[1]['state'], 'draft')
        self.assertEqual(dialog.selected_ids(), [jobs[1]['id']])
    def test_cancel_upload_confirmation_never_binds_account_or_submits(self):
        from app_plugins.builtin.ai_agent_assistant import reference_ui
        dialog = Mock(); dialog.exec.return_value = QtWidgets.QDialog.DialogCode.Rejected
        with patch.object(reference_ui, 'ReferenceUploadDialog', return_value=dialog), patch.object(self.plugin, 'submit') as submit:
            self.plugin.confirm_local_references({'upload_plan': [{'id': 'fixture'}]})
        dialog.manager.bind.assert_not_called(); submit.assert_not_called()
    def test_upload_preview_shows_task_image_and_no_public_share_checkbox(self):
        from app_plugins.builtin.ai_agent_assistant import reference_ui
        from app_plugins.builtin.ai_agent_assistant.references import image_record
        image = Path(self.temp.name)/'preview.png'; Image.new('RGB', (64, 96), 'green').save(image)
        record = image_record(image, SimpleNamespace(check_canceled=Mock()), thumbnail=True)
        manager = Mock(); manager.snapshot.return_value = {'bindings': {}, 'accounts': [
            {'id': 'fixture', 'email': 'fixture@example.test', 'scopes': ['https://www.googleapis.com/auth/drive']}]}
        with patch.object(reference_ui, 'GoogleAccountManager', return_value=manager):
            dialog = reference_ui.ReferenceUploadDialog(self.plugin, [{'task_id': '01', 'files': [record]}])
        self.assertFalse(dialog.table.item(0, 0).icon().isNull())
        self.assertEqual(dialog.table.item(0, 1).text(), '01')
        self.assertEqual(dialog.accounts.currentData(), 'fixture')
        self.assertEqual(dialog.findChildren(QtWidgets.QCheckBox), [])
        dialog.folder.setText(PARENT); dialog.validate_and_accept()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        manager.bind.assert_not_called(); dialog.deleteLater()


if __name__ == '__main__': unittest.main()

"""Freeze and receipt checks must reject same-name images and stale uploads."""
import copy
from pathlib import Path
import unittest

import test_flow_transfer_recovery as transfers
from flow_reference_fixtures import upload_receipt
from app_plugins.builtin.flow_assistant.reference_image import freeze_reference


class ReferenceImageTests(unittest.TestCase):
    setUpClass = transfers.TransferTests.__dict__['setUpClass']
    setUp = transfers.TransferTests.setUp
    service = transfers.TransferTests.service
    jobs = transfers.TransferTests.jobs
    client = transfers.TransferTests.client
    upload = transfers.TransferTests.upload
    request = transfers.TransferTests.request
    actions = transfers.TransferTests.actions

    def test_same_filename_different_content_and_directory_never_share_upload_name(self):
        service = self.service(); first, _ = self.upload(service)
        other_dir = self.target / 'other'; other_dir.mkdir(); image = other_dir / self.image.name
        image.write_bytes(b'different person with exactly the same filename')
        service.store.enqueue(self.jobs(target={'target_dir': str(other_dir)}, images=[image]))
        row = service.store.rows()[-1]
        service.store.update(row['id'], 'uploading', attempt='c' * 32)
        a = first['remote']['upload_reference']; b = freeze_reference(service.store.get(row['id']), service.downloads_root)
        self.assertNotEqual(a['filename'], b['filename']); self.assertNotEqual(a['sha256'], b['sha256'])
        self.assertEqual(Path(a['path']).read_bytes(), self.image.read_bytes())
        self.assertEqual(Path(b['path']).read_bytes(), image.read_bytes())
        self.assertEqual(self.image.name, image.name)

    def test_upload_uses_frozen_attempt_path_and_changed_copy_is_rejected(self):
        service = self.service(); row, client = self.upload(service)
        request = next(value for value in self.actions(client) if value['action'] == 'upload_images')
        reference = row['remote']['upload_reference']
        self.assertEqual(request['params']['paths'], [reference['path']])
        self.assertNotEqual(request['params']['paths'], [str(self.image)])
        Path(reference['path']).write_bytes(b'wrong image')
        with self.assertRaisesRegex(ValueError, '内容不符'): freeze_reference(row, service.downloads_root)

    def test_missing_wrong_or_old_reference_receipt_never_reserves_credits_or_generates(self):
        for wrong in ('missing', 'attempt', 'path', 'hash', 'unverified'):
            with self.subTest(wrong=wrong):
                self.setUp(); service = self.service(); row, client = self.upload(service)
                data = upload_receipt(service, row['id']); data = copy.deepcopy(data)
                if wrong == 'missing': data = {}
                elif wrong == 'attempt': data['reference']['upload_id'] = 'd' * 32
                elif wrong == 'path': data['reference']['path'] = str(self.image)
                elif wrong == 'hash': data['reference']['sha256'] = '0' * 64
                else: data['reference']['verified'] = False
                service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': True, 'data': data})
                self.assertEqual(service.store.get(row['id'])['status'], 'failed')
                self.assertFalse(service.store.attempt_charged(row['attempt']))
                self.assertNotIn('run_tasks', [value['action'] for value in self.actions(client)])
                service.execute({'action': 'stop'})

    def test_paid_task_keeps_the_exact_uploaded_reference_instead_of_original_basename(self):
        service = self.service(); row, client = self.upload(service)
        data = upload_receipt(service, row['id'])
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': True, 'data': data})
        run = next(value for value in self.actions(client) if value['action'] == 'run_tasks')
        task = run['params']['tasks'][0]
        self.assertEqual(task['reference'], data['reference'])
        self.assertEqual(task['local_image_path'], data['reference']['path'])
        self.assertEqual(task['image_name'], data['reference']['filename'])
        self.assertNotEqual(task['image_name'], self.image.name)

    def test_verified_reused_media_keeps_current_attempt_proof_and_is_charged_only_once(self):
        service = self.service(); row, client = self.upload(service)
        data = upload_receipt(service, row['id'])
        data['reference'].update(media_id='same-previously-uploaded-media', reused=True, source_upload_id='d' * 32)
        request = self.request(service, 'upload_images')
        service._response(client, {'request_id': request, 'success': True, 'data': data})
        service._response(client, {'request_id': request, 'success': True, 'data': data})
        runs = [value for value in self.actions(client) if value['action'] == 'run_tasks']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['params']['tasks'][0]['reference']['upload_id'], row['attempt'])
        self.assertEqual(runs[0]['params']['tasks'][0]['reference']['media_id'], 'same-previously-uploaded-media')
        self.assertEqual(service.store.get(row['id'])['remote']['reference_receipt'], data['reference'])

    def test_reused_marker_cannot_authorize_an_old_attempt_receipt(self):
        service = self.service(); row, client = self.upload(service)
        data = upload_receipt(service, row['id'])
        data['reference'].update(upload_id='d' * 32, reused=True)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': True, 'data': data})
        self.assertEqual(service.store.get(row['id'])['status'], 'failed')
        self.assertFalse(service.store.attempt_charged(row['attempt']))
        self.assertNotIn('run_tasks', [value['action'] for value in self.actions(client)])

    def test_upload_request_binds_expected_account_and_manual_reset_changes_asset_revision(self):
        service = self.service(); row, client = self.upload(service)
        request = next(value for value in self.actions(client) if value['action'] == 'upload_images')
        self.assertEqual(request['params']['expected_account'], client['account'])
        result = service.store.reset_tasks([row['id']], versions={row['id']: {
            key: row[key] for key in ('status', 'attempt', 'browser')}})
        self.assertEqual(result['reset'], [row['id']])
        current = service.store.get(row['id']); revision = current['payload']['reference_reset']
        self.assertEqual(len(revision), 32)
        service.pending.clear()
        service.store.update(row['id'], 'uploading', attempt='e' * 32, browser=client['id'],
                             remote={'account': client['account'], 'cost': 2})
        service.start_upload(service.store.get(row['id']))
        request = [value for value in self.actions(client) if value['action'] == 'upload_images'][-1]
        self.assertEqual(request['params']['reference_reset'], revision)

    def test_bulk_reset_and_legacy_per_clip_revisions_do_not_force_one_upload_per_clip(self):
        service=self.service()
        service.store.enqueue(self.jobs(count=3))
        rows=service.store.rows()
        versions={row['id']:{key:row[key] for key in ('status','attempt','browser')} for row in rows}
        service.store.reset_tasks([row['id'] for row in rows],versions=versions)
        rows=service.store.rows()
        revisions={row['payload']['reference_reset'] for row in rows}
        self.assertEqual(len(revisions),1,'one bulk reset must not create a new image revision for every clip')
        sha=rows[0]['payload']['image_identity']['sha256']
        self.assertEqual(service.store.reference_revision(sha),next(iter(revisions)))
        # Old versions stored a different token on each clip. The newest
        # explicit reset is now canonical for all remaining clips of that image.
        for index,row in enumerate(rows):
            payload={**row['payload'],'reference_reset':f'{index+1:032x}'}
            service.store.set_payload(row['id'],payload)
        selected=service.store.reference_revision(sha)
        self.assertEqual(selected,f'{len(rows):032x}')
        first=service.store.get(rows[0]['id'])
        service.store.reset_tasks([first['id']],versions={first['id']:{key:first[key] for key in ('status','attempt','browser')}})
        fresh=service.store.get(first['id'])['payload']['reference_reset']
        self.assertNotEqual(fresh,selected)
        self.assertEqual(service.store.reference_revision(sha),fresh,'a later explicit reset invalidates an old cached asset once')


if __name__ == '__main__': unittest.main()

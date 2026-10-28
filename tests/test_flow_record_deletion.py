"""Record deletion never removes media, refunds budget or revives paid work."""
import copy
import json
import time
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
import test_flow_assistant as fixtures
import test_flow_setup as setup_fixtures
from app_plugins.builtin.flow_assistant.store import QueueStore


class DeletionTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    ui = setup_fixtures.SetupTests.ui

    def test_safe_records_and_their_history_are_deleted_but_media_and_drafts_remain(self):
        store = QueueStore(self.namespace)
        states = ['queued', 'completed', 'cancelled', 'failed', 'archive_failed', 'download_parked', 'needs_review']
        store.enqueue(self.jobs(count=len(states)))
        rows = store.rows()
        for row, state in zip(rows, states): store.update(row['id'], state)
        output = self.target / 'keep.mp4'; output.write_bytes(b'user finished video')
        download = self.root / 'result.mp4'; download.write_bytes(b'browser original')
        image = self.image.read_bytes(); store.save_draft('a' * 64, {'prompt': 'keep draft'})
        result = store.delete_records([row['id'] for row in rows])
        self.assertEqual(len(result['deleted']), len(states)); self.assertFalse(result['skipped'])
        self.assertFalse(QueueStore(self.namespace).rows())
        with store.connection() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)
        self.assertEqual(self.image.read_bytes(), image); self.assertEqual(output.read_bytes(), b'user finished video')
        self.assertEqual(download.read_bytes(), b'browser original'); self.assertIn('a' * 64, store.drafts())

    def test_mixed_selection_keeps_running_and_unconfirmed_records(self):
        store = QueueStore(self.namespace)
        states = ['queued', 'completed', 'preparing', 'uploading', 'submitting', 'generating', 'downloading', 'archiving', 'uncertain', 'download_failed']
        store.enqueue(self.jobs(count=len(states))); rows = store.rows()
        for row, state in zip(rows, states): store.update(row['id'], state)
        result = store.delete_records([row['id'] for row in rows])
        self.assertEqual(len(result['deleted']), 2); self.assertEqual(len(result['skipped']), 8)
        self.assertEqual([row['status'] for row in store.rows()], states[2:])
        result = store.delete_records([row['id'] for row in rows], confirmed_finished=True)
        self.assertEqual(len(result['deleted']), 2); self.assertEqual(len(result['skipped']), 6)

    def test_deletion_does_not_refund_batch_or_remove_replay_guard(self):
        service = self.service(); jobs = self.jobs(count=2)
        batch = service.store.enqueue(jobs, limit_total=10, request_id='original-request')
        first, second = service.store.rows()
        self.assertTrue(service.store.reserve(first['id'], 'a' * 32, 7, 'page'))
        service.store.update(first['id'], 'completed', attempt='a' * 32, browser='browser')
        service.store.delete_records([first['id']])
        self.assertEqual(service.store.batches()[batch]['reserved'], 7)
        self.assertFalse(service.store.reserve(second['id'], 'b' * 32, 4, 'page'))
        service.store.delete_records([second['id']])
        service.execute({'action': 'enqueue', 'request_id': 'original-request', 'items': [self.args], 'start_after_save': True})
        self.assertFalse(service.store.rows()); self.assertFalse(service.released)

    def test_service_clears_only_deleted_releases_and_stale_pending_requests(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2)); first, second = service.store.rows()
        service.released.update([first['id'], second['id']])
        service.pending['old'] = {'job': first['id'], 'action': 'prepare', 'client': 'old', 'attempt': 'old'}
        results = []; service.records_deleted.connect(results.append)
        service.execute({'action': 'delete_records', 'ids': [first['id']], 'request_id': 'delete-one'})
        self.assertTrue(results[-1]['success']); self.assertEqual(results[-1]['deleted'], [first['id']])
        self.assertEqual(service.released, {second['id']}); self.assertNotIn('old', service.pending)
        self.assertIsNotNone(service.store.get(second['id']))

    def test_cleanup_worker_and_download_retry_are_protected_even_if_row_looks_finished(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2)); first, second = service.store.rows()
        service.store.update(first['id'], 'completed'); service.archiving.add(first['id'])
        service.store.update(second['id'], 'download_failed', attempt='b' * 32, browser='browser')
        service.pending['download'] = {'job': second['id'], 'action': 'retry_download', 'client': 'browser', 'attempt': 'b' * 32}
        results = []; service.records_deleted.connect(results.append)
        service.execute({'action': 'delete_records', 'ids': [first['id'], second['id']], 'confirmed_finished': True})
        self.assertFalse(results[-1]['deleted']); self.assertEqual(len(results[-1]['skipped']), 2)
        service.archiving.clear(); service.pending.clear()

    def test_late_deleted_download_is_acknowledged_without_archiving_or_recreating(self):
        service = self.service(); client = self.client(service)
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'completed', attempt='a' * 32, browser=client['id'])
        service.store.delete_records([row['id']])
        client['socket'].sendTextMessage.reset_mock()
        with patch.object(service, 'archive') as archive:
            service._download(client, {'batch_id': 'a' * 32, 'event_id': 'late', 'data': {'state': 'complete'}})
        archive.assert_not_called(); self.assertFalse(service.store.rows())
        self.assertEqual(json.loads(client['socket'].sendTextMessage.call_args.args[0]), {'type': 'ack', 'event_id': 'late'})
        client['socket'].sendTextMessage.reset_mock()
        service._download({**client, 'id': 'other-browser'}, {'batch_id': 'a' * 32, 'event_id': 'not-owned'})
        client['socket'].sendTextMessage.assert_not_called()

    def test_failed_delete_transaction_keeps_records_and_releases(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.released.add(row['id']); results = []; service.records_deleted.connect(results.append)
        with patch.object(service.store, 'delete_records', side_effect=OSError('private volume unavailable')):
            service.execute({'action': 'delete_records', 'ids': [row['id']], 'request_id': 'failed'})
        self.assertFalse(results[-1]['success']); self.assertIn(row['id'], service.released)
        self.assertIsNotNone(service.store.get(row['id']))

    def test_repeated_delete_and_late_archive_callback_are_harmless(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.delete_records([row['id']]); self.assertFalse(service.store.delete_records([row['id']])['deleted'])
        service._archived({'id': row['id'], 'cleanup_only': True}); self.assertFalse(service.store.rows())

    def test_database_failure_rolls_back_jobs_history_and_tombstones_together(self):
        import sqlite3
        store = QueueStore(self.namespace); store.enqueue(self.jobs(count=2)); rows = store.rows()
        for index, row in enumerate(rows):
            store.update(row['id'], 'completed', attempt=f'{index + 1:032x}', browser='browser')
        with store.connection() as db:
            before = db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            db.execute('CREATE TRIGGER refuse_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, "test failure"); END')
        with self.assertRaises(sqlite3.IntegrityError): store.delete_records([row['id'] for row in rows])
        self.assertEqual(len(store.rows()), 2)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deleted_attempts').fetchone()[0], 0)

    def test_ui_delete_needs_confirmation_and_never_defaults_to_destructive_action(self):
        dialog, _, commands = self.ui(); store = QueueStore(self.namespace); store.enqueue(self.jobs())
        state = {'jobs': store.rows(), 'clients': [], 'paused': True, 'preferences': {}, 'spaces': []}
        dialog.update_state(state); dialog.queue.selectRow(1)
        self.assertEqual(dialog.delete_shortcut.context(), QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        def cancel(box):
            self.assertEqual(box.defaultButton().text(), '取消')
            box.defaultButton().click(); return 0
        with patch.object(QtWidgets.QMessageBox, 'exec', cancel): dialog.delete_records()
        self.assertFalse(any(command['action'] == 'delete_records' for command in commands))
        def confirm(box):
            next(button for button in box.buttons() if button.text() == '删除记录').click(); return 0
        with patch.object(QtWidgets.QMessageBox, 'exec', confirm): dialog.delete_records()
        command = commands[-1]; self.assertEqual(command['action'], 'delete_records')
        self.assertEqual(command['ids'], [state['jobs'][0]['id']]); self.assertFalse(command['confirmed_finished'])
        dialog.records_deleted({'request_id': command['request_id'], 'success': True, 'deleted': command['ids'], 'skipped': []})
        self.assertTrue(dialog.delete_button.isEnabled()); self.assertIn('未删除', dialog.note.text())

    def test_uncertain_deletion_requires_an_explicit_unchecked_checkbox(self):
        dialog, _, commands = self.ui(); store = QueueStore(self.namespace); store.enqueue(self.jobs())
        row = store.rows()[0]; store.update(row['id'], 'uncertain')
        dialog.update_state({'jobs': store.rows(), 'clients': [], 'paused': True, 'preferences': {}, 'spaces': []})
        def confirm(box):
            self.assertIsNotNone(box.checkBox()); self.assertFalse(box.checkBox().isChecked())
            box.checkBox().setChecked(True)
            next(button for button in box.buttons() if button.text() == '删除记录').click(); return 0
        with patch.object(QtWidgets.QMessageBox, 'exec', confirm): dialog.delete_records([row['id']])
        self.assertTrue(commands[-1]['confirmed_finished'])

    def test_force_removes_every_local_state_and_keeps_media_budget_and_late_receipts(self):
        store=QueueStore(self.namespace)
        states=['failed','uncertain','download_failed','preparing','uploading','submitting','generating','downloading','archiving']
        batch=store.enqueue(self.jobs(count=len(states),budget=0));rows=store.rows()
        for index,(row,status) in enumerate(zip(rows,states)):
            attempt=f'{index+1:032x}'
            store.update(row['id'],status,attempt=attempt,browser='original')
            self.assertTrue(store.reserve(row['id'],attempt,1,'fixture'))
        output=self.target/'keep.mp4';output.write_bytes(b'keep original')
        image=self.image.read_bytes()
        result=store.delete_records([r['id'] for r in rows],busy_ids=[r['id'] for r in rows],force=True)
        self.assertEqual(len(result['deleted']),len(states));self.assertFalse(result['skipped'])
        self.assertFalse(store.rows());self.assertEqual(store.batches()[batch]['reserved'],len(states))
        self.assertTrue(all(store.was_deleted(f'{i+1:032x}','original') for i in range(len(states))))
        self.assertEqual(output.read_bytes(),b'keep original');self.assertEqual(self.image.read_bytes(),image)

    def test_force_error_with_stale_pending_is_deleted_without_retaining_an_expired_hold(self):
        service=self.service();client=self.client(service)
        service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.store.update(row['id'],'failed',attempt='a'*32,browser=client['id'])
        service.pending['stale']={'job':row['id'],'action':'upload_images','attempt':'a'*32,
            'client':client['id'],'at':time.time()-100000}
        service.released.add(row['id'])
        service.execute({'action':'delete_records','ids':[row['id']],'force':True})
        self.assertIsNone(service.store.get(row['id']));self.assertNotIn(row['id'],service.released)
        self.assertTrue(service.pending['stale']['retired']);self.assertFalse(service.store.reset_holds())
        service._response(client,{'request_id':'stale','success':True})
        self.assertNotIn('stale',service.pending);self.assertFalse(service.store.rows())

    def test_force_active_upload_retires_callbacks_and_releases_short_hold_on_reply(self):
        service=self.service();client=self.client(service)
        service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.store.update(row['id'],'uploading',attempt='b'*32,browser=client['id'],
            remote={'profile':client.get('profile',''),'account':client.get('account','')})
        service.pending['upload']={'job':row['id'],'action':'upload_images','attempt':'b'*32,
            'client':client['id'],'at':time.time()}
        service.execute({'action':'delete_records','ids':[row['id']],'force':True})
        self.assertIsNone(service.store.get(row['id']));self.assertTrue(service.store.reset_holds())
        self.assertGreater(client['reset_wait_until'],time.time())
        service._response(client,{'request_id':'upload','success':True})
        self.assertFalse(service.store.reset_holds());self.assertEqual(client['reset_wait_until'],0)
        self.assertFalse(service.store.rows())

    def test_force_late_download_and_archive_do_not_recreate_deleted_running_record(self):
        service=self.service();client=self.client(service)
        service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.store.update(row['id'],'generating',attempt='c'*32,browser=client['id'])
        service.execute({'action':'delete_records','ids':[row['id']],'force':True})
        client['socket'].sendTextMessage.reset_mock()
        with patch.object(service,'archive') as archive:
            service._download(client,{'batch_id':'c'*32,'event_id':'late-forced','data':{'state':'complete'}})
        archive.assert_not_called();self.assertFalse(service.store.rows())
        self.assertEqual(json.loads(client['socket'].sendTextMessage.call_args.args[0]),{'type':'ack','event_id':'late-forced'})
        service._archived({'id':row['id'],'attempt':'c'*32,'output':'not-adopted.mp4'})
        self.assertFalse(service.store.rows())

    def test_force_ui_is_explicit_and_does_not_claim_cloud_finished(self):
        dialog,_,commands=self.ui();store=QueueStore(self.namespace);store.enqueue(self.jobs())
        row=store.rows()[0];store.update(row['id'],'download_failed')
        dialog.update_state({'jobs':store.rows(),'clients':[],'paused':True,'preferences':{},'spaces':[]})
        self.assertIn('强制删除选中记录…',[a.text() for a in dialog.queue_menu_button.menu().actions()])
        def confirm(box):
            self.assertEqual(box.defaultButton().text(),'取消');self.assertIsNone(box.checkBox())
            self.assertIn('仍可能继续',box.text())
            next(button for button in box.buttons() if button.text()=='强制删除记录').click();return 0
        with patch.object(QtWidgets.QMessageBox,'exec',confirm):dialog.delete_records([row['id']],force=True)
        self.assertTrue(commands[-1]['force']);self.assertFalse(commands[-1]['confirmed_finished'])

    def test_force_database_failure_leaves_pending_callbacks_and_rows_intact(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.pending['stale']={'job':row['id'],'action':'prepare','attempt':'a'*32,'client':'old','at':time.time()}
        results=[];service.records_deleted.connect(results.append)
        before=copy.deepcopy(service.pending)
        with patch.object(service.store,'delete_records',side_effect=OSError('unavailable')):
            service.execute({'action':'delete_records','ids':[row['id']],'force':True,'request_id':'force-failed'})
        self.assertFalse(results[-1]['success']);self.assertEqual(service.pending,before)
        self.assertIsNotNone(service.store.get(row['id']))


if __name__ == '__main__': unittest.main()

"""No live accounts, paid calls or real private data: queue and UI boundaries."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtGui, QtWidgets
import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.store import QueueStore, choose_client
from app_plugins.builtin.flow_assistant.policy import cost_key, draft_key, media_review
from app_plugins.builtin.flow_assistant.archive import archive_download, archive_package, cleanup_download


class ProductionTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    jobs = fixtures.FlowTests.jobs
    service = fixtures.FlowTests.service
    client = fixtures.FlowTests.client

    def test_unaffordable_online_browser_cannot_block_backup_launch(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=8, budget=0))
        client = self.client(service, credits=5)
        service.preferences.update(concurrency=1, profiles=['Backup'], profile_projects={'Backup': 'https://labs.google/project/backup'})
        service.observed_costs[cost_key(service.store.rows()[0]['payload'])] = 7
        launched = []; service.launch_requested.connect(launched.append)
        service.paused = False; service._tick()
        self.assertEqual(launched, ['Backup'])
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        self.assertFalse(any(json.loads(c.args[0]).get('action') == 'run_tasks' for c in client['socket'].sendTextMessage.call_args_list))

    def test_zero_credits_with_unknown_cost_is_not_selected(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=8))
        client = self.client(service, credits=0)
        self.assertIsNone(choose_client([client], service.store.rows(), 1, service.store.rows()[0]['payload'], {}))

    def test_duplicate_online_windows_of_a_busy_account_do_not_block_backup(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2))
        one = self.client(service, 'One'); two = self.client(service, 'Two'); two['account'] = one['account']
        row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=one['id'], remote={'account': one['account']})
        service.preferences.update(concurrency=2, profiles=['Backup'], profile_projects={'Backup': 'https://labs.google/project/backup'})
        launched = []; service.launch_requested.connect(launched.append)
        service.paused = False; service._tick()
        self.assertEqual(launched, ['Backup'])

    def test_shared_underbudget_setting_does_not_fail_the_remaining_batch(self):
        service = self.service(); service.store.enqueue(self.jobs(count=3, budget=2))
        client = self.client(service); service.paused = False; service._tick()
        request = next(key for key, value in service.pending.items() if value['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash', 'cost': 7}})
        sent = client['socket'].sendTextMessage.call_count
        service._tick()
        self.assertEqual([row['status'] for row in service.store.rows()], ['queued'] * 3)
        self.assertEqual(client['socket'].sendTextMessage.call_count, sent)

    def test_environment_error_pauses_one_browser_without_failing_the_remaining_queue(self):
        service = self.service(); service.store.enqueue(self.jobs(count=3, duration=8, budget=0))
        client = self.client(service); service.paused = False; service._tick()
        request_id = next(key for key, request in service.pending.items() if request['action'] == 'prepare')
        service._response(client, {'request_id': request_id, 'success': False, 'error': '当前模型不支持 8 秒'})
        sent = client['socket'].sendTextMessage.call_count
        service._tick()
        self.assertEqual([r['status'] for r in service.store.rows()], ['queued'] * 3)
        self.assertEqual(client['socket'].sendTextMessage.call_count, sent)
        self.assertIn('profile:Profile 8', service.preferences['browser_blocks'])
        service.execute({'action': 'resume_browser', 'keys': ['profile:Profile 8']})
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_account_changed_during_prepare_is_quarantined_before_upload(self):
        service = self.service(); service.store.enqueue(self.jobs()); client = self.client(service)
        service.paused = False; service._tick()
        key = next(key for key, value in service.pending.items() if value['action'] == 'prepare')
        service._response(client, {'request_id': key, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash', 'cost': 7, 'email': 'other@example.test'}})
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        actions = [json.loads(call.args[0]).get('action') for call in client['socket'].sendTextMessage.call_args_list]
        self.assertNotIn('upload_images', actions); self.assertNotIn('run_tasks', actions)

    def test_batch_cap_is_atomic_and_attempt_reservation_is_idempotent(self):
        store = QueueStore(self.namespace); batch = store.enqueue(self.jobs(count=20), limit_total=10)
        rows = store.rows()
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda pair: store.reserve(pair[1]['id'], f'{pair[0]:032x}', 7, 'page'), enumerate(rows)))
        self.assertEqual(sum(results), 1)
        winner = results.index(True)
        self.assertTrue(store.reserve(rows[winner]['id'], f'{winner:032x}', 7, 'page'))
        self.assertEqual(store.batches()[batch]['reserved'], 7)

    def test_expansion_and_submission_idempotency_prevent_double_queueing(self):
        service = self.service(); acks = []; failures = []
        service.submitted.connect(acks.append); service.submission_failed.connect(failures.append)
        command = {'action': 'enqueue', 'request_id': 'one-command', 'items': [{**self.args, 'count': 3}], 'batch_limit': 30}
        service.execute(command); service.execute(command)
        self.assertEqual(len(service.store.rows()), 3)
        self.assertEqual(acks[-1]['count'], 3)
        self.assertEqual(acks[0]['batch_id'], acks[1]['batch_id'])
        service.execute({**command, 'request_id': 'new-command'})
        self.assertEqual(len(service.store.rows()), 3)
        self.assertEqual(len(failures[-1]['duplicate_ids']), 3)
        service.execute({**command, 'request_id': 'new-command', 'allow_duplicates': True})
        self.assertEqual(len(service.store.rows()), 6)

    def test_cap_is_rechecked_after_upload_before_any_paid_submit(self):
        service = self.service(); batch = service.store.enqueue(self.jobs(count=2), limit_total=10)
        client = self.client(service); rows = service.store.rows()
        for index, row in enumerate(rows):
            service.store.update(row['id'], 'uploading', attempt=f'{index:032x}', browser=client['id'], remote={'cost': 7, 'cost_source': 'page'})
            service.request(client, 'upload_images', job=row['id'])
            request_id = next(key for key, req in service.pending.items() if req['action'] == 'upload_images')
            service._response(client, {'request_id': request_id, 'success': True})
        runs = [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list if json.loads(call.args[0]).get('action') == 'run_tasks']
        self.assertEqual(len(runs), 1)
        self.assertEqual(service.store.rows()[1]['status'], 'queued')
        self.assertEqual(service.store.batches()[batch]['reserved'], 7)

    def test_unknown_page_cost_cannot_bypass_batch_cap_using_a_user_budget(self):
        service = self.service(); service.store.enqueue(self.jobs(budget=7), limit_total=10)
        client = self.client(service); service.paused = False; service._tick()
        request = next(key for key, value in service.pending.items() if value['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash', 'cost': None}})
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        actions = [json.loads(c.args[0]).get('action') for c in client['socket'].sendTextMessage.call_args_list]
        self.assertNotIn('upload_images', actions); self.assertNotIn('run_tasks', actions)
        self.assertFalse(service.observed_costs)

    def bound_row(self, service, status='download_failed', finished=True):
        service.store.enqueue(self.jobs())
        row = service.store.rows()[0]; client = self.client(service)
        remote = {'account': client['account'], 'profile': client['profile'], 'cloud_finished': finished,
                  'upstream_task': 'task', 'page': 'https://labs.google/fx/tools/flow/project/original',
                  'bindings': [{'schema': 2, 'source': 'generation_response', 'taskId': 'task', 'submissionId': 'submission', 'mediaIds': ['media'],
                    'readyIds': ['media'], 'urls': {'media': 'https://flow-content.google/video/media'}}]}
        service.store.update(row['id'], status, attempt='a' * 32, browser=client['id'], remote=remote)
        return service.store.get(row['id']), client

    def test_download_release_preserves_binding_and_allows_next_job(self):
        service = self.service(); row, client = self.bound_row(service)
        self.assertIsNone(choose_client([client], service.store.rows(), 1))
        service.execute({'action': 'release_download', 'ids': [row['id']]})
        parked = service.store.get(row['id'])
        self.assertEqual(parked['status'], 'download_parked')
        self.assertEqual(parked['remote']['bindings'], row['remote']['bindings'])
        self.assertEqual(choose_client([client], service.store.rows(), 1)['id'], client['id'])
        service.execute({'action': 'retry', 'ids': [row['id']]})
        command = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(command['action'], 'retry_download')
        self.assertIn('restore', command['params'])
        self.assertFalse(any(json.loads(c.args[0]).get('action') == 'run_tasks' for c in client['socket'].sendTextMessage.call_args_list))

    def test_release_needs_finished_confirmation_and_rejects_active_generation(self):
        service = self.service(); row, client = self.bound_row(service, finished=False)
        errors = []; service.failed.connect(errors.append)
        service.execute({'action': 'release_download', 'ids': [row['id']]})
        self.assertEqual(service.store.get(row['id'])['status'], 'download_failed')
        self.assertIn('确认', errors[-1])
        service.store.update(row['id'], 'generating')
        service.execute({'action': 'release_download', 'ids': [row['id']], 'confirmed_finished': True})
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')

    def test_expired_link_can_only_be_replaced_for_the_same_saved_google_media(self):
        service = self.service(); row, client = self.bound_row(service)
        service.execute({'action': 'release_download', 'ids': [row['id']]})
        before = service.store.get(row['id'])['remote']['bindings']
        service.execute({'action': 'refresh_download_url', 'job': row['id'], 'url': 'https://flow-content.google/video/another'})
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'], before)
        service.execute({'action': 'refresh_download_url', 'job': row['id'], 'url': 'https://evil.test/video/media'})
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'], before)
        fresh = 'https://flow-content.google/video/media?signature=fresh'
        service.execute({'action': 'refresh_download_url', 'job': row['id'], 'url': fresh})
        request = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(request['action'], 'retry_download')
        self.assertEqual(request['params']['restore']['bindings'][0]['urls']['media'], fresh)

    def test_retry_failed_only_releases_failed_rows_not_unrelated_waiting_jobs(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2)); rows = service.store.rows()
        service.store.update(rows[0]['id'], 'failed')
        service.execute({'action': 'retry', 'ids': [], 'only_failed': True})
        self.assertTrue(service.paused); self.assertEqual(service.released, {rows[0]['id']})

    def test_legacy_queue_rows_remain_unchanged_when_new_tables_are_added(self):
        import sqlite3
        path = self.namespace.file('queue.sqlite3').path; path.parent.mkdir(parents=True, exist_ok=True)
        old = self.jobs()[0]
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,payload TEXT,status TEXT,attempt TEXT,browser TEXT,remote TEXT,error TEXT,updated REAL)')
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', (old['id'], json.dumps(old), 'completed', 'old', 'browser', '{}', '', 1))
            db.commit()
        finally:
            db.close()
        store = QueueStore(self.namespace)
        self.assertEqual(store.get(old['id'])['payload'], old)
        self.assertEqual(store.get(old['id'])['status'], 'completed')
        self.assertFalse(store.batches()); self.assertFalse(store.drafts())

    def test_late_old_socket_disconnect_cannot_fail_the_new_browser_connection(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        current = self.client(service)
        old_socket = MagicMock(); service.clients[old_socket] = {**current, 'socket': old_socket}
        service.store.update(row['id'], 'preparing', attempt='b' * 32, browser=current['id'])
        service._disconnected(old_socket)
        self.assertEqual(service.store.get(row['id'])['status'], 'preparing')

    def test_one_hundred_images_expand_to_three_hundred_distinct_persisted_jobs(self):
        from app_plugins.builtin.flow_assistant.store import make_jobs
        images = []
        for number in range(100):
            path = self.target / f'image-{number}.png'; path.write_bytes(f'fixture-{number}'.encode()); images.append(path)
        jobs = make_jobs(self.args['target'], images, 'bulk test', count=3)
        store = QueueStore(self.namespace); batch = store.enqueue(jobs, limit_total=2000)
        saved = QueueStore(self.namespace).rows()
        self.assertEqual(len(saved), 300)
        self.assertEqual(len({row['payload']['output'] for row in saved}), 300)
        self.assertEqual(store.batches()[batch]['limit_total'], 2000)

    def test_old_running_plugin_cannot_silently_use_new_budget_ui(self):
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        old = SimpleNamespace(version='0.2.0')
        with self.assertRaisesRegex(RuntimeError, '旧服务'):
            FlowDialog(old)

    def test_watchdog_queries_without_regenerating_and_reports_unresolved_result(self):
        service = self.service(); row, client = self.bound_row(service, 'generating', finished=False)
        remote = {**row['remote'], 'activity_at': time.time() - 2000}
        for count in range(3):
            service.store.update(row['id'], 'generating', remote={**remote, 'watch_count': count, 'watch_at': time.time() - 61})
            service.watch_stalled()
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        actions = [json.loads(c.args[0]).get('action') for c in client['socket'].sendTextMessage.call_args_list]
        self.assertIn('snapshot', actions); self.assertNotIn('run_tasks', actions)

    def test_identical_snapshots_do_not_reset_stall_clock_or_rewrite_database(self):
        service = self.service(); row, client = self.bound_row(service, 'generating', finished=False)
        snapshot = {'batch_id': row['attempt'], 'running': True, 'url': row['remote']['page'],
                    'tasks': [{'taskId': 'task', 'download_path': service.download_name(row), 'mediaBindings': []}]}
        service._snapshot(client, snapshot); first = service.store.get(row['id'])
        service._snapshot(client, snapshot); second = service.store.get(row['id'])
        self.assertEqual(first, second)

    def test_signed_url_refresh_is_not_progress_and_cannot_clear_stall_attention(self):
        service = self.service(); row, client = self.bound_row(service, 'generating', finished=False)
        task = {'taskId': 'task', 'download_path': service.download_name(row), 'mediaBindings': row['remote']['bindings']}
        snapshot = {'batch_id': row['attempt'], 'running': True, 'tasks': [task]}
        service._snapshot(client, snapshot)
        current = service.store.get(row['id']); activity = time.time() - 2000
        service.store.update(row['id'], 'generating', remote={**current['remote'], 'activity_at': activity, 'watch_count': 2})
        service.watch_stalled(); self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        task['mediaBindings'] = copy.deepcopy(task['mediaBindings'])
        task['mediaBindings'][0]['urls']['media'] += '?signature=refreshed'
        service._snapshot(client, snapshot)
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uncertain'); self.assertEqual(current['remote']['activity_at'], activity)

    def test_priority_editing_only_affects_unsubmitted_jobs(self):
        service = self.service(); service.store.enqueue(self.jobs(count=2)); rows = service.store.rows()
        service.execute({'action': 'priority', 'ids': [rows[1]['id']], 'value': 10})
        client = self.client(service); service.paused = False; service._tick()
        self.assertEqual(service.store.get(rows[1]['id'])['status'], 'preparing')
        before = service.store.get(rows[1]['id'])['payload']
        service.execute({'action': 'edit', 'ids': [rows[1]['id']], 'value': dict(prompt='new', duration=8, aspect='9:16', resolution='720p', budget=7)})
        self.assertEqual(service.store.get(rows[1]['id'])['payload'], before)

    def test_multi_edit_preserves_individual_prompts_and_rejects_partial_updates(self):
        service = self.service(); service.store.enqueue(self.jobs(prompt='one') + self.jobs(prompt='two'))
        rows = service.store.rows(); ids = [row['id'] for row in rows]
        service.execute({'action': 'edit', 'ids': ids, 'value': dict(prompt=None, duration=6, aspect='9:16', resolution='720p', budget=7)})
        self.assertEqual([row['payload']['prompt'] for row in service.store.rows()], ['one', 'two'])
        self.assertEqual([row['payload']['duration'] for row in service.store.rows()], [6, 6])
        service.store.update(ids[1], 'generating', attempt='a' * 32)
        service.execute({'action': 'priority', 'ids': ids, 'value': 100})
        self.assertEqual([row['payload']['priority'] for row in service.store.rows()], [0, 0])

    def test_disk_failure_before_paid_submit_pauses_queue_without_charging(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        client = self.client(service)
        service.store.update(row['id'], 'uploading', attempt='a' * 32, browser=client['id'], remote={'cost': 7, 'cost_source': 'page'})
        service.request(client, 'upload_images', job=row['id']); request = next(iter(service.pending))
        with patch('model.MediaStorage.require_media_output', side_effect=OSError('任务磁盘空间不足')):
            service._response(client, {'request_id': request, 'success': True})
        self.assertTrue(service.paused); self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertEqual(next(iter(service.store.batches().values()))['reserved'], 0)
        self.assertFalse(any(json.loads(c.args[0]).get('action') == 'run_tasks' for c in client['socket'].sendTextMessage.call_args_list))

    def test_drafts_survive_store_reopen_and_are_scoped_to_image_and_task(self):
        store = QueueStore(self.namespace); one = draft_key(self.target, self.image)
        two = draft_key(self.target, self.target / 'other.png')
        store.save_draft(one, {'prompt': 'first', 'options': {'duration': 6}})
        store.save_draft(two, {'prompt': 'second', 'options': {'duration': 8}})
        saved = QueueStore(self.namespace).drafts()
        self.assertEqual(saved[one]['prompt'], 'first'); self.assertEqual(saved[two]['options']['duration'], 8)

    def test_new_private_tables_stop_writing_on_volume_loss_without_plaintext_fallback(self):
        from model.AppDataStorage import ApplicationStorage
        from model.ProtectedStorage import StorageUnavailable
        guard = SimpleNamespace(check=MagicMock())
        namespace = ApplicationStorage(self.root / 'encrypted', guard=guard).for_plugin('flow_assistant')
        store = QueueStore(namespace); key = draft_key(self.target, self.image)
        store.save_draft(key, {'prompt': 'kept private'})
        path = namespace.file('queue.sqlite3').path
        before = path.read_bytes()
        guard.check.side_effect = StorageUnavailable('test volume lost')
        with self.assertRaises(StorageUnavailable): store.save_draft(key, {'prompt': 'must not leak'})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.target / 'queue.sqlite3').exists())

    def archive_row(self):
        store = QueueStore(self.namespace); store.enqueue(self.jobs(duration=8)); row = store.rows()[0]
        row['attempt'] = 'a' * 32; downloads = self.root / 'downloads'
        source = downloads / 'FlowBridge' / row['attempt'] / 'result.mp4'; source.parent.mkdir(parents=True)
        source.write_bytes(b'ftyp' + b'x' * 100)
        row['remote']['download'] = {'filename': str(source), 'state': 'complete', 'mediaId': 'media', 'submissionId': 'submission'}
        return row, downloads, source

    def test_quality_anomaly_is_preserved_for_review_not_auto_deleted(self):
        row, downloads, source = self.archive_row()
        info = {'duration': 3, 'video': {'width': 320, 'height': 180}}
        with patch('model.node_workflow.render.probe', return_value=info), patch('model.node_workflow.render.resolve_tools', return_value=('ffmpeg', 'ffprobe')):
            result = archive_package(row, downloads, cleanup=True)
        self.assertGreaterEqual(len(result['review']), 3)
        self.assertTrue(source.exists()); self.assertTrue(Path(result['output']).exists())

    def test_opt_in_cleanup_requires_success_and_matching_checksums(self):
        row, downloads, source = self.archive_row()
        info = {'duration': 8, 'video': {'width': 720, 'height': 1280}}
        with patch('model.node_workflow.render.probe', return_value=info), patch('model.node_workflow.render.resolve_tools', return_value=('ffmpeg', 'ffprobe')):
            result = archive_package(row, downloads, cleanup=True)
        self.assertFalse(result['review']); self.assertTrue(result['download_cleaned'])
        self.assertFalse(source.exists()); self.assertTrue(Path(result['output']).exists()); self.assertTrue(self.image.exists())

    def test_low_task_disk_space_never_removes_download_or_creates_incomplete_output(self):
        row, downloads, source = self.archive_row()
        with patch('app_plugins.builtin.flow_assistant.archive.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(OSError, '空间不足'): archive_download(row, downloads, verify=lambda _: None)
        self.assertTrue(source.exists()); self.assertFalse(Path(row['payload']['output']).exists())

    def test_cleanup_rejects_changed_output_and_anomalous_result(self):
        row, downloads, source = self.archive_row()
        output = Path(archive_download(row, downloads, verify=lambda _: None)); output.write_bytes(b'user-modified output')
        with self.assertRaisesRegex(ValueError, '校验不同'):
            cleanup_download({**row, 'status': 'completed'}, downloads)
        with self.assertRaisesRegex(ValueError, '待复核'):
            cleanup_download({**row, 'status': 'needs_review'}, downloads)
        self.assertTrue(source.exists()); self.assertEqual(output.read_bytes(), b'user-modified output')

    def ui(self):
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        image = QtGui.QImage(120, 200, QtGui.QImage.Format.Format_RGB32); image.fill(0xff40aabb)
        image.save(str(self.image)); second = self.target / 'second.png'; image.save(str(second))
        plugin = SimpleNamespace(command=MagicMock(), context=SimpleNamespace(parent_widget=SimpleNamespace()))
        dialog = FlowDialog(plugin); dialog.set_targets([self.args['target']]); self.addCleanup(dialog.close)
        return dialog, plugin

    def test_per_image_drafts_restore_parameters_without_preview_or_state_rollback(self):
        dialog, plugin = self.ui(); panel = dialog.manual
        panel.prompt.setPlainText('first prompt'); panel.options.duration.setCurrentIndex(panel.options.duration.findData(6))
        key = panel.current_key; panel.picker.images.setCurrentRow(1)
        panel.prompt.setPlainText('second prompt'); panel.options.duration.setCurrentIndex(panel.options.duration.findData(10))
        panel.picker.images.setCurrentRow(0)
        self.assertEqual(panel.prompt.toPlainText(), 'first prompt'); self.assertEqual(panel.options.duration.currentData(), 6)
        panel.apply_state({'drafts': {key: {'prompt': 'old stale prompt', 'options': {'duration': 8}}}})
        self.assertEqual(panel.prompt.toPlainText(), 'first prompt')

    def test_submit_next_changes_image_only_after_matching_success_ack(self):
        dialog, plugin = self.ui(); panel = dialog.manual; original = panel.picker.image_path
        panel.prompt.setPlainText('submit'); panel.enqueue(advance=True); request = panel.request_id
        panel.acknowledged({'request_id': 'old', 'count': 1}); self.assertEqual(panel.picker.image_path, original)
        panel.failed({'request_id': request, 'error': 'save failed'}); self.assertEqual(panel.picker.image_path, original)
        panel.enqueue(advance=True); request = panel.request_id
        panel.acknowledged({'request_id': request, 'count': 1})
        self.assertNotEqual(panel.picker.image_path, original)

    def test_duplicate_ui_keeps_draft_until_explicit_variant_confirmation(self):
        dialog, plugin = self.ui(); panel = dialog.manual
        panel.prompt.setPlainText('variant'); panel.enqueue(); request = panel.request_id
        with patch.object(dialog, 'confirm_duplicate', return_value=True):
            panel.failed({'request_id': request, 'error': 'duplicate', 'duplicate_ids': ['original']})
        command = plugin.command.emit.call_args.args[0]
        self.assertTrue(command['allow_duplicates']); self.assertEqual(command['request_id'], request)
        self.assertEqual(panel.prompt.toPlainText(), 'variant')

    def test_fresh_panel_loads_draft_options_and_queue_group_headers_are_not_jobs(self):
        dialog, plugin = self.ui(); panel = dialog.manual
        key = panel.current_key
        state = {'jobs': [], 'clients': [], 'paused': True, 'preferences': {}, 'batches': {}, 'spaces': [],
                 'drafts': {key: {'prompt': 'restored', 'options': {'duration': 6, 'count': 3}, 'batch_limit': 30}}}
        dialog.update_state(state)
        self.assertEqual(panel.prompt.toPlainText(), 'restored'); self.assertEqual(panel.options.count.value(), 3)
        self.assertEqual(panel.batch_limit.value(), 30)
        store = QueueStore(self.namespace); store.enqueue(self.jobs(count=3))
        state.update(jobs=store.rows(), batches=store.batches()); dialog.update_state(state)
        self.assertEqual(dialog.queue.rowCount(), 4)
        dialog.queue.selectRow(0); self.assertEqual(dialog.selected_ids(), [])
        dialog.queue_tools.search.setText('not-found'); self.assertEqual(dialog.queue.rowCount(), 0)

    def test_result_preview_reuses_host_player_and_seeks_without_rendering(self):
        from app_plugins.builtin.flow_assistant.result_preview import ResultPreview
        class Surface(QtWidgets.QWidget):
            positionChanged = QtCore.pyqtSignal(float)
            errorOccurred = QtCore.pyqtSignal(str)
            def __init__(self, _ffmpeg, parent, **_kwargs):
                super().__init__(parent); self.prepare = MagicMock(); self.play = MagicMock(); self.release = MagicMock()
            def pause(self): pass
        output = self.target / 'result.mp4'; output.write_bytes(b'fixture')
        row = {'payload': {'output': str(output), 'duration': 8}, 'remote': {'media': {'duration': 8}}}
        with patch('model.MediaPlayback.FfplayReviewSurface', Surface):
            preview = ResultPreview(row)
            preview.slider.setValue(5000); preview.seek()
            self.assertEqual(preview.surface.prepare.call_args.args, (str(output), 0, 8, 4))
            preview.close(); preview.surface.release.assert_called_once()

    def test_ui_layout_screenshot_with_drafts_and_recent_batch(self):
        import os
        path = os.environ.get('LZX_FLOW_PRODUCTION_SCREENSHOT')
        if not path: self.skipTest('optional visual QA snapshot')
        dialog, plugin = self.ui(); store = QueueStore(self.namespace); store.enqueue(self.jobs(count=3, duration=8))
        dialog.update_state({'jobs': store.rows(), 'clients': [], 'paused': True, 'preferences': {},
            'drafts': {}, 'batches': store.batches(), 'spaces': [{'path': 'Downloads', 'free': 10**10}]})
        dialog.show(); self.app.processEvents()
        dialog.grab().save(path)
        dialog.tabs.setCurrentIndex(0); self.app.processEvents(); dialog.grab().save(str(Path(path).with_name('flow-queue.png')))


if __name__ == '__main__': unittest.main()

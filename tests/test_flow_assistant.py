import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from flow_reference_fixtures import upload_receipt, distinct_jobs
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import ApplicationStorage
from app_plugins.builtin.flow_assistant.store import QueueStore, choose_client, make_jobs
from app_plugins.builtin.flow_assistant.archive import archive_download
from app_plugins.builtin.flow_assistant.service import EXTENSION_ID, FlowService
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        import os
        if os.environ.get('LZX_FLOW_SCREENSHOT'):
            from qt_compat import QtGui
            font_id = QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families: cls.app.setFont(QtGui.QFont(families[0], 9))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.namespace = ApplicationStorage(self.root / 'private').for_plugin('flow_assistant')
        self.target = self.root / 'tasks' / '1002' / 'reels' / '8010'
        self.target.mkdir(parents=True)
        self.image = self.target / 'chosen.png'
        self.image.write_bytes(b'fake image for source identity tests')
        self.args = dict(target={'target_dir': str(self.target), 'label': '8010',
                                'task': {'task_id': '8010', 'task_video_prompt': 'original'}},
                         images=[self.image], prompt='preserve\nthis prompt', budget=7)

    def jobs(self, **updates):
        return make_jobs(**{**self.args, **updates})

    def service(self):
        self.namespace.file('pairing.json').write_json({'token': 'x' * 40, 'port': 0})
        service = FlowService(self.namespace, str(self.root / 'downloads'))
        service.start()
        self.addCleanup(lambda: service.execute({'action': 'stop'}))
        return service

    def test_snapshot_names_are_stable_and_batches_transactional(self):
        store = QueueStore(self.namespace)
        jobs = self.jobs(count=2)
        store.enqueue(jobs)
        rows = store.rows()
        self.assertEqual([Path(r['payload']['output']).name for r in rows],
                         ['Flow_001_chosen.mp4', 'Flow_002_chosen.mp4'])
        self.assertEqual(rows[0]['payload']['prompt'], 'preserve\nthis prompt')
        duplicate = self.jobs()[0]
        with self.assertRaises(Exception): store.enqueue([duplicate, duplicate])
        self.assertEqual(len(store.rows()), 2)
        self.assertFalse(list(self.target.glob('*.mp4')))

    def test_restart_never_requeues_a_possible_paid_submission(self):
        store = QueueStore(self.namespace)
        store.enqueue(self.jobs())
        row = store.rows()[0]
        store.update(row['id'], 'submitting', attempt='a' * 32, browser='browser')
        store.recover()
        self.assertEqual(store.get(row['id'])['status'], 'uncertain')
        store.retry([row['id']])
        self.assertEqual(store.get(row['id'])['status'], 'uncertain')
        store.retry([row['id']], regenerate=True)
        self.assertEqual(store.get(row['id'])['status'], 'queued')

    def test_same_google_account_is_not_parallelized_and_download_state_is_held(self):
        client = {'id': 'one', 'account': 'same@example.test', 'credits': 50, 'credits_at': time.time()}
        duplicate = {**client, 'id': 'two'}
        active = {'status': 'generating', 'browser': 'one', 'remote': {'account': client['account']}}
        self.assertIsNone(choose_client([client, duplicate], [active], 3))
        active['status'] = 'download_failed'
        self.assertIsNone(choose_client([client, duplicate], [active], 3))
        other = {**client, 'id': 'three', 'account': 'other@example.test'}
        self.assertEqual(choose_client([client, duplicate, other], [active], 3)['id'], 'three')

    def test_existing_parameter_guard_yields_before_native_actions(self):
        from model.FlowParameterGuard import _FlowUiaController
        controller = _FlowUiaController.__new__(_FlowUiaController)
        controller.bridge_pause_requested = lambda: True
        controller._select = MagicMock(); controller._invoke = MagicMock(); controller._legacy_action = MagicMock()
        self.assertFalse(controller._activate(object()))
        self.assertFalse(controller._open_popup(object()))
        controller._select.assert_not_called(); controller._invoke.assert_not_called()

    def test_retry_interrupted_download_does_not_retry_archive_or_generation(self):
        store = QueueStore(self.namespace); store.enqueue(self.jobs()); row = store.rows()[0]
        store.update(row['id'], 'download_failed', remote={'bindings': [{'mediaIds': ['media']}],
                                                          'download': {'state': 'interrupted'}})
        store.retry([row['id']])
        self.assertEqual(store.get(row['id'])['status'], 'download_failed')

    def test_changed_image_fails_before_any_browser_submit(self):
        service = self.service(); service.store.enqueue(self.jobs())
        fake = MagicMock()
        service.clients[fake] = {'socket': fake, 'authorized': True, 'id': 'browser', 'account': 'a@test.test',
                                'credits': 50, 'credits_at': time.time()}
        self.image.write_bytes(b'changed')
        service.paused = False; service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'failed')
        fake.sendTextMessage.assert_not_called()

    def test_model_verification_failure_never_uploads_or_generates(self):
        service = self.service(); service.store.enqueue(self.jobs())
        row = service.store.rows()[0]
        fake = MagicMock()
        client = {'socket': fake, 'authorized': True, 'id': 'browser', 'account': 'a@test.test',
                  'credits': 50, 'credits_at': time.time()}
        service.clients[fake] = client
        service.store.update(row['id'], 'preparing', attempt='a' * 32, browser='browser')
        service.request(client, 'prepare', job=row['id'])
        request_id = next(iter(service.pending))
        service._response(client, {'request_id': request_id, 'success': True, 'data': {'verified': False, 'model': 'veo'}})
        self.assertEqual(service.store.get(row['id'])['status'], 'failed')
        self.assertEqual(fake.sendTextMessage.call_count, 1)

    def test_prepared_job_is_committed_before_send_and_late_reply_is_ignored(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        fake = MagicMock()
        client = {'socket': fake, 'authorized': True, 'id': 'browser', 'account': 'a@test.test', 'credits': 50}
        service.clients[fake] = client
        service.store.update(row['id'], 'uploading', attempt='a' * 32, browser='browser',
                             remote={'cost': 7, 'cost_source': 'page'})
        service.request(client, 'upload_images', job=row['id'])
        request_id = next(iter(service.pending))
        seen = []
        fake.sendTextMessage.side_effect = lambda _message: seen.append(service.store.get(row['id'])['status'])
        service._response(client, {'request_id': request_id, 'success': True, 'data': upload_receipt(service, row['id'])})
        self.assertEqual(seen, ['submitting'])
        request_id = next(iter(service.pending))
        service.store.update(row['id'], 'queued', attempt='b' * 32)
        service._response(client, {'request_id': request_id, 'success': True})
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')

    def test_download_archive_is_bound_preserves_original_and_never_overwrites(self):
        store = QueueStore(self.namespace); store.enqueue(self.jobs()); row = store.rows()[0]
        row['attempt'] = 'a' * 32
        downloads = self.root / 'downloads'
        source = downloads / 'FlowBridge' / row['attempt'] / 'result.mp4'
        source.parent.mkdir(parents=True); source.write_bytes(b'ftyp' + b'x' * 100)
        row['remote']['download'] = {'filename': str(source), 'state': 'complete', 'mediaId': 'media', 'submissionId': 'sub'}
        output = Path(archive_download(row, downloads, verify=lambda path: None))
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertTrue(source.exists())
        self.assertEqual(archive_download(row, downloads, verify=lambda path: None), str(output))
        output.write_bytes(b'existing different content')
        with self.assertRaises(FileExistsError): archive_download(row, downloads, verify=lambda path: None)
        self.assertEqual(output.read_bytes(), b'existing different content')
        row['remote']['download']['filename'] = str(self.image)
        with self.assertRaises(ValueError): archive_download(row, downloads, verify=lambda path: None)

    def test_websocket_requires_extension_origin_and_pair_token(self):
        from PyQt6.QtWebSockets import QWebSocket
        from qt_compat import QtTest
        service = self.service()
        good = QWebSocket(f'chrome-extension://{EXTENSION_ID}')
        bad = QWebSocket('https://example.test')
        url = QtCore.QUrl(f'ws://127.0.0.1:{service.server.serverPort()}')
        messages = []
        good.textMessageReceived.connect(messages.append)
        good.open(url); bad.open(url)
        for _ in range(20):
            QtTest.QTest.qWait(10)
            if service.clients: break
        good.sendTextMessage(json.dumps({'type': 'hello', 'protocol': 1, 'token': 'wrong', 'browser_id': 'bad'}))
        QtTest.QTest.qWait(30)
        self.assertFalse(any(c.get('authorized') for c in service.clients.values()))
        good = QWebSocket(f'chrome-extension://{EXTENSION_ID}'); good.textMessageReceived.connect(messages.append)
        good.open(url)
        for _ in range(100):
            QtTest.QTest.qWait(10)
            if good.isValid(): break
        self.assertTrue(good.isValid(), good.errorString())
        good.sendTextMessage(json.dumps({'type': 'hello', 'protocol': 1, 'token': 'x' * 40, 'browser_id': 'real-browser'}))
        for _ in range(100):
            QtTest.QTest.qWait(10)
            if any(json.loads(message).get('type') == 'welcome' for message in messages): break
        self.assertTrue(any(json.loads(message).get('type') == 'welcome' for message in messages))
        good.close(); bad.close(); QtTest.QTest.qWait(20)

    def test_ui_keeps_drafts_until_store_ack_and_uses_task_prompt(self):
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        plugin = SimpleNamespace(ui_api_version=2, command=MagicMock(), context=SimpleNamespace(parent_widget=SimpleNamespace()))
        dialog = FlowDialog(plugin)
        try:
            dialog.set_targets([self.args['target']])
            dialog.add_all_images()
            self.assertEqual(dialog.compose.item(0, 2).text(), 'original')
            dialog.enqueue()
            self.assertEqual(dialog.compose.rowCount(), 1)
            self.assertFalse(dialog.submit_button.isEnabled())
            import os
            if os.environ.get('LZX_FLOW_SCREENSHOT'):
                dialog.show(); self.app.processEvents(); dialog.grab().save(os.environ['LZX_FLOW_SCREENSHOT'])
        finally: dialog.close()

    def test_plugin_worker_starts_and_stops_without_a_live_account(self):
        from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
        from qt_compat import QtTest
        self.namespace.file('pairing.json').write_json({'token': 'x' * 40, 'port': 0})
        parent = QtWidgets.QWidget()
        context = SimpleNamespace(storage=self.namespace, parent_widget=parent, log=MagicMock(),
            register_command=MagicMock(), register_settings_page=MagicMock(), load_config=MagicMock(return_value={
                'flow_assistant_timing': {'credit_validity_seconds': 43200, 'queue_tick_seconds': 2},
                'flow_assistant_browser_options': {'focus_before_action': False}}))
        plugin = FlowAssistantPlugin(); plugin.register(context)
        self.assertEqual(context.register_command.call_count, 2)
        self.assertEqual({call.args[0].page_id for call in context.register_settings_page.call_args_list},
                         {'timing', 'browser'})
        plugin.start()
        try:
            for _ in range(100):
                QtTest.QTest.qWait(10)
                if plugin.snapshot: break
            self.assertIn('jobs', plugin.snapshot)
            self.assertEqual(plugin.snapshot['preferences']['timing']['credit_validity_seconds'], 43200)
            self.assertFalse(plugin.snapshot['preferences']['browser_options']['focus_before_action'])
            plugin.apply_settings({'flow_assistant_timing': {'credit_validity_seconds': 21600},
                                   'flow_assistant_browser_options': {'focus_before_action': True}})
            for _ in range(100):
                QtTest.QTest.qWait(10)
                if plugin.snapshot['preferences']['timing']['credit_validity_seconds'] == 21600: break
            self.assertEqual(plugin.snapshot['preferences']['timing']['credit_validity_seconds'], 21600)
            self.assertTrue(plugin.snapshot['preferences']['browser_options']['focus_before_action'])
            self.assertTrue(plugin.stop())
            self.assertFalse(plugin.thread.isRunning())
        finally:
            plugin.stop(); parent.close()

    def test_local_closed_loop_archives_a_real_synthetic_mp4_once(self):
        self._local_closed_loop('result.mp4')

    def test_local_closed_loop_archives_a_chrome_uniquified_mp4_once(self):
        self._local_closed_loop('result (1).mp4')

    def _local_closed_loop(self, downloaded_name):
        import os
        import shutil
        import subprocess
        from PyQt6.QtWebSockets import QWebSocket
        from qt_compat import QtTest
        actual = Path(os.environ.get('LZX_TEST_BASE_ROOT') or Path(__file__).parents[1])
        ffmpeg, ffprobe = actual / 'runtime/ffmpeg/ffmpeg.exe', actual / 'runtime/ffmpeg/ffprobe.exe'
        if not ffmpeg.is_file(): self.skipTest('bundled FFmpeg unavailable')
        sample = self.root / 'synthetic.mp4'
        subprocess.run([str(ffmpeg), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=blue:s=360x640:r=10', '-t', '0.2', '-c:v', 'libx264', str(sample)],
            check=True, timeout=20, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        service = self.service(); service.store.enqueue(self.jobs(resolution='360p')); service.paused = False
        socket = QWebSocket(f'chrome-extension://{EXTENSION_ID}')
        runs, downloads = [], []
        def message(text):
            value = json.loads(text)
            action = value.get('action')
            response = {'request_id': value.get('request_id'), 'success': True}
            if action == 'get_points': response['data'] = {'email': 'synthetic@example.test', 'remaining_points': 50}
            elif action == 'snapshot':
                socket.sendTextMessage(json.dumps({'type': 'snapshot', 'data': {
                    'url': 'https://labs.google/fx/tools/flow/project/synthetic-project', 'tasks': []}}))
                return
            elif action == 'prepare': response['data'] = {'verified': True, 'model': 'omni_1_1_flash', 'cost': 7}
            elif action == 'run_tasks':
                runs.append(value)
                batch = value['params']['batch_id']
                source = self.root / 'downloads' / 'FlowBridge' / batch / downloaded_name
                source.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(sample, source); downloads.append(source)
                socket.sendTextMessage(json.dumps(response))
                socket.sendTextMessage(json.dumps({'type': 'snapshot', 'data': {'batch_id': batch,
                    'running': True, 'tasks': [{'taskId': 'upstream-task', 'download_path': f'FlowBridge/{batch}/result.mp4',
                        'mediaBindings': [{'schema': 2, 'source': 'generation_response', 'taskId': 'upstream-task',
                            'submissionId': 'submission', 'mediaIds': ['media'], 'readyIds': ['media'],
                            'urls': {'media': 'https://flow-content.google/video/media'}}]}]}}))
                socket.sendTextMessage(json.dumps({'type': 'download', 'event_id': 'complete-event', 'batch_id': batch,
                    'data': {'filename': str(source), 'state': 'complete', 'mediaId': 'media', 'submissionId': 'submission',
                             'taskId': 'upstream-task', 'download_path': f'FlowBridge/{batch}/result.mp4'}}))
                return
            elif action == 'upload_images': response['data'] = {'reference': {**value['params']['reference'], 'media_id': 'uploaded-reference', 'verified': True}}
            else: return
            socket.sendTextMessage(json.dumps(response))
        socket.textMessageReceived.connect(message)
        socket.connected.connect(lambda: socket.sendTextMessage(json.dumps({'type': 'hello', 'protocol': 1,
            'token': 'x' * 40, 'browser_id': 'synthetic-browser',
            'project': 'https://labs.google/fx/tools/flow/project/synthetic-project'})))
        with patch('model.node_workflow.render.resolve_tools', return_value=(str(ffmpeg), str(ffprobe))):
            socket.open(QtCore.QUrl(f'ws://127.0.0.1:{service.server.serverPort()}'))
            for _ in range(500):
                QtTest.QTest.qWait(10)
                if service.store.rows()[0]['status'] == 'completed': break
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'completed', row['error'])
        self.assertEqual(len(runs), 1)
        self.assertEqual(Path(row['payload']['output']).read_bytes(), sample.read_bytes())
        self.assertTrue(downloads[0].is_file())
        socket.close(); QtTest.QTest.qWait(20)

    def client(self, service, profile='Profile 8', credits=50):
        fake = MagicMock()
        value = {'socket': fake, 'authorized': True, 'id': profile + '-id', 'profile': profile,
                 'extension_version': EXTENSION_VERSION,
                 'account': profile + '@example.test', 'credits': credits, 'credits_at': time.time(),
                 'project': 'https://labs.google/fx/tools/flow/project/synthetic-project',
                 'saved_project': 'https://labs.google/fx/tools/flow/project/synthetic-project'}
        service.clients[fake] = value
        return value

    def test_duration_lanes_do_not_steal_other_durations(self):
        clients = [{'id': str(n), 'profile': f'Profile {n}', 'account': f'{n}@example.test',
                    'credits': points, 'credits_at': time.time()} for n, points in [(6, 90), (8, 20), (10, 80), (11, 70)]]
        prefs = {'profile_lanes': {'Profile 6': 6, 'Profile 8': 8, 'Profile 10': 10}, 'allow_general': True, 'auto_lane': False}
        self.assertEqual(choose_client(clients, [], 4, {'duration': 8}, prefs)['id'], '8')
        clients[1]['blocked'] = '积分不足'
        self.assertEqual(choose_client(clients, [], 4, {'duration': 8}, prefs)['id'], '11')
        prefs['allow_general'] = False
        self.assertIsNone(choose_client(clients, [], 4, {'duration': 8}, prefs))
        self.assertEqual(choose_client(clients, [], 4, {'duration': None}, prefs)['id'], '11')

    def test_known_cost_uses_smallest_sufficient_balance(self):
        clients = [{'id': str(n), 'account': f'{n}@example.test', 'credits': n, 'credits_at': time.time()}
                   for n in (5, 20, 100)]
        self.assertEqual(choose_client(clients, [], 4, {'duration': 8}, {}, cost=10)['credits'], 20)
        self.assertIsNone(choose_client(clients, [], 4, {'duration': 8}, {}, cost=101))

    def test_manual_release_only_dispatches_clicked_job_not_batch(self):
        service = self.service()
        service.execute({'action': 'enqueue', 'items': [{**self.args, 'duration': 8, 'budget': 0}]})
        ack = []; service.submitted.connect(ack.append)
        service.execute({'action': 'enqueue', 'source': 'manual', 'request_id': 'manual-one',
                         'items': [{**self.args, 'duration': 8, 'budget': 0, 'prompt': 'manual prompt'}]})
        client = self.client(service)
        service._tick()
        rows = service.store.rows()
        self.assertTrue(service.paused)
        self.assertEqual([r['status'] for r in rows], ['queued', 'preparing'])
        self.assertEqual(rows[1]['payload']['prompt'], 'manual prompt')
        self.assertEqual(rows[1]['payload']['submission_mode'], 'manual')
        self.assertEqual(ack[0]['request_id'], 'manual-one')
        self.assertEqual(service.preferences['profile_lanes']['Profile 8'], 8)
        request = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(request['params']['duration'], 8)

    def test_pause_and_restart_hold_unsubmitted_manual_jobs(self):
        service = self.service()
        service.execute({'action': 'enqueue', 'source': 'manual', 'items': [self.args]})
        row = service.store.rows()[0]
        self.assertIn(row['id'], service.released)
        service.execute({'action': 'pause'})
        self.assertFalse(service.released)
        client = self.client(service); service._tick()
        client['socket'].sendTextMessage.assert_not_called()
        service.execute({'action': 'submit_selected', 'ids': [row['id']]})
        self.assertIn(row['id'], service.released)
        # A new coordinator does not inherit in-memory releases.
        other = FlowService(self.namespace, str(self.root / 'downloads'))
        self.assertFalse(other.released)
        self.assertTrue(other.paused)
        self.assertEqual(QueueStore(self.namespace).get(row['id'])['status'], 'queued')

    def test_fifo_reassigns_existing_browser_before_later_matching_duration(self):
        service = self.service()
        service.store.enqueue(distinct_jobs(self, 1, duration=10, budget=0) + distinct_jobs(self, 1, duration=6, budget=0))
        six = self.client(service, 'Profile 6')
        service.preferences.update({'profile_lanes': {'Profile 6': 6, 'Profile 8': 8, 'Profile 10': 10},
                                    'profiles': ['Profile 8', 'Profile 10'], 'allow_general': False,
                                    'profile_projects': {'Profile 8': 'https://labs.google/project/eight',
                                                         'Profile 10': 'https://labs.google/project/ten'}})
        service.paused = False; service._tick()
        self.assertEqual([r['status'] for r in service.store.rows()], ['preparing', 'queued'])
        self.assertEqual(json.loads(six['socket'].sendTextMessage.call_args.args[0])['params']['duration'], 10)
        launched = []; service.launch_requested.connect(launched.append)
        service._tick()
        self.assertEqual(launched, ['Profile 8'])

    def test_lane_is_persistent_and_can_be_changed_without_losing_queue(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=8))
        service.execute({'action': 'lane', 'scope': 'profile', 'key': 'Profile 8', 'duration': 8})
        self.assertEqual(QueueStore(self.namespace).settings()['profile_lanes']['Profile 8'], 8)
        service.execute({'action': 'lane', 'scope': 'profile', 'key': 'Profile 8', 'duration': 10})
        self.assertEqual(len(service.store.rows()), 1)
        self.assertEqual(service.store.rows()[0]['payload']['duration'], 8)
        errors = []; service.failed.connect(errors.append)
        service.execute({'action': 'lane', 'scope': 'profile', 'key': 'Profile 8', 'duration': 7})
        self.assertTrue(errors)
        self.assertEqual(service.preferences['profile_lanes']['Profile 8'], 10)

    def test_site_verification_blocks_new_manual_release(self):
        service = self.service(); service.safety_paused = True
        acks = []; service.submitted.connect(acks.append)
        service.execute({'action': 'enqueue', 'source': 'manual', 'items': [self.args]})
        self.assertFalse(service.released)
        self.assertTrue(acks[0]['held'])
        self.assertEqual(service.store.rows()[0]['status'], 'queued')

    def test_failed_manual_commit_reports_its_request_and_retains_existing_queue(self):
        service = self.service(); service.store.enqueue(self.jobs())
        errors = []; service.submission_failed.connect(errors.append)
        service.execute({'action': 'enqueue', 'source': 'manual', 'request_id': 'bad',
                         'items': [{**self.args, 'prompt': ''}]})
        self.assertEqual(errors[0]['request_id'], 'bad')
        self.assertEqual(len(service.store.rows()), 1)
        self.assertFalse(service.released)

    def test_manual_preview_uses_full_image_and_ack_cannot_clear_batch_drafts(self):
        from qt_compat import QtGui
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
        image = QtGui.QImage(360, 640, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#40b8cc')); image.save(str(self.image))
        plugin = FlowAssistantPlugin()
        plugin.context = SimpleNamespace(parent_widget=SimpleNamespace(), log=MagicMock())
        commands = []; plugin.command.connect(commands.append)
        dialog = FlowDialog(plugin); plugin.dialog = dialog
        try:
            dialog.set_targets([self.args['target']]); dialog.add_all_images()
            manual = dialog.manual
            self.assertFalse(manual.picker.preview.image.isNull())
            self.assertEqual(manual.picker.preview.image.size(), QtCore.QSize(360, 640))
            self.assertIn('360 × 640', manual.picker.info.text())
            self.assertEqual(manual.prompt.toPlainText(), 'original')
            manual.prompt.setPlainText('one exact manual prompt'); manual.enqueue()
            request = commands[-1]
            self.assertEqual(request['source'], 'manual')
            self.assertEqual(request['items'][0]['images'], [str(self.image)])
            self.assertEqual(request['items'][0]['prompt'], 'one exact manual prompt')
            self.assertFalse(manual.isEnabled())
            plugin._failed('unrelated browser warning')
            self.assertFalse(manual.isEnabled())
            plugin._submitted({'source': 'manual', 'request_id': 'old request', 'count': 1})
            self.assertFalse(manual.isEnabled())
            plugin._submitted({'source': 'manual', 'request_id': request['request_id'], 'count': 1})
            self.assertTrue(manual.isEnabled())
            self.assertEqual(dialog.compose.rowCount(), 1)
            self.assertEqual(manual.prompt.toPlainText(), 'one exact manual prompt')
            import os
            if os.environ.get('LZX_FLOW_MANUAL_SCREENSHOT'):
                dialog.show(); self.app.processEvents(); dialog.grab().save(os.environ['LZX_FLOW_MANUAL_SCREENSHOT'])
        finally:
            dialog.close()

    def test_switching_manual_tasks_keeps_correct_image_and_prompt_draft(self):
        from qt_compat import QtGui
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        image = QtGui.QImage(200, 100, QtGui.QImage.Format.Format_RGB32); image.fill(0); image.save(str(self.image))
        second = self.target.parent / '8011'; second.mkdir()
        image.save(str(second / 'second.png'))
        other = {'target_dir': str(second), 'label': '8011', 'task': {'task_id': '8011', 'task_video_prompt': 'second prompt'}}
        plugin = SimpleNamespace(ui_api_version=2, command=MagicMock(), context=SimpleNamespace(parent_widget=SimpleNamespace()))
        dialog = FlowDialog(plugin)
        try:
            dialog.set_targets([self.args['target'], other])
            manual = dialog.manual
            manual.prompt.setPlainText('first edited draft')
            manual.picker.target.setCurrentIndex(1)
            self.assertEqual(manual.prompt.toPlainText(), 'second prompt')
            self.assertEqual(Path(manual.picker.image_path).parent, second)
            manual.picker.target.setCurrentIndex(0)
            self.assertEqual(manual.prompt.toPlainText(), 'first edited draft')
            self.assertEqual(Path(manual.picker.image_path), self.image)
            image.fill(0xffaabbcc); image.save(str(self.image))
            manual.enqueue()
            self.assertIn('变化', manual.status.text())
            self.assertTrue(all(call.args[0]['action'] == 'draft_save' for call in plugin.command.emit.call_args_list))
        finally: dialog.close()

    def test_browser_lane_editor_is_not_reset_by_stale_state(self):
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        plugin = SimpleNamespace(ui_api_version=2, command=MagicMock(), context=SimpleNamespace(parent_widget=SimpleNamespace()))
        dialog = FlowDialog(plugin)
        try:
            state = {'jobs': [], 'clients': [{'id': 'browser', 'profile': 'Profile 8', 'credits': 40}],
                     'paused': True, 'preferences': {}, 'downloads_root': str(self.root)}
            dialog.update_state(copy.deepcopy(state))
            combo = dialog.lane_edits[('profile', 'Profile 8')]
            combo.setCurrentIndex(combo.findData(8))
            dialog.update_state(copy.deepcopy(state))
            self.assertEqual(combo.currentData(), 8)
            state['preferences']['profile_lanes'] = {'Profile 8': 8}
            state['preferences']['profile_lane_modes'] = {'Profile 8': 'fixed'}
            dialog.update_state(state)
            self.assertNotIn(('profile', 'Profile 8'), dialog.pending_lanes)
        finally: dialog.close()


if __name__ == '__main__': unittest.main()

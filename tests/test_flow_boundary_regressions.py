"""Offline regressions for request attempts, bound downloads and subtitle overlap."""
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from qt_compat import QtWidgets
from model.AppDataStorage import ApplicationStorage
from model.node_workflow.contracts import SubtitleCue, SubtitleTrack
from model.node_workflow.subtitle_nodes import shift
from app_plugins.builtin.flow_assistant.archive import archive_download
from app_plugins.builtin.flow_assistant.service import FlowService
from app_plugins.builtin.flow_assistant.store import QueueStore, make_jobs


class FlowBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.namespace = ApplicationStorage(self.root / 'private').for_plugin('flow_assistant')
        self.target = self.root / 'task'
        self.target.mkdir()
        image = self.target / 'image.png'
        image.write_bytes(b'offline reference image')
        self.jobs = make_jobs({'target_dir': str(self.target), 'label': 'test', 'task': {}},
                              [image], 'offline test only', budget=7)

    def service(self):
        self.namespace.file('pairing.json').write_json({'token': 'x' * 40, 'port': 0})
        service = FlowService(self.namespace, str(self.root / 'downloads'))
        service.start()
        self.addCleanup(lambda: service.execute({'action': 'stop'}))
        service.store.enqueue(self.jobs)
        socket = MagicMock()
        client = {'socket': socket, 'authorized': True, 'id': 'browser',
                  'account': 'offline@example.test', 'credits': 50, 'credits_at': time.time()}
        service.clients[socket] = client
        return service, client, service.store.rows()[0]['id']

    def test_stale_timeout_cannot_change_a_new_attempt_or_browser(self):
        service, client, job_id = self.service()
        for action in ('prepare', 'upload_images', 'run_tasks', 'retry_download'):
            for changed in ('attempt', 'browser'):
                with self.subTest(action=action, changed=changed):
                    service.store.update(job_id, 'preparing', attempt='a' * 32, browser='browser')
                    service.request(client, action, job=job_id)
                    request_id = next(iter(service.pending))
                    service.pending[request_id]['at'] = time.time() - 200
                    service.store.update(job_id, 'generating', **{changed: 'b' * 32})
                    before = copy.deepcopy(service.store.get(job_id))
                    service._tick()
                    self.assertEqual(service.store.get(job_id), before)
                    self.assertNotIn(request_id, service.pending)

    def test_disconnect_retry_cannot_be_failed_by_the_previous_request(self):
        service, client, job_id = self.service()
        service.store.update(job_id, 'preparing', attempt='a' * 32, browser=client['id'])
        service.request(client, 'prepare', job=job_id)
        for request in service.pending.values():
            request['at'] = time.time() - 200
        service._disconnected(client['socket'])
        self.assertEqual(service.store.get(job_id)['status'], 'failed')
        service.store.retry([job_id])
        service.store.update(job_id, 'preparing', attempt='b' * 32, browser=client['id'])
        service._tick()
        self.assertEqual(service.store.get(job_id)['status'], 'preparing')
        self.assertFalse(service.pending)

    def test_current_attempt_still_times_out_without_automatically_regenerating(self):
        service, client, job_id = self.service()
        for action, status in (('prepare', 'queued'), ('upload_images', 'uploading'),
                               ('run_tasks', 'uncertain'), ('retry_download', 'download_failed')):
            with self.subTest(action=action):
                service.store.update(job_id, 'preparing' if action == 'prepare' else 'submitting',
                                     attempt='a' * 32, browser=client['id'])
                service.request(client, action, job=job_id)
                for request in service.pending.values():
                    request['at'] = time.time() - 200
                sent_before = client['socket'].sendTextMessage.call_count
                service._tick()
                self.assertEqual(service.store.get(job_id)['status'], status)
                later = client['socket'].sendTextMessage.call_args_list[sent_before:]
                import json
                self.assertTrue(all(json.loads(call.args[0]).get('action') in {'snapshot', 'get_points'} for call in later))

    def archive_job(self, name='result.mp4'):
        store = QueueStore(self.namespace)
        store.enqueue(self.jobs)
        row = store.rows()[0]
        row['attempt'] = 'a' * 32
        downloads = self.root / 'downloads'
        source = downloads / 'FlowBridge' / row['attempt'] / name
        source.parent.mkdir(parents=True)
        source.write_bytes(b'ftyp' + b'x' * 100)
        row['remote']['download'] = {'filename': str(source), 'state': 'complete',
                                    'mediaId': 'bound-media', 'submissionId': 'bound-submission'}
        return row, downloads, source

    def test_chrome_uniquified_download_archives_and_is_idempotent(self):
        row, downloads, source = self.archive_job('result (1).mp4')
        output = Path(archive_download(row, downloads, verify=lambda _: None))
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertTrue(source.is_file())
        self.assertEqual(archive_download(row, downloads, verify=lambda _: None), str(output))
        output.write_bytes(b'existing result must be preserved')
        with self.assertRaises(FileExistsError):
            archive_download(row, downloads, verify=lambda _: None)

    def test_uniquified_download_still_rejects_other_names_batches_and_states(self):
        row, downloads, source = self.archive_job()
        for name in ('result (0).mp4', 'result (01).mp4', 'result-copy.mp4', 'other.mp4',
                     'result (1).mp4.exe'):
            with self.subTest(name=name):
                invalid = source.with_name(name)
                invalid.write_bytes(source.read_bytes())
                row['remote']['download']['filename'] = str(invalid)
                with self.assertRaises(ValueError):
                    archive_download(row, downloads, verify=lambda _: None)
        other = downloads / 'FlowBridge' / ('b' * 32) / 'result (1).mp4'
        other.parent.mkdir()
        other.write_bytes(source.read_bytes())
        row['remote']['download']['filename'] = str(other)
        with self.assertRaises(ValueError):
            archive_download(row, downloads, verify=lambda _: None)
        row['remote']['download']['filename'] = str(source)
        row['remote']['download']['state'] = 'in_progress'
        with self.assertRaises(ValueError):
            archive_download(row, downloads, verify=lambda _: None)

    def test_chrome_suffix_numbers_are_not_limited_to_the_first_duplicate(self):
        row, downloads, source = self.archive_job()
        for name in ('result (2).mp4', 'result (123).mp4', 'result (3).MP4'):
            with self.subTest(name=name):
                renamed = source.with_name(name)
                renamed.write_bytes(source.read_bytes())
                row['remote']['download']['filename'] = str(renamed)
                output = Path(archive_download(row, downloads, verify=lambda _: None))
                self.assertEqual(output.read_bytes(), source.read_bytes())


class SubtitleOverlapTests(unittest.TestCase):
    def test_shift_uses_latest_end_not_the_end_of_the_last_started_cue(self):
        source = SubtitleTrack((SubtitleCue(0, 10, 'long'), SubtitleCue(1, 2, 'short')),
                               'task', 10, 'word')
        moved = shift({'subtitles': source, 'offset': 5}, {}, None)['subtitles']
        self.assertEqual(moved.duration, 15)
        self.assertEqual(moved.cues, (SubtitleCue(5, 15, 'long'), SubtitleCue(6, 7, 'short')))
        self.assertEqual((moved.task_key, moved.timing), ('task', 'word'))
        moved.verify()
        self.assertEqual(source.duration, 10)

    def test_negative_shift_clips_overlaps_and_keeps_existing_duration(self):
        source = SubtitleTrack((SubtitleCue(0, 10, 'long'), SubtitleCue(1, 2, 'short')), '', 10)
        moved = shift({'subtitles': source, 'offset': -1.5}, {}, None)['subtitles']
        self.assertEqual(moved.duration, 10)
        self.assertEqual(moved.cues, (SubtitleCue(0, 8.5, 'long'), SubtitleCue(0, .5, 'short')))
        with self.assertRaisesRegex(ValueError, '没有可见内容'):
            shift({'subtitles': source, 'offset': -10}, {}, None)


if __name__ == '__main__':
    unittest.main()

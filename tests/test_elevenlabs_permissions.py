"""Synthetic keys, mocked HTTP/SDK only; no paid requests or user audio."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from model.ApiKeyHelper import api_key_id, select_api_keys_for_attempt, record_api_key_status
from model.ElevenLabsPermissions import (TTS, ALIGNMENT, USER, inspect_key, permission_value,
    describe_permissions)
from model.ElevenLabsService import ElevenLabsService
from model.ConfigTransactions import merge_latest_api_statuses


KEYS = ['synthetic-key-alpha-not-real', 'synthetic-key-beta-not-real', 'synthetic-key-gamma-not-real']
WORDS = [dict(text='Hello', start=.1, end=.4), dict(text='world.', start=.5, end=.8)]


class ApiError(Exception):
    def __init__(self, status, code):
        self.status_code = status
        self.body = {'detail': {'status': code}}


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'elevenlabs_api_keys': KEYS, 'untouched': {'business': True}}), encoding='utf-8')

    def statuses(self):
        return json.loads(self.config.read_text('utf-8')).get('elevenlabs_api_key_statuses', {})

    def test_one_pool_routes_each_feature_independently_and_prefers_confirmed_permissions(self):
        states = {api_key_id(KEYS[0]): {'permission_text_to_speech': False, 'permission_forced_alignment': True},
                  api_key_id(KEYS[1]): {'permission_text_to_speech': True, 'permission_forced_alignment': False}}
        self.assertEqual(select_api_keys_for_attempt(KEYS, states, capability=TTS)[0], [KEYS[1], KEYS[2]])
        self.assertEqual(select_api_keys_for_attempt(KEYS, states, capability=ALIGNMENT)[0], [KEYS[0], KEYS[2]])
        self.assertEqual(select_api_keys_for_attempt(KEYS, states)[0], KEYS)

    def test_known_permission_denials_have_a_distinct_reason_not_invalid_keys(self):
        states = {api_key_id(key): {'permission_text_to_speech': False} for key in KEYS}
        keys, _next, result = select_api_keys_for_attempt(KEYS, states, capability=TTS)
        self.assertEqual(keys, [])
        self.assertEqual(result['mode'], 'no_permission')
        self.assertEqual(result['invalid_count'], 0)

    def test_legacy_success_and_alignment_markers_are_migrated_without_new_lists(self):
        self.assertTrue(permission_value({'last_success_unix': 123}, TTS))
        self.assertFalse(permission_value({'forced_alignment_status': 'permission_denied'}, ALIGNMENT))
        self.assertTrue(permission_value({'forced_alignment_status': 'available'}, ALIGNMENT))
        self.assertFalse(permission_value({'permission_text_to_speech': False, 'last_success_unix': 123}, TTS))

    def test_http_401_missing_permissions_is_not_an_invalid_key(self):
        from model.AudioHelper import _classify_api_error
        self.assertEqual(_classify_api_error(ApiError(401, 'missing_permissions'))[0], 'permission')
        self.assertEqual(_classify_api_error(ApiError(401, 'invalid_api_key'))[0], 'invalid')
        self.assertEqual(_classify_api_error(ApiError(403, 'voice_access_denied'))[0], 'request')

    def test_read_only_probe_does_not_invent_tts_or_alignment_permissions(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'user_id': 'synthetic-user',
            'subscription': {'character_count': 10, 'character_limit': 100}}).encode()
        with patch('model.ElevenLabsPermissions.urllib.request.urlopen', return_value=response) as http:
            result = inspect_key(KEYS[0])
        request = http.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'https://api.elevenlabs.io/v1/user')
        self.assertEqual(http.call_count, 1)
        self.assertTrue(permission_value(result, USER))
        self.assertIsNone(permission_value(result, TTS))
        self.assertIsNone(permission_value(result, ALIGNMENT))
        self.assertIn('待确认', describe_permissions(result))
        self.assertEqual(result['permission_quota_limit'], 100)
        self.assertNotIn(KEYS[0], json.dumps(result))

    def test_missing_user_read_does_not_disable_generation_or_mark_key_invalid(self):
        error = urllib.error.HTTPError('https://api.elevenlabs.io/v1/user', 401, 'denied', {},
            io.BytesIO(b'{"detail":{"status":"missing_permissions"}}'))
        with patch('model.ElevenLabsPermissions.urllib.request.urlopen', side_effect=error):
            result = inspect_key(KEYS[0])
        self.assertFalse(permission_value(result, USER))
        self.assertTrue(result['permission_authentication'])
        self.assertIsNone(permission_value(result, TTS))
        self.assertEqual(select_api_keys_for_attempt([KEYS[0]], {api_key_id(KEYS[0]): result}, capability=TTS)[0], [KEYS[0]])

    def test_invalid_probe_is_separate_from_network_uncertainty_and_offline_never_requests(self):
        error = urllib.error.HTTPError('https://api.elevenlabs.io/v1/user', 401, 'invalid', {},
            io.BytesIO(b'{"detail":{"status":"invalid_api_key"}}'))
        with patch('model.ElevenLabsPermissions.urllib.request.urlopen', side_effect=error):
            result = inspect_key(KEYS[0])
        self.assertFalse(result['permission_authentication'])
        with patch('model.ElevenLabsPermissions.urllib.request.urlopen', side_effect=TimeoutError(KEYS[0])):
            result = inspect_key(KEYS[0])
        self.assertNotIn('permission_authentication', result)
        self.assertNotIn(KEYS[0], json.dumps(result))
        from model.OfflineMode import OfflineModeError
        with patch('model.OfflineMode.require_online', side_effect=OfflineModeError('offline')), \
                patch('model.ElevenLabsPermissions.urllib.request.urlopen') as http:
            self.assertEqual(inspect_key(KEYS[0])['permission_probe_status'], 'offline')
        http.assert_not_called()

    def test_automatic_alignment_skips_explicit_rejection_and_learns_success_without_touching_srt(self):
        service = ElevenLabsService()
        binding = service.bind(json.loads(self.config.read_text('utf-8')), self.config)
        audio = self.root/'task_audio.mp3'; audio.write_bytes(b'synthetic audio')
        original = audio.with_suffix('.srt'); original.write_bytes(b'DaVinci original')
        denied, allowed = MagicMock(), MagicMock()
        denied.forced_alignment.create.side_effect = ApiError(401, 'missing_permissions')
        allowed.forced_alignment.create.return_value = {'words': WORDS}
        with patch('elevenlabs.client.ElevenLabs', side_effect=[denied, allowed]) as factory:
            result = service.forced_align(binding, audio, 'Hello world.', 1.)
        self.assertEqual(result, WORDS)
        self.assertEqual([call.kwargs['api_key'] for call in factory.call_args_list], KEYS[:2])
        self.assertFalse(permission_value(self.statuses()[api_key_id(KEYS[0])], ALIGNMENT))
        self.assertTrue(permission_value(self.statuses()[api_key_id(KEYS[1])], ALIGNMENT))
        self.assertIn(KEYS[0], service.tts_credentials(binding)[0])
        self.assertEqual(original.read_bytes(), b'DaVinci original')
        self.assertTrue(audio.with_name('task_audio[动态].srt').is_file())

    def test_alignment_does_not_retry_an_ambiguous_error_on_another_key(self):
        service = ElevenLabsService()
        binding = service.bind(json.loads(self.config.read_text('utf-8')), self.config)
        audio = self.root/'task_audio.mp3'; audio.write_bytes(b'synthetic audio')
        client = MagicMock(); client.forced_alignment.create.side_effect = TimeoutError('unknown result')
        with patch('elevenlabs.client.ElevenLabs', return_value=client) as factory:
            with self.assertRaisesRegex(ValueError, '结果不明确'):
                service.forced_align(binding, audio, 'Hello world.', 1.)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNone(permission_value(self.statuses()[api_key_id(KEYS[0])], ALIGNMENT))

    def test_alignment_known_denial_is_not_uploaded_again_and_tts_denial_does_not_block_alignment(self):
        record_api_key_status(self.config, KEYS[0], permission_forced_alignment=False)
        record_api_key_status(self.config, KEYS[1], permission_text_to_speech=False, permission_forced_alignment=True)
        service = ElevenLabsService()
        binding = service.bind(json.loads(self.config.read_text('utf-8')), self.config)
        audio = self.root/'task_audio.mp3'; audio.write_bytes(b'synthetic audio')
        client = MagicMock(); client.forced_alignment.create.return_value = {'words': WORDS}
        with patch('elevenlabs.client.ElevenLabs', return_value=client) as factory:
            self.assertEqual(service.forced_align(binding, audio, 'Hello world.', 1.), WORDS)
        self.assertEqual(factory.call_args.kwargs['api_key'], KEYS[1])

    def test_tts_permission_failure_rotates_only_after_explicit_rejection_and_persists_per_feature(self):
        from model.AudioHelper import _save_audio_with_api_keys
        target = self.root/'audio.mp3'
        def generate(client):
            if client.key == KEYS[0]:
                raise ApiError(401, 'missing_permissions')
            return [b'complete audio']
        with patch('model.AudioHelper.ElevenLabs', side_effect=lambda api_key: SimpleNamespace(key=api_key)) as factory:
            self.assertTrue(_save_audio_with_api_keys(KEYS, str(target), generate,
                api_key_status_config=self.config, progress_callback=lambda _: None))
        self.assertEqual(factory.call_count, 2)
        self.assertFalse(permission_value(self.statuses()[api_key_id(KEYS[0])], TTS))
        self.assertNotEqual(self.statuses()[api_key_id(KEYS[0])].get('status'), 'invalid')
        self.assertTrue(permission_value(self.statuses()[api_key_id(KEYS[1])], TTS))
        self.assertEqual(select_api_keys_for_attempt(KEYS, self.statuses(), capability=TTS)[0], [KEYS[1], KEYS[2]])
        self.assertTrue(json.loads(self.config.read_text('utf-8'))['untouched']['business'])

    def test_partial_audio_or_completed_long_chunks_never_restart_with_another_key(self):
        from model.AudioHelper import _save_audio_with_api_keys
        from model.ElevenLabsTimings import TimedAudio
        for completed in (False, True):
            with self.subTest(completed_long_chunk=completed):
                target = self.root/'old.mp3'; target.write_bytes(b'old output')
                def chunks():
                    if not completed:
                        yield b'partial audio'
                    raise ApiError(401, 'missing_permissions')
                audio = TimedAudio(chunks()); audio.completed_requests = int(completed)
                with patch('model.AudioHelper.ElevenLabs') as factory:
                    self.assertFalse(_save_audio_with_api_keys(KEYS, str(target), lambda _: audio,
                        api_key_status_config=None, progress_callback=lambda _: None))
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(target.read_bytes(), b'old output')

    def test_form_permission_deltas_preserve_new_worker_quota_and_newer_authentication(self):
        key = api_key_id(KEYS[0])
        record_api_key_status(self.config, KEYS[0], status='exhausted', character_count=100,
            last_success_unix=200, permission_authentication=True, permission_authentication_at=200)
        local = {'elevenlabs_api_key_statuses': {key: {'status': 'available'}}}
        merged = merge_latest_api_statuses(self.config, local, permission_updates={key: {
            'permission_user_read': True, 'permission_authentication': False, 'permission_authentication_at': 100}})
        self.assertEqual(merged['elevenlabs_api_key_statuses'][key]['status'], 'exhausted')
        self.assertEqual(merged['elevenlabs_api_key_statuses'][key]['character_count'], 100)
        self.assertTrue(merged['elevenlabs_api_key_statuses'][key]['permission_authentication'])

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'requires explicit test FFmpeg')
    def test_real_long_audio_join_never_restarts_paid_first_chunk_after_later_scope_denial(self):
        import base64
        from model.AudioHelper import CreateAudioLatest
        ffmpeg = os.environ['NODE_FFMPEG']
        fixture = self.root/'fixture.mp3'
        subprocess.run([ffmpeg, '-y', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(fixture)],
            check=True, capture_output=True, timeout=20)
        response = {'audio_base64': base64.b64encode(fixture.read_bytes()).decode(), 'alignment': None}
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.side_effect = [response, ApiError(401, 'missing_permissions')]
        target = self.root/'long.mp3'; target.write_bytes(b'old complete audio')
        with patch('model.AudioHelper.ElevenLabs', return_value=client) as factory, \
                patch('model.AudioHelper._speech_ffmpeg', return_value=ffmpeg):
            self.assertFalse(CreateAudioLatest('word '*500, str(target), 'voice', api_keys=KEYS,
                api_key_status_config=None, progress_callback=lambda _: None))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(client.text_to_dialogue.convert_with_timestamps.call_count, 2)
        self.assertEqual(target.read_bytes(), b'old complete audio')
        self.assertFalse(list(self.root.glob('.ele-audio-*')))


class KeySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        from PYUI.main_setting_pyui import MainSettingDialog
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'config.json'
        self.path.write_text(json.dumps({'elevenlabs_api_keys': [], 'untouched': 42, 'run_upload': False}), encoding='utf-8')
        self.location = patch('PYUI.main_setting_pyui.CONFIG_PATH', self.path)
        self.location.start(); self.addCleanup(self.location.stop)
        self.dialog = MainSettingDialog()
        self.addCleanup(self.cleanup_dialog)
        def unexpected_dialog(_parent, title, message, *_args, **_kwargs):
            raise AssertionError(title + ': ' + message)
        for name in ('warning', 'critical'):
            guard = patch('PYUI.main_setting_pyui.QMessageBox.' + name, side_effect=unexpected_dialog)
            guard.start(); self.addCleanup(guard.stop)

    def cleanup_dialog(self):
        from qt_compat import QtCore
        if self.dialog is not None:
            self.dialog.reject()
            self.dialog.deleteLater()
        QtCore.QThreadPool.globalInstance().waitForDone(3000)
        self.app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def pump(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def test_add_starts_detection_off_ui_thread_and_has_no_separate_alignment_selector(self):
        release, started = threading.Event(), threading.Event()
        owner = threading.get_ident()
        def inspect(_key):
            self.assertNotEqual(threading.get_ident(), owner)
            started.set()
            if not release.wait(3):
                raise TimeoutError('test probe not released')
            return {'permission_user_read': True, 'permission_probe_status': 'partial'}
        with patch('model.ElevenLabsPermissions.inspect_key', side_effect=inspect):
            self.dialog.elevenlabs_api_key_edit.setText(KEYS[0])
            self.dialog.add_api_key()
            self.assertTrue(started.wait(2))
            self.assertTrue(self.dialog.elevenlabs_key_probe.busy)
            self.assertEqual(self.dialog.get_api_keys(), [KEYS[0]])
            self.assertFalse(hasattr(self.dialog, 'elevenlabs_alignment_key'))
            release.set()
            self.pump(lambda: not self.dialog.elevenlabs_key_probe.busy)
        item = self.dialog.elevenlabs_api_key_list.item(0)
        self.assertIn('待确认', item.text())
        self.assertNotIn(KEYS[0], item.text() + item.toolTip())
        self.assertTrue(self.dialog.save_config())
        saved = json.loads(self.path.read_text('utf-8'))
        self.assertTrue(saved['elevenlabs_api_key_statuses'][api_key_id(KEYS[0])]['permission_user_read'])
        self.assertEqual(saved['untouched'], 42)
        if os.environ.get('LZX_ELE_KEYS_SCREENSHOT'):
            self.dialog.settingTabWidget.setCurrentWidget(self.dialog.tab_4)
            self.dialog.show()
            self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(os.environ['LZX_ELE_KEYS_SCREENSHOT']))

    def test_ok_automatically_waits_without_freezing_and_saves_after_probe(self):
        release, started = threading.Event(), threading.Event()
        def inspect(_key):
            started.set(); release.wait(3)
            return {'permission_user_read': True}
        with patch('model.ElevenLabsPermissions.inspect_key', side_effect=inspect):
            self.dialog.elevenlabs_api_key_edit.setText(KEYS[0])
            self.dialog.accept()
            self.assertTrue(started.wait(2))
            self.assertTrue(self.dialog._accept_after_probe)
            self.assertEqual(json.loads(self.path.read_text('utf-8'))['elevenlabs_api_keys'], [])
            release.set()
            self.pump(lambda: json.loads(self.path.read_text('utf-8'))['elevenlabs_api_keys'] == [KEYS[0]])
        self.assertTrue(json.loads(self.path.read_text('utf-8'))['elevenlabs_api_key_statuses'][api_key_id(KEYS[0])]['permission_user_read'])

    def test_deleted_key_does_not_return_when_its_probe_finishes(self):
        release, started = threading.Event(), threading.Event()
        def inspect(_key):
            started.set(); release.wait(3)
            return {'permission_user_read': True}
        with patch('model.ElevenLabsPermissions.inspect_key', side_effect=inspect):
            self.dialog.elevenlabs_api_key_edit.setText(KEYS[0]); self.dialog.add_api_key()
            self.assertTrue(started.wait(2))
            self.dialog.elevenlabs_api_key_list.item(0).setSelected(True)
            self.dialog.remove_selected_api_keys()
            release.set(); self.pump(lambda: not self.dialog.elevenlabs_key_probe.busy)
        self.assertEqual(self.dialog.get_api_keys(), [])
        self.assertNotIn(api_key_id(KEYS[0]), self.dialog.api_key_statuses)

    def test_reset_ignores_an_older_probe_result(self):
        release, started = threading.Event(), threading.Event()
        def inspect(_key):
            started.set(); release.wait(3)
            return {'permission_authentication': False}
        with patch('model.ElevenLabsPermissions.inspect_key', side_effect=inspect):
            self.dialog.elevenlabs_api_key_edit.setText(KEYS[0]); self.dialog.add_api_key()
            self.assertTrue(started.wait(2))
            self.dialog.elevenlabs_api_key_list.item(0).setSelected(True)
            self.dialog.reset_selected_api_key_statuses()
            release.set(); self.pump(lambda: not self.dialog.elevenlabs_key_probe.busy)
        self.assertTrue(self.dialog.save_config())
        self.assertNotIn(api_key_id(KEYS[0]), json.loads(self.path.read_text('utf-8'))['elevenlabs_api_key_statuses'])

    def test_cancel_during_probe_does_not_save_keys_or_destroy_a_running_qthread(self):
        from qt_compat import QtCore
        release, started = threading.Event(), threading.Event()
        def inspect(_key):
            started.set(); release.wait(3)
            return {'permission_user_read': True}
        with patch('model.ElevenLabsPermissions.inspect_key', side_effect=inspect):
            self.dialog.elevenlabs_api_key_edit.setText(KEYS[0]); self.dialog.add_api_key()
            self.assertTrue(started.wait(2))
            self.dialog.reject(); self.dialog.deleteLater(); self.dialog = None
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            release.set()
            self.assertTrue(QtCore.QThreadPool.globalInstance().waitForDone(3000))
            self.app.processEvents()
        self.assertEqual(json.loads(self.path.read_text('utf-8'))['elevenlabs_api_keys'], [])


if __name__ == '__main__':
    unittest.main()

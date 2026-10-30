"""Synthetic credentials and mocked providers only. Never spend real credits."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model.ApiKeyHelper import api_key_id, record_api_key_status
from model.ElevenLabsKeyPolicy import (PREFERENCES, AttemptPlan, KeyCandidates,
    normalize_preferences, paid_access_error, select_tts_candidates)
from model.ElevenLabsPermissions import TTS, ALIGNMENT
from model.ElevenLabsService import ElevenLabsService

NORMAL = 'synthetic-normal-key-long-0001'
OTHER = 'synthetic-normal-key-long-0002'
PAID = 'synthetic-paid-key-long-0003'
PREFS = {api_key_id(PAID): {'paid': True, 'label': '付费保留账号'}}
KEYS = [PAID, NORMAL, OTHER]


class APIError(Exception):
    def __init__(self, code, status, message='synthetic rejected request'):
        super().__init__('Synthetic provider failure')
        self.status_code = code
        self.body = {'detail': {'code': status, 'message': message}}


class PaidKeyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'elevenlabs_api_keys': KEYS, PREFERENCES: PREFS}), 'utf-8')
        self.output = self.root/'audio.mp3'

    def state(self):
        return json.loads(self.config.read_text('utf-8'))

    def generate(self, behavior, *, keys=KEYS):
        from model.AudioHelper import _save_audio_with_api_keys
        calls = []
        def convert(client):
            calls.append(client.key)
            result = behavior(client.key)
            if isinstance(result, BaseException):
                raise result
            return result
        with patch('model.AudioHelper.ElevenLabs', side_effect=lambda api_key: SimpleNamespace(key=api_key)), \
             patch('model.AudioHelper.get_subscription_snapshot', return_value={'reset_unix': int(time.time())+3600}):
            result = _save_audio_with_api_keys(keys, str(self.output), convert,
                api_key_status_config=self.config, progress_callback=lambda _: None)
        return result, calls

    def test_paid_first_and_confirmed_never_beats_an_unknown_normal_key(self):
        record_api_key_status(self.config, PAID, permission_text_to_speech=True)
        result, calls = self.generate(lambda _: [b'complete'])
        self.assertTrue(result)
        self.assertEqual(calls, [NORMAL])

    def test_scope_denials_try_all_normal_before_paid(self):
        result, calls = self.generate(lambda key: [b'complete'] if key == PAID else APIError(401, 'missing_permissions'))
        self.assertTrue(result)
        self.assertEqual(calls, [NORMAL, OTHER, PAID])
        self.assertFalse(self.state()['elevenlabs_api_key_statuses'][api_key_id(NORMAL)]['permission_text_to_speech'])
        self.assertEqual(self.state()[PREFERENCES], PREFS)

    def test_one_denied_normal_does_not_skip_a_working_normal(self):
        result, calls = self.generate(lambda key: APIError(401, 'missing_permissions') if key == NORMAL else [b'complete'])
        self.assertTrue(result)
        self.assertEqual(calls, [NORMAL, OTHER])

    def test_plan_voice_model_errors_allow_paid_without_disabling_all_tts(self):
        for code in ('subscription_required', 'paid_plan_required', 'voice_access_denied', 'model_access_denied'):
            with self.subTest(code=code):
                result, calls = self.generate(lambda key: [b'complete'] if key == PAID else APIError(403, code))
                self.assertTrue(result)
                self.assertEqual(calls, [NORMAL, OTHER, PAID])
                self.assertNotIn('permission_text_to_speech', self.state().get('elevenlabs_api_key_statuses', {}).get(api_key_id(NORMAL), {}))

    def test_legacy_paid_voice_402_is_not_mistaken_for_exhausted_quota(self):
        result, calls = self.generate(lambda key: [b'complete'] if key == PAID else APIError(
            402, 'payment_required', 'Free users cannot use library voices via the API.'))
        self.assertTrue(result)
        self.assertEqual(calls, [NORMAL, OTHER, PAID])
        self.assertNotEqual(self.state().get('elevenlabs_api_key_statuses', {}).get(api_key_id(NORMAL), {}).get('status'), 'exhausted')

    def test_quota_rate_invalid_timeout_and_server_failure_do_not_use_paid(self):
        errors = [APIError(402, 'insufficient_credits'), APIError(429, 'rate_limit_exceeded'),
                  APIError(401, 'invalid_api_key'), TimeoutError('unknown outcome'), APIError(500, 'internal_error')]
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, 'status_code', None)):
                self.config.write_text(json.dumps({'elevenlabs_api_keys': KEYS, PREFERENCES: PREFS}), 'utf-8')
                result, calls = self.generate(lambda _: error)
                self.assertFalse(result)
                self.assertNotIn(PAID, calls)

    def test_generic_forbidden_or_payment_errors_do_not_authorize_paid(self):
        for error in (APIError(403, 'forbidden'), APIError(402, 'payment_required')):
            result, calls = self.generate(lambda _: error)
            self.assertFalse(result)
            self.assertNotIn(PAID, calls)

    def test_unconfirmed_or_server_permission_text_cannot_enable_paid_later(self):
        for error in (APIError(None, 'missing_permissions'), APIError(500, 'missing_permissions'),
                      APIError(None, 'subscription_required'), APIError(500, 'paid_plan_required')):
            result, calls = self.generate(lambda _: error)
            self.assertFalse(result)
            self.assertEqual(calls, [NORMAL])
            self.assertNotIn('permission_text_to_speech', self.state().get('elevenlabs_api_key_statuses', {}).get(api_key_id(NORMAL), {}))

    def test_known_endpoint_denials_can_go_direct_to_paid(self):
        for key in (NORMAL, OTHER):
            record_api_key_status(self.config, key, permission_text_to_speech=False)
        service = ElevenLabsService()
        token = service.bind(self.state(), self.config)
        candidates, path = service.tts_credentials(token)
        self.assertEqual(Path(path), self.config)
        result, calls = self.generate(lambda _: [b'complete'], keys=candidates)
        self.assertTrue(result)
        self.assertEqual(calls, [PAID])

    def test_all_paid_pool_does_not_silently_spend(self):
        result, calls = self.generate(lambda _: [b'complete'], keys=[PAID])
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertEqual(select_tts_candidates([PAID], {}, preferences=PREFS)[2]['mode'], 'paid_reserved')

    def test_stale_permission_denial_cannot_override_quota_or_invalid_protection(self):
        for status in ('invalid', 'exhausted', 'cooldown'):
            for key in (NORMAL, OTHER):
                record_api_key_status(self.config, key, status=status,
                    reset_unix=int(time.time())+3600, retry_after_unix=int(time.time())+3600,
                    permission_text_to_speech=False)
            result, calls = self.generate(lambda _: [b'complete'])
            self.assertFalse(result)
            self.assertEqual(calls, [])

    def test_partial_audio_keeps_original_and_never_tries_paid(self):
        self.output.write_bytes(b'old complete')
        def partial():
            yield b'partial'
            raise APIError(401, 'missing_permissions')
        result, calls = self.generate(lambda _: partial())
        self.assertFalse(result)
        self.assertEqual(calls, [NORMAL])
        self.assertEqual(self.output.read_bytes(), b'old complete')

    def test_empty_path_session_carries_billing_policy_without_second_key_list(self):
        candidates, _, _ = select_tts_candidates(KEYS, {}, preferences=PREFS)
        self.assertIsInstance(candidates, KeyCandidates)
        from model.AudioHelper import _credentials
        self.assertIs(_credentials(candidates, None, None, None)[0], candidates)
        plan = AttemptPlan(candidates, candidates.statuses, candidates.preferences, TTS)
        self.assertEqual(list(plan.attempts()), [NORMAL, OTHER])

    def test_paid_marker_saved_while_waiting_is_respected_before_request(self):
        def behavior(key):
            state = self.state()
            state[PREFERENCES][api_key_id(OTHER)] = {'paid': True}
            self.config.write_text(json.dumps(state), 'utf-8')
            return APIError(402, 'insufficient_credits')
        result, calls = self.generate(behavior)
        self.assertFalse(result)
        self.assertEqual(calls, [NORMAL])

    def test_new_paid_marker_during_permission_retry_does_not_jump_a_normal_key(self):
        last_normal = 'synthetic-normal-key-long-0004'
        def behavior(key):
            if key == NORMAL:
                state = self.state()
                state[PREFERENCES][api_key_id(OTHER)] = {'paid': True}
                self.config.write_text(json.dumps(state), 'utf-8')
                return APIError(401, 'missing_permissions')
            return [b'complete']
        result, calls = self.generate(behavior, keys=KEYS+[last_normal])
        self.assertTrue(result)
        self.assertEqual(calls, [NORMAL, last_normal])

    def test_alignment_has_independent_paid_permission_fallback(self):
        audio = self.root/'task_audio.mp3'
        audio.write_bytes(b'synthetic')
        original = audio.with_suffix('.srt')
        original.write_bytes(b'original DaVinci subtitle')
        service = ElevenLabsService()
        token = service.bind(self.state(), self.config)
        calls = []
        def factory(api_key):
            calls.append(api_key)
            client = Mock()
            if api_key == PAID:
                client.forced_alignment.create.return_value = {'words': [{'text': 'Hello', 'start': 0., 'end': .8}]}
            else:
                client.forced_alignment.create.side_effect = APIError(401, 'missing_permissions')
            return client
        with patch('elevenlabs.client.ElevenLabs', side_effect=factory):
            self.assertEqual(service.forced_align(token, audio, 'Hello', 1.)[0]['text'], 'Hello')
        self.assertEqual(calls, [NORMAL, OTHER, PAID])
        self.assertEqual(original.read_bytes(), b'original DaVinci subtitle')
        self.assertNotIn('permission_text_to_speech', self.state()['elevenlabs_api_key_statuses'][api_key_id(NORMAL)])

    def test_preferences_are_strict_pruned_and_not_inferred_from_quota(self):
        value = {api_key_id(PAID): {'paid': 'false', 'label': 'a'*200},
                 api_key_id(NORMAL): {'paid': True}, 'invalid-id': {'paid': True}}
        result = normalize_preferences(value, [PAID])
        self.assertFalse(result[api_key_id(PAID)]['paid'])
        self.assertEqual(len(result[api_key_id(PAID)]['label']), 80)
        self.assertEqual(len(result), 1)


class PaidKeySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets, QtGui
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if os.environ.get('LZX_ELE_PAID_SCREENSHOT'):
            QtGui.QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc')
            cls.app.setFont(QtGui.QFont('Microsoft YaHei', 9))

    def test_mark_notes_probe_reset_save_and_reload_preserve_role(self):
        from PYUI.main_setting_pyui import MainSettingDialog
        from qt_compat import QtCore
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'config.json'
            path.write_text(json.dumps({'elevenlabs_api_keys': [NORMAL, PAID], 'run_upload': False}), 'utf-8')
            with patch('PYUI.main_setting_pyui.CONFIG_PATH', path), \
                 patch('PYUI.main_setting_pyui.QMessageBox.warning', side_effect=AssertionError('Unexpected warning')), \
                 patch('PYUI.main_setting_pyui.QMessageBox.critical', side_effect=AssertionError('Unexpected error')):
                dialog = MainSettingDialog()
                try:
                    item = dialog.elevenlabs_api_key_list.item(1)
                    row = dialog.elevenlabs_api_key_list.itemWidget(item)
                    row.paid.setChecked(True)
                    row.label.textEdited.emit('我的付费备用')
                    dialog._api_probe_result(api_key_id(PAID), {'permission_user_read': True})
                    item.setSelected(True)
                    dialog.reset_selected_api_key_statuses()
                    self.assertTrue(row.paid.isChecked())
                    self.assertIn('我的付费备用', item.text())
                    self.assertNotIn(PAID, item.text()+item.toolTip()+row.state.text())
                    self.assertTrue(dialog.save_config())
                    saved = json.loads(path.read_text('utf-8'))
                    self.assertEqual(saved[PREFERENCES][api_key_id(PAID)], {'paid': True, 'label': '我的付费备用'})
                    record_api_key_status(path, PAID, status='exhausted')
                    self.assertEqual(json.loads(path.read_text('utf-8'))[PREFERENCES], saved[PREFERENCES])
                    dialog2 = MainSettingDialog()
                    try:
                        restored = dialog2.elevenlabs_api_key_list.itemWidget(dialog2.elevenlabs_api_key_list.item(1))
                        self.assertTrue(restored.paid.isChecked())
                        self.assertEqual(restored.label.text(), '我的付费备用')
                    finally:
                        dialog2.reject(); dialog2.deleteLater()
                    dialog.settingTabWidget.setCurrentWidget(dialog.tab_4)
                    dialog.show(); self.app.processEvents()
                    if os.environ.get('LZX_ELE_PAID_SCREENSHOT'):
                        self.assertTrue(dialog.grab().save(os.environ['LZX_ELE_PAID_SCREENSHOT']))
                finally:
                    dialog.reject(); dialog.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()

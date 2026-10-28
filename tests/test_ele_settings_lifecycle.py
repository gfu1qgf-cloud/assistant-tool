import os
import sys
import tempfile
import time
import threading
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
root = (Path(sys.argv[1]) if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir()
        else Path(__file__).parents[1] if Path(__file__).parent.name == 'tests'
        else Path(__file__).parent / 'project')
sys.path.insert(0, str(root))
from qt_compat import QtCore, QtWidgets
from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController


class Monitor(QtCore.QThread):
    single_pass = False
    def __init__(self, parent):
        super().__init__(parent)
        self.release = threading.Event()
        self.entered = threading.Event()
    def stop(self):
        self.release.set()
    def run(self):
        self.entered.set()
        self.release.wait(4)


class SettingsLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_old_monitor_finished_cannot_delete_new_generation(self):
        window = QtWidgets.QWidget()
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=None),
                                             SimpleNamespace(parent_widget=window, log=Mock()))
        for _ in range(25):
            old = Monitor(window)
            controller.review_status_thread = old
            old.finished.connect(lambda current=old: controller.on_review_status_monitor_finished(current))
            old.start()
            self.assertTrue(old.entered.wait(1))
            self.assertTrue(controller.stop_review_status_monitor())
            new = Monitor(window)
            controller.review_status_thread = new
            new.start()
            self.assertTrue(new.entered.wait(1))
            for _ in range(4):
                self.app.processEvents()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.assertIs(controller.review_status_thread, new)
            self.assertTrue(new.isRunning())
            new.stop()
            self.assertTrue(new.wait(1000))
            controller.review_status_thread = None
            new.deleteLater()
        window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_only_key_changes_skip_unrelated_service_apply(self):
        before = {'elevenlabs_api_keys': ['synthetic-old'], 'kept': 'unchanged'}
        after = {**before, 'elevenlabs_api_keys': ['synthetic-old', 'synthetic-new']}
        host = Mock()
        window = SimpleNamespace(load_config=Mock(side_effect=[before, after]), plugin_host=host,
            gemini_keys=Mock(), appendLog=Mock(), config_reload=None)
        with patch('PYUI.main_pyui.MainSettingDialog.get_settings', return_value={'elevenlabs_api_keys': after['elevenlabs_api_keys']}), \
             patch('PYUI.main_pyui.globalValue.loaded_whisper_model_name', return_value='large-v3'):
            MainDialog.openSettings(window)
        host.apply_settings.assert_not_called()
        self.assertIn('ElevenLabs', window.appendLog.call_args.args[0])

    def test_real_add_button_then_save_keeps_existing_keys_and_profiles(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, \
             patch('model.ElevenLabsPermissions.inspect_key',return_value={'permission_probe_status':'partial'}):
            os.chdir(directory)
            original = {'elevenlabs_api_keys': ['synthetic-existing'], 'run_upload': False,
                'audio_settings': {'test': {'model': 'elevenlabs', 'voice_ids': ['synthetic-voice']}},
                'private_business_data': {'keep': ['unchanged']}}
            Path('config.json').write_text(json.dumps(original), encoding='utf-8')
            dialog = MainSettingDialog()
            try:
                dialog.elevenlabs_api_key_edit.setText('synthetic-added')
                dialog.add_api_key_btn.click()
                self.assertEqual(dialog.get_api_keys(), ['synthetic-existing', 'synthetic-added'])
                dialog.ok_btn.click()
                deadline=time.monotonic()+2
                while dialog.result()!=QtWidgets.QDialog.DialogCode.Accepted and time.monotonic()<deadline:
                    self.app.processEvents();time.sleep(.005)
                self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
                saved = json.loads(Path('config.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['elevenlabs_api_keys'], ['synthetic-existing', 'synthetic-added'])
                self.assertEqual(saved['private_business_data'], original['private_business_data'])
                self.assertIn('test', saved['audio_settings'])
            finally:
                dialog.elevenlabs_key_probe.stop()
                dialog.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                os.chdir(previous)

    def test_late_probe_after_qt_signal_destruction_drops_result_and_releases_owned_worker(self):
        from PyQt6 import sip
        from PYUI.elevenlabs_key_probe import _Probe,_ACTIVE
        worker=_Probe([('synthetic-key',0)])
        _ACTIVE[worker.identifier]=worker
        sip.delete(worker.signals)
        with patch('model.ElevenLabsPermissions.inspect_key',return_value={'permission_probe_status':'partial'}):
            worker.run()
        self.assertNotIn(worker.identifier,_ACTIVE)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)

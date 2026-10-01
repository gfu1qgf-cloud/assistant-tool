import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from model.GeminiKeyManager import GeminiKeyManager, KEYS_CONFIG_KEY, STATUSES_CONFIG_KEY, key_id
from PYUI.gemini_keys_pyui import GeminiKeysEditor
from app_plugins.host import PluginContext
from app_plugins.builtin.video_prompt_assistant.vision import analyze_image
from model import VideoElementDetector as detector


class GeminiKeyManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.now = [1000.0]
        self.manager = GeminiKeyManager({KEYS_CONFIG_KEY: "key-one; key-two；key-one"},
                                        clock=lambda: self.now[0])

    def test_legacy_and_explicit_empty_keys(self):
        self.assertEqual(self.manager.request_keys("m"), ["key-one", "key-two"])
        self.manager.apply_config({"gemini_api_key": "legacy"})
        self.assertEqual(self.manager.request_key(), "legacy")
        self.manager.apply_config({KEYS_CONFIG_KEY: [], "gemini_api_key": "legacy"})
        self.assertIsNone(self.manager.request_key())

    def test_quota_status_survives_restart_and_expires(self):
        self.manager.report_failure("key-one", "m", 429, 30)
        restored = GeminiKeyManager(self.manager.snapshot(), clock=lambda: self.now[0])
        self.assertEqual(restored.request_keys("m"), ["key-two"])
        self.assertEqual(restored.request_keys("other"), ["key-one", "key-two"])
        self.now[0] += 36
        self.assertEqual(restored.request_key("m"), "key-one")

    def test_403_is_model_specific_but_invalid_key_is_global(self):
        self.manager.report_failure("key-one", "m", 403)
        self.assertFalse(self.manager.is_available("key-one", "m"))
        self.assertTrue(self.manager.is_available("key-one", "other"))
        self.manager.report_failure("key-two", "m", 400, message="API_KEY_INVALID: key-two")
        self.assertFalse(self.manager.is_available("key-two", "other"))
        self.assertIn("密钥无效", self.manager.status_text("key-two"))
        serialized = json.dumps(self.manager.snapshot()[STATUSES_CONFIG_KEY])
        self.assertNotIn("key-two", serialized)

    def test_network_and_parameter_errors_do_not_disable_keys(self):
        for code in (None, 500, 400, 404):
            self.manager.report_failure("key-one", "m", code)
            self.assertTrue(self.manager.is_available("key-one", "m"))

    def test_success_clears_only_its_model_cooldown(self):
        self.manager.report_failure("key-one", "m", 429)
        self.manager.report_failure("key-one", "other", 429)
        self.now[0] += 1
        self.manager.report_success("key-one", "m")
        self.assertTrue(self.manager.is_available("key-one", "m"))
        self.assertFalse(self.manager.is_available("key-one", "other"))

    def test_stale_config_does_not_erase_worker_feedback(self):
        stale = self.manager.snapshot()
        self.manager.report_failure("key-one", "m", 429)
        self.manager.apply_config(stale)
        self.assertEqual(self.manager.request_keys("m"), ["key-two"])

    def test_removed_key_cannot_return_after_late_worker_report(self):
        self.manager.apply_config({KEYS_CONFIG_KEY: ["key-two"]})
        self.manager.report_success("key-one", "m")
        self.assertEqual(self.manager.request_keys("m"), ["key-two"])
        self.assertNotIn(key_id("key-one"), self.manager.snapshot()[STATUSES_CONFIG_KEY])

    def test_concurrent_reports_and_saved_revision(self):
        workers = [threading.Thread(target=self.manager.report_failure,
                    args=("key-one", f"model-{i}", 429)) for i in range(30)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        states = self.manager.snapshot()[STATUSES_CONFIG_KEY][key_id("key-one")]["models"]
        self.assertEqual(len(states), 30)
        revision = self.manager.revision
        self.manager.report_success("key-two", "m")
        self.manager.mark_saved(revision)
        self.assertTrue(self.manager.dirty)

    def test_ui_reset_and_cancel_are_staged_not_live(self):
        self.manager.report_failure("key-one", "m", 401)
        editor = GeminiKeysEditor(self.manager.snapshot())
        editor.key_list.item(0).setSelected(True)
        editor.reset_selected()
        self.assertFalse(self.manager.is_available("key-one", "m"))
        self.manager.apply_config(editor.get_config())
        self.assertTrue(self.manager.is_available("key-one", "m"))
        editor.deleteLater()

    def test_plugin_contexts_share_same_manager(self):
        host = SimpleNamespace(main_window=SimpleNamespace(gemini_keys=self.manager))
        self.assertIs(PluginContext(host, "first").gemini_keys,
                      PluginContext(host, "second").gemini_keys)

    def test_feedback_signal_is_queued_to_main_thread(self):
        class Receiver(QtCore.QObject):
            @QtCore.pyqtSlot()
            def receive(inner):
                inner.thread_id = threading.get_ident()
        receiver = Receiver()
        self.manager.changed.connect(receiver.receive, QtCore.Qt.ConnectionType.QueuedConnection)
        worker = threading.Thread(target=self.manager.report_success, args=("key-one", "m"))
        worker.start()
        worker.join()
        self.assertFalse(hasattr(receiver, "thread_id"))
        self.app.processEvents()
        self.assertEqual(receiver.thread_id, threading.get_ident())

    def test_image_analysis_rotates_and_reports_shared_state(self):
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {},
            io.BytesIO(b'{"error":{"message":"retry in 30s"}}'))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"candidates": [{"content": {"parts": [
            {"text": '{"scene":"person","subject":"人物"}'}]}}]}).encode()
        with patch("urllib.request.urlopen", side_effect=[error, response]) as urlopen:
            self.assertEqual(analyze_image(b"jpeg", model="m", gemini_keys=self.manager)["scene"], "person")
        self.assertEqual(self.manager.request_keys("m"), ["key-two"])
        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args.args[0]
        self.assertNotIn("key-two", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "key-two")

    def test_cached_video_works_even_when_all_keys_unavailable(self):
        self.manager.report_failure("key-one", "m", 401)
        self.manager.report_failure("key-two", "m", 401)
        with patch.object(detector, "read_cached_detection", return_value={"cached": True}), \
             patch.object(detector, "read_gemini_api_keys", side_effect=AssertionError("must not prompt")):
            self.assertEqual(detector.detect_video_element(Path("video.mp4"), gemini_keys=self.manager),
                             {"cached": True})

    def test_detector_uses_shared_state_and_redacts_error(self):
        error = urllib.error.HTTPError("https://example.test", 400, "bad key", {},
            io.BytesIO(b'{"error":{"message":"API_KEY_INVALID key-one"}}'))
        response = {"candidates": [{"content": {"parts": [{"text": '{"found":false,"confidence":1}'}]}}]}
        with patch.object(detector, "get_gemini_model", return_value="m"), \
             patch.object(detector, "get_target_description", return_value="test target"), \
             patch.object(detector, "call_gemini_once", side_effect=[error, response]):
            stats = {"duration_seconds": 1, "candidate_frame_count": 0, "kept_frame_count": 0,
                     "skipped_similar_frame_count": 0}
            result = detector.ask_gemini(b"jpeg", [], Path("video.mp4"), stats,
                                        self.manager.request_keys("m"), self.manager)
        self.assertFalse(result["found"])
        self.assertFalse(self.manager.is_available("key-one", "other"))
        self.assertIn("[已隐藏密钥]", self.manager.redact("failure key-one"))

    def test_main_save_atomic_failure_keeps_keys_and_worker_state(self):
        from PYUI.main_pyui import MainDialog
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({KEYS_CONFIG_KEY: ["key-one", "key-two"], "unrelated": "keep"}))
            fake = SimpleNamespace(config_name=str(path), gemini_keys=self.manager, appendLog=Mock())
            fake.dump = lambda: self.manager.update_config(json.loads(path.read_text()))
            with patch("PYUI.main_pyui.os.replace", side_effect=OSError("test failure")), \
                 patch("PYUI.main_pyui.QMessageBox.critical") as alert:
                self.assertFalse(MainDialog.saveCurrentConfig(fake, show_errors=False,
                                 gemini_config={KEYS_CONFIG_KEY: ["new"], STATUSES_CONFIG_KEY: {}}))
            alert.assert_not_called()
            self.assertEqual(self.manager.request_keys(), ["key-one", "key-two"])
            self.manager.report_failure("key-one", "m", 429)
            self.assertTrue(MainDialog.saveCurrentConfig(fake, show_errors=False))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["unrelated"], "keep")
            self.assertIn(key_id("key-one"), saved[STATUSES_CONFIG_KEY])
            self.assertFalse(self.manager.dirty)

    def test_main_save_merges_external_key_edit_without_erasing_it(self):
        from PYUI.main_pyui import MainDialog
        from model.ConfigHotReload import ConfigHotReload
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(self.manager.snapshot()))
            fake = SimpleNamespace(config_name=str(path), gemini_keys=self.manager, appendLog=Mock())
            fake.dump = lambda: self.manager.update_config(json.loads(path.read_text()))
            monitor = ConfigHotReload(path, fake.dump, Mock(), Mock())
            monitor.timer.stop()
            fake.config_reload = monitor
            self.manager.report_failure("key-one", "m", 429)
            external = self.manager.snapshot()
            external[KEYS_CONFIG_KEY] = ["new-external-key"]
            external["unrelated"] = "external-value"
            path.write_text(json.dumps(external))
            self.assertTrue(MainDialog.saveCurrentConfig(fake, show_errors=False))
            saved = json.loads(path.read_text())
            self.assertEqual(saved[KEYS_CONFIG_KEY], ["new-external-key"])
            self.assertEqual(saved["unrelated"], "external-value")
            self.assertEqual(saved[STATUSES_CONFIG_KEY], {})
            self.assertEqual(self.manager.request_key("m"), "new-external-key")
            monitor.deleteLater()

    def test_external_config_updates_main_broker_without_restarting(self):
        from PYUI.main_pyui import MainDialog
        fake = SimpleNamespace(gemini_keys=self.manager, appendLog=Mock(), showDesktopNotification=Mock())
        old = {KEYS_CONFIG_KEY: ["key-one", "key-two"]}
        new = {KEYS_CONFIG_KEY: ["externally-added"]}
        self.assertEqual(MainDialog.applyExternalConfig(fake, old, new), set())
        self.assertEqual(self.manager.request_key("m"), "externally-added")


if __name__ == "__main__":
    unittest.main()

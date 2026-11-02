"""Plugin extraction regressions; temporary files and fake monitor only."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.host import PluginHost
from app_plugins.builtin.sheet_monitor import SheetMonitorPlugin
from app_plugins.builtin.video_sorter import VideoSorterPlugin
from model.AppDataStorage import ApplicationStorage


class Window(QtWidgets.QWidget):
    def __init__(self, root):
        super().__init__(); self.loaded_project_dir = Path(root)/'project'; self.loaded_project_dir.mkdir()
        self.config = {'google_sheet_monitor': {'enabled': False, 'sheet_url': 'fixture-sheet', 'poll_seconds': 75},
                       'video_sorting_station_path': str(Path(root)/'videos'), 'video_match_ratio_threshold': .07}
        Path(self.config['video_sorting_station_path']).mkdir()
        self.logs = []; self.notifications = []
    def load_config(self): return dict(self.config)
    def appendLog(self, message, **_kwargs): self.logs.append(message)
    def saveCurrentConfig(self, **_kwargs): self.plugin_host.update_runtime_config(self.config); return True
    def showDesktopNotification(self, *args, **kwargs): self.notifications.append((args, kwargs))


class FakeMonitor(QtCore.QThread):
    status = QtCore.pyqtSignal(str)
    detected = QtCore.pyqtSignal(dict)
    download_result = QtCore.pyqtSignal(dict)
    queue_changed = QtCore.pyqtSignal(int)
    log = QtCore.pyqtSignal(str)
    def __init__(self, settings, api_config=None, parent=None):
        super().__init__(parent); self.targets = []; self.settings = dict(settings)
    def set_project_dir(self, project, *, validated=False): self.targets.append((project, validated))
    def stop(self): self.requestInterruption()
    def run(self):
        while not self.isInterruptionRequested(): self.msleep(5)


class ExtractedPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.window = Window(self.temp.name); self.host = PluginHost(self.window); self.window.plugin_host = self.host
        self.monitor = self.host.install(SheetMonitorPlugin()); self.sorter = self.host.install(VideoSorterPlugin())
    def tearDown(self):
        self.monitor.stop(); self.sorter.stop()
        self.window.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def test_menus_only_no_main_cards_or_automatic_sort(self):
        self.assertIn('sheet_monitor.open', self.host._commands)
        self.assertIn('video_sorter.open', self.host._commands)
        self.assertEqual(self.host._main_widgets, {})
        self.assertIsNone(self.sorter.assign_video_thread)
        self.assertEqual(self.monitor.google_sheet_monitor_settings['poll_seconds'], 75)

    def test_monitor_authorization_owned_by_plugin_and_old_core_binding_resolves(self):
        entry = self.host.google_authorizations.resolve('core.sheet_monitor')
        self.assertEqual(entry.owner, 'sheet_monitor'); self.assertEqual(entry.key, 'sheet_monitor.sheets')

    def test_disabled_plugins_do_not_start_and_preserve_settings(self):
        self.host._enabled_states.update({'sheet_monitor': False, 'video_sorter': False})
        before = dict(self.window.config)
        with patch('app_plugins.builtin.sheet_monitor.plugin.GoogleSheetMonitorThread') as worker:
            self.host.start_all(); self.host.update_runtime_config(self.window.config)
        worker.assert_not_called()
        for key, value in before.items(): self.assertEqual(self.window.config[key], value)

    def test_monitor_receives_project_change_and_clear_without_core_thread_access(self):
        self.monitor.google_sheet_monitor_settings['enabled'] = True
        with patch('app_plugins.builtin.sheet_monitor.plugin.GoogleSheetMonitorThread', FakeMonitor):
            self.monitor.start(); worker = self.monitor.google_sheet_monitor_thread
            self.host.notify_project_changed(Path(self.temp.name)/'another', validated=True)
            self.host.notify_project_changed(None)
            self.assertEqual(worker.targets[-2:], [(Path(self.temp.name)/'another', True), (None, False)])
            self.assertTrue(self.monitor.stop())
        self.assertIsNone(self.monitor.google_sheet_monitor_thread)

    def test_monitor_timeout_retains_thread_owner(self):
        worker = Mock(); worker.isRunning.return_value = True; worker.wait.return_value = False
        self.monitor.google_sheet_monitor_thread = worker
        self.assertFalse(self.monitor.stop())
        self.assertIs(self.monitor.google_sheet_monitor_thread, worker); worker.deleteLater.assert_not_called()
        self.monitor.google_sheet_monitor_thread = None

    def test_open_monitor_cancel_keeps_config_and_baseline(self):
        before = dict(self.window.config)
        with patch('app_plugins.builtin.sheet_monitor.plugin.GoogleSheetMonitorDialog.get_settings', return_value=None), \
             patch('app_plugins.builtin.sheet_monitor.plugin.reset_monitor_baseline') as reset:
            self.monitor.openGoogleSheetMonitor()
        self.assertEqual(self.window.config, before); reset.assert_not_called()

    def test_sorter_window_has_own_paths_threshold_and_keeps_job_when_hidden(self):
        dialog = self.sorter.open_dialog()
        self.assertEqual(dialog.target.text(), str(self.window.loaded_project_dir))
        self.assertEqual(dialog.threshold.value(), .07)
        worker = Mock(); worker.isRunning.return_value = True
        worker.wait.return_value = False
        self.sorter.assign_video_thread = worker
        dialog.close(); self.assertIs(self.sorter.assign_video_thread, worker)
        self.assertFalse(self.sorter.can_close()[0]); worker.requestInterruption.assert_called_once()
        self.sorter.assign_video_thread = None

    def test_failed_settings_restart_preserves_previous_monitor_settings(self):
        previous = dict(self.monitor.google_sheet_monitor_settings)
        with patch.object(self.monitor, 'stopGoogleSheetMonitor', return_value=False):
            self.assertFalse(self.monitor.apply_settings({'google_sheet_monitor': {'enabled': True}}))
        self.assertEqual(self.monitor.google_sheet_monitor_settings, previous)

    def test_sorter_storage_error_is_contained_in_window(self):
        dialog = self.sorter.open_dialog()
        with patch.object(self.sorter.context, 'load_config', side_effect=OSError('fixture volume unavailable')), \
             patch.object(QtWidgets.QMessageBox, 'warning') as warning:
            dialog.run(); dialog.save_defaults()
        self.assertEqual(warning.call_count, 2)
        self.assertTrue(dialog.run_button.isEnabled()); self.assertIsNone(self.sorter.assign_video_thread)

    def test_sort_worker_preserves_both_original_and_existing_match_copy(self):
        from app_plugins.builtin.video_sorter.worker import VideoAssignmentThread
        from app_plugins.builtin.video_sorter import worker as module
        source = Path(self.window.config['video_sorting_station_path'])
        target = self.window.loaded_project_dir
        (source/'a.mp4').write_bytes(b'new-video')
        (target/'a.mp4').write_bytes(b'old-video'); (target/'a_match.mp4').write_bytes(b'old-match')
        matcher = Mock(); matcher.get_features.return_value = (None, object()); matcher.match.return_value = 1.0
        with patch.object(module, 'FeatureMatcher', return_value=matcher) as factory:
            factory.scan_images_recursively.return_value = [{'desc': object(), 'folder': str(target), 'name': 'picture.png'}]
            factory.get_video_frame_clean.return_value = object()
            worker = VideoAssignmentThread(target, source); worker.run()
        self.assertEqual((target/'a.mp4').read_bytes(), b'old-video')
        self.assertEqual((target/'a_match.mp4').read_bytes(), b'old-match')
        self.assertEqual((target/'a_match2.mp4').read_bytes(), b'new-video')


if __name__ == '__main__': unittest.main()

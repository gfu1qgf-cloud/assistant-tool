import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from model.FirstRun import (first_use_state, initial_settings, create_normal_config,
                            FirstUseDecision, read_config_object, DEFAULT_DISABLED_PLUGINS)
from PYUI.first_run_pyui import FirstRunDialog, configure_first_use


class FirstUseModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'config.example.json').write_text(json.dumps({'run_upload': False, 'gemini_api_keys': []}), encoding='utf-8')

    def test_release_build_manifest_does_not_look_like_private_user_data(self):
        (self.root / 'BUILD_INFO.json').write_text('{}')
        self.assertEqual(first_use_state(self.root), 'fresh')

    def test_existing_configuration_and_binding_never_trigger_new_setup(self):
        (self.root / 'config.json').write_text('{broken')
        self.assertEqual(first_use_state(self.root), 'existing')
        (self.root / 'storage_bootstrap.json').write_text('{broken')
        with patch('model.PrivateDataCatalog.catalog', side_effect=AssertionError('must not read old plaintext')):
            self.assertEqual(first_use_state(self.root), 'bound')

    def test_missing_config_with_old_credentials_is_recovery_not_fresh(self):
        (self.root / 'GoogleSheetsToken.json').write_text('synthetic old token')
        self.assertEqual(first_use_state(self.root), 'recovery')
        with self.assertRaises(ValueError):
            create_normal_config(self.root, {})
        self.assertFalse((self.root / 'config.json').exists())

    def test_corrupt_old_inventory_is_recovery_without_parsing_it(self):
        (self.root / 'InventoryState.json').write_text('{invalid')
        with patch('model.PrivateDataCatalog.catalog', side_effect=AssertionError('no parsing for detection')):
            self.assertEqual(first_use_state(self.root), 'recovery')

    def test_normal_first_config_contains_chosen_settings(self):
        value = initial_settings(self.root, task_path=str(self.root), offline=True, model='small')
        create_normal_config(self.root, value)
        result = json.loads((self.root / 'config.json').read_text())
        self.assertEqual(result['smart_video_editor']['whisper_model_size'], 'small')
        self.assertEqual(result['task_path'], str(self.root))
        self.assertTrue(result['offline_mode'])
        self.assertFalse(result['run_upload'])
        self.assertEqual(first_use_state(self.root), 'existing')
        self.assertFalse(list(self.root.glob('.first-use-*')))

    def test_requested_optional_plugins_default_off_but_flow_is_unchanged(self):
        value = initial_settings(self.root)
        self.assertTrue(all(value['plugin_enabled'][key] is False for key in DEFAULT_DISABLED_PLUGINS))
        self.assertTrue(value['plugin_enabled'].get('flow_assistant', True))
        self.assertTrue(value['plugin_enabled'].get('batch_text_video', True))

    def test_existing_plugin_choices_are_not_rewritten(self):
        config = {'plugin_enabled': {key: True for key in DEFAULT_DISABLED_PLUGINS}}
        file = self.root / 'config.json'
        original = json.dumps(config).encode('utf-8')
        file.write_bytes(original)
        configure_first_use(self.root)
        self.assertEqual(file.read_bytes(), original)

    def test_shipped_example_matches_first_use_plugin_defaults(self):
        root = Path(__file__).resolve().parents[1]
        value = read_config_object(root / 'config.example.json')
        self.assertTrue(all(value['plugin_enabled'][key] is False for key in DEFAULT_DISABLED_PLUGINS))

    def test_existing_config_is_never_overwritten(self):
        target = self.root / 'config.json'
        target.write_bytes(b'old exact bytes')
        with self.assertRaises(ValueError):
            create_normal_config(self.root, {'new': True}, restoring=True)
        self.assertEqual(target.read_bytes(), b'old exact bytes')

    def test_missing_example_bad_object_and_invalid_task_directory_are_explained(self):
        with self.assertRaisesRegex(ValueError, '任务目录'):
            initial_settings(self.root, task_path=str(self.root / 'missing'))
        (self.root / 'config.example.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, '对象'):
            initial_settings(self.root)
        (self.root / 'config.example.json').unlink()
        with self.assertRaisesRegex(ValueError, '完整程序包'):
            initial_settings(self.root)

    def test_explicit_restore_preserves_backup_and_other_files(self):
        token = self.root / 'GoogleSheetsToken.json'
        token.write_bytes(b'synthetic token')
        value = {'business': 'synthetic retained'}
        create_normal_config(self.root, value, restoring=True)
        self.assertEqual(token.read_bytes(), b'synthetic token')
        self.assertEqual(read_config_object(self.root / 'config.json'), value)

    def test_write_failure_does_not_create_half_config(self):
        with patch('model.FirstRun.tempfile.mkstemp', side_effect=PermissionError('readonly')):
            with self.assertRaises(PermissionError):
                create_normal_config(self.root, {})
        self.assertFalse((self.root / 'config.json').exists())

    def test_concurrent_target_creation_is_not_clobbered(self):
        import model.FirstRun as first
        old_mkstemp = first.tempfile.mkstemp
        def race(*args, **kwargs):
            result = old_mkstemp(*args, **kwargs)
            (self.root / 'config.json').write_bytes(b'other process')
            return result
        with patch.object(first.tempfile, 'mkstemp', side_effect=race):
            with self.assertRaises(FileExistsError):
                create_normal_config(self.root, {'new': True})
        self.assertEqual((self.root / 'config.json').read_bytes(), b'other process')


class FirstUseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'config.example.json').write_text('{"run_upload": false}')

    def test_normal_wizard_creates_and_next_launch_skips(self):
        dialog = FirstRunDialog(self.root)
        dialog.accept()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        self.assertTrue(dialog.decision.created)
        self.assertEqual(configure_first_use(self.root).mode, 'existing')
        dialog.deleteLater()

    def test_vc_choice_keeps_seed_in_memory_without_plaintext_copy(self):
        dialog = FirstRunDialog(self.root, force_vc=True)
        self.assertFalse(dialog.normal.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.decision.mode, 'veracrypt')
        self.assertIsInstance(dialog.decision.initial_config, dict)
        self.assertFalse((self.root / 'config.json').exists())
        dialog.deleteLater()

    def test_cancel_and_readonly_error_do_not_start(self):
        dialog = FirstRunDialog(self.root)
        with patch('PYUI.first_run_pyui.create_normal_config', side_effect=PermissionError('readonly')):
            dialog.accept()
        self.assertIsNone(dialog.decision)
        self.assertIn('可写位置', dialog.status.text())
        dialog.reject()
        self.assertFalse((self.root / 'config.json').exists())
        dialog.deleteLater()

    def test_recovery_page_only_allows_restore_or_exit(self):
        (self.root / 'config2.json').write_text('{}')
        dialog = FirstRunDialog(self.root)
        self.assertEqual(dialog.state, 'recovery')
        self.assertFalse(dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok).isEnabled())
        dialog.accept()
        self.assertFalse((self.root / 'config.json').exists())
        self.assertIsNone(dialog.decision)
        dialog.deleteLater()

    def test_bound_startup_does_not_open_first_use_dialog(self):
        (self.root / 'storage_bootstrap.json').write_text('invalid but bound')
        with patch('PYUI.first_run_pyui.FirstRunDialog') as dialog:
            self.assertEqual(configure_first_use(self.root).mode, 'existing')
        dialog.assert_not_called()

    def test_whisper_error_dialog_contains_retention_guidance_and_redacts_secrets(self):
        from PYUI import startup_failure as ui
        with patch.object(ui.QtWidgets, 'QMessageBox') as factory:
            ui.show_whisper_startup_failure(RuntimeError('api_key=synthetic-secret'),
                config_path='config.json', log_path='logs/assistant-tool.log')
            box = factory.return_value
            self.assertIn('程序将退出', box.setText.call_args.args[0])
            self.assertIn('不会删除或重置', box.setInformativeText.call_args.args[0])
            self.assertIn('logs/assistant-tool.log', box.setInformativeText.call_args.args[0])
            self.assertNotIn('synthetic-secret', box.setDetailedText.call_args.args[0])

    def test_whisper_failure_cleans_logging_and_storage_before_exit(self):
        from PYUI import startup_failure as ui
        app, storage = object(), MagicMock()
        with patch.object(ui, 'show_whisper_startup_failure') as show, \
             patch('model.AppLogger.shutdown_application_logging') as logging_stop, \
             patch('PyQt6.sip.delete') as delete:
            self.assertEqual(ui.exit_after_whisper_failure(RuntimeError('failed'), app=app, storage=storage,
                config_path='config.json'), 3)
        show.assert_called_once()
        logging_stop.assert_called_once_with(close=True)
        storage.close.assert_called_once()
        delete.assert_called_once_with(app)

    def test_real_whisper_failure_dialog_constructs_with_qt6_enums(self):
        from PYUI.startup_failure import show_whisper_startup_failure
        observed = []
        def inspect(box):
            observed.append(box.text())
            self.assertEqual(box.icon(), QtWidgets.QMessageBox.Icon.Critical)
            self.assertEqual(box.button(QtWidgets.QMessageBox.StandardButton.Close).text(), '退出程序')
            return QtWidgets.QMessageBox.StandardButton.Close
        with patch.object(QtWidgets.QMessageBox, 'exec', new=inspect):
            show_whisper_startup_failure(RuntimeError('synthetic'), config_path='config.json')
        self.assertEqual(len(observed), 1)

    def test_optional_wizard_screenshot(self):
        target = os.environ.get('LZX_FIRST_USE_SCREENSHOT')
        if target:
            dialog = FirstRunDialog(self.root)
            for label in dialog.findChildren(QtWidgets.QLabel):
                if str(self.root) in label.text():
                    label.setText('程序所在目录 / config.json（隔离示例）')
            dialog.show()
            self.app.processEvents()
            self.assertTrue(dialog.grab().save(target))
            dialog.reject()
            dialog.deleteLater()

    def test_optional_initialization_failure_screenshot(self):
        target = os.environ.get('LZX_INIT_FAILURE_SCREENSHOT')
        if not target:
            return
        from PYUI.startup_failure import show_whisper_startup_failure
        app = self.app
        def capture(box):
            box.show()
            app.processEvents()
            self.assertTrue(box.grab().save(target))
            box.close()
            return QtWidgets.QMessageBox.StandardButton.Close
        with patch.object(QtWidgets.QMessageBox, 'exec', new=capture):
            show_whisper_startup_failure(RuntimeError('模型下载连接超时（隔离示例，未进行真实下载）'),
                config_path='程序所在目录 / config.json', log_path='程序数据目录 / logs / assistant-tool.log')


class FreshVcTests(unittest.TestCase):
    def setUp(self):
        from test_protected_storage import Provider
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.program, self.volume = base / 'program', base / 'volume'
        self.program.mkdir()
        self.volume.mkdir()
        self.provider = Provider(self.volume)
        self.space = patch('model.ProtectedStorage.shutil.disk_usage', return_value=SimpleNamespace(free=10**10))
        self.space.start()
        self.addCleanup(self.space.stop)

    def initialize(self, report=lambda *_: None):
        from model.ProtectedStorage import initialize_fresh
        return initialize_fresh(self.program, self.provider, self.provider.volume, {'run_upload': False}, report)

    def test_new_vc_configuration_has_valid_marker_and_no_plaintext(self):
        from model.ProtectedStorage import read_marker
        (self.program / 'BUILD_INFO.json').write_text('{}')
        root, marker = self.initialize()
        self.assertEqual(read_marker(root), marker)
        self.assertEqual(json.loads((root / 'config.json').read_text()), {'run_upload': False})
        self.assertFalse((self.program / 'config.json').exists())

    def test_vc_never_overwrites_old_directory_or_missing_bound_data(self):
        from model.ProtectedStorage import StorageUnavailable
        (self.volume / 'AssistantToolData').mkdir()
        with self.assertRaises(StorageUnavailable):
            self.initialize()
        (self.volume / 'AssistantToolData').rmdir()
        (self.program / 'storage_bootstrap.json').write_text('broken')
        with self.assertRaises(StorageUnavailable):
            self.initialize()
        self.assertFalse((self.volume / 'AssistantToolData').exists())

    def test_existing_private_records_block_fresh_vc_initialization(self):
        from model.ProtectedStorage import StorageUnavailable
        (self.program / 'GoogleDriveToken.json').write_text('old synthetic')
        with self.assertRaises(StorageUnavailable):
            self.initialize()
        self.assertEqual((self.program / 'GoogleDriveToken.json').read_text(), 'old synthetic')

    def test_volume_loss_and_cancel_do_not_publish_complete_config(self):
        from model.ProtectedStorage import StorageUnavailable
        from model.VeraCryptVolumes import VolumeError
        def cancel(message, percent):
            if percent == 90:
                raise StorageUnavailable('test cancellation')
        with self.assertRaises(StorageUnavailable):
            self.initialize(cancel)
        self.assertFalse((self.volume / 'AssistantToolData').exists())
        self.assertFalse((self.program / 'config.json').exists())
        self.provider.available = False
        with self.assertRaises(VolumeError):
            self.initialize()

    def test_first_vc_config_can_bind_and_public_build_info_stays_external(self):
        from model.ProtectedStorage import bind
        (self.program / 'BUILD_INFO.json').write_text('{"public": true}')
        root, marker = self.initialize()
        with patch('model.AppDataStorage.bind_application_storage'), \
             patch('app_paths.bind_data_root'), patch.dict(os.environ), patch('tempfile.tempdir', tempfile.tempdir):
            storage = bind(self.program, self.provider, self.provider.volume, root, marker)
            try:
                self.assertEqual(storage.resolve(self.program / 'BUILD_INFO.json'), self.program / 'BUILD_INFO.json')
                self.assertTrue((root / 'Temp').is_dir())
            finally:
                storage.close()

    def test_first_vc_chooser_runs_real_worker_and_returns_verified_selection(self):
        from PYUI.protected_storage_pyui import StorageChooser
        from qt_compat import QtCore
        import time
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.provider.mounted = lambda: [self.provider.volume]
        chooser = StorageChooser(self.program, provider=self.provider, initial_config={'run_upload': False})
        self.assertIn('新建首次配置', chooser.migrate_button.text())
        chooser.closed_confirm.setChecked(True)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.Yes):
            chooser.start_migration()
        deadline = time.monotonic() + 5
        while chooser.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        self.assertIsNone(chooser.worker)
        self.assertIsNotNone(chooser.selection)
        self.assertFalse((self.program / 'config.json').exists())
        self.assertTrue((chooser.selection[2] / 'config.json').is_file())
        chooser.deleteLater()
        app.processEvents()

    def test_bootstrap_write_failure_keeps_volume_copy_and_closes_storage(self):
        from PYUI import protected_storage_pyui as ui
        from model.ProtectedStorage import StorageUnavailable
        root, marker = self.initialize()
        storage = MagicMock()
        with patch.object(ui, 'StorageChooser') as factory, patch.object(ui, 'bind', return_value=storage), \
             patch.object(ui, 'commit_bootstrap', side_effect=PermissionError('readonly')):
            factory.return_value.exec.return_value = QtWidgets.QDialog.Accepted
            factory.return_value.selection = (self.provider, self.provider.volume, root, marker)
            with self.assertRaisesRegex(StorageUnavailable, '卷内副本已保留'):
                ui.configure_startup(self.program, force=True, initial_config={})
        storage.close.assert_called_once()
        self.assertTrue((root / 'config.json').is_file())
        self.assertFalse((self.program / 'config.json').exists())


class MainStartupFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def main_function(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'main.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        namespace = {'QtWidgets': SimpleNamespace(QApplication=lambda args: self.app, QMessageBox=MagicMock()),
            'QtCore': MagicMock(), 'QtGui': MagicMock(), 'QtNetwork': MagicMock(),
            'sys': SimpleNamespace(argv=[], version='isolated test'), 'APP_ROOT': root, 'configure_offline': MagicMock(),
            'install_qt_network_guard': MagicMock(), 'is_offline': lambda: False}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'main.py', 'exec'), namespace)
        return namespace['main'], namespace

    def test_cancel_first_use_stops_before_private_storage_or_model(self):
        main, namespace = self.main_function()
        with patch('PYUI.first_run_pyui.configure_first_use', return_value=None), \
             patch('PYUI.protected_storage_pyui.configure_startup') as storage:
            self.assertEqual(main(), 0)
        storage.assert_not_called()

    def test_online_model_failure_explains_then_exits_without_main_window(self):
        import globalValue
        from PYUI import main_pyui
        main, namespace = self.main_function()
        with patch('PYUI.first_run_pyui.configure_first_use', return_value=FirstUseDecision()), \
             patch('PYUI.protected_storage_pyui.configure_startup', return_value=None), \
             patch('model.AppLogger.configure_application_logging', return_value=(MagicMock(), Path('test.log'))), \
             patch('model.AppLogger.install_exception_hooks'), \
             patch.object(globalValue.globalValue, 'get_whisper_model', side_effect=RuntimeError('synthetic model failure')), \
             patch.object(main_pyui, 'MainDialog') as window, \
             patch('PYUI.startup_failure.exit_after_whisper_failure', return_value=3) as failure:
            self.assertEqual(main(), 3)
        window.assert_not_called()
        failure.assert_called_once()
        self.assertIn('synthetic model failure', str(failure.call_args.args[0]))
        namespace['install_qt_network_guard'].assert_called_once()


if __name__ == '__main__':
    unittest.main()

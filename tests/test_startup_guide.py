import copy
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from PyQt6 import QtTest
from PYUI.first_run_pyui import FirstRunDialog
from PYUI.startup_guide_pyui import StartupGuideDialog
from PYUI.protected_storage_pyui import StorageChooser
from test_protected_storage import Provider


class StartupGuideTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.volume = base / 'program', base / 'volume'
        self.root.mkdir()
        self.volume.mkdir()
        (self.root / 'config.example.json').write_text('{"run_upload": false}', encoding='utf-8')
        self.provider = Provider(self.volume)
        self.provider.mounted = lambda: [self.provider.volume]
        self.expected = {'volume_id': self.provider.volume.volume_id, 'storage_id': 'a' * 32}
        self.widgets = []
        self.addCleanup(self.cleanup_widgets)

    def cleanup_widgets(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def own(self, widget):
        self.widgets.append(widget)
        return widget

    def chooser(self, **kwargs):
        return self.own(StorageChooser(self.root, provider=self.provider, **kwargs))

    def test_bound_startup_real_click_opens_guide_and_returns_to_same_volume(self):
        (self.root / 'storage_bootstrap.json').write_text('bound fixture')
        chooser = self.chooser(expected=self.expected)
        chooser.show()
        self.app.processEvents()
        observed = []
        def close_guide():
            guide = self.app.activeModalWidget()
            observed.append((isinstance(guide, StartupGuideDialog), guide.create_button.isEnabled()))
            guide.return_button.click()
        QtCore.QTimer.singleShot(0, close_guide)
        with patch('PYUI.first_run_pyui.read_config_object', side_effect=AssertionError('no private reads')):
            QtTest.QTest.mouseClick(chooser.guide_button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(observed, [(True, False)])
        self.assertTrue(chooser.isVisible())
        self.assertIs(chooser.volumes.currentData(), self.provider.volume)
        self.assertIsNone(chooser.selection)
        self.assertIsNone(chooser.initial_config)
        self.assertFalse((self.root / 'config.json').exists())

    def test_cancel_guide_does_not_exit_storage_chooser_or_mutate_draft(self):
        seed = {'task_path': '', 'offline_mode': False, 'custom': [1]}
        chooser = self.chooser(initial_config=copy.deepcopy(seed))
        chooser.show()
        with patch.object(StartupGuideDialog, 'exec', new=lambda guide: (guide.initial_config['custom'].append(2), guide.reject(), guide.result())[-1]):
            chooser.open_guide()
        self.assertTrue(chooser.isVisible())
        self.assertEqual(chooser.initial_config, seed)
        self.assertEqual(chooser.result(), QtWidgets.QDialog.DialogCode.Rejected)

    def test_fresh_entry_edits_real_first_run_dialog_without_plaintext_write(self):
        chooser = self.chooser()
        chooser.show()
        observed = []
        def submit_first(dialog):
            observed.append(dialog.normal.isEnabled())
            self.assertTrue(dialog.draft_only)
            self.assertTrue(dialog.restore.isHidden())
            self.assertEqual(dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).text(), '返回向导')
            dialog.model.setCurrentIndex(dialog.model.findData('small'))
            dialog.task_path.setText(str(self.volume))
            dialog.accept()
            return dialog.result()
        def enter_first():
            guide = self.app.activeModalWidget()
            self.assertIsInstance(guide, StartupGuideDialog)
            guide.create_button.click()
        QtCore.QTimer.singleShot(0, enter_first)
        with patch.object(FirstRunDialog, 'exec', new=submit_first):
            chooser.guide_button.click()
        self.assertEqual(observed, [False])
        self.assertEqual(chooser.initial_config['smart_video_editor']['whisper_model_size'], 'small')
        self.assertEqual(chooser.initial_config['task_path'], str(self.volume))
        self.assertIn('新建首次配置', chooser.migrate_button.text())
        self.assertIn('尚未写入', chooser.status.text())
        self.assertTrue(chooser.isVisible())
        self.assertEqual({p.name for p in self.root.iterdir()}, {'config.example.json'})
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_reopening_existing_draft_prefills_task_model_and_offline(self):
        seed = {'task_path': str(self.volume), 'offline_mode': True,
                'smart_video_editor': {'whisper_model_size': 'medium'}}
        dialog = self.own(FirstRunDialog(self.root, force_vc=True, initial_config=seed))
        self.assertEqual(dialog.task_path.text(), str(self.volume))
        self.assertEqual(dialog.model.currentData(), 'medium')
        self.assertTrue(dialog.offline.isChecked())
        dialog.accept()
        self.assertEqual(dialog.decision.mode, 'veracrypt')
        self.assertFalse((self.root / 'config.json').exists())

    def test_nested_wizard_real_modal_buttons_return_draft_to_chooser(self):
        chooser = self.chooser()
        chooser.show()
        observed = []
        def fill_draft():
            form = self.app.activeModalWidget()
            observed.append(type(form))
            form.model.setCurrentIndex(form.model.findData('large-v3'))
            form.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).click()
        def enter_form():
            guide = self.app.activeModalWidget()
            observed.append(type(guide))
            QtCore.QTimer.singleShot(0, fill_draft)
            guide.create_button.click()
        QtCore.QTimer.singleShot(0, enter_form)
        QtTest.QTest.mouseClick(chooser.guide_button, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(observed, [StartupGuideDialog, FirstRunDialog])
        self.assertEqual(chooser.initial_config['smart_video_editor']['whisper_model_size'], 'large-v3')
        self.assertTrue(chooser.isVisible())
        self.assertIsNone(chooser.selection)
        self.assertFalse((self.root / 'config.json').exists())

    def test_forced_offline_cannot_be_unchecked_by_draft(self):
        with patch('model.OfflineMode.is_offline', return_value=True):
            dialog = self.own(FirstRunDialog(self.root, force_vc=True, initial_config={'offline_mode': False}))
        self.assertTrue(dialog.offline.isChecked())
        self.assertFalse(dialog.offline.isEnabled())

    def test_draft_only_disallows_plaintext_restore_even_if_called(self):
        dialog = self.own(FirstRunDialog(self.root, draft_only=True))
        self.assertTrue(dialog.vc.isChecked())
        self.assertFalse(dialog.normal.isEnabled())
        with patch.object(QtWidgets.QFileDialog, 'getOpenFileName') as choose:
            dialog.restore_config()
        choose.assert_not_called()
        self.assertFalse((self.root / 'config.json').exists())

    def test_bound_and_plaintext_existing_guides_do_not_reset_settings(self):
        target = self.root / 'config.json'
        original = b'{"plugin_enabled":{"flow_assistant":true},"custom":"retained"}'
        target.write_bytes(original)
        for bound in (False, True):
            with self.subTest(bound=bound):
                guide = self.own(StartupGuideDialog(self.root, bound=bound, initial_config={'new': True}))
                self.assertFalse(guide.create_button.isEnabled())
                self.assertIsNone(guide.initial_config)
                with patch('PYUI.startup_guide_pyui.FirstRunDialog') as factory:
                    guide.edit_initial_config()
                factory.assert_not_called()
                guide.return_button.click()
                self.assertEqual(target.read_bytes(), original)

    def test_missing_main_config_with_old_data_blocks_fresh_creation(self):
        token = self.root / 'GoogleSheetsToken.json'
        token.write_text('synthetic old data')
        guide = self.own(StartupGuideDialog(self.root))
        self.assertEqual(guide.state, 'recovery')
        self.assertFalse(guide.create_button.isEnabled())
        guide.edit_initial_config()
        self.assertFalse((self.root / 'config.json').exists())
        self.assertEqual(token.read_text(), 'synthetic old data')

    def test_config_appearing_while_guide_open_rejects_new_draft(self):
        chooser = self.chooser()
        def accepted(guide):
            guide.initial_config = {'new': True}
            (self.root / 'config.json').write_bytes(b'other process')
            return QtWidgets.QDialog.DialogCode.Accepted
        with patch.object(StartupGuideDialog, 'exec', new=accepted):
            chooser.open_guide()
        self.assertIsNone(chooser.initial_config)
        self.assertIn('未采用新配置', chooser.status.text())
        self.assertEqual((self.root / 'config.json').read_bytes(), b'other process')

    def test_busy_migration_temporarily_disables_entry_then_restores(self):
        chooser = self.chooser()
        chooser.worker = object()
        chooser.selected_changed()
        self.assertFalse(chooser.guide_button.isEnabled())
        with patch('PYUI.startup_guide_pyui.StartupGuideDialog') as factory:
            chooser.open_guide()
        factory.assert_not_called()
        chooser.worker = None
        chooser.selected_changed()
        self.assertTrue(chooser.guide_button.isEnabled())

    def test_cancel_nested_first_run_keeps_guide_open_and_draft_unchanged(self):
        seed = {'task_path': '', 'offline_mode': False}
        guide = self.own(StartupGuideDialog(self.root, initial_config=seed))
        guide.show()
        with patch.object(FirstRunDialog, 'exec', return_value=QtWidgets.QDialog.DialogCode.Rejected):
            guide.create_button.click()
        self.assertTrue(guide.isVisible())
        self.assertEqual(guide.initial_config, seed)
        self.assertFalse((self.root / 'config.json').exists())

    def test_no_volume_still_allows_guide(self):
        self.provider.mounted = lambda: []
        chooser = self.chooser(expected=self.expected)
        self.assertEqual(chooser.volumes.count(), 0)
        self.assertFalse(chooser.use_button.isEnabled())
        self.assertTrue(chooser.guide_button.isEnabled())

    def test_optional_real_startup_screenshot(self):
        target = os.environ.get('LZX_STARTUP_GUIDE_SCREENSHOT')
        if not target:
            return
        chooser = self.chooser(expected=self.expected)
        chooser.volumes.setItemText(0, 'V:\\ · 可用 120.0 GB · 已有数据目录（隔离示例）')
        chooser.status.setText('请选择加密卷。顶部向导入口始终可见，已有用户也可打开。')
        chooser.show()
        self.app.processEvents()
        self.assertTrue(chooser.grab().save(target))

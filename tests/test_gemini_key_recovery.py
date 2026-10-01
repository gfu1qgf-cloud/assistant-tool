import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtWidgets
from model.GeminiKeyRecovery import read_backup_keys, restore_keys_from_backup, preserve_unedited_keys
from model.GeminiKeyManager import GeminiKeyManager, KEYS_CONFIG_KEY
from PYUI.gemini_keys_pyui import GeminiKeysEditor


class GeminiRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_recover_merges_only_keys_and_keeps_original_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            current, backup = Path(folder) / 'config.json', Path(folder) / 'old.json'
            original = {'gemini_api_keys': ['current'], 'other': {'setting': 17}, 'gemini_api_key_statuses': {'keep': {}}}
            current.write_text(json.dumps(original), encoding='utf-8')
            backup.write_text(json.dumps({'gemini_api_keys': ['current', 'old', 'old'], 'other': 'obsolete'}), encoding='utf-8')
            result = restore_keys_from_backup(current, backup)
            expected = dict(original, gemini_api_keys=['current', 'old'])
            self.assertEqual(json.loads(current.read_text()), expected)
            self.assertEqual(json.loads(Path(result['backup']).read_text()), original)
            self.assertEqual(result['added'], 1)
            self.assertEqual(restore_keys_from_backup(current, backup)['added'], 0)

    def test_invalid_backup_does_not_touch_current_config(self):
        with tempfile.TemporaryDirectory() as folder:
            current, backup = Path(folder) / 'config.json', Path(folder) / 'old.json'
            current.write_text('{"gemini_api_keys":["keep"]}')
            original = current.read_bytes()
            for text in ('[]', '{}', '{'):
                backup.write_text(text)
                with self.assertRaises(ValueError):
                    restore_keys_from_backup(current, backup)
                self.assertEqual(current.read_bytes(), original)

    def test_legacy_backup_and_explicit_empty_list(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.json'
            path.write_text('{"gemini_api_key":"legacy"}')
            self.assertEqual(read_backup_keys(path), ['legacy'])
            path.write_text('{"gemini_api_keys":[],"gemini_api_key":"legacy"}')
            with self.assertRaises(ValueError):
                read_backup_keys(path)

    def test_automatic_save_protects_keys_but_not_external_deletion(self):
        config = {KEYS_CONFIG_KEY: []}
        self.assertTrue(preserve_unedited_keys(config, {KEYS_CONFIG_KEY: ['existing']}))
        self.assertEqual(config[KEYS_CONFIG_KEY], ['existing'])
        self.assertFalse(preserve_unedited_keys({KEYS_CONFIG_KEY: []}, {KEYS_CONFIG_KEY: []}))
        self.assertFalse(preserve_unedited_keys({KEYS_CONFIG_KEY: ['new']}, {KEYS_CONFIG_KEY: ['old']}))

    def test_removing_last_keys_requires_confirmation(self):
        editor = GeminiKeysEditor({KEYS_CONFIG_KEY: ['key-one']})
        editor.key_list.item(0).setSelected(True)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            editor.remove_selected()
        self.assertEqual(editor.keys(), ['key-one'])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            editor.remove_selected()
        self.assertEqual(editor.keys(), [])
        editor.deleteLater()

    def test_main_auto_save_cannot_erase_keys_but_explicit_edit_can(self):
        from PYUI.main_pyui import MainDialog
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({KEYS_CONFIG_KEY: ['stored'], 'other': 'keep'}))
            manager = GeminiKeyManager({KEYS_CONFIG_KEY: []})
            fake = SimpleNamespace(config_name=str(path), gemini_keys=manager, appendLog=Mock())
            fake.load_config = lambda: json.loads(path.read_text())
            fake.dump = lambda: manager.update_config(fake.load_config())
            self.assertTrue(MainDialog.saveCurrentConfig(fake, show_errors=False))
            self.assertEqual(fake.load_config()[KEYS_CONFIG_KEY], ['stored'])
            self.assertEqual(manager.request_keys(), ['stored'])
            self.assertTrue(MainDialog.saveCurrentConfig(fake, show_errors=False,
                            gemini_config={KEYS_CONFIG_KEY: []}))
            self.assertEqual(fake.load_config()[KEYS_CONFIG_KEY], [])
            self.assertEqual(fake.load_config()['other'], 'keep')


if __name__ == '__main__':
    unittest.main()

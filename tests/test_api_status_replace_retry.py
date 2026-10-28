import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model.ApiKeyHelper import record_api_key_status


class ApiStatusReplaceRetryTests(unittest.TestCase):
    def test_windows_transient_denial_retries_same_atomic_file_and_preserves_other_settings(self):
        from model.AppDataStorage import data_io
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            path.write_text(json.dumps({'keep': {'setting': 1}}), encoding='utf8')
            original = data_io.replace
            calls = []
            def replace(source, target):
                calls.append((source, target))
                if len(calls) < 3:
                    error = PermissionError('synthetic sharing denial')
                    error.winerror = 32
                    raise error
                return original(source, target)
            with patch('model.ApiKeyHelper.os.name', 'nt'), patch.object(data_io, 'replace', side_effect=replace), \
                    patch('model.ApiKeyHelper.time.sleep') as sleep:
                record_api_key_status(path, 'synthetic-key', state='ok')
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(set(calls)), 1)
            self.assertEqual(sleep.call_count, 2)
            saved = json.loads(path.read_text('utf8'))
            self.assertEqual(saved['keep'], {'setting': 1})
            self.assertEqual(len(saved['elevenlabs_api_key_statuses']), 1)
            self.assertFalse(list(Path(folder).glob('*.tmp')))

    def test_permanent_failure_is_bounded_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            path.write_bytes(b'{"keep":true}')
            denied = PermissionError('synthetic permanent denial'); denied.winerror = 5
            with patch('model.ApiKeyHelper.os.name', 'nt'), \
                    patch('model.ApiKeyHelper.data_io.replace', side_effect=denied) as replace, \
                    patch('model.ApiKeyHelper.time.sleep') as sleep, self.assertRaises(PermissionError):
                record_api_key_status(path, 'synthetic-key', state='ok')
            self.assertEqual(replace.call_count, 5)
            self.assertEqual(sleep.call_count, 4)
            self.assertEqual(path.read_bytes(), b'{"keep":true}')
            self.assertFalse(list(Path(folder).glob('*.tmp')))

    def test_nonsharing_denial_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            error = PermissionError('synthetic nonsharing denial'); error.winerror = 19
            with patch('model.ApiKeyHelper.os.name', 'nt'), \
                    patch('model.ApiKeyHelper.data_io.replace', side_effect=error) as replace, \
                    patch('model.ApiKeyHelper.time.sleep') as sleep, self.assertRaises(PermissionError):
                record_api_key_status(path, 'synthetic-key', state='ok')
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(path.exists())

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from model.AppDataStorage import ApplicationStorage, data_file, data_directory, data_io


class AppDataStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = ApplicationStorage(self.root)
    def tearDown(self): self.temp.cleanup()

    def test_redirects_and_sidecars_do_not_need_path_comparison(self):
        old, new = self.root/'old'/'settings.json', self.root/'private'/'settings.json'
        storage = ApplicationStorage(self.root, redirects=[(old, new),
            (self.root/'old'/'media', self.root/'private'/'media')])
        # Reproduce the failing Path comparison site, without modifying any
        # interpreter internals or a running application.
        with patch.object(type(old), '__eq__', side_effect=TypeError("'NoneType' object is not callable")):
            self.assertEqual(str(storage.resolve(str(old))), str(new))
            self.assertEqual(str(storage.resolve(str(old)+'-wal')), str(new)+'-wal')
            self.assertEqual(str(storage.resolve(self.root/'old'/'media'/'a.png')),
                             str(self.root/'private'/'media'/'a.png'))
            self.assertEqual(str(storage.resolve(self.root/'old'/'media2'/'a.png')),
                             str(self.root/'old'/'media2'/'a.png'))

    def test_namespace_does_not_create_blank_state_until_requested(self):
        area = self.storage.for_plugin('example')
        self.assertFalse(area.path.exists())
        file = area.file('records/state.json')
        self.assertFalse(file.exists())
        file.write_json({'name': '旧记录', 'unknown': [1, 2]}, indent=2)
        self.assertEqual(file.read_json(), {'name': '旧记录', 'unknown': [1, 2]})
        self.assertTrue(file.path.is_relative_to(self.root / 'PluginData/example'))

    def test_namespace_rejects_escape_absolute_and_alternate_stream_names(self):
        area = self.storage.for_plugin('example')
        for name in ('', '.', '..', '../secret', 'dir/../../secret', '/secret', 'C:\\secret', 'file:stream'):
            with self.subTest(name=name), self.assertRaises(ValueError): area.file(name)
        for name in ('../other', 'C:\\root', ''):
            with self.subTest(name=name), self.assertRaises(ValueError): self.storage.for_plugin(name)

    def test_file_handle_is_created_by_host_and_closed_after_use(self):
        file = self.storage.for_plugin('example').file('records.txt')
        with file.open('w', encoding='utf-8') as handle:
            self.assertFalse(handle.closed)
            handle.write('hello')
        self.assertTrue(handle.closed)
        self.assertEqual(file.read_text(), 'hello')

    def test_plugin_context_requests_namespace_from_host(self):
        from types import SimpleNamespace
        from app_plugins.host import PluginContext
        context = PluginContext(SimpleNamespace(data_storage=self.storage), 'demo-plugin')
        file = context.storage.file('state.json')
        self.assertEqual(file.path, self.root / 'PluginData/demo-plugin/state.json')
        file.write_json({'keep': True})
        self.assertEqual(file.read_json(), {'keep': True})

    def test_atomic_failure_keeps_old_bytes_and_no_temp_file(self):
        file = data_file(self.root / 'old.json')
        file.write_json({'old': 'keep'})
        before = file.read_bytes()
        with self.assertRaises(RuntimeError):
            with file.atomic_writer() as handle:
                handle.write('incomplete')
                raise RuntimeError('failed')
        self.assertEqual(file.read_bytes(), before)
        with patch('model.AppDataStorage.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): file.write_json({'new': 'not committed'})
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [file.path])

    def test_invalid_json_never_resets_or_creates_a_new_default(self):
        file = data_file(self.root / 'invalid.json')
        file.write_bytes(b'{invalid')
        with self.assertRaises(json.JSONDecodeError): file.read_json()
        self.assertEqual(file.read_bytes(), b'{invalid')
        with self.assertRaises(FileNotFoundError): data_file(self.root / 'missing.json').read_json()

    def test_database_connection_preserves_existing_schema_rows_and_wal_location(self):
        path = self.root / 'old.sqlite3'
        db = sqlite3.connect(path)
        db.execute('create table records (name text)')
        db.execute('insert into records values (?)', ('旧记录',))
        db.commit(); db.close()
        with patch('model.AppDataStorage.sqlite3.connect', wraps=sqlite3.connect) as created:
            db = data_file(path).connect(timeout=9)
            self.assertEqual(db.execute('select name from records').fetchone()[0], '旧记录')
            db.execute('pragma journal_mode=WAL')
            db.execute('insert into records values (?)', ('new',)); db.commit()
            self.assertTrue(path.with_name(path.name+'-wal').exists())
            db.close()
        created.assert_called_once()
        self.assertEqual(created.call_args.kwargs['timeout'], 9)

    def test_atomic_concurrent_writers_never_leave_partial_json(self):
        file = data_file(self.root / 'state.json')
        errors = []
        def worker(index):
            try: file.write_json({'index': index, 'payload': 'x'*1000})
            except BaseException as error: errors.append(error)
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(file.read_json()['payload']), 1000)
        self.assertEqual(list(self.root.iterdir()), [file.path])

    def test_legacy_paths_descriptors_and_cleanup_do_not_move_data(self):
        original = self.root / 'legacy.txt'
        original.write_bytes(b'keep')
        self.assertEqual(data_file(original).path, original)
        descriptor, name = data_io.mkstemp(dir=self.root)
        import os
        with os.fdopen(descriptor, 'wb') as handle: handle.write(b'new')
        data_io.replace(name, original)
        self.assertEqual(original.read_bytes(), b'new')
        with data_directory(self.root).temporary() as folder:
            folder.file('item.txt').write_text('temporary')
            path = folder.path
        self.assertFalse(path.exists())
        with self.assertRaises(AttributeError): data_io.system('not available')

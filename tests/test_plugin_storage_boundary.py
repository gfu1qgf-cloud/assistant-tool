from pathlib import Path
import unittest
from model.PluginStorageBoundary import direct_io_calls


class PluginStorageBoundaryTests(unittest.TestCase):
    def test_all_plugin_source_uses_host_data_io(self):
        root = Path(__file__).resolve().parents[1] / 'app_plugins'
        failures = []
        for path in root.rglob('*.py'):
            for line, operation in direct_io_calls(path.read_text(encoding='utf-8-sig')):
                failures.append(f'{path.relative_to(root)}:{line}: {operation}')
        self.assertFalse(failures, '\n'.join(failures))

    def test_direct_creation_read_write_copy_and_database_are_detected(self):
        code = '''
import tempfile as tmp
from sqlite3 import connect as db
from pathlib import Path
import shutil
with open('state.json', 'w') as handle: handle.write('x')
Path('state.json').read_text()
Path('state.json').write_text('new')
tmp.mkstemp()
db('db.sqlite3')
shutil.copy2('source', 'destination')
'''
        self.assertEqual(len(direct_io_calls(code)), 6)

    def test_granted_handles_and_legacy_broker_are_allowed(self):
        code = '''
from model.AppDataStorage import data_io, data_file as item
import os
with context.storage.file('state.json').atomic_writer() as handle: handle.write('x')
managed = context.storage.file('state.json')
managed.read_text()
item(path).write_bytes(b'x')
descriptor, path = data_io.mkstemp(dir=root)
with os.fdopen(descriptor, 'w') as handle: handle.write('x')
data_io.replace(path, destination)
'''
        self.assertEqual(direct_io_calls(code), [])

    def test_keyword_default_modes_and_fd_creation_are_detected(self):
        code = '''
from pathlib import Path
from io import open as text_open
import os
Path('a').open(mode='w')
Path('b').open()
text_open('c', 'w')
os.open('d', os.O_CREAT)
'''
        self.assertEqual(len(direct_io_calls(code)), 4)

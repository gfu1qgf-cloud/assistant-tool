"""Each index transaction owns and closes its SQLite handle on every path."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from model.image_retrieval.index import ImageSearchIndex


class ImageIndexConnectionTests(unittest.TestCase):
    def test_success_closes_without_waiting_for_gc_and_allows_windows_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            index=ImageSearchIndex(Path(directory)/'index')
            with index._connect() as connection:
                connection.execute('SELECT 1').fetchone()
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            destination=index.path.with_suffix('.moved')
            index.path.rename(destination)
            self.assertTrue(destination.is_file())

    def test_exception_rolls_back_and_closes_original_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            index=ImageSearchIndex(Path(directory)/'index')
            with index._connect() as connection:connection.execute('CREATE TABLE proof(value INTEGER)')
            with self.assertRaisesRegex(RuntimeError,'synthetic rollback'):
                with index._connect() as connection:
                    connection.execute('INSERT INTO proof VALUES(1)')
                    raise RuntimeError('synthetic rollback')
            with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
            with index._connect() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM proof').fetchone()[0],0)

    def test_initialization_failure_still_closes_the_new_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            index=ImageSearchIndex(Path(directory)/'index')
            connection=Mock();connection.execute.side_effect=PermissionError('synthetic guard')
            with patch('model.image_retrieval.index.data_file') as file:
                file.return_value.connect.return_value=connection
                with self.assertRaises(PermissionError):
                    with index._connect():self.fail('setup failed before transaction')
            connection.close.assert_called_once()

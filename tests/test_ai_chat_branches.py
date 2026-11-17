import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from app_plugins.builtin.ai_model_client.store import ChatStore


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ChatStore(Path(self.temp.name) / 'history')
        self.identifier = self.store.create()

    def tearDown(self):
        self.temp.cleanup()

    def result(self, text):
        return dict(text=text, provider='mistral', model='test', usage={}, elapsed=1, warning='')

    def turn(self, text, answer):
        prompt = self.store.begin_request(self.identifier, text, [], 'mistral', 'test')
        self.store.complete_request(self.identifier, prompt, self.result(answer))
        return prompt, self.store.messages(self.identifier)[-1]['id']

    def test_regenerate_and_continue_preserve_both_complete_histories(self):
        first_prompt, first = self.turn('问题一', '原回答一')
        self.turn('原后续问题', '原后续回答')
        new = self.store.begin_request(self.identifier, '', [], 'mistral', 'test', regenerate_id=first)
        self.assertEqual([r['text'] for r in self.store.request_messages(self.identifier, new, 20)], ['问题一'])
        self.store.complete_request(self.identifier, new, self.result('新回答一'))
        self.turn('新后续问题', '新后续回答')
        self.store.select_branch(self.identifier, first_prompt)
        self.assertEqual([r['text'] for r in self.store.messages(self.identifier)], ['问题一', '原回答一', '原后续问题', '原后续回答'])
        self.store.select_branch(self.identifier, new)
        self.assertEqual([r['text'] for r in self.store.messages(self.identifier)], ['问题一', '新回答一', '新后续问题', '新后续回答'])
        self.assertEqual(len(self.store.messages(self.identifier, all_branches=True)), 8)
        restored = ChatStore(self.store.root)
        self.assertEqual(restored.messages(self.identifier)[-1]['text'], '新后续回答')

    def test_late_completion_does_not_switch_the_selected_branch(self):
        original_prompt, answer = self.turn('问题', '旧回答')
        new = self.store.begin_request(self.identifier, '', [], 'mistral', 'test', regenerate_id=answer)
        self.store.select_branch(self.identifier, original_prompt)
        self.assertEqual(self.store.request_messages(self.identifier, new, 20)[-1]['text'], '问题')
        self.store.complete_request(self.identifier, new, self.result('迟到的新回答'))
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'], '旧回答')
        self.store.select_branch(self.identifier, new)
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'], '迟到的新回答')

    def test_failed_regeneration_keeps_original_and_navigation(self):
        original, answer = self.turn('问题', '保留原回答')
        new = self.store.begin_request(self.identifier, '', [], 'mistral', 'test', regenerate_id=answer)
        self.store.fail_request(self.identifier, new, '网络失败', partial={'text': '收到一半'})
        info = self.store.branch_navigation(self.identifier)[new]
        self.assertEqual(info, {'targets': [original, new], 'index': 1})
        self.store.select_branch(self.identifier, original)
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'], '保留原回答')
        self.store.select_branch(self.identifier, new)
        self.assertEqual(self.store.messages(self.identifier)[-1]['metadata']['partial']['text'], '收到一半')

    def test_foreign_branch_and_regeneration_ids_are_rejected(self):
        prompt, answer = self.turn('问题', '回答')
        other = self.store.create()
        for action in (lambda: self.store.select_branch(other, answer),
                       lambda: self.store.regeneration_prompt(other, answer),
                       lambda: self.store.regeneration_prompt(self.identifier, prompt)):
            with self.assertRaises(ValueError):
                action()

    def legacy(self):
        root = Path(self.temp.name) / 'legacy'
        root.mkdir()
        path = root / 'conversations.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,provider TEXT,model TEXT,
                options TEXT DEFAULT '{}',draft TEXT DEFAULT '',images TEXT DEFAULT '[]',created REAL,updated REAL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation TEXT,role TEXT,text TEXT,
                images TEXT DEFAULT '[]',provider TEXT,model TEXT,status TEXT,error TEXT DEFAULT '',metadata TEXT DEFAULT '{}',created REAL);''')
            db.execute("INSERT INTO conversations VALUES('old','旧标题','mistral','test',?, '保留草稿','[]',1,2)", (json.dumps({'rounds': 7}),))
            for role, text in (('user', '原问题'), ('assistant', '原回答')):
                db.execute("INSERT INTO messages(conversation,role,text,provider,model,status,created) VALUES('old',?,?,'mistral','test','done',1)", (role, text))
            db.commit()
        return root

    def test_legacy_migration_preserves_text_options_and_local_backup(self):
        root = self.legacy()
        migrated = ChatStore(root)
        self.assertEqual([r['text'] for r in migrated.messages('old')], ['原问题', '原回答'])
        self.assertEqual(migrated.get('old')['draft'], '保留草稿')
        self.assertEqual(migrated.get('old')['options'], {'rounds': 7})
        backups = list(root.glob('conversations.pre-branches-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertNotIn('parent_id', [r[1] for r in db.execute('PRAGMA table_info(messages)')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        ChatStore(root)
        self.assertEqual(len(list(root.glob('conversations.pre-branches-*.sqlite3'))), 1)

    def test_migration_failure_rolls_back_parent_and_leaf_columns(self):
        root = self.legacy()
        store = object.__new__(ChatStore)
        store.root = root
        class FailingConnection:
            def __init__(self, connection): self.connection = connection
            def execute(self, sql, *args):
                if sql.startswith('ALTER TABLE conversations'):
                    raise sqlite3.OperationalError('synthetic interrupted migration')
                return self.connection.execute(sql, *args)
            def backup(self, other): self.connection.backup(other)
        with closing(sqlite3.connect(root / 'conversations.sqlite3')) as db:
            db.row_factory = sqlite3.Row
            with self.assertRaises(sqlite3.OperationalError):
                try:
                    store._migrate_branches(FailingConnection(db))
                except Exception:
                    db.rollback()
                    raise
            self.assertNotIn('parent_id', [r[1] for r in db.execute('PRAGMA table_info(messages)')])
            self.assertNotIn('active_leaf', [r[1] for r in db.execute('PRAGMA table_info(conversations)')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)

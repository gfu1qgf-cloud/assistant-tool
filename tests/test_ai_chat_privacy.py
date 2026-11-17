import io
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from qt_compat import QtCore, QtGui, QtWidgets
from tests import test_ai_model_client as base
from tests import test_ai_chat_branches as branches
from app_plugins.builtin.ai_model_client.store import ChatStore
from app_plugins.builtin.ai_model_client.private_store import PrivateChatStore


def image_bytes(color='blue'):
    buffer = io.BytesIO()
    Image.new('RGB',(32,20),color).save(buffer,format='PNG')
    return buffer.getvalue()


class PurgeTests(unittest.TestCase):
    setUp = branches.BranchTests.setUp
    tearDown = branches.BranchTests.tearDown
    result = branches.BranchTests.result
    turn = branches.BranchTests.turn

    def backup(self):
        path = self.store.root / 'conversations.pre-branches-fixture.sqlite3'
        backup = sqlite3.connect(path)
        try:
            with self.store.connection() as db:
                db.backup(backup)
        finally:
            backup.close()
        return path

    def test_purge_all_branches_drafts_batches_and_automatic_backup_bytes(self):
        secret = 'UNIQUE_PRIVATE_HISTORY_6a4b1f0e'
        original, answer = self.turn(secret + '-question',secret + '-answer')
        edited = self.store.begin_request(self.identifier,secret + '-edited',[],'mistral','test',regenerate_id=answer)
        self.store.complete_request(self.identifier,edited,self.result(secret + '-alternative'))
        pending = self.store.begin_request(self.identifier,secret + '-batch',[],'openai','test')
        self.store.prepare_batch(self.identifier,pending,'fingerprint',{'model':'test','input':secret})
        self.store.update(self.identifier,title=secret,draft=secret,options={'system':secret})
        other = self.store.create()
        self.store.update(other,title='KEEP_OTHER_CONVERSATION')
        backup = self.backup()
        self.store.purge_conversation(self.identifier)
        self.assertEqual(self.store.pending_purges(),[])
        self.assertEqual([r['id'] for r in self.store.conversations()],[other])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE conversation=?',(self.identifier,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chat_batches').fetchone()[0],0)
        for path in self.store.root.glob('*.sqlite3*'):
            if path.is_file():
                self.assertNotIn(secret.encode(),path.read_bytes(),path.name)
        self.assertTrue(backup.exists())
        restored = ChatStore(self.store.root)
        self.assertEqual(restored.get(other)['title'],'KEEP_OTHER_CONVERSATION')

    def test_purge_removes_unique_images_but_keeps_shared_images_and_other_records(self):
        unique = self.store.import_image(image_bytes('red'))
        shared = self.store.import_image(image_bytes('blue'))
        self.store.update(self.identifier,images=[unique,shared])
        other = self.store.create()
        self.store.update(other,images=[shared])
        self.backup()
        unique_path = self.store.asset_path(unique)
        shared_path = self.store.asset_path(shared)
        self.store.purge_conversation(self.identifier)
        self.assertFalse(unique_path.exists())
        self.assertTrue(shared_path.exists())
        self.assertEqual(self.store.get(other)['images'],[shared])

    def test_interrupted_cleanup_keeps_a_queue_and_resumes_without_history(self):
        secret = 'RESUME_PURGE_SECRET_f1d3'
        self.turn(secret,secret)
        self.backup()
        with patch.object(self.store,'_compact_database',side_effect=RuntimeError('synthetic disk busy')):
            with self.assertRaises(RuntimeError):
                self.store.purge_conversation(self.identifier)
        self.assertEqual(self.store.pending_purges(),[self.identifier])
        restored = ChatStore(self.store.root)
        self.assertEqual(restored.pending_purges(),[])
        self.assertEqual(restored.conversations(),[])
        for path in restored.root.glob('*.sqlite3*'):
            if path.is_file(): self.assertNotIn(secret.encode(),path.read_bytes())

    def test_purge_does_not_call_cloud_api(self):
        pending = self.store.begin_request(self.identifier,'pending',[],'openai','test')
        self.store.prepare_batch(self.identifier,pending,'fingerprint',{'model':'test','input':'pending'})
        with patch('requests.sessions.Session.request') as request:
            self.store.purge_conversation(self.identifier)
            request.assert_not_called()
        self.assertEqual(self.store.conversations(),[])


class MemoryPrivacyTests(unittest.TestCase):
    def test_private_store_never_uses_chat_files_or_directories(self):
        with patch('app_plugins.builtin.ai_model_client.store.data_file',side_effect=AssertionError('disk chat file used')), \
             patch('app_plugins.builtin.ai_model_client.store.data_directory',side_effect=AssertionError('disk directory used')):
            store = PrivateChatStore()
            identifier = store.create()
            image = store.import_image(image_bytes())
            prompt = store.begin_request(identifier,'MEMORY_ONLY_PRIVATE', [image],'mistral','test')
            store.complete_request(identifier,prompt,dict(text='MEMORY_ONLY_ANSWER',provider='mistral',model='test',usage={},elapsed=1,warning=''))
            self.assertIsInstance(store.asset_resource(image),bytes)
            self.assertEqual(len(store.encoded_image(image)['data'])>0,True)
            self.assertEqual(store.messages(identifier)[-1]['text'],'MEMORY_ONLY_ANSWER')
            self.assertIsNone(store.path)
            store.close_private()
            self.assertEqual(store._assets,{})
            with self.assertRaises(ValueError): store.messages(identifier)

    def test_private_store_cannot_prepare_batch(self):
        store = PrivateChatStore()
        try:
            with self.assertRaisesRegex(ValueError,'无痕'):
                store.prepare_batch('id',1,'fingerprint',{'input':'secret'})
        finally:
            store.close_private()

    def test_private_gemini_failure_does_not_persist_api_error_body(self):
        from app_plugins.builtin.ai_model_client.private_store import _PrivateGeminiKeys
        from unittest.mock import Mock
        delegate = Mock()
        _PrivateGeminiKeys(delegate).report_failure('key','model',400,message='PRIVATE_ERROR_BODY')
        self.assertNotIn('PRIVATE_ERROR_BODY',str(delegate.report_failure.call_args))

    def test_private_memory_delete_keeps_only_still_referenced_images(self):
        store = PrivateChatStore()
        try:
            first, other = store.create(), store.create()
            image = store.import_image(image_bytes())
            store.update(first,images=[image]); store.update(other,images=[image])
            store.purge_conversation(first)
            self.assertIsInstance(store.asset_resource(image),bytes)
            store.purge_conversation(other)
            self.assertEqual(store._assets,{})
        finally:
            store.close_private()


class PrivacyUITests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait
    result = base.UITests.result

    def test_private_dialog_text_images_and_failures_do_not_reach_main_history_or_logs(self):
        dialog = self.dialog.open_private_chat()
        private_store = dialog.store
        dialog.consent = True
        dialog.provider.setCurrentIndex(dialog.provider.findData('mistral'))
        dialog.model.setCurrentText('mistral-small-latest')
        secret = 'UI_PRIVATE_SECRET_19f2'
        image = private_store.import_image(image_bytes())
        dialog.pending_images = [image]
        dialog.render_attachments()
        dialog.input.setPlainText(secret)
        self.context.logs.clear()
        with patch.object(dialog.client,'chat',side_effect=base.AIProviderError(secret)):
            dialog.send()
            self.wait(lambda:dialog.handle is None)
        self.assertEqual(self.context.logs,[])
        self.assertTrue(dialog.is_private)
        self.assertFalse(dialog.request_mode.model().item(1).isEnabled())
        resource = next(iter(dialog.transcript.allowed_images.values()))
        self.assertIsInstance(resource,bytes)
        for path in self.store.root.glob('*.sqlite3*'):
            if path.is_file(): self.assertNotIn(secret.encode(),path.read_bytes())
        dialog.close()
        self.assertTrue(private_store.closed)
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_closing_private_window_waits_for_worker_then_clears_memory(self):
        dialog = self.dialog.open_private_chat()
        store = dialog.store
        dialog.consent = True
        dialog.provider.setCurrentIndex(dialog.provider.findData('mistral'))
        dialog.model.setCurrentText('mistral-small-latest')
        started, release = threading.Event(), threading.Event()
        def chat(*args,**kwargs):
            started.set(); release.wait(4)
            return self.result()
        try:
            with patch.object(dialog.client,'chat',side_effect=chat):
                dialog.input.setPlainText('PRIVATE_CLOSING_SECRET')
                dialog.send()
                self.wait(started.is_set)
                dialog.close()
                self.assertFalse(dialog.isVisible())
                release.set()
                self.wait(lambda:store.closed)
        finally:
            release.set()
        self.assertEqual(store._assets,{})

    def test_permanent_delete_clears_ui_without_creating_replacement_history(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        prompt = self.store.begin_request(self.dialog.current_id,'DELETE_UI_SECRET',[],'mistral','test')
        self.store.complete_request(self.dialog.current_id,prompt,self.result())
        self.dialog.render_transcript()
        with patch('app_plugins.builtin.ai_model_client.ui.QtWidgets.QMessageBox.question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.dialog.delete_conversation()
            self.wait(lambda:self.dialog.handle is None)
        self.assertIsNone(self.dialog.current_id)
        self.assertEqual(self.store.conversations(),[])
        self.assertEqual(self.dialog.input.toPlainText(),'')
        self.assertNotIn('DELETE_UI_SECRET',self.dialog.transcript.toPlainText())
        self.dialog.input.setPlainText('全新的对话')
        with patch.object(self.context.ai_client,'chat',return_value=self.result()):
            self.dialog.send()
            self.wait(lambda:self.dialog.handle is None)
        self.assertEqual(len(self.store.conversations()),1)
        self.assertEqual(self.store.messages(self.dialog.current_id)[0]['text'],'全新的对话')

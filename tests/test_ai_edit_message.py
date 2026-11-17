import unittest
from unittest.mock import patch
from qt_compat import QtWidgets
from tests import test_ai_model_client as base
from tests import test_ai_chat_branches as branch_base


class EditStoreTests(unittest.TestCase):
    setUp = branch_base.BranchTests.setUp
    tearDown = branch_base.BranchTests.tearDown
    turn = branch_base.BranchTests.turn
    result = branch_base.BranchTests.result

    def test_edit_ancestor_creates_sibling_and_preserves_original_followups(self):
        self.turn('前置问题', '前置回答')
        original, _answer = self.turn('原问题', '原回答')
        self.turn('原后续问题', '原后续回答')
        edited = self.store.begin_request(self.identifier, '修改后的问题', [], 'mistral', 'test', edit_id=original)
        self.assertEqual([r['text'] for r in self.store.request_messages(self.identifier, edited, 20)], ['前置问题','前置回答','修改后的问题'])
        self.store.complete_request(self.identifier, edited, self.result('新版回答'))
        self.assertEqual([r['text'] for r in self.store.messages(self.identifier)], ['前置问题','前置回答','修改后的问题','新版回答'])
        self.store.select_branch(self.identifier, original)
        self.assertEqual([r['text'] for r in self.store.messages(self.identifier)], ['前置问题','前置回答','原问题','原回答','原后续问题','原后续回答'])
        self.store.select_branch(self.identifier, edited)
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'], '新版回答')

    def test_edit_rejects_assistant_or_foreign_messages(self):
        prompt, answer = self.turn('问题','回答')
        other = self.store.create()
        for identifier, target in ((other,prompt),(self.identifier,answer)):
            with self.assertRaises(ValueError):
                self.store.begin_request(identifier,'修改',[],'mistral','test',edit_id=target)

    def test_pending_original_can_finish_without_replacing_the_edited_branch(self):
        original = self.store.begin_request(self.identifier,'旧的待返回问题',[],'openai','test')
        edited = self.store.begin_request(self.identifier,'新版问题',[],'mistral','test',edit_id=original)
        self.store.complete_request(self.identifier,edited,self.result('新版回答'))
        self.store.complete_request(self.identifier,original,self.result('旧分支迟到回答'))
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'],'新版回答')
        self.store.select_branch(self.identifier,original)
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'],'旧分支迟到回答')


class EditUITests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait
    result = base.UITests.result

    def original_message(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        original = self.store.begin_request(self.dialog.current_id,'原问题',[],'mistral','mistral-small-latest')
        self.store.complete_request(self.dialog.current_id,original,{**self.result(),'text':'原回答'})
        self.dialog.render_transcript()
        return original, self.dialog.transcript.cards[original]

    def test_edit_button_opens_original_and_cancel_does_not_send_or_change_history(self):
        original, card = self.original_message()
        before = self.store.messages(self.dialog.current_id,all_branches=True)
        def cancel(dialog):
            editor = dialog.findChild(QtWidgets.QPlainTextEdit,'editChatMessage')
            self.assertEqual(editor.toPlainText(),'原问题')
            editor.setPlainText('未发送的修改')
            dialog.reject()
            return QtWidgets.QDialog.DialogCode.Rejected
        with patch.object(self.context.ai_client,'chat') as chat, patch('app_plugins.builtin.ai_model_client.ui.QtWidgets.QDialog.exec',new=cancel):
            card.edit_button.click()
            chat.assert_not_called()
        self.assertEqual(self.store.messages(self.dialog.current_id,all_branches=True),before)

    def test_edit_dialog_send_uses_new_text_keeps_draft_and_switches_versions(self):
        original, card = self.original_message()
        self.dialog.input.setPlainText('下一条草稿')
        def submit(dialog):
            editor = dialog.findChild(QtWidgets.QPlainTextEdit,'editChatMessage')
            editor.setPlainText('改成这个新问题')
            buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).click()
            return dialog.result()
        with patch.object(self.context.ai_client,'chat',return_value={**self.result(),'text':'修改后的回答'}) as chat, patch('app_plugins.builtin.ai_model_client.ui.QtWidgets.QDialog.exec',new=submit):
            card.edit_button.click()
            self.wait(lambda:self.dialog.handle is None)
        self.assertEqual([r['text'] for r in chat.call_args.args[2]],['改成这个新问题'])
        self.assertEqual(self.store.messages(self.dialog.current_id)[0]['text'],'改成这个新问题')
        self.assertEqual(self.dialog.input.toPlainText(),'下一条草稿')
        answer = self.dialog.transcript.cards[self.store.messages(self.dialog.current_id)[-1]['id']]
        self.assertEqual(answer.branch_counter.text(),'2 / 2')
        answer.branch_previous.click()
        self.assertEqual([r['text'] for r in self.store.messages(self.dialog.current_id)],['原问题','原回答'])

    def test_invalid_edit_keeps_dialog_text_and_does_not_create_branch(self):
        _original, card = self.original_message()
        self.dialog.context_chars.setValue(2)
        def submit(dialog):
            editor = dialog.findChild(QtWidgets.QPlainTextEdit,'editChatMessage')
            editor.setPlainText('超过文字预算的新问题')
            buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).click()
            self.assertEqual(editor.toPlainText(),'超过文字预算的新问题')
            self.assertNotEqual(dialog.result(),QtWidgets.QDialog.DialogCode.Accepted)
            dialog.reject()
            return QtWidgets.QDialog.DialogCode.Rejected
        with patch.object(self.context.ai_client,'chat') as chat, patch('app_plugins.builtin.ai_model_client.ui.QtWidgets.QDialog.exec',new=submit):
            card.edit_button.click()
            chat.assert_not_called()
        self.assertEqual(len(self.store.messages(self.dialog.current_id,all_branches=True)),2)

    def test_edit_keeps_original_images_and_current_draft_attachments(self):
        _original, _card = self.original_message()
        from PIL import Image
        import io
        raw = io.BytesIO(); Image.new('RGB',(20,10),'blue').save(raw,format='PNG')
        attachment = self.store.import_image(raw.getvalue())
        original = self.store.begin_request(self.dialog.current_id,'带图问题',[attachment],'mistral','test')
        self.store.complete_request(self.dialog.current_id,original,{**self.result(),'text':'图像回答'})
        self.dialog.pending_images = [attachment]
        self.dialog.input.setPlainText('保留草稿')
        with patch.object(self.context.ai_client,'chat',return_value={**self.result(),'text':'改后图像回答'}) as chat:
            self.dialog.send(edit_id=original,edited_text='修改带图问题')
            self.wait(lambda:self.dialog.handle is None)
        self.assertEqual(len(chat.call_args.args[2][-1]['images']),1)
        self.assertEqual(self.dialog.pending_images,[attachment])
        self.assertEqual(self.dialog.input.toPlainText(),'保留草稿')

import unittest
from unittest.mock import patch
from qt_compat import QtCore
from tests import test_ai_model_client as base


class RetryEntryTests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait
    result = base.UITests.result

    def select_model(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')

    def answer(self, text='原回答'):
        self.select_model()
        prompt = self.store.begin_request(self.dialog.current_id, '原问题', [], 'mistral', 'mistral-small-latest')
        self.store.complete_request(self.dialog.current_id, prompt, {**self.result(), 'text': text})
        self.dialog.render_transcript()
        for _ in range(6): self.app.processEvents()
        return prompt

    def test_fixed_retry_regenerates_completed_answer_and_keeps_draft_and_old_branch(self):
        original = self.answer()
        self.dialog.input.setPlainText('下一条尚未发送的草稿')
        with patch.object(self.context.ai_client, 'chat', return_value={**self.result(), 'text':'新回答'}) as chat:
            self.dialog.retry_button.click()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual([r['text'] for r in chat.call_args.args[2]], ['原问题'])
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['text'], '新回答')
        self.assertEqual(self.dialog.input.toPlainText(), '下一条尚未发送的草稿')
        self.store.select_branch(self.dialog.current_id, original)
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['text'], '原回答')
        self.assertEqual(len(self.store.messages(self.dialog.current_id, all_branches=True)), 4)

    def test_fixed_retry_resends_failed_original_instead_of_current_draft(self):
        self.select_model()
        original = self.store.begin_request(self.dialog.current_id, '失败的问题', [], 'mistral', 'mistral-small-latest')
        self.store.fail_request(self.dialog.current_id, original, '模拟网络错误')
        self.dialog.render_transcript()
        self.dialog.input.setPlainText('保留草稿')
        with patch.object(self.context.ai_client, 'chat', return_value={**self.result(), 'text':'重试成功'}) as chat:
            self.dialog.retry_button.click()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual([r['text'] for r in chat.call_args.args[2]], ['失败的问题'])
        self.assertEqual(self.store.messages(self.dialog.current_id)[0]['id'], original)
        self.assertEqual(self.dialog.input.toPlainText(), '保留草稿')
        self.assertEqual(len(self.store.messages(self.dialog.current_id)), 2)

    def test_pending_request_is_not_retried_or_duplicated(self):
        self.select_model()
        self.store.begin_request(self.dialog.current_id, '正在等待的请求', [], 'mistral', 'mistral-small-latest')
        with patch.object(self.context.ai_client, 'chat') as chat:
            self.dialog.retry_button.click()
            chat.assert_not_called()
        self.assertIn('等待', self.dialog.status.text())
        self.assertEqual(len(self.store.messages(self.dialog.current_id)), 1)

    def test_long_answer_has_header_retry_and_fixed_bottom_retry(self):
        self.answer('\n\n'.join('长回答第%d段。' % i for i in range(150)))
        view = self.dialog.transcript
        card = view.cards[self.store.messages(self.dialog.current_id)[-1]['id']]
        self.assertTrue(card.regenerate_button.isVisible())
        self.assertLess(card.regenerate_button.mapTo(card, QtCore.QPoint(0,0)).y(), card.body.y())
        for value in (0, view.verticalScrollBar().maximum()):
            view.verticalScrollBar().setValue(value)
            for _ in range(3): self.app.processEvents()
            self.assertTrue(self.dialog.retry_button.isVisible())
            self.assertTrue(self.dialog.retry_button.isEnabled())
            position = self.dialog.retry_button.mapTo(self.dialog, QtCore.QPoint(0,0))
            self.assertLess(position.y(), self.dialog.height())

    def test_menu_retry_and_loaded_version_are_explicit(self):
        self.answer()
        menu = self.dialog.layout().menuBar().actions()[0].menu()
        action = next(action for action in menu.actions() if action.text() == '重试最新回答')
        self.assertEqual(action.shortcut().toString(), 'Ctrl+R')
        with patch.object(self.context.ai_client, 'chat', return_value={**self.result(), 'text':'菜单重试回答'}):
            action.trigger()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['text'], '菜单重试回答')
        from app_plugins.builtin.ai_model_client.version import APP_VERSION
        self.assertIn(APP_VERSION, self.dialog.windowTitle())

    def test_empty_or_offline_retry_does_not_call_provider(self):
        with patch.object(self.context.ai_client, 'chat') as chat:
            self.dialog.retry_request()
            self.assertIn('没有', self.dialog.status.text())
            self.answer()
            with patch('app_plugins.builtin.ai_model_client.ui.is_offline', return_value=True):
                self.dialog.retry_request()
                self.assertIn('离线', self.dialog.status.text())
            chat.assert_not_called()

import unittest
from unittest.mock import patch
from qt_compat import QtCore
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client.request_context import select_context
from app_plugins.builtin.ai_model_client.ui import AIClientDialog


class ContextPolicyTests(unittest.TestCase):
    def history(self):
        return [dict(role=role,text=text,status='done',images=[],metadata={}) for role,text in (
            ('user','old question'),('assistant','old answer'),('user','recent question'),('assistant','recent answer'))]

    def test_zero_history_only_sends_current_and_system_budget(self):
        rows, info = select_context(self.history(), {'text':'new','images':[]}, rounds=0, system='system')
        self.assertEqual([r['text'] for r in rows], ['new'])
        self.assertEqual(info['text_chars'], 9)

    def test_budget_omits_whole_turns_without_modifying_history(self):
        history = self.history()
        original = repr(history)
        rows, info = select_context(history, {'text':'new','images':[]}, text_limit=35)
        self.assertEqual([r['text'] for r in rows], ['recent question','recent answer','new'])
        self.assertEqual(info['omitted_messages'], 2)
        self.assertEqual(repr(history), original)

    def test_oversize_current_is_rejected_instead_of_cut(self):
        with self.assertRaisesRegex(ValueError, '不会截断'):
            select_context([], {'text':'keep every character','images':[]}, text_limit=3)
        with self.assertRaisesRegex(ValueError, '128000'):
            select_context([], {'text':'x'*128001,'images':[]})

    def test_history_images_omitted_current_images_kept(self):
        history = self.history()
        history[0]['images'] = [{'file':'old'}]
        rows, info = select_context(history, {'text':'new','images':[{'file':'current'}]}, include_history_images=False)
        self.assertEqual(rows[0]['images'], [])
        self.assertEqual(rows[-1]['images'], [{'file':'current'}])
        self.assertEqual(info['images'], 1)
        self.assertEqual(history[0]['images'], [{'file':'old'}])


class ControlUITests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait
    result = base.UITests.result

    def test_font_change_scales_existing_code_and_persists_without_keys_change(self):
        prompt = self.store.begin_request(self.dialog.current_id, '问题', [], 'mistral', 'test')
        self.store.complete_request(self.dialog.current_id, prompt, {**self.result(), 'text':'## 标题\n\n```python\nprint(123)\n```'})
        self.dialog.render_transcript()
        old_config = self.context.load_config()
        card = self.dialog.transcript.cards[self.store.messages(self.dialog.current_id)[-1]['id']]
        self.dialog.font_size.setValue(24)
        for _ in range(6): self.app.processEvents()
        self.assertEqual(card.body.document().defaultFont().pixelSize(), 24)
        self.assertIn('print(123)', card.body.toPlainText())
        self.assertEqual(self.context.config['ai_model_client']['chat_font_px'], 24)
        self.assertEqual(self.context.config['gemini_api_keys'], old_config['gemini_api_keys'])
        other = AIClientDialog(self.context, self.store)
        self.assertEqual(other.font_size.value(), 24)
        other.close(); other.deleteLater()

    def test_over_budget_send_does_not_create_pending_message_or_call_model(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        self.dialog.context_chars.setValue(2)
        self.dialog.input.setPlainText('不要截断我的输入')
        with patch.object(self.context.ai_client, 'chat') as chat:
            self.dialog.send()
            chat.assert_not_called()
        self.assertEqual(self.store.messages(self.dialog.current_id), [])
        self.assertEqual(self.dialog.input.toPlainText(), '不要截断我的输入')
        self.assertIn('超过', self.dialog.status.text())

    def test_regenerate_buttons_switch_branch_and_context(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        original = self.store.begin_request(self.dialog.current_id, '原问题', [], 'mistral', 'test')
        self.store.complete_request(self.dialog.current_id, original, {**self.result(), 'text':'旧回答'})
        self.dialog.render_transcript()
        card = self.dialog.transcript.cards[self.store.messages(self.dialog.current_id)[-1]['id']]
        with patch.object(self.context.ai_client, 'chat', return_value={**self.result(),'text':'新回答'}) as chat:
            card.regenerate_button.click()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual([r['text'] for r in chat.call_args.args[2]], ['原问题'])
        new_card = self.dialog.transcript.cards[self.store.messages(self.dialog.current_id)[-1]['id']]
        self.assertEqual(new_card.branch_counter.text(), '2 / 2')
        new_card.branch_previous.click()
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['text'], '旧回答')
        self.assertEqual(len(self.store.messages(self.dialog.current_id, all_branches=True)), 4)

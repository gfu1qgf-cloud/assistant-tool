"""Regression checks for visible text, message cards and reading position."""
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from qt_compat import QtCore, QtGui
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client.rendering import message_html
from app_plugins.builtin.ai_model_client.rendering import _qt_markdown
from app_plugins.builtin.ai_model_client.ui import StreamBuffer


class ChatUITests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait
    result = base.UITests.result

    def answer(self, text, prompt='测试问题'):
        request = self.store.begin_request(self.dialog.current_id, prompt, [], 'mistral', 'test-model')
        self.store.complete_request(self.dialog.current_id, request, {**self.result(), 'text': text})
        self.dialog.render_transcript()
        for _ in range(8):
            self.app.processEvents()
        return self.dialog.transcript.cards[self.store.messages(self.dialog.current_id)[-1]['id']]

    def test_thematic_breaks_do_not_consume_the_answer_as_front_matter(self):
        for prefix in ('---\n', '\n---\n', '\ufeff---\r\n'):
            original = prefix + '\n第一段正文。\n\n**第二段正文。**\n\n---\n\n后记。'
            doc = QtGui.QTextDocument()
            doc.setHtml(message_html(original))
            self.assertEqual(''.join(re.findall('[\u4e00-\u9fff]', doc.toPlainText())), '第一段正文第二段正文后记')
        for literal in ('    ---\n    正文', '\t---\n\t正文', '```\n---\n正文\n---\n```'):
            self.assertEqual(_qt_markdown(literal), literal)

    def test_raw_view_and_copy_preserve_every_character(self):
        raw = '---\n\n## 标题\n\n**这是正文**\n\n---\n\n最后一段。'
        card = self.answer(raw)
        self.assertIn('这是正文', card.body.toPlainText())
        card.raw_button.setChecked(True)
        self.assertEqual(card.body.toPlainText(), raw)
        with patch('app_plugins.builtin.ai_model_client.chat_view.set_internal_clipboard_text') as copied:
            card.copy_text()
            copied.assert_called_once_with(raw)
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['text'], raw)

    def test_card_content_grows_and_last_line_remains_visible(self):
        card = self.answer('\n\n'.join('第%d段的正文。' % i for i in range(100)) + '\n\n这是最后一句。')
        self.app.processEvents()
        body = card.body
        last = body.document().find('这是最后一句。')
        self.assertFalse(last.isNull())
        rect = body.cursorRect(last)
        self.assertGreater(body.height(), 1000)
        self.assertLessEqual(rect.bottom(), body.viewport().height())
        self.assertEqual(body.verticalScrollBar().maximum(), 0)

    def test_stream_does_not_jump_when_reading_history(self):
        self.answer('\n\n'.join('历史段落%d。' % i for i in range(60)))
        prompt = self.store.begin_request(self.dialog.current_id, '继续', [], 'mistral', 'test')
        self.dialog.stream = StreamBuffer()
        self.dialog.operation = {'conversation': self.dialog.current_id, 'prompt_id': prompt}
        self.dialog.render_transcript()
        self.app.processEvents()
        self.dialog.started = __import__('time').monotonic()
        bar = self.dialog.transcript.verticalScrollBar()
        bar.setValue(100)
        before = bar.value()
        self.dialog.stream.append('新回答。\n\n' * 50)
        self.dialog.show_stream()
        for _ in range(4):
            self.app.processEvents()
        self.assertLessEqual(abs(bar.value() - before), 2)
        self.assertIn('新回答。', self.dialog.transcript.cards['stream'].body.toPlainText())
        self.dialog.jump_to_latest()
        self.assertEqual(bar.value(), bar.maximum())
        self.dialog.stream = self.dialog.operation = None

    def test_search_filters_titles_without_touching_messages(self):
        first = self.dialog.current_id
        self.store.update(first, title='可检索的对话')
        self.dialog.new_conversation()
        self.dialog.history_search.setText('可检索')
        self.dialog.reload_history()
        visible = [self.dialog.history.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                   for i in range(self.dialog.history.count()) if not self.dialog.history.item(i).isHidden()]
        self.assertEqual(visible, [first])
        self.assertEqual(self.store.get(first)['title'], '可检索的对话')

    def test_visible_cards_block_external_resources(self):
        card = self.answer('![image](https://example.com/track.png)\n\n<script>literal</script>')
        self.assertIn('<script>literal</script>', card.body.toPlainText())
        for url in ('https://example.com/track.png', 'file:///C:/private.png', 'data:image/png;base64,AA'):
            self.assertIsNone(card.body.loadResource(2, QtCore.QUrl(url)))

    def test_user_bubble_is_on_right_and_assistant_on_left(self):
        assistant = self.answer('这是助手的回复。', prompt='你好')
        for _ in range(8):
            self.app.processEvents()
        view = self.dialog.transcript
        user = next(card for card in view.cards.values() if card.row['role'] == 'user')
        self.assertGreater(user.geometry().left(), assistant.geometry().left())
        self.assertEqual(assistant.geometry().left(), 0)
        self.assertLess(user.width(), view.column.width())


if __name__ == '__main__':
    unittest.main()

import threading
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client.rendering import message_html, is_web_link, _qt_markdown
from model.OfflineMode import OfflineModeError


EXAMPLE = """# 在线素材

可使用 **Pexels**，也可选择 [Met](https://metmuseum.github.io/)。

- 按主题搜索
- 仅下载选中内容

| 平台 | 用途 |
| --- | --- |
| Pexels | 图片、视频 |
| Met | 古典绘画 |

> 保留来源。

```python
print('素材')
```
"""


class TableCompatibilityTests(unittest.TestCase):
    TABLE = "| A | B |\n|---|---|\n| C | D |"

    def test_only_display_copy_gets_blank_line(self):
        original = "说明：\n" + self.TABLE
        self.assertEqual(_qt_markdown(original), "说明：\n\n" + self.TABLE)
        self.assertEqual(original, "说明：\n" + self.TABLE)
        self.assertEqual(_qt_markdown(_qt_markdown(original)), _qt_markdown(original))

    def test_already_valid_table_is_unchanged(self):
        for text in (self.TABLE, "说明：\n\n" + self.TABLE, "说明：\r\n\r\n" + self.TABLE):
            with self.subTest(text=text):
                self.assertEqual(_qt_markdown(text), text)

    def test_unfinished_invalid_or_literal_pipes_are_unchanged(self):
        for text in ("说明：\n| A | B |", "说明：\n| A | B |\n|---",
                     "说明：\n| A | B |\n|---|---|---|",
                     "说明：\nA \\| B\n--- \\| ---", "x\n| A | B |\n|foo|bar|"):
            with self.subTest(text=text):
                self.assertEqual(_qt_markdown(text), text)

    def test_fenced_code_never_changes_including_unclosed_and_long_fences(self):
        for text in ("```markdown\n说明：\n" + self.TABLE + "\n```",
                     "~~~text\n说明：\n" + self.TABLE + "\n~~~",
                     "````markdown\n```\n说明：\n" + self.TABLE + "\n````",
                     "```markdown\n说明：\n" + self.TABLE):
            with self.subTest(text=text):
                self.assertEqual(_qt_markdown(text), text)

    def test_indented_code_and_tab_indentation_are_unchanged(self):
        for indent in ("    ", "\t", "  \t"):
            text = "说明：\n" + "\n".join(indent + line for line in self.TABLE.splitlines())
            with self.subTest(indent=indent):
                self.assertEqual(_qt_markdown(text), text)

    def test_real_table_after_closed_code_and_escaped_pipe_alignment(self):
        for header in ("| A\\|B | C |", "A\\|B | C", "| A\\\\ | C |"):
            table = header + "\n|:---|---:|\n| D | E |"
            text = "```text\n示例\n```\n说明：\n" + table
            with self.subTest(header=header):
                self.assertEqual(_qt_markdown(text), text.replace("说明：\n", "说明：\n\n"))


class MarkdownTests(base.UITests):
    def add_answer(self, text=EXAMPLE):
        prompt = self.store.begin_request(self.dialog.current_id, "试试排版", [], "mistral", "test")
        self.store.complete_request(self.dialog.current_id, prompt, {**self.result(), "text": text})
        self.dialog.render_transcript()

    def test_markdown_formats_headings_table_code_and_link(self):
        self.add_answer()
        output = self.dialog.transcript.toHtml()
        text = self.dialog.transcript.toPlainText()
        self.assertIn("<table", output)
        self.assertIn("<ul", output)
        self.assertIn("font-weight:700", output)
        self.assertIn('href="https://metmuseum.github.io/"', output)
        self.assertIn("print('素材')", text)
        self.assertNotIn("# 在线素材", text)
        self.assertNotIn("| 平台 |", text)

    def test_table_immediately_after_paragraph_renders_as_table(self):
        # Some providers omit the optional GFM blank line before a table.
        answer = ("### 4. **政策的灰色地带**\n"
                  "说明是基于**风险评估**：\n"
                  "| 风险级别 | 例子 | 平台反应 |\n"
                  "|-----------|------|----------|\n"
                  "| **高风险** | 例子一 | 拦截 |\n"
                  "| **低风险** | 例子二 | 正常回答 |\n"
                  "\n---\n### 5. 后面的标题\n后面的正文。")
        self.add_answer(answer)
        output = self.dialog.transcript.toHtml()
        self.assertEqual(output.count("<table"), 1)
        self.assertNotIn("| 风险级别 |", self.dialog.transcript.toPlainText())
        self.assertIn("后面的正文。", self.dialog.transcript.toPlainText())
        self.assertEqual(self.store.messages(self.dialog.current_id)[-1]["text"], answer)

    def test_code_table_is_literal_and_following_answer_table_is_rendered(self):
        answer = "```text\n示例：\n" + TableCompatibilityTests.TABLE + "\n```\n\n说明：\n" + TableCompatibilityTests.TABLE
        self.add_answer(answer)
        self.assertEqual(self.dialog.transcript.toHtml().count("<table"), 1)
        self.assertIn(TableCompatibilityTests.TABLE, self.dialog.transcript.toPlainText())
        with patch("app_plugins.builtin.ai_model_client.ui.set_internal_clipboard_text") as copied:
            self.dialog.copy_answer()
            copied.assert_called_once_with(answer)

    def test_multiple_answers_keep_table_outside_following_message(self):
        self.add_answer("说明：\n" + TableCompatibilityTests.TABLE)
        self.add_answer("接下来的回答。\n\n- 普通列表")
        self.assertEqual(self.dialog.transcript.toHtml().count("<table"), 1)
        document = self.dialog.transcript.document()
        cursor = document.find("接下来的回答。")
        self.assertIsNone(cursor.currentTable())

    def test_nested_lists_do_not_gain_empty_bullets_during_html_roundtrip(self):
        for answer in ("- Parent\n  - Child one\n  - Child two\n- Next parent",
                       "1. Parent\n   1. Child one\n   2. Child two\n2. Next parent",
                       "- **Parent**\n  1. Child one\n  2. Child two\n- Next parent"):
            with self.subTest(answer=answer):
                document = QtGui.QTextDocument()
                document.setHtml(message_html(answer))
                block = document.begin()
                items = []
                while block.isValid():
                    if block.textList():
                        items.append(block.text())
                    block = block.next()
                self.assertEqual(items, ["Parent", "Child one", "Child two", "Next parent"])

    def test_stream_recognizes_table_when_separator_arrives(self):
        from app_plugins.builtin.ai_model_client.ui import StreamBuffer
        prompt = self.store.begin_request(self.dialog.current_id, "问题", [], "mistral", "test")
        self.dialog.operation = {"conversation": self.dialog.current_id, "prompt_id": prompt}
        self.dialog.stream = StreamBuffer()
        self.dialog.started = base.time.monotonic()
        self.dialog.render_transcript()
        try:
            for part in ("说明：\n| 类型 | 操作 |\n", "|---|---|\n", "| 甲 | 乙 |\n",
                         "\n结束正文。\n\n", "另一张表：\n| A | B |\n|---|---|\n| C | D |\n"):
                self.dialog.stream.append(part)
                self.dialog.show_stream()
                self.assertEqual(self.dialog.transcript.toPlainText().count("问题"), 1)
                self.assertEqual(self.dialog.transcript.toPlainText().count("说明："), 1)
            self.assertEqual(self.dialog.transcript.toHtml().count("<table"), 2)
            self.assertEqual(self.dialog.transcript.toPlainText().count("结束正文。"), 1)
        finally:
            self.dialog.stream = self.dialog.operation = None

    def test_raw_html_is_literal_not_rendered(self):
        self.add_answer('<script>danger</script>\n\n<img src="file:///C:/private.png">')
        self.assertIn('<script>danger</script>', self.dialog.transcript.toPlainText())
        self.assertIn('<img src="file:///C:/private.png">', self.dialog.transcript.toPlainText())
        self.assertNotIn('<script>', self.dialog.transcript.toHtml())

    def test_code_indentation_and_literal_font_css_are_preserved(self):
        code = "def example():\n    return \"font-family:'monospace';\""
        self.add_answer("```python\n" + code + "\n```")
        self.assertIn(code, self.dialog.transcript.toPlainText())
        self.assertIn("Consolas", self.dialog.transcript.toHtml())

    def test_literal_list_html_inside_code_keeps_newlines(self):
        code = "<div>\n<ul>\n    <li>literal</li>\n</ul>\n<ol>\n</ol>"
        self.add_answer("```html\n" + code + "\n```")
        self.assertIn(code, self.dialog.transcript.toPlainText())
        self.assertNotIn("<ul", self.dialog.transcript.toHtml())

    def test_remote_and_arbitrary_local_resources_are_blocked(self):
        for url in ("https://example.com/track.png", "file:///C:/private.png", "data:image/png;base64,AAA"):
            with self.subTest(url=url):
                self.assertIsNone(self.dialog.transcript.loadResource(2, QtCore.QUrl(url)))
        self.add_answer("![remote](https://example.com/track.png)\n\n![local](file:///C:/private.png)")
        self.assertEqual(self.dialog.transcript.allowed_images, {})

    def test_registered_local_attachment_still_displays(self):
        image = QtGui.QImage(20, 10, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor("red"))
        path = base.Path(self.temp.name) / "attachment.png"
        image.save(str(path))
        attached = self.store.import_image(path)
        self.store.begin_request(self.dialog.current_id, "图片", [attached], "mistral", "test")
        self.dialog.render_transcript()
        url, stored = next(iter(self.dialog.allowed_images.items()))
        result = self.dialog.transcript.loadResource(2, QtCore.QUrl(url))
        self.assertIsInstance(result, QtGui.QImage)
        self.assertFalse(result.isNull())
        self.assertTrue(stored.is_file())
        self.assertIsNone(self.dialog.transcript.loadResource(1, QtCore.QUrl(url)))

    def test_copy_export_and_request_context_keep_raw_markdown(self):
        self.add_answer()
        with patch("app_plugins.builtin.ai_model_client.ui.set_internal_clipboard_text") as copied:
            self.dialog.copy_answer()
            copied.assert_called_once_with(EXAMPLE)
        self.assertIn(EXAMPLE, self.store.export_text(self.dialog.current_id))
        prompt = self.store.begin_request(self.dialog.current_id, "next", [], "mistral", "test")
        self.assertEqual(self.store.request_messages(self.dialog.current_id, prompt, 20)[1]["text"], EXAMPLE)

    def test_plain_text_toggle_is_saved_per_conversation(self):
        self.add_answer()
        first = self.dialog.current_id
        self.dialog.markdown.setChecked(False)
        self.assertIn("# 在线素材", self.dialog.transcript.toPlainText())
        self.assertNotIn("<table", self.dialog.transcript.toHtml())
        self.dialog.new_conversation()
        self.assertTrue(self.dialog.markdown.isChecked())
        self.dialog.reload_history(first)
        self.assertFalse(self.dialog.markdown.isChecked())
        self.assertIn("# 在线素材", self.dialog.transcript.toPlainText())

    def test_only_explicit_web_links_or_saved_attachments_open(self):
        with patch("app_plugins.builtin.ai_model_client.ui.QtGui.QDesktopServices.openUrl") as opened:
            self.dialog.open_attachment(QtCore.QUrl("https://www.pexels.com/api/documentation/"))
            opened.assert_called_once()
            for url in ("javascript:alert(1)", "file:///C:/private.png", "data:text/html,test",
                        "ftp://example.com", "https://user:pass@example.com", "/relative"):
                self.dialog.open_attachment(QtCore.QUrl(url))
            self.assertEqual(opened.call_count, 1)

    def test_offline_mode_blocks_clicked_web_links(self):
        with patch("app_plugins.builtin.ai_model_client.ui.require_online", side_effect=OfflineModeError("离线已拦截")), \
                patch("app_plugins.builtin.ai_model_client.ui.QtGui.QDesktopServices.openUrl") as opened:
            self.dialog.open_attachment(QtCore.QUrl("https://www.pexels.com"))
            opened.assert_not_called()
            self.assertIn("离线已拦截", self.dialog.status.text())

    def test_stream_replaces_only_answer_and_keeps_markdown_table(self):
        self.select_model()
        started, release = threading.Event(), threading.Event()
        def chat(*args, on_delta=None):
            on_delta(EXAMPLE)
            started.set()
            release.wait(3)
            on_delta("\n\n最后一句")
            return {**self.result(), "text": EXAMPLE + "\n\n最后一句"}
        try:
            with patch.object(self.context.ai_client, "chat", side_effect=chat):
                self.dialog.input.setPlainText("原始问题")
                self.dialog.send()
                self.wait(started.is_set)
                self.dialog.show_stream()
                self.assertIn("<table", self.dialog.transcript.toHtml())
                self.assertEqual(self.dialog.transcript.toPlainText().count("原始问题"), 1)
                self.dialog.markdown.setChecked(False)
                self.assertIn("# 在线素材", self.dialog.transcript.toPlainText())
                self.dialog.markdown.setChecked(True)
                self.assertNotIn("# 在线素材", self.dialog.transcript.toPlainText())
                release.set()
                self.wait(lambda: self.dialog.handle is None)
                text = self.dialog.transcript.toPlainText()
                self.assertEqual(text.count("最后一句"), 1)
                self.assertEqual(text.count("原始问题"), 1)
                self.assertNotIn("生成中", text)
        finally:
            release.set()

    def test_incremental_table_and_code_do_not_leave_old_fragments(self):
        from app_plugins.builtin.ai_model_client.ui import StreamBuffer
        prompt = self.store.begin_request(self.dialog.current_id, "问题", [], "mistral", "test")
        self.dialog.operation = {"conversation": self.dialog.current_id, "prompt_id": prompt}
        self.dialog.stream = StreamBuffer()
        self.dialog.started = base.time.monotonic()
        self.dialog.render_transcript()
        for part in ("# 标题\n\n", "| A | B |\n|---|---|\n", "| 甲 | 乙 |\n", "\n```python\n", "print(1)\n```"):
            self.dialog.stream.append(part)
            self.dialog.show_stream()
            self.assertEqual(self.dialog.transcript.toPlainText().count("标题"), 1)
            self.assertEqual(self.dialog.transcript.toPlainText().count("问题"), 1)
        self.assertEqual(self.dialog.transcript.toPlainText().count("print(1)"), 1)
        self.assertEqual(self.dialog.transcript.toHtml().count("<table"), 1)
        self.dialog.stream = self.dialog.operation = None

    def test_unknown_protocol_is_not_a_web_link(self):
        self.assertFalse(is_web_link(QtCore.QUrl("https:///")))
        self.assertFalse(is_web_link(QtCore.QUrl("mailto:test@example.com")))
        self.assertTrue(is_web_link(QtCore.QUrl("http://example.com/a?q=1")))
        self.assertIn("&lt;script&gt;", message_html("<script>x</script>"))


if __name__ == "__main__":
    unittest.main()

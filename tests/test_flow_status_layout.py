"""Status refreshes must not move tabs, editors, tables or action buttons."""
import os
from pathlib import Path
import unittest

from qt_compat import QtCore, QtTest, QtWidgets
import test_flow_setup as fixtures


class StatusLayoutTests(unittest.TestCase):
    setUpClass = fixtures.SetupTests.__dict__['setUpClass']
    setUp = fixtures.SetupTests.setUp
    ui = fixtures.SetupTests.ui

    def settle(self):
        for _ in range(3): self.app.processEvents()
        QtTest.QTest.qWait(20)

    def area(self, label):
        self.assertFalse(isinstance(label.parentWidget(), QtWidgets.QScrollArea))
        self.assertTrue(hasattr(label, '_status_text'))
        return label

    def test_long_and_short_status_updates_keep_controls_in_place_on_every_page(self):
        dialog, _launcher, _commands = self.ui()
        dialog.resize(1200, 940); dialog.show(); self.settle()
        labels = (dialog.note, dialog.connection_summary, dialog.progress, dialog.queue_tools.summary,
                  dialog.manual.route_note,
                  dialog.manual.estimate, dialog.manual.status, dialog.batch_estimate)
        pages = ((0, (dialog.queue, dialog.queue_tools.search, dialog.queue_menu_button)),
                 (dialog.tabs.indexOf(dialog.batch), (dialog.manual.prompt, dialog.manual.submit, dialog.manual.picker,dialog.compose,dialog.submit_button)),
                 (dialog.tabs.indexOf(dialog.settings_page), (dialog.setup.profile, dialog.setup.project, dialog.setup.connect_button, dialog.concurrent)))
        for page, controls in pages:
            with self.subTest(page=page):
                dialog.tabs.setCurrentIndex(page)
                for label in labels: label.setText('已连接')
                self.settle()
                tracked = (dialog, dialog.tabs, dialog.connection_button, *controls)
                before = [widget.geometry().getRect() for widget in tracked]
                for label in labels:
                    label.setText('排队任务仍保留；正在等待原浏览器和下载结果。' * 100)
                self.settle()
                self.assertEqual([widget.geometry().getRect() for widget in tracked], before)
                for label in labels: label.setText('完成')
                self.settle()
                self.assertEqual([widget.geometry().getRect() for widget in tracked], before)
        self.assertGreater(self.area(dialog.connection_summary).mapTo(dialog, QtCore.QPoint()).y(), dialog.tabs.geometry().bottom())
        self.assertGreater(self.area(dialog.progress).geometry().top(), dialog.queue.geometry().bottom())
        long_message = '\n'.join('状态详情 ' + str(i) for i in range(30))
        dialog.note.setText(long_message); self.settle()
        self.assertEqual(dialog.note.text(), long_message)
        self.assertEqual(dialog.note.toolTip(), long_message)
        self.assertFalse(dialog.findChildren(QtWidgets.QScrollArea))
        # The connection guide retains its original inline, wrapped labels.
        self.assertTrue(dialog.setup.connection_label.wordWrap())
        self.assertFalse(hasattr(dialog.setup.connection_label, '_status_text'))
        self.assertLess(dialog.setup.connection_label.y(), dialog.setup.connect_button.y())
        screenshot = os.environ.get('LZX_FLOW_STATUS_SCREENSHOT')
        if screenshot:
            dialog.tabs.setCurrentIndex(0)
            dialog.note.setText('已跳过千分账号；任务保留在队列，等待小额账号或下一次确认。')
            dialog.connection_summary.setText('账号并行 1/4 · 浏览器：已连接 4 · 可接单 1 · 执行中 1\n排队任务 30 条；状态信息在底部固定区域更新。')
            self.settle(); dialog.grab().save(str(Path(screenshot)))


if __name__ == '__main__': unittest.main()

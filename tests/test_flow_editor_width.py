"""Real Qt resize behavior; locking never changes prompt text or drafts."""
from types import SimpleNamespace
import unittest
from qt_compat import QtCore,QtWidgets
from PyQt6 import QtTest
from app_plugins.builtin.flow_assistant.editor_width import EditorWidthLock


class EditorWidthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def panel(self):
        panel=QtWidgets.QWidget();self.addCleanup(panel.close)
        commands=[];panel.dialog=SimpleNamespace(send=lambda action,**values:commands.append({'action':action,**values}))
        layout=QtWidgets.QVBoxLayout(panel);header=QtWidgets.QHBoxLayout();layout.addLayout(header)
        panel.prompt=QtWidgets.QPlainTextEdit(panel);layout.addWidget(panel.prompt)
        lock=EditorWidthLock(panel,header);panel.resize(520,300)
        return panel,lock,commands

    def test_default_locks_visible_width_and_does_not_follow_window_resize(self):
        panel,lock,commands=self.panel();panel.prompt.setPlainText('台词长度保持稳定。'*15)
        panel.show();QtTest.QTest.qWait(15);width=panel.prompt.width();text=panel.prompt.toPlainText()
        self.assertTrue(lock.checkbox.isChecked());self.assertEqual(panel.prompt.maximumWidth(),width)
        panel.resize(850,400);QtTest.QTest.qWait(10)
        self.assertEqual(panel.prompt.width(),width);self.assertEqual(panel.prompt.toPlainText(),text)
        self.assertEqual(commands[-1]['value']['manual_editor_width'],width)

    def test_unlock_resize_relock_and_reload_capture_the_new_width(self):
        panel,lock,commands=self.panel();panel.show();QtTest.QTest.qWait(10);old=panel.prompt.width()
        lock.checkbox.setChecked(False);panel.resize(750,300);QtTest.QTest.qWait(10)
        self.assertGreater(panel.prompt.width(),old)
        lock.checkbox.setChecked(True);saved=commands[-1]['value'];new=panel.prompt.width()
        self.assertEqual(saved['manual_editor_width'],new)
        other,restored,_=self.panel();restored.apply(saved);other.show();QtTest.QTest.qWait(10)
        self.assertEqual(other.prompt.width(),new)

    def test_old_snapshot_does_not_undo_a_pending_user_toggle(self):
        panel,lock,commands=self.panel();panel.show();QtTest.QTest.qWait(10)
        lock.checkbox.setChecked(False);lock.apply({'manual_editor_width_locked':True,'manual_editor_width':400})
        self.assertFalse(lock.checkbox.isChecked())


if __name__=='__main__':unittest.main()

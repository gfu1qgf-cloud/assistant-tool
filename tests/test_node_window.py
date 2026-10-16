"""Magnetic independent windows never occupy or resize the host layout."""
import unittest
from qt_compat import QtCore, QtWidgets
from PyQt6.QtTest import QTest
from PYUI.node_window import NodeWindow


class NodeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.owner = QtWidgets.QWidget()
        self.owner.resize(140, 120)
        self.owner.move(70, 70)
        self.owner.show()
        self.frame = NodeWindow(self.owner)
        self.frame.resize(180, 140)
        self.frame.setCentralWidget(QtWidgets.QLabel('Nodes'))
        self.frame.show()
        self.settle()

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def tearDown(self):
        self.owner.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.settle()

    def test_follow_uses_absolute_owner_position_without_drift(self):
        self.frame.set_attached(True, 'right', 15)
        self.settle()
        for shift in (20, -10, 30, -25, 5):
            self.owner.move(self.owner.pos() + QtCore.QPoint(shift, 7))
            self.settle()
            main, frame = self.owner.frameGeometry(), self.frame.frameGeometry()
            self.assertEqual(frame.left(), main.right() + 1 + self.frame.GAP)
            self.assertEqual(frame.top(), main.top() + 15)
            self.assertTrue(self.frame.attached)

    def test_free_window_does_not_follow_and_manual_drag_detaches(self):
        self.frame.set_attached(True, 'right', 0)
        self.frame.move(self.frame.pos() + QtCore.QPoint(50, 0))
        self.settle()
        self.assertFalse(self.frame.attached)
        before = self.frame.pos()
        self.owner.move(self.owner.pos() + QtCore.QPoint(10, 12))
        self.settle()
        self.assertEqual(self.frame.pos(), before)

    def test_four_sides_snap_but_distant_window_does_not(self):
        for side in ('right', 'left', 'bottom', 'top'):
            self.frame.set_attached(False)
            self.frame.anchor, self.frame.offset = side, 0
            target = self.frame.target_position(self.owner.frameGeometry(), self.frame.frameGeometry())
            self.frame.move_frame(target + QtCore.QPoint(3, 3), clamp=False)
            self.settle()
            self.assertEqual(self.frame.snap_candidate()[0], side)
            self.frame.snap_after_drag()
            self.assertTrue(self.frame.attached)
        self.frame.set_attached(False)
        self.frame.move_frame(QtCore.QPoint(500, 500), clamp=False)
        self.assertIsNone(self.frame.snap_candidate())

    def test_attached_resize_preserves_anchor(self):
        self.frame.set_attached(True, 'right', 7)
        self.owner.resize(160, 150)
        self.settle()
        self.assertEqual(self.frame.frameGeometry().left(), self.owner.frameGeometry().right() + 1 + self.frame.GAP)

    def test_follow_is_clamped_to_screen_and_can_restore_original_offset(self):
        self.frame.set_attached(True, 'right', 0)
        original = self.owner.pos()
        screen = self.owner.screen().availableGeometry()
        self.owner.move(screen.right() - 50, 70)
        self.settle()
        self.assertTrue(screen.contains(self.frame.frameGeometry()))
        self.assertTrue(self.frame.attached)
        self.owner.move(original)
        self.settle()
        self.assertEqual(self.frame.frameGeometry().left(), self.owner.frameGeometry().right() + 1 + self.frame.GAP)

    def test_close_hides_preserves_widget_and_minimize_restores_only_visible_window(self):
        panel = self.frame.centralWidget()
        self.frame.close()
        self.assertFalse(self.frame.isVisible())
        self.assertIs(self.frame.centralWidget(), panel)
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        self.settle()
        self.assertFalse(self.frame.isVisible())
        self.frame.show()
        self.owner.showMinimized()
        self.settle()
        self.assertFalse(self.frame.isVisible())
        self.owner.showNormal()
        self.settle()
        self.assertTrue(self.frame.isVisible())

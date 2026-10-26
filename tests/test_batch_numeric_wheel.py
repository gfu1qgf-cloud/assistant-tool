import unittest
from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.numeric_wheel import protect_numeric_wheels


class NumericWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.root=QtWidgets.QWidget();self.root.resize(360,260)
        protect_numeric_wheels(self.root)
        self.addCleanup(self.root.deleteLater)
        layout=QtWidgets.QVBoxLayout(self.root)
        self.scroll=QtWidgets.QScrollArea();self.scroll.setWidgetResizable(True);layout.addWidget(self.scroll)
        self.content=QtWidgets.QWidget();self.content.setMinimumHeight(1600)
        box=QtWidgets.QVBoxLayout(self.content)
        self.spin=QtWidgets.QDoubleSpinBox();self.spin.setRange(-1000,1000);self.spin.setValue(50)
        box.addWidget(self.spin);box.addStretch()
        self.scroll.setWidget(self.content)
        self.root.show();self.app.processEvents()

    def wheel(self,target,amount=-120,*,pixels=0,modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
        local=QtCore.QPointF(target.rect().center())
        global_position=QtCore.QPointF(target.mapToGlobal(local.toPoint()))
        event=QtGui.QWheelEvent(local,global_position,QtCore.QPoint(0,pixels),QtCore.QPoint(0,amount),
            QtCore.Qt.MouseButton.NoButton,modifiers,QtCore.Qt.ScrollPhase.NoScrollPhase,False)
        self.app.sendEvent(target,event);self.app.processEvents()

    def test_focused_unfocused_and_ctrl_wheels_never_edit_and_scroll_panel(self):
        for focused in (False,True):
            for modifiers in (QtCore.Qt.KeyboardModifier.NoModifier,QtCore.Qt.KeyboardModifier.ControlModifier):
                with self.subTest(focused=focused,modifiers=modifiers):
                    self.scroll.verticalScrollBar().setValue(0)
                    self.spin.setFocus() if focused else self.spin.clearFocus()
                    changed=[];self.spin.valueChanged.connect(changed.append)
                    self.wheel(self.spin,modifiers=modifiers)
                    self.spin.valueChanged.disconnect(changed.append)
                    self.assertEqual(self.spin.value(),50)
                    self.assertEqual(changed,[])
                    self.assertGreater(self.scroll.verticalScrollBar().value(),0)

    def test_editable_child_touchpad_and_dynamic_integer_spin_are_covered(self):
        self.wheel(self.spin.lineEdit(),amount=0,pixels=-42)
        self.assertEqual(self.spin.value(),50)
        self.assertGreater(self.scroll.verticalScrollBar().value(),0)
        other=QtWidgets.QSpinBox(self.content);other.setValue(12)
        self.content.layout().insertWidget(1,other)
        self.wheel(other,120)
        self.assertEqual(other.value(),12)

    def test_keyboard_entry_up_down_and_mouse_arrows_still_change_value(self):
        self.spin.setFocus();self.spin.selectAll()
        QTest.keyClicks(self.spin,'73.5');QTest.keyClick(self.spin,QtCore.Qt.Key.Key_Return)
        self.assertAlmostEqual(self.spin.value(),73.5)
        QTest.keyClick(self.spin,QtCore.Qt.Key.Key_Up)
        self.assertAlmostEqual(self.spin.value(),74.5)
        option=QtWidgets.QStyleOptionSpinBox();self.spin.initStyleOption(option)
        rect=self.spin.style().subControlRect(QtWidgets.QStyle.ComplexControl.CC_SpinBox,option,
            QtWidgets.QStyle.SubControl.SC_SpinBoxUp,self.spin)
        QTest.mouseClick(self.spin,QtCore.Qt.MouseButton.LeftButton,pos=rect.center())
        self.assertAlmostEqual(self.spin.value(),75.5)

    def test_modal_child_and_reparented_controls_are_covered_without_registration(self):
        dialog=QtWidgets.QDialog(self.root);self.addCleanup(dialog.deleteLater)
        spin=QtWidgets.QSpinBox(dialog);spin.setValue(22)
        self.wheel(spin,120);self.assertEqual(spin.value(),22)
        spin.setParent(self.content);self.wheel(spin,-120);self.assertEqual(spin.value(),22)

    def test_unrelated_window_spin_keeps_its_own_wheel_policy(self):
        window=QtWidgets.QWidget();self.addCleanup(window.deleteLater)
        spin=QtWidgets.QSpinBox(window);spin.setValue(10)
        self.wheel(spin,120)
        self.assertEqual(spin.value(),11)

    def test_real_visual_properties_controls_are_covered_standalone(self):
        from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
        editor=VisualProperties();self.addCleanup(editor.deleteLater)
        spin=editor.controls['key_similarity'];spin.setEnabled(True);spin.setValue(.28)
        self.wheel(spin,120);self.assertAlmostEqual(spin.value(),.28)
        self.assertIs(protect_numeric_wheels(),protect_numeric_wheels())

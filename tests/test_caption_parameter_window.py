"""Real bound inspector comparisons, navigation, and transient visual feedback."""
import copy
import unittest

from qt_compat import QtCore, QtTest, QtWidgets
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, effects, PREFIX
from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets, same_value
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.property_sections import PropertySection


class CaptionParameterWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def panel(self):
        layer = normalize_layers([apply_style(new_caption(), PREFIX+'默认白字', reset_layout=True)])[0]
        layer['caption_effects']['floating_amplitude'] = 37
        other = new_text_component(); other['id'] = 'other'
        panel = LayerPanel([layer, other], '.'); panel.select_layer_id(layer['id']); panel.hide_catalog()
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        scroll.resize(400, 760); scroll.show(); self.app.processEvents()
        self.addCleanup(scroll.deleteLater)
        return panel, scroll

    def window(self, panel):
        owner = panel.caption_properties
        owner.show_parameters(); self.app.processEvents()
        return owner.parameter_window

    def items(self, window):
        return {tuple(item.data(0, QtCore.Qt.ItemDataRole.UserRole)): item for item in
                (window.table.topLevelItem(i) for i in range(window.table.topLevelItemCount()))}

    def test_shortcut_remains_visible_when_basic_is_collapsed_and_titles_stay_plain(self):
        panel, _ = self.panel(); editor = panel.editor
        editor.sections['basic'].set_expanded(False); self.app.processEvents()
        self.assertTrue(panel.caption_properties.parameter_button.isVisible())
        self.assertIs(panel.caption_properties.parameter_button.parentWidget(), editor.sections['basic'].header)
        self.assertFalse(any('当前使用' in section.toggle.text() for section in editor.findChildren(PropertySection)))
        self.assertFalse(any('#1765ad' in section.toggle.styleSheet() for section in editor.findChildren(PropertySection)))

    def test_comparison_modes_distinguish_preset_values_and_current_overrides_without_editing(self):
        panel, _ = self.panel(); before = copy.deepcopy(panel.current()); changes = []
        panel.changed.connect(lambda: changes.append(1))
        window = self.window(panel); key = ('effect', 'floating_amplitude')
        self.assertEqual(self.items(window)[key].text(1), '37')
        window.mode.setCurrentIndex(window.mode.findData('preset'))
        self.assertNotIn(key, self.items(window))
        window.mode.setCurrentIndex(window.mode.findData('overrides'))
        self.assertEqual(self.items(window)[key].text(1), '37')
        self.assertEqual(float(self.items(window)[key].text(2)), effects(apply_style(new_caption(), PREFIX+'默认白字'))['floating_amplitude'])
        self.assertEqual(panel.current(), before); self.assertFalse(changes)

    def test_search_and_all_parameters_include_font_geometry_and_color_targets(self):
        panel, _ = self.panel(); window = self.window(panel)
        window.only_changed.setChecked(False)
        for key in (('layer','font_min'), ('layer','width'), ('effect','color_high'), ('effect','floating_amplitude')):
            self.assertIn(key, self.items(window))
        window.search.setText('漂浮振幅')
        shown = [item for item in self.items(window).values() if not item.isHidden()]
        self.assertEqual(len(shown), 1); self.assertEqual(shown[0].text(0), '漂浮振幅')

    def test_real_double_click_opens_all_ancestors_scrolls_and_flashes_without_edits(self):
        panel, scroll = self.panel(); owner = panel.caption_properties
        for section in panel.editor.findChildren(PropertySection): section.set_expanded(False)
        before = copy.deepcopy(panel.current()); changes = []
        panel.changed.connect(lambda: changes.append(1))
        window = self.window(panel)
        item = self.items(window)[('effect', 'floating_amplitude')]
        window.table.scrollToItem(item); self.app.processEvents()
        point = window.table.visualItemRect(item).center()
        QtTest.QTest.mouseClick(window.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        QtTest.QTest.mouseDClick(window.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        QtTest.QTest.qWait(80)
        target = owner.effects_editor.controls['floating_amplitude']
        self.assertFalse(window.isVisible())
        self.assertIsNotNone(window.flash); self.assertIs(window.flash.parentWidget(), target)
        self.assertEqual(window.flash.animation.loopCount(), 4)
        self.assertTrue(window.flash.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        ancestor = target.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, PropertySection): self.assertTrue(ancestor.toggle.isChecked())
            ancestor = ancestor.parentWidget()
        point = target.mapTo(scroll.viewport(), target.rect().center())
        self.assertTrue(scroll.viewport().rect().contains(point), point)
        self.assertEqual(panel.current(), before); self.assertFalse(changes)
        original_style = target.styleSheet()
        QtTest.QTest.qWait(2350); self.app.processEvents()
        self.assertFalse(target.findChildren(QtWidgets.QWidget, 'caption_parameter_flash'))
        self.assertEqual(target.styleSheet(), original_style)

    def test_color_and_paired_font_fields_locate_the_real_controls(self):
        panel, _ = self.panel(); window = self.window(panel); window.only_changed.setChecked(False)
        for key in (('effect', 'color_high'), ('layer', 'font_min'), ('layer', 'width')):
            window.locate(self.items(window)[key]); QtTest.QTest.qWait(60)
            self.assertIs(window.flash.parentWidget(), parameter_targets(panel.caption_properties)[key][0])

    def test_selection_change_clears_stale_rows_and_pending_navigation(self):
        panel, _ = self.panel(); window = self.window(panel)
        item = self.items(window)[('effect', 'floating_amplitude')]
        window.locate(item)
        panel.select_layer_id('other'); self.app.processEvents(); self.app.processEvents()
        self.assertIsNone(window.owner.layer)
        self.assertEqual(window.table.topLevelItemCount(), 0)
        self.assertIsNone(window.pending); self.assertIsNone(window.flash)

    def test_preset_change_refreshes_open_window_and_never_changes_saved_effects(self):
        panel, _ = self.panel(); window = self.window(panel)
        combo = panel.caption_properties.controls['caption_preset']
        combo.setCurrentIndex(combo.findData('appear')); self.app.processEvents(); self.app.processEvents()
        self.assertIn(combo.currentText(), window.title.text())
        self.assertNotIn(('effect', 'floating_amplitude'), self.items(window))
        before = copy.deepcopy(panel.current()); window.refresh()
        self.assertEqual(panel.current(), before)

    def test_equivalent_color_spellings_and_float_noise_are_not_false_changes(self):
        self.assertTrue(same_value('color_high', '#FFFFFF', ['#ffffff']))
        self.assertTrue(same_value('outline_opacity', 35, 34.99999999999999))


if __name__ == '__main__': unittest.main()

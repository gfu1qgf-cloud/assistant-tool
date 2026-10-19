import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import componentize_layers, normalize_layers
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store
from app_plugins.builtin.batch_text_video.preview import Preview
from app_plugins.builtin.batch_text_video.preview_guides import snapped_geometry, aligned_geometry
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class InspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close(); widget.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def panel(self):
        return LayerPanel(componentize_layers(None,DEFAULTS),'.',settings=DEFAULTS)

    def image(self):
        return {'id':'picture','kind':'image','name':'测试图片','enabled':True,'opacity':100,
            'source':'fixed','path':'example.png','x':31,'y':44,'width':40,'height':30}

    def test_component_panels_are_lazy_cached_and_loading_is_read_only(self):
        panel = self.panel()
        self.assertEqual(len(panel.inspector.editors),1)
        seen=[]; panel.changed.connect(lambda:seen.append(1))
        panel.select_layer_id('title'); first=panel.editor
        panel.select_layer_id('body'); panel.select_layer_id('title')
        self.assertIs(first,panel.editor)
        self.assertEqual(seen,[])
        self.assertNotIn('image',panel.inspector.editors)

    def test_incompatible_live_class_gets_restart_notice_not_mixed_api(self):
        from app_plugins.builtin.batch_text_video.component_inspector import require_editor_api
        with self.assertRaisesRegex(RuntimeError,'重启'): require_editor_api(type('OldPanel',(),{}))
        require_editor_api(LayerPanel)
        with patch.dict('sys.modules',{'app_plugins.builtin.batch_text_video.visual_media':object()}):
            with self.assertRaisesRegex(RuntimeError,'重启'): require_editor_api(LayerPanel)

    def test_text_color_is_in_the_font_group_not_another_tab(self):
        panel=self.panel(); panel.select_layer_id('title')
        editor=panel.text_properties
        self.assertTrue(editor.style_sections.sections['font'].content.isAncestorOf(editor.controls['color']))
        self.assertEqual(set(editor.style_sections.sections),{'font','outline','shadow','background'})
        self.assertEqual(editor.findChildren(QtWidgets.QTabWidget),[])

    def test_hidden_component_cannot_overwrite_another_component(self):
        panel=self.panel(); panel.select_layer_id('title'); editor=panel.text_properties
        panel.select_layer_id('body'); before=copy.deepcopy(panel.current())
        editor.changed.emit({'color':'#ff0055','x':11})
        self.assertEqual(panel.current(),before)

    def test_position_survives_reselection_and_rebuild(self):
        panel=LayerPanel([self.image(),*componentize_layers(None,DEFAULTS)],'.',settings=DEFAULTS)
        panel.select_layer_id('picture')
        panel.properties['x'].setValue(64); panel.properties['y'].setValue(26)
        panel.select_layer_id('title'); panel.select_layer_id('picture'); panel.rebuild('picture')
        self.assertEqual((panel.current()['x'],panel.current()['y']),(64,26))
        self.assertEqual((panel.properties['x'].value(),panel.properties['y'].value()),(64,26))

    def test_all_component_types_supply_their_own_editor(self):
        from app_plugins.builtin.batch_text_video.captions import new_caption
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark
        from app_plugins.builtin.batch_text_video.node_effects import new_effect
        layers=[self.image(),new_caption(),new_watermark(),new_effect(),new_text_component()]
        panel=LayerPanel(layers,'.',settings=DEFAULTS)
        for layer in panel.values():
            panel.select_layer_id(layer['id'])
            self.assertTrue(panel.editor.sections or panel.editor.targets)
            self.assertEqual(panel.editor.layer['id'],layer['id'])
        self.assertEqual(set(panel.inspector.editors),{item['kind'] for item in panel.values()})

    def test_flat_geometry_is_revealable_across_selection(self):
        panel=self.panel(); panel.select_layer_id('title')
        panel.reveal_property('position')
        panel.select_layer_id('body'); panel.select_layer_id('title')
        self.assertIs(panel.editor.targets['position'],panel.editor.geometry_editor)
        self.assertNotIn('position',panel.editor.sections)

    def test_geometry_unit_switch_does_not_move_or_change_anchors(self):
        panel=self.panel(); panel.select_layer_id('title'); before=copy.deepcopy(panel.current())
        panel.units.setCurrentIndex(panel.units.findData('pixels'))
        self.assertEqual(panel.current(),before)
        self.assertEqual(panel.editor.geometry_editor.units.currentData(),'pixels')
        panel.editor.geometry_editor.units.setCurrentIndex(0)
        self.assertEqual(panel.units.currentData(),'percent')
        self.assertEqual(panel.current(),before)

    def test_right_inspector_left_lower_copy_and_no_horizontal_scroll(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(BatchTextVideoDialog,'request_background'),\
                patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=Store(directory)); dialog.resize(1400,950); dialog.show()
            dialog.tabs.setCurrentIndex(dialog.layer_tab_index)
            self.app.processEvents()
            self.assertIs(dialog.left_column.widget(1),dialog.task_text_panel)
            self.assertIs(dialog.splitter.widget(2),dialog.right_tabs)
            self.assertTrue(dialog.task_text_panel.isAncestorOf(dialog.body))
            self.assertTrue(dialog.right_tabs.isAncestorOf(dialog.layer_panel.inspector))
            self.assertTrue(dialog.tabs.isAncestorOf(dialog.layer_panel.list))
            self.assertEqual(dialog.layer_scroll.horizontalScrollBar().maximum(),0)
            for identifier in ('title','body'):
                dialog.layer_panel.select_layer_id(identifier)
                for section in dialog.layer_panel.editor.sections.values(): section.set_expanded(True)
                self.app.processEvents()
                self.assertEqual(dialog.layer_scroll.horizontalScrollBar().maximum(),0)
            if os.environ.get('LZX_INSPECTOR_SCREENSHOT'):
                dialog.title.setEnabled(True); dialog.body.setEnabled(True); dialog.name.setEnabled(True)
                dialog.title.setPlainText('Nádej prináša svetlo')
                dialog.body.setPlainText('Každý nový deň je novou príležitosťou.\nZastav sa na chvíľu a nájdi pokoj.')
                dialog.name.setText('1015-reels1000')
                for key,section in dialog.layer_panel.editor.sections.items():
                    section.set_expanded(key in {'source','font'})
                background=QtGui.QImage(540,960,QtGui.QImage.Format.Format_ARGB32)
                painter=QtGui.QPainter(background)
                gradient=QtGui.QLinearGradient(0,0,540,960)
                gradient.setColorAt(0,QtGui.QColor('#446db1')); gradient.setColorAt(1,QtGui.QColor('#132d4f'))
                painter.fillRect(background.rect(),gradient)
                painter.setPen(QtCore.Qt.PenStyle.NoPen); painter.setBrush(QtGui.QColor('#7899bf'))
                painter.drawEllipse(QtCore.QPointF(380,210),110,110); painter.end()
                dialog.preview.background=background
                from app_plugins.builtin.batch_text_video.layout import render_preview_overlay
                dialog.preview.overlay,_=render_preview_overlay(dialog.title.toPlainText(),dialog.body.toPlainText(),
                    1080,1920,dialog.read_settings())
                dialog.layer_panel.reveal_property('font'); self.app.processEvents()
                self.assertTrue(dialog.grab().save(os.environ['LZX_INSPECTOR_SCREENSHOT']))
            dialog.close()

    def test_nodes_reuse_component_editors_without_hardcoded_tabs(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import new_layer
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        results=[]
        editor=LayerNodeEditor({'layer':new_layer('text')},results.append)
        self.assertEqual(editor.panel.editor.mode,'node_layer')
        editor.panel.text_properties.controls['font_max'].setValue(72)
        self.assertEqual(results[-1]['layer']['font_max'],72)
        caption=CaptionNodeEditor({},results.append)
        self.assertEqual(caption.panel.editor.mode,'node_caption')
        self.assertNotIn('source',caption.panel.editor.sections)


class CanvasAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self):
        return dict(id='one',name='图片',kind='image',enabled=True,opacity=100,x=49.2,y=35,width=20,height=20)

    def test_snapping_uses_display_pixels_and_does_not_modify_inputs(self):
        layer=self.layer(); before=copy.deepcopy(layer)
        value,guides=snapped_geometry(layer,[layer],'one',(400,700))
        self.assertEqual(value['x'],50); self.assertTrue(guides); self.assertEqual(layer,before)
        value,_=snapped_geometry(layer,[layer],'one',(2000,2000))
        self.assertEqual(value['x'],49.2)

    def test_snap_to_other_layer_edges_and_center(self):
        layer={**self.layer(),'x':34.7,'y':17}
        other={**self.layer(),'id':'two','x':45,'y':60,'width':20}
        value,guides=snapped_geometry(layer,[layer,other],'one',(500,500))
        self.assertEqual(value['x'],35)
        self.assertEqual(guides[0][1],35)

    def test_resize_only_snaps_moving_edge(self):
        layer={**self.layer(),'x':39.6,'width':20,'y':30}
        value,_=snapped_geometry(layer,[layer],'one',(500,500),handle=(1,0))
        self.assertAlmostEqual(value['x']-value['width']/2,29.6)
        self.assertEqual(value['x']+value['width']/2,50)
        self.assertEqual(value['y'],30)

    def test_context_alignment_emits_one_commit_and_keeps_size(self):
        widget=Preview(); widget.resize(400,700); layer=self.layer(); widget.set_layers([layer],'one')
        edits=[]; commits=[]
        widget.geometryEdited.connect(lambda i,v:edits.append((i,v)))
        widget.geometryCommitted.connect(lambda:commits.append(1))
        menu=widget.create_context_menu(widget.layer_rect(layer).center())
        align=next(a.menu() for a in menu.actions() if a.text()=='对齐到画布')
        next(a for a in align.actions() if a.text()=='画布正中').trigger()
        self.assertEqual(len(edits),1); self.assertEqual(len(commits),1)
        self.assertEqual((edits[0][1]['x'],edits[0][1]['y']),(50,50))
        self.assertEqual(edits[0][1]['width'],20)
        menu.deleteLater(); widget.deleteLater()

    def test_oversize_alignment_is_not_silently_clamped(self):
        self.assertIsNone(aligned_geometry({**self.layer(),'width':300},'left'))
        self.assertEqual(aligned_geometry({**self.layer(),'width':300},'center')['x'],50)

    def test_drag_snaps_with_guide_and_alt_bypasses_it(self):
        for modifiers,expected in ((QtCore.Qt.KeyboardModifier.NoModifier,50),
                (QtCore.Qt.KeyboardModifier.AltModifier,49.5)):
            widget=Preview(); widget.resize(500,800)
            layer={**self.layer(),'x':45,'y':35}; widget.set_layers([layer],layer['id'])
            start=widget.layer_rect(layer).center()
            end=start+QtCore.QPointF(widget.canvas_rect().width()*.045,0)
            def mouse(kind,position,button):
                return QtGui.QMouseEvent(kind,position,button,QtCore.Qt.MouseButton.LeftButton,modifiers)
            widget.mousePressEvent(mouse(QtCore.QEvent.Type.MouseButtonPress,start,QtCore.Qt.MouseButton.LeftButton))
            widget.mouseMoveEvent(mouse(QtCore.QEvent.Type.MouseMove,end,QtCore.Qt.MouseButton.NoButton))
            self.assertAlmostEqual(widget.layers[0]['x'],expected)
            self.assertEqual(bool(widget._alignment_guides),not bool(modifiers))
            widget.mouseReleaseEvent(mouse(QtCore.QEvent.Type.MouseButtonRelease,end,QtCore.Qt.MouseButton.LeftButton))
            self.assertEqual(widget._alignment_guides,[])
            widget.deleteLater()

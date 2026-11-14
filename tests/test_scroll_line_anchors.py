import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.scrolling_text import (
    new_scrolling_component,normalize_scroll,scroll_endpoints,scroll_timing,scroll_position,
    render_scroll_assets,render_scroll_preview,prepare_scroll_plane)
from app_plugins.builtin.batch_text_video.scrolling_text_ui import ScrollTextProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import Store,fresh_state


def component(**values):
    layer=new_scrolling_component();layer.update(text='First line\nSecond line\nLast line',font='Open Sans',font_max=72,
        outline_width=0,shadow_enabled=False,padding=4,width=80,height=60,scroll_edge_fade=0,
        scroll_start_anchor='first_center',scroll_end_anchor='last_center')
    layer.update(values);return normalize_layers([layer])[0]


class LineAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_start_and_end_choices_use_different_reference_lines(self):
        ui=ScrollTextProperties();self.addCleanup(ui.deleteLater);ui.set_layer(component())
        self.assertEqual([ui.start_anchor.itemText(i) for i in range(ui.start_anchor.count())],['首行居中','首行贴顶','首行贴底','从框外进入'])
        self.assertEqual([ui.end_anchor.itemText(i) for i in range(ui.end_anchor.count())],['尾行居中','尾行贴顶','尾行贴底','滚出框外'])
        labels=[v.text() for v in ui.findChildren(QtWidgets.QLabel)]
        self.assertIn('起始偏移',labels);self.assertIn('结束偏移',labels);self.assertNotIn('提前结束',labels)
        if os.environ.get('LZX_SCROLL_LINE_QA'):
            ui.resize(640,400);ui.show();self.app.processEvents()
            ui.grab().save(str(Path(os.environ['LZX_SCROLL_LINE_QA'])/'scroll-line-controls.png'));ui.hide()

    def test_all_nine_combinations_align_actual_first_and_last_rows_in_both_directions(self):
        for direction in ('up','down'):
            layer=component(text_animation=direction);assets=render_scroll_assets(layer,360,640)
            first,last=assets['line_bounds'];height=assets['height']
            for start in ('top','center','bottom'):
                for end in ('top','center','bottom'):
                    layer.update(scroll_start_anchor='first_'+start,scroll_end_anchor='last_'+end)
                    a,b=scroll_endpoints(layer,height,assets['text_height'],line_bounds=assets['line_bounds'])
                    self.assertAlmostEqual(a+{'top':first[0],'center':sum(first)/2,'bottom':first[1]}[start],{'top':0,'center':height/2,'bottom':height}[start])
                    self.assertAlmostEqual(b+{'top':last[0],'center':sum(last)/2,'bottom':last[1]}[end],{'top':0,'center':height/2,'bottom':height}[end])

    def test_reference_rows_follow_automatic_wrap_not_just_explicit_newlines(self):
        layer=component(text=('A long paragraph with automatic wrapping and distinct words. '*6),width=30,font_max=100)
        assets=render_scroll_assets(layer,360,640)
        self.assertGreater(assets['line_bounds'][1][0],assets['line_bounds'][0][1])
        first,last=scroll_endpoints(layer,assets['height'],assets['text_height'],line_bounds=assets['line_bounds'])
        self.assertGreater(first,last)
        self.assertNotAlmostEqual(first,(assets['height']-assets['text_height'])/2)

    def test_line_spacing_paragraph_gaps_and_blank_paragraphs_do_not_become_fake_tail_rows(self):
        layer=component(text='\nFirst\n\nLast\n\n',line_spacing=1.8,paragraph_spacing=18)
        assets=render_scroll_assets(layer,360,640)
        first,last=assets['line_bounds'];self.assertGreater(first[0],0)
        self.assertLess(last[1],assets['text_height']);self.assertGreater(last[0],first[1])
        layer['scroll_end_anchor']='last_bottom'
        _a,b=scroll_endpoints(layer,assets['height'],assets['text_height'],line_bounds=assets['line_bounds'])
        self.assertAlmostEqual(b+last[1],assets['height'])

    def test_tail_means_last_reading_line_even_with_different_font_metrics(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.scrolling_text import rendered_line_bounds
        lines=[SimpleNamespace(y=lambda:0,height=lambda:40),SimpleNamespace(y=lambda:30,height=lambda:5)]
        layout=SimpleNamespace(lineCount=lambda:2,lineAt=lambda i:lines[i])
        self.assertEqual(rendered_line_bounds([(layout,0)],2,50),((0,44),(30,39)))

    def test_effect_margin_and_background_bleed_do_not_change_reference_system(self):
        layer=component(outline_width=8,shadow_enabled=True,shadow_blur=3,shadow_y=4,
            background_enabled=True,background_mode='text',background_extra_top=23,background_extra_bottom=17)
        assets=render_scroll_assets(layer,360,640)
        self.assertGreater(assets['strip_top'],0)
        with tempfile.TemporaryDirectory() as root:
            prepared=prepare_scroll_plane(layer,360,640,8,root)
        self.assertEqual(prepared['line_bounds'],assets['line_bounds'])
        expected=scroll_endpoints(layer,assets['height'],assets['text_height'],line_bounds=assets['line_bounds'])
        self.assertEqual(prepared['timing'][2:],expected)

    def test_signed_time_fields_use_forward_positive_and_backward_negative(self):
        ui=ScrollTextProperties();self.addCleanup(ui.deleteLater)
        original=component(scroll_start_delay=-1.5,scroll_end_early=2);ui.set_layer(original)
        self.assertEqual(ui.controls['scroll_start_delay'].value(),-1.5)
        self.assertEqual(ui.controls['scroll_end_early'].value(),-2)
        self.assertLess(ui.controls['scroll_end_early'].minimum(),0)
        values=[];ui.changed.connect(values.append)
        ui.controls['scroll_end_early'].setValue(1.25)
        self.assertEqual(values[-1]['scroll_end_early'],-1.25)
        reloaded={**original,**values[-1]};ui.set_layer(reloaded)
        self.assertEqual(ui.controls['scroll_end_early'].value(),1.25)
        self.assertEqual(original['scroll_end_early'],2)

    def test_negative_start_advances_first_frame_positive_end_has_not_arrived_at_last(self):
        layer=component(scroll_start_delay=-2,scroll_end_early=-2)
        bounds=((0,20),(180,200))
        a,b=scroll_endpoints(layer,100,200,line_bounds=bounds)
        start,finish,*_=scroll_timing(layer,10,100,200,line_bounds=bounds)
        self.assertEqual((start,finish),(-2,12))
        at_zero=scroll_position(layer,10,100,200,0,line_bounds=bounds)
        at_end=scroll_position(layer,10,100,200,10,line_bounds=bounds)
        self.assertAlmostEqual(at_zero,a+(b-a)*2/14);self.assertNotEqual(at_zero,a)
        self.assertAlmostEqual(at_end,a+(b-a)*12/14);self.assertNotEqual(at_end,b)

    def test_early_finish_and_late_start_hold_and_outside_window_values_are_not_clamped(self):
        layer=component(scroll_start_delay=2,scroll_end_early=2);bounds=((0,20),(180,200))
        position=lambda t:scroll_position(layer,10,100,200,t,line_bounds=bounds)
        self.assertEqual(position(0),position(2));self.assertEqual(position(8),position(10))
        layer.update(scroll_start_delay=-10,scroll_end_early=12)
        self.assertEqual(scroll_timing(layer,10,100,200,line_bounds=bounds)[:2],(-10,-2))
        self.assertEqual(position(0),position(10))
        layer.update(scroll_start_delay=11,scroll_end_early=-12)
        self.assertEqual(position(0),position(10))

    def test_invalid_reversed_or_nonfinite_windows_fail_without_silently_adjusting(self):
        layer=component(scroll_start_delay=9,scroll_end_early=2)
        with self.assertRaisesRegex(ValueError,'起始偏移'):scroll_timing(layer,10,100,200,line_bounds=((0,20),(180,200)))
        for key in ('scroll_start_delay','scroll_end_early'):
            with self.assertRaises(ValueError):normalize_scroll({**layer,key:float('inf')})

    def test_old_whole_block_anchors_and_positive_early_end_keep_exact_numeric_behavior(self):
        for anchor,y in (('top',0),('center',-50),('bottom',-100)):
            layer=component(scroll_start_anchor=anchor,scroll_end_anchor=anchor,scroll_start_delay=2,scroll_end_early=3)
            self.assertEqual(scroll_timing(layer,10,100,200),(2,7,y,y))
            ui=ScrollTextProperties();self.addCleanup(ui.deleteLater);ui.set_layer(layer)
            self.assertEqual(ui.start_anchor.currentData(),anchor);self.assertIn('旧版',ui.start_anchor.currentText())
            self.assertEqual(ui.controls['scroll_end_early'].value(),-3)

    def test_negative_fields_save_reload_and_existing_undo_without_sign_flipping(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=Store(root));self.addCleanup(dialog.deleteLater)
            try:
                dialog.layer_panel.add_scrolling_text();dialog.persist();identifier=dialog.layer_panel.current()['id']
                spin=dialog.layer_panel.scroll_properties.controls['scroll_start_delay'];spin.setValue(-2);dialog.persist()
                self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()['scroll_start_delay'],0)
                self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()['scroll_start_delay'],-2)
                dialog.layer_panel.scroll_properties.controls['scroll_end_early'].setValue(-1);dialog.persist()
                saved=next(v for v in dialog.store.load()['settings']['layers'] if v['id']==identifier)
                self.assertEqual(saved['scroll_end_early'],1)
            finally:dialog.close();self.app.processEvents()

    def test_vertical_text_has_real_top_and_bottom_row_bounds(self):
        layer=component(text='天地玄黄\n宇宙',writing_mode='vertical_rl')
        assets=render_scroll_assets(layer,360,640)
        self.assertGreater(assets['line_bounds'][1][0],assets['line_bounds'][0][0])
        image,_=render_scroll_preview(layer,360,640,duration=4,time=3)
        self.assertFalse(image.isNull())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'explicit FFmpeg required')
    def test_real_export_agrees_with_preview_at_signed_windows_for_both_directions(self):
        import test_batch_scrolling_text as helper_module
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        helper=helper_module.RealScrollTextTests()
        for direction,start,end in [('up',-1,-1),('down',-.5,1)]:
            with self.subTest(direction=direction),tempfile.TemporaryDirectory() as root:
                layer=component(text_animation=direction,scroll_start_delay=start,scroll_end_early=end,font_max=100,
                    scroll_start_anchor='first_center',scroll_end_anchor='last_center')
                state,job=helper.state(root,layer,duration=4)
                output,_=render_static_text(job,state,root,preview=True)
                for t in (0.,1.,3.5):
                    actual=helper.frame(output,t)
                    expected,_=render_scroll_preview(layer,240,160,duration=4,time=t)
                    from qt_compat import QtGui
                    # Compare text on the decoded background colour, rather than
                    # attributing RGB/YUV conversion of the source to scrolling.
                    background=QtGui.QImage(240,160,QtGui.QImage.Format.Format_ARGB32);background.fill(actual.pixelColor(0,0))
                    painter=QtGui.QPainter(background);painter.drawImage(0,0,expected);painter.end()
                    if os.environ.get('LZX_SCROLL_LINE_QA'):
                        root_path=Path(os.environ['LZX_SCROLL_LINE_QA'])
                        actual.save(str(root_path/f'{direction}-{t}-actual.png'))
                        background.save(str(root_path/f'{direction}-{t}-expected.png'))
                    for axis in (0,1):
                        a=[p[axis] for p in helper.whites(actual)];b=[p[axis] for p in helper.whites(background)]
                        self.assertTrue(a and b,(direction,t))
                        self.assertLessEqual(abs(min(a)-min(b)),2,(direction,t))
                        self.assertLessEqual(abs(max(a)-max(b)),2,(direction,t))
                    # Qt and FFmpeg round fractional drawing positions differently.
                    aligned=[]
                    for offset in (-1,0,1):
                        shifted=QtGui.QImage(240,160,QtGui.QImage.Format.Format_ARGB32);shifted.fill(actual.pixelColor(0,0))
                        painter=QtGui.QPainter(shifted);painter.drawImage(0,offset,expected);painter.end()
                        aligned.append(helper.image_difference(actual,shifted))
                    self.assertLess(min(aligned),1.5,(direction,t))

import copy
import os
from pathlib import Path
from unittest.mock import patch
import test_caption_cursor as original
from app_plugins.builtin.batch_text_video.caption_cursor import DEFAULTS,image_enabled
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets


class AdjustableCursorTests(original.CursorTests):
    # Run the original real-FFmpeg cursor, follow, node, undo and cleanup
    # regressions with thick/tall/translucent settings as well as defaults.
    def layer(self,**changes):
        return super().layer(**{'caption_cursor_width':18.,'caption_cursor_height':160.,
            'caption_cursor_opacity':65.,**changes})

    def test_old_projects_get_previous_bar_defaults_without_source_mutation(self):
        layer=self.layer()
        for key in ('caption_cursor_width','caption_cursor_height','caption_cursor_opacity'):layer.pop(key)
        before=copy.deepcopy(layer);value=normalize_layers([layer])[0]
        self.assertEqual(layer,before)
        self.assertEqual([value[k] for k in ('caption_cursor_width','caption_cursor_height','caption_cursor_opacity')],[2.,100.,100.])

    def test_width_height_centre_and_independent_opacity_use_shared_geometry(self):
        layer=self.layer(caption_active_opacity=40)
        primitives,events=make_primitives(self.words(2),layer,540,960,1)
        cursor=next(p for p in primitives if p['z']==2)
        event=events[0]
        self.assertAlmostEqual(cursor['width'],9.)
        self.assertAlmostEqual(cursor['height'],event['height']*1.6)
        from app_plugins.builtin.batch_text_video.caption_style_render import motion_at
        _size,_dx,dy,_alpha,_blur=motion_at(cursor,event['start'])
        from app_plugins.builtin.batch_text_video.caption_follow import offset_at
        follow_y=offset_at(cursor['follow_track'],event['start'])[1]
        self.assertAlmostEqual(cursor['y']+dy+cursor['height']/2,event['y']+follow_y+event['height']/2)
        self.assertAlmostEqual(cursor['opacity'],.4*.65)

    def test_bar_ui_fields_and_parameter_overview_are_editable_and_mode_specific(self):
        panel=CaptionProperties();self.addCleanup(panel.deleteLater);panel.set_layer(self.layer())
        values=[];panel.changed.connect(values.append)
        panel.controls['caption_cursor_width'].setValue(12.5)
        panel.controls['caption_cursor_height'].setValue(130)
        panel.controls['caption_cursor_opacity'].setValue(42)
        self.assertEqual([values[-1][key] for key in ('caption_cursor_width','caption_cursor_height','caption_cursor_opacity')],[12.5,130,42])
        for key in ('caption_cursor_width','caption_cursor_height','caption_cursor_opacity'):
            self.assertIn(('layer',key),parameter_targets(panel))
        from app_plugins.builtin.batch_text_video.caption_parameter_values import describe
        for key,unit in (('caption_cursor_width','px'),('caption_cursor_height','%'),('caption_cursor_opacity','%')):
            info=describe(panel.controls[key],values[-1][key],key)
            self.assertEqual(info['unit'],unit)
        panel.controls['caption_cursor_mode'].setCurrentIndex(panel.controls['caption_cursor_mode'].findData('image'))
        self.assertTrue(panel.controls['caption_cursor_width'].isHidden())
        self.assertTrue(panel.controls['caption_cursor_height'].isHidden())
        self.assertFalse(panel.controls['caption_cursor_opacity'].isHidden())
        panel.controls['caption_cursor_mode'].setCurrentIndex(panel.controls['caption_cursor_mode'].findData('bar'))
        self.assertEqual(panel.controls['caption_cursor_width'].value(),12.5)
        self.assertEqual(panel.controls['caption_cursor_height'].value(),130)
        panel.show();panel.sections.sections['caption'].set_expanded(True);self.app.processEvents()
        root=os.environ.get('LZX_CURSOR_QA')
        if root:panel.grab().save(str(Path(root)/'bar-parameters.png'))
        panel.close()

    def test_invalid_dimensions_are_rejected_and_zero_opacity_hides_both_modes(self):
        for field,value in (('caption_cursor_width',0),('caption_cursor_width',101),('caption_cursor_height',0),
                ('caption_cursor_height',301),('caption_cursor_opacity',101),('caption_cursor_opacity',True),('caption_cursor_width',float('nan'))):
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):self.layer(**{field:value})
        for mode in ('bar','image'):
            layer=self.layer(caption_cursor_mode=mode,caption_cursor_image='missing.png',caption_cursor_opacity=0)
            self.assertFalse(image_enabled(layer))
            with patch('app_plugins.builtin.batch_text_video.caption_cursor.read_image',side_effect=AssertionError('Hidden cursor image read')):
                values,_=make_primitives(self.words(2),layer,540,960,1)
            self.assertFalse(any(p['z']==2 for p in values))

    def test_bar_thickness_survives_project_save_undo_redo(self):
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        state=fresh_state();state['settings']['layers']=[self.layer(id='bar-test')]
        store=Store(self.root/'bar-state');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'):dialog=BatchTextVideoDialog(store=store)
        try:
            dialog.layer_panel.select_layer_id('bar-test');editor=dialog.layer_panel.caption_properties
            editor.controls['caption_cursor_width'].setValue(11)
            dialog.persist();self.assertEqual(dialog.layer_panel.current()['caption_cursor_width'],11)
            self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()['caption_cursor_width'],18)
            self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()['caption_cursor_width'],11)
        finally:dialog.edit_timer.stop();dialog.close();dialog.deleteLater();self.app.processEvents()

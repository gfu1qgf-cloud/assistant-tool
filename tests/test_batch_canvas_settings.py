import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import canvas_settings as canvas
from app_plugins.builtin.batch_text_video.canvas_ui import CanvasDialog, apply_settings, reference_text
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, PreviewWorker
from app_plugins.builtin.batch_text_video import engine

PREFIX='app_plugins.builtin.batch_text_video.'
FFMPEG=os.environ.get('NODE_FFMPEG','')


class CanvasPolicyTests(unittest.TestCase):
    def test_presets_and_legacy_sizes(self):
        for key,_,width,height in canvas.PRESETS:
            self.assertEqual(engine.output_size({'aspect':key}),(width,height))
            self.assertEqual(engine.output_size({'aspect':key},dict(video=dict(width=333,height=777))),(width,height))
        self.assertEqual(engine.output_size({}), (1080,1920))
        self.assertEqual(engine.output_size({'aspect':'source'}), (1080,1920))
        self.assertEqual(engine.output_size({'aspect':'source'},dict(video=dict(width=641,height=481,tags={'rotate':90}))),(480,640))

    def test_custom_even_dimensions_and_strict_invalid_values(self):
        self.assertEqual(engine.output_size(dict(aspect='custom',canvas_width=3840,canvas_height=2160)),(3840,2160))
        for width,height in ((True,1920),(1080.0,1920),(1081,1920),(0,1920),(8194,64),(8192,8192)):
            with self.subTest(width=width,height=height),self.assertRaises(ValueError):
                canvas.normalized(dict(aspect='custom',canvas_width=width,canvas_height=height))
        with self.assertRaises(ValueError):canvas.normalized({'aspect':'unknown'})

    def test_empty_background_uses_selected_canvas_and_never_overlay_size(self):
        from app_plugins.builtin.batch_text_video.image_background import probe_background
        for aspect,width,height in (('custom',640,480),('square',1080,1080),('source',1080,1920)):
            settings=fresh_state()['settings'];settings.update(aspect=aspect,canvas_width=640,canvas_height=480)
            settings['layers']=[dict(kind='image',path='large-4096x4096.png')]
            media=probe_background('', 'unused', settings)
            self.assertTrue(media['empty_background'])
            self.assertEqual(engine.output_size(settings,media),(width,height))

    def test_follow_source_uses_per_job_assigned_background(self):
        from app_plugins.builtin.batch_text_video.background_visual import background_job
        state=fresh_state();state['backgrounds']=['wide.mp4','tall.mp4']
        state['settings'].update(aspect='source',background_pool_id='builtin:background')
        state['jobs']=[dict(id='a',path='old.mp4'),dict(id='b',path='old.mp4')]
        metadata={'wide.mp4':dict(video=dict(width=1920,height=1080)), 'tall.mp4':dict(video=dict(width=1080,height=1920))}
        for job,expected in zip(state['jobs'],((1920,1080),(1080,1920))):
            actual=background_job(job,state)
            self.assertEqual(engine.output_size(state['settings'],metadata[actual['path']]),expected)

    def test_old_projects_migrate_and_custom_canvas_is_project_owned(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);state=fresh_state()
            state['settings'].pop('canvas_width');state['settings'].pop('canvas_height')
            state=store.save(state,0)
            self.assertEqual(canvas.normalized(state['settings']),canvas.DEFAULTS)
            state['settings'].update(aspect='custom',canvas_width=640,canvas_height=480)
            state=store.save(state,state['revision']);state=store.create_profile(state,'other')
            self.assertEqual(engine.output_size(state['settings']),(1080,1920))
            state=store.select_profile(state,'default')
            self.assertEqual(engine.output_size(state['settings']),(640,480))
            self.assertEqual(engine.output_size(store.profile_state('default')['settings']),(640,480))


class CanvasUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.poster=patch.object(BatchTextVideoDialog,'request_background');self.poster.start();self.addCleanup(self.poster.stop)
        self.overlay=patch.object(BatchTextVideoDialog,'_start_overlay');self.overlay.start();self.addCleanup(self.overlay.stop)
        self.dialog=BatchTextVideoDialog(store=Store(self.temp.name));self.addCleanup(self.close_editor)

    def close_editor(self):
        self.dialog.close();self.dialog.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_menu_and_project_button_open_same_dialog_cancel_keeps_state(self):
        before=copy.deepcopy(self.dialog.state)
        self.assertEqual(self.dialog.settings_menu.title(),'设置')
        self.assertEqual(self.dialog.canvas_settings_action.text(),'画布比例与尺寸…')
        with patch(PREFIX+'canvas_ui.CanvasDialog') as modal:
            modal.return_value.exec.return_value=QtWidgets.QDialog.DialogCode.Rejected
            self.dialog.canvas_settings_action.trigger();self.dialog.canvas_edit_button.click()
            self.assertEqual(modal.call_count,2)
        self.assertEqual(self.dialog.state,before)

    def test_apply_is_one_undo_step_and_survives_reload(self):
        before=copy.deepcopy(self.dialog.read_settings()['layers'])
        self.assertTrue(apply_settings(self.dialog,dict(aspect='custom',canvas_width=640,canvas_height=480)))
        self.assertEqual(self.dialog.preview.canvas_size,QtCore.QSize(640,480))
        self.assertEqual(self.dialog.layer_panel.canvas_size,QtCore.QSize(640,480))
        self.assertIn('640×480',self.dialog.canvas_edit_button.text())
        self.assertEqual(len(self.dialog.edit_history.past),1)
        self.assertEqual(self.dialog.store.load()['settings']['layers'],before)
        self.assertTrue(self.dialog.undo_edit())
        self.assertEqual(self.dialog.preview.canvas_size,QtCore.QSize(1080,1920))
        self.assertTrue(self.dialog.redo_edit())
        self.assertEqual(engine.output_size(self.dialog.store.load()['settings']),(640,480))

    def test_failed_save_does_not_change_widgets_or_history(self):
        old=self.dialog._editor_snapshot();history=len(self.dialog.edit_history.past)
        with patch.object(self.dialog.store,'save',side_effect=ValueError('conflict')):
            self.assertFalse(apply_settings(self.dialog,dict(aspect='square',canvas_width=1080,canvas_height=1920)))
        self.assertEqual(self.dialog._editor_snapshot(),old)
        self.assertEqual(len(self.dialog.edit_history.past),history)

    def test_busy_project_cannot_change_canvas(self):
        with patch.object(self.dialog,'is_batch_busy',return_value=True):
            self.assertFalse(apply_settings(self.dialog,dict(aspect='square')))
        self.assertEqual(self.dialog.controls['aspect'].currentData(),'portrait')

    def test_remove_background_does_not_reuse_previous_media_dimensions(self):
        self.dialog.controls['aspect'].setCurrentIndex(self.dialog.controls['aspect'].findData('source'))
        self.dialog._media=dict(video=dict(width=640,height=480),duration=1)
        self.dialog._preview_path=None
        with patch.object(self.dialog,'active_entry',return_value=dict(path='')):
            self.dialog.update_preview()
            self.assertEqual(self.dialog.preview.canvas_size,QtCore.QSize(1080,1920))
        self.assertTrue(apply_settings(self.dialog,dict(aspect='custom',canvas_width=480,canvas_height=640)))
        self.assertEqual(self.dialog.preview.canvas_size,QtCore.QSize(480,640))

    def test_reference_names_bottom_source_and_waits_for_matching_metadata(self):
        self.dialog._media=dict(video=dict(width=1920,height=1080))
        self.dialog._preview_path=('old.mp4',)
        with patch.object(self.dialog,'active_entry',return_value=dict(path='new.mp4')):
            text=reference_text(self.dialog)
            self.assertIn('new.mp4',text);self.assertIn('尚未读取',text);self.assertNotIn('1920×1080',text)
            self.dialog._preview_path=('new.mp4',)
            text=reference_text(self.dialog);self.assertIn('1920×1080',text)
            self.dialog._media={'video':{'width':0,'height':0}}
            self.assertIn('尺寸信息无效',reference_text(self.dialog))
            self.dialog._background_error='读取失败'
            self.assertIn('尺寸暂不可用',reference_text(self.dialog))
        with patch.object(self.dialog,'active_entry',return_value=dict(path='')):
            self.assertIn('无底层背景',reference_text(self.dialog))
            self.assertIn('默认1080×1920',reference_text(self.dialog))

    def test_custom_dialog_validation_swap_and_reference_explanation(self):
        dialog=CanvasDialog(self.dialog.read_settings(),self.dialog)
        try:
            dialog.aspect.setCurrentIndex(dialog.aspect.findData('custom'))
            dialog.width_spin.setValue(640);dialog.height_spin.setValue(480);dialog.swap_button.click()
            self.assertEqual(dialog.values()['canvas_width'],480);self.assertEqual(dialog.values()['canvas_height'],640)
            dialog.width_spin.setValue(641)
            self.assertFalse(dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled())
            dialog.aspect.setCurrentIndex(dialog.aspect.findData('source'))
            self.assertTrue(dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).isEnabled())
            self.assertIn('底层背景',dialog.reference_label.text());self.assertIn('批量导出',dialog.reference_label.text())
            screenshot=os.environ.get('LZX_CANVAS_SCREENSHOT')
            if screenshot:
                dialog.show();self.app.processEvents();dialog.grab().save(screenshot)
                dialog.aspect.setCurrentIndex(dialog.aspect.findData('custom'))
                dialog.width_spin.setValue(640);dialog.height_spin.setValue(480)
                self.app.processEvents();dialog.grab().save(str(Path(screenshot).with_name('canvas-custom-settings.png')))
        finally:dialog.close();dialog.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_custom_resize_rejects_stale_poster_with_same_aspect(self):
        self.assertTrue(apply_settings(self.dialog,dict(aspect='custom',canvas_width=640,canvas_height=480)))
        self.dialog._media=None
        old=dict(path='',voice_path='',configured='',aspect='custom',fit='contain',blur=0,
                 canvas_width=320,canvas_height=320,image=b'',media=dict(video=dict(width=320,height=320)))
        self.dialog._background_ready(old)
        self.assertIsNone(self.dialog._media)


@unittest.skipUnless(FFMPEG,'Real FFmpeg required')
class RealCanvasMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_custom_controls_only_playback_and_export_match(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);image=QtGui.QImage(64,64,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtGui.QColor('red'))
            picture=root/'overlay.png';image.save(str(picture))
            for width,height in ((320,320),(320,400),(400,300)):
                with self.subTest(size=(width,height)):
                    state=fresh_state();state['settings'].update(aspect='custom',canvas_width=width,canvas_height=height,
                        output_dir=str(root),ffmpeg_path=FFMPEG,music_enabled=False,overwrite=True,darkness=0,
                        unvoiced_duration_mode='fixed',unvoiced_duration_seconds=.2,
                        layers=normalize_layers([dict(id='p',kind='image',path=str(picture),x=50,y=50,width=50,height=50,
                            use_saved_matte=False)]))
                    job=dict(id='j',path='',title='',body='',name=f'{width}x{height}',status='待生成')
                    state['jobs']=[job]
                    result=engine.render_static_text(job,state,root/'cache')
                    ffprobe=Path(FFMPEG).with_name('ffprobe.exe')
                    actual=engine.probe(result[0],str(ffprobe))['video']
                    self.assertEqual((actual['width'],actual['height']),(width,height))
                    decoded=subprocess.run([FFMPEG,'-nostdin','-v','error','-i',str(result[0]),'-frames:v','1',
                        '-f','image2pipe','-c:v','png','pipe:1'],capture_output=True,check=True,timeout=20)
                    frame=QtGui.QImage.fromData(decoded.stdout)
                    self.assertGreater(frame.pixelColor(width//2,height//2).red(),200)
                    self.assertLess(frame.pixelColor(0,0).red(),10)
                    ready=[];worker=PlaybackWorker(job,state,root/'playback',preview_key(job,state,preview_limit=.2),preview_limit=.2)
                    worker.ready.connect(ready.append);worker.run()
                    self.assertEqual(len(ready),1);self.assertNotIn('error',ready[0],ready[0])
                    preview=engine.probe(ready[0]['path'],str(ffprobe))['video']
                    self.assertEqual((preview['width'],preview['height']),(width,height))

    def test_background_poster_and_actual_export_use_custom_canvas(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);image=QtGui.QImage(240,120,QtGui.QImage.Format.Format_RGB32);image.fill(QtGui.QColor('red'))
            source=root/'background.png';image.save(str(source))
            settings=fresh_state()['settings'];settings.update(aspect='custom',canvas_width=320,canvas_height=400,
                auto_crop_black_bars=False,ffmpeg_path=FFMPEG,music_enabled=False,output_dir=str(root),overwrite=True,
                unvoiced_duration_seconds=.2,layers=[],darkness=0)
            for fit in ('contain','cover'):
                settings['fit']=fit;ready=[]
                worker=PreviewWorker(str(source),FFMPEG,frame_settings=settings,cache_directory=root)
                worker.ready.connect(ready.append);worker.run()
                self.assertNotIn('error',ready[0],ready[0])
                poster=QtGui.QImage.fromData(ready[0]['image']);self.assertEqual(poster.size(),QtCore.QSize(320,400))
                state=fresh_state();state['settings']=copy.deepcopy(settings)
                job=dict(id='j',path=str(source),title='',body='',name=fit,status='待生成')
                result=engine.render_static_text(job,state,root/'cache')
                stream=engine.probe(result[0],str(Path(FFMPEG).with_name('ffprobe.exe')))['video']
                self.assertEqual((stream['width'],stream['height']),(320,400))

    def test_source_mode_exports_each_actual_background_and_empty_fallback(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);state=fresh_state();paths=[]
            for index,(width,height) in enumerate(((320,180),(180,320))):
                path=root/f'bg-{index}.png';image=QtGui.QImage(width,height,QtGui.QImage.Format.Format_RGB32)
                image.fill(QtGui.QColor('red'));image.save(str(path));paths.append(str(path))
            state['backgrounds']=paths
            state['settings'].update(aspect='source',background_pool_id='builtin:background',layers=[],music_enabled=False,
                ffmpeg_path=FFMPEG,output_dir=str(root),overwrite=True,darkness=0,unvoiced_duration_seconds=.1)
            state['settings']['layers']=normalize_layers([dict(id='overlay',kind='image',path=paths[0],
                width=20,height=20,x=50,y=50,use_saved_matte=False)])
            state['jobs']=[dict(id=str(i),path='not-the-selected-background.mp4',title='',body='',name='source-'+str(i),status='待生成') for i in range(2)]
            for job,expected in zip(state['jobs'],((320,180),(180,320))):
                output,_=engine.render_static_text(job,state,root/'cache')
                stream=engine.probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))['video']
                self.assertEqual((stream['width'],stream['height']),expected)
            state['settings']['background_pool_id']='none'
            empty=dict(state['jobs'][0],name='no-reference')
            output,_=engine.render_static_text(empty,state,root/'cache')
            stream=engine.probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))['video']
            self.assertEqual((stream['width'],stream['height']),(1080,1920))

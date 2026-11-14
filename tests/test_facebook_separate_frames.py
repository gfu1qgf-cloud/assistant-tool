"""Flat task outputs, optional full-frame export, isolated mode/source caches."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app_plugins.builtin.facebook_contact_sheet import engine,task_cache,batch,ui,batch_ui
from qt_compat import QtWidgets

FIRST='https://www.facebook.com/reel/101/'
SECOND='https://www.facebook.com/reel/202/'


class FlatReferenceCacheTests(unittest.TestCase):
    def test_different_sources_share_one_flat_folder_without_overwriting_cache(self):
        with tempfile.TemporaryDirectory() as root:
            folder=task_cache.cache_directory(root,FIRST);folder.mkdir()
            self.assertEqual(folder,Path(root)/'Facebook参考大图')
            self.assertEqual(folder,task_cache.cache_directory(root,SECOND))
            for url in (FIRST,SECOND):
                image=folder/(task_cache.reference_key(url)+'.jpg');image.write_bytes(b'jpeg')
                task_cache.remember_sheets(folder,url,[image])
            self.assertNotEqual(task_cache.cached_sheets(folder,FIRST),task_cache.cached_sheets(folder,SECOND))
            self.assertFalse(any(path.is_dir() for path in folder.iterdir()))

    def test_switching_mode_never_reuses_the_other_mode(self):
        with tempfile.TemporaryDirectory() as root:
            folder=task_cache.cache_directory(root,FIRST);folder.mkdir()
            sheet=folder/'sheet.jpg';sheet.write_bytes(b'sheet')
            frame=folder/'frame.jpg';frame.write_bytes(b'frame')
            task_cache.remember_sheets(folder,FIRST,[sheet])
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=True),[])
            task_cache.remember_sheets(folder,FIRST,[frame],separate=True)
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=True),[frame])
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=False),[sheet])
            self.assertEqual(task_cache.cached_sheets(folder,FIRST),[frame])

    def test_legacy_subfolder_remains_readable_and_is_not_migrated_or_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            folder=task_cache.cache_directory(root,FIRST);old=folder/task_cache.reference_key(FIRST);old.mkdir(parents=True)
            image=old/'old.jpg';image.write_bytes(b'old')
            (old/'manifest.json').write_text(json.dumps({'version':1,'key':task_cache.reference_key(FIRST),
                'analysis_version':engine.ANALYSIS_VERSION,'files':['old.jpg']}),encoding='utf8')
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=False),[image])
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=True),[])
            self.assertTrue(image.is_file())

    def test_manifest_cannot_escape_the_reference_folder(self):
        with tempfile.TemporaryDirectory() as root:
            folder=task_cache.cache_directory(root,FIRST);folder.mkdir()
            task_cache.manifest_path(folder,FIRST,True).write_text(json.dumps({'version':2,'key':task_cache.reference_key(FIRST),
                'analysis_version':engine.ANALYSIS_VERSION,'separate':True,'files':['../outside.jpg']}),encoding='utf8')
            self.assertEqual(task_cache.cached_sheets(folder,FIRST,separate=True),[])


class SeparateFrameRenderTests(unittest.TestCase):
    def test_real_video_exports_full_size_separate_frames_and_retains_collage_option(self):
        import cv2
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            video=Path(root)/'source.avi'
            writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(160,90))
            self.assertTrue(writer.isOpened())
            try:
                for color in ((0,0,230),(0,230,0)):
                    for _ in range(10):writer.write(np.full((90,160,3),color,dtype=np.uint8))
            finally:writer.release()
            output=Path(root)/'Facebook参考大图';cuts=[engine.Cut(1,'手动')];progress=[]
            frames=engine.render_contact_sheets(video,2,cuts,output,separate=True,filename_prefix='FB_first',progress=progress.append)
            self.assertEqual(len(frames),2);self.assertEqual(progress[-1],100)
            for frame in frames:
                with Image.open(frame) as image:self.assertEqual(image.size,(160,90))
            with Image.open(frames[0]) as image:self.assertGreater(image.getpixel((80,45))[0],200)
            sheet=engine.render_contact_sheets(video,2,cuts,output,separate=False,filename_prefix='FB_first')
            self.assertEqual(len(sheet),1);self.assertTrue(all(path.is_file() for path in frames))
            other=engine.render_contact_sheets(video,2,cuts,output,separate=True,filename_prefix='FB_other',title='X'*200)
            self.assertTrue(other[0].name.startswith('FB_other_'))
            self.assertFalse(any(path.is_dir() for path in output.iterdir()))

    def test_cancellation_releases_decoder_and_does_not_report_success(self):
        from unittest.mock import Mock
        capture=Mock();capture.isOpened.return_value=True
        with tempfile.TemporaryDirectory() as root,patch('cv2.VideoCapture',return_value=capture):
            with self.assertRaises(engine.AnalysisCanceled):
                engine.render_contact_sheets('synthetic',2,[],root,separate=True,canceled=lambda:True)
            capture.release.assert_called_once()


class ReferenceOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_single_window_exposes_separate_option_and_reexport_uses_it(self):
        dialog=ui.ContactSheetDialog()
        try:
            self.assertIn('分别保存',dialog.separate_images.text())
            self.assertFalse(dialog.separate_images.isChecked())
            dialog.separate_images.setChecked(True)
            with tempfile.TemporaryDirectory() as root:
                source=Path(root)/'source.mp4';source.touch()
                dialog.output_edit.setText(root);dialog.source_edit.setText(str(source))
                with patch.object(dialog._manager,'submit',side_effect=ValueError('synthetic stop before worker')) as submit, \
                     patch.object(QtWidgets.QMessageBox,'information'):
                    dialog._analyze()
                self.assertTrue(submit.call_args.kwargs['payload']['separate'])
        finally:dialog.dispose();dialog.deleteLater()

    def test_batch_distinct_urls_are_not_deduplicated_by_flat_folder(self):
        dialog=batch_ui.BatchContactSheetDialog()
        try:
            with tempfile.TemporaryDirectory() as root:
                jobs=[batch.BatchJob('1',FIRST,root),batch.BatchJob('2',SECOND,root)]
                dialog.set_jobs(jobs);dialog.separate_images.setChecked(True)
                with patch.object(dialog._manager,'submit',side_effect=ValueError('synthetic')) as submit, \
                     patch.object(QtWidgets.QMessageBox,'information'):
                    dialog._start()
                first=submit.call_args.kwargs
                self.assertTrue(first['payload']['settings']['separate'])
                dialog._jobs=list(reversed(jobs))
                with patch.object(dialog._manager,'submit',side_effect=ValueError('synthetic')) as submit, \
                     patch.object(QtWidgets.QMessageBox,'information'):
                    dialog._start()
                self.assertNotEqual(first['dedup_key'],submit.call_args.kwargs['dedup_key'])
        finally:dialog._manager.shutdown();dialog.deleteLater()

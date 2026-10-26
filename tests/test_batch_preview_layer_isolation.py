import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.design_preview import resolve_preview_sources
from app_plugins.builtin.batch_text_video.layers import normalize_layers, ImageCache
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.ui import OverlayWorker


class PreviewLayerIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def fixture(self,path='unused.mov'):
        return normalize_layers([
            dict(id='empty',kind='image',name='旧图片池',path='unused.png',source='pool',
                pool_id='missing',sequence_items=[],sequence_cursor=0,use_saved_matte=False),
            dict(id='rose',kind='image',name='玫瑰',path=str(path),source='fixed',width=100,height=100,
                use_saved_matte=False,key_mode='none'),
            dict(id='title',kind='title',enabled=False),dict(id='body',kind='body',enabled=False)])

    def test_empty_pool_only_hides_that_layer_in_copy_and_refill_recovers(self):
        layers=self.fixture();original=copy.deepcopy(layers)
        resolved,notices=resolve_preview_sources(layers)
        self.assertFalse(resolved[0]['enabled']);self.assertTrue(resolved[1]['enabled'])
        self.assertIn('旧图片池',notices[0]);self.assertIn('序列为空',notices[0])
        self.assertEqual(layers,original)
        with self.assertRaisesRegex(ValueError,'序列为空'):resolve_sources(layers)
        layers[0]['sequence_items']=[dict(id='one',path='returned.png')]
        recovered,notices=resolve_preview_sources(layers)
        self.assertTrue(recovered[0]['enabled']);self.assertEqual(notices,[])

    def test_cancel_is_not_hidden_as_an_asset_warning(self):
        from app_plugins.builtin.batch_text_video.engine import Cancelled
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):resolve_preview_sources(self.fixture(),cancel=cancel)

    def test_export_keeps_strict_empty_source_validation(self):
        from app_plugins.builtin.batch_text_video.engine import prepare_static_text
        state=fresh_state();state['settings']['layers']=self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            state['settings']['output_dir']=folder
            with self.assertRaisesRegex(ValueError,'序列为空'):
                with prepare_static_text(dict(id='j',title='',body='',name='test',path=''),state,folder):pass

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Explicit real FFmpeg required')
    def test_dnxhr_movie_is_visible_even_with_an_unrelated_empty_pool(self):
        ffmpeg=os.environ['NODE_FFMPEG']
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);png=root/'rose.png';movie=root/'rose.mov'
            image=QtGui.QImage(320,180,QtGui.QImage.Format.Format_RGBA8888);image.fill(QtGui.QColor('black'))
            painter=QtGui.QPainter(image);painter.fillRect(0,120,320,60,QtGui.QColor(220,15,30));painter.end()
            image.save(str(png))
            result=subprocess.run([ffmpeg,'-y','-v','error','-loop','1','-i',str(png),'-t','0.4',
                '-r','10','-c:v','dnxhd','-profile:v','dnxhr_sq','-pix_fmt','yuv422p',str(movie)],
                capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            settings=fresh_state()['settings'];settings['layers']=self.fixture(movie)
            request=('', '',160,240,settings);original=copy.deepcopy(request)
            worker=OverlayWorker(request,ImageCache());results=[];worker.ready.connect(results.append)
            worker.run();self.assertEqual(len(results),1)
            actual=results[0];self.assertNotIn('error',actual,actual.get('error'))
            self.assertTrue(actual['design_notices'])
            self.assertTrue(any('旧图片池' in text for text in actual['design_notices']))
            self.assertTrue(actual['planes'])
            rose=actual['planes'][1][1]
            self.assertTrue(any(rose.pixelColor(x,y).red()>120 and rose.pixelColor(x,y).green()<80
                for x in range(0,160,8) for y in range(0,240,8)))
            self.assertEqual(request,original)

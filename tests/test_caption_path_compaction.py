import copy
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import text_outline,caption_style_render as renderer
from app_plugins.builtin.batch_text_video.captions import _path_commands,write_caption_ass,filter_path
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts


def old_commands(path):return re.sub(r'-?\d+\.\d+',lambda m:str(round(float(m[0])*8)),_path_commands(path))


def parse(source):
    path=QtGui.QPainterPath();tokens=source.split();index=0;command=None
    while index<len(tokens):
        if tokens[index] in {'m','l','b'}:command=tokens[index];index+=1
        count=6 if command=='b' else 2;values=[float(v)/8 for v in tokens[index:index+count]];index+=count
        if command=='m':path.moveTo(*values);command='l'
        elif command=='l':path.lineTo(*values)
        else:path.cubicTo(*values)
    return path


def mask(path):
    image=QtGui.QImage(500,250,QtGui.QImage.Format.Format_ARGB32_Premultiplied);image.fill(0)
    painter=QtGui.QPainter(image);painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.translate(30,30);painter.setPen(QtCore.Qt.PenStyle.NoPen);painter.setBrush(QtCore.Qt.GlobalColor.white)
    painter.drawPath(path);painter.end()
    return image


class CaptionCompactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([]);ensure_fonts()

    def test_line_commands_coalesced_without_removing_any_vertices(self):
        path=QtGui.QPainterPath();path.moveTo(0,0)
        for i in range(1,1001):path.lineTo(i/10,0)
        for i in range(1,1001):path.lineTo(100,i/10)
        path.lineTo(0,100);path.closeSubpath()
        original=old_commands(path);compact=text_outline.ass_commands(path)
        self.assertLess(len(compact),len(original)*.85)
        self.assertEqual(parse(original),parse(compact))
        self.assertEqual(mask(parse(original)),mask(parse(compact)))
        self.assertEqual(compact.count('m '),1)

    def test_reversals_holes_and_cubic_controls_are_preserved(self):
        path=QtGui.QPainterPath();path.moveTo(-2,-2);path.lineTo(20,-2);path.lineTo(5,-2);path.lineTo(40,-2)
        path.cubicTo(80,0,70,100,40,100);path.lineTo(-2,100);path.closeSubpath()
        path.addEllipse(QtCore.QRectF(10,20,15,40))
        old,new=old_commands(path),text_outline.ass_commands(path)
        self.assertEqual(old.count('m '),new.count('m '));self.assertEqual(old.count('b '),new.count('b '))
        self.assertEqual(mask(parse(old)),mask(parse(new)))
        self.assertIn('160 -16 40 -16 320 -16',new)

    def test_real_glyphs_accents_and_outside_counters_are_pixel_identical(self):
        font=QtGui.QFont('Open Sans');font.setPixelSize(90)
        for word in ('Božia','B8o','láska','A人'):
            with self.subTest(word=word):
                ink=QtGui.QPainterPath();ink.addText(0,100,font,word)
                ring=text_outline.outside_ring(ink.simplified(),10)
                original=old_commands(ring);compact=text_outline.ass_commands(ring)
                self.assertLessEqual(len(compact),len(original))
                self.assertEqual(mask(parse(original)),mask(parse(compact)))

    def layer(self):
        layer=new_follow_caption();layer.update(font='Open Sans',font_min=90,font_max=90,caption_random_size=False,
            caption_follow_mode='line',caption_display_mode='char',caption_cursor=False,outline_position='outside',
            outline_width=10,shadow_enabled=False,background_enabled=False,caption_clip_feather=10)
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none')
        return normalize_layers([layer])[0]

    def test_budget_errors_report_actual_resource_instead_of_unrelated_effects(self):
        layer=self.layer();words=[dict(text='Božia',start=0.,end=1.)]
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'caption.ass'
            with patch.object(renderer,'MAX_ROWS',0):
                with self.assertRaisesRegex(ValueError,'绘制指令超过'):write_caption_ass(path,words,layer,320,180,1.)
            with patch.object(renderer,'MAX_ASS_BYTES',256):
                with self.assertRaisesRegex(ValueError,'矢量脚本超过'):write_caption_ass(path,words,layer,320,180,1.)
            self.assertFalse(path.exists())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Explicit real FFmpeg required')
    def test_real_libass_frames_match_old_path_writer_exactly(self):
        ffmpeg=os.environ['NODE_FFMPEG'];layer=self.layer();before=copy.deepcopy(layer)
        words=[dict(text='Božia',start=0.,end=.8),dict(text='láska',start=.8,end=1.8)]
        frames=[];sizes=[]
        with tempfile.TemporaryDirectory() as temporary:
            for name,callback in (('before',old_commands),('after',text_outline.ass_commands)):
                path=Path(temporary)/(name+'.ass')
                with patch.object(text_outline,'ass_commands',callback):write_caption_ass(path,words,layer,320,180,2.)
                sizes.append(path.stat().st_size)
                result=subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','color=c=0x335533:s=320x180:r=12:d=2',
                    '-vf','ass=filename='+filter_path(path),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'],
                    capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));frames.append(result.stdout)
        self.assertEqual(len(frames[0]),24*320*180*3);self.assertEqual(frames[0],frames[1])
        self.assertLess(sizes[1],sizes[0]);self.assertEqual(layer,before)


if __name__=='__main__':unittest.main()

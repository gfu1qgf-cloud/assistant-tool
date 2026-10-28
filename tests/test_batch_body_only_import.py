import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.task_texts import split_title_body, entry_from_record, merge_sheet_entries
from app_plugins.builtin.batch_text_video.task_text_ui import TaskTextImportDialog
from app_plugins.builtin.batch_text_video.pools import merge_copies, pairing_plan, apply_pairs, bind_pool_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.copy_binding import bind_task_copy
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.layers import componentize_layers

FFMPEG=os.environ.get('NODE_FFMPEG','')


def record(root, **values):
    return dict(task_id='1000', task_type='reels', target_dir=str(root),
                source_row=2, source_sheet='registration.ods', task_audio_text='Prvá veta.\n\nDruhý odsek.',
                task_name='中文全文\n\n保留第二段', **values)


class BodyOnlyImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_single_paragraph_and_all_lines_are_body_without_fabricated_title(self):
        for source,expected in (('Jedna veta.','Jedna veta.'), ('\ufeff第一段\r\n\r\n第二段','第一段\n\n第二段'),
                                ('\n\ufeff  开头\r后续\n','开头\n后续')):
            self.assertEqual(split_title_body(source,no_title=True),('',expected))
        for value in ('','\n \t','\ufeff \r\n'):
            with self.assertRaisesRegex(ValueError,'正文为空'):split_title_body(value,no_title=True)
        for value in ('Single line','\nBody',''):
            with self.assertRaises(ValueError):split_title_body(value)
        self.assertEqual(split_title_body('Title\nBody'),('Title','Body'))

    def test_dialog_accepts_single_line_in_both_language_sources(self):
        with tempfile.TemporaryDirectory() as root:
            item=record(root);item.update(task_audio_text='Single paragraph.',task_name='只有一段正文。')
            original=copy.deepcopy(item)
            for source,expected in (('task_audio_text','Single paragraph.'),('task_name','只有一段正文。')):
                dialog=TaskTextImportDialog(records=[item])
                try:
                    dialog.source.setCurrentIndex(dialog.source.findData(source))
                    self.assertNotEqual(dialog.list.item(0,3).text(),'可导入')
                    dialog.no_title.setChecked(True)
                    self.assertEqual(dialog.list.horizontalHeaderItem(2).text(),'正文预览')
                    self.assertEqual(dialog.list.item(0,3).text(),'可导入')
                    self.assertEqual(dialog.preview.toPlainText(),expected)
                    dialog.accept_selection();self.assertEqual(len(dialog.entries),1)
                    self.assertEqual(dialog.entries[0]['title'],'');self.assertEqual(dialog.entries[0]['body'],expected)
                    self.assertFalse(dialog.entries[0]['requires_title'])
                finally:dialog.close();dialog.deleteLater()
            self.assertEqual(item,original)

    def test_toggle_preserves_unchecked_valid_rows_and_enables_formerly_invalid_rows(self):
        with tempfile.TemporaryDirectory() as root:
            records=[record(root),record(root),record(root)]
            for index,item in enumerate(records):item['task_id']=str(1000+index)
            records[1]['task_audio_text']='正文只有一行';records[2]['task_audio_text']=' '
            dialog=TaskTextImportDialog(records=records)
            try:
                dialog.list.item(0,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
                dialog.list.setCurrentCell(1,0);dialog.no_title.setChecked(True)
                self.assertEqual(dialog.list.currentRow(),1)
                self.assertEqual(dialog.list.item(0,0).checkState(),QtCore.Qt.CheckState.Unchecked)
                self.assertEqual(dialog.list.item(1,0).checkState(),QtCore.Qt.CheckState.Checked)
                self.assertEqual(dialog.list.item(2,0).checkState(),QtCore.Qt.CheckState.Unchecked)
                dialog.no_title.setChecked(False)
                self.assertNotEqual(dialog.list.item(1,3).text(),'可导入')
                self.assertEqual(dialog.list.item(0,0).checkState(),QtCore.Qt.CheckState.Unchecked)
            finally:dialog.close();dialog.deleteLater()

    def test_empty_body_still_blocked_in_explicit_body_only_mode(self):
        with tempfile.TemporaryDirectory() as root:
            item=record(root);item['task_audio_text']='\ufeff\n '
            dialog=TaskTextImportDialog(records=[item]);dialog.no_title.setChecked(True);dialog.check_all(True)
            try:
                with patch.object(QtWidgets.QMessageBox,'warning') as warning:dialog.accept_selection()
                self.assertTrue(warning.called);self.assertFalse(dialog.entries)
            finally:dialog.close();dialog.deleteLater()

    def test_import_metadata_and_audio_subtitles_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);voice=root/'task_audio.mp3';subtitle=root/'task_audio.srt'
            voice.write_bytes(b'voice');subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nText\n',encoding='utf8')
            before=(voice.read_bytes(),subtitle.read_bytes())
            item=entry_from_record(record(root),no_title=True)
            self.assertEqual(item['title'],'');self.assertEqual(item['body'],'Prvá veta.\n\nDruhý odsek.')
            self.assertFalse(item['requires_title']);self.assertEqual(item['task_dir'],str(root.resolve()))
            self.assertEqual(Path(item['voice_path']),voice);self.assertEqual(Path(item['task_subtitle_path']),subtitle)
            self.assertEqual((voice.read_bytes(),subtitle.read_bytes()),before)

    def test_reimport_switches_mode_without_duplicates_or_resetting_rotations(self):
        with tempfile.TemporaryDirectory() as root:
            source=record(root);state=fresh_state()
            merge_copies(state,[entry_from_record(source)],sheet=True)
            item=state['copy_pool'][0];identifier=item['id'];item['voice_override']=True;item['voice_path']='manual.wav'
            item['sequence_cursor']=7
            self.assertEqual(merge_copies(state,[entry_from_record(source,no_title=True)],sheet=True),(0,1))
            item=state['copy_pool'][0]
            self.assertEqual((item['id'],item['title'],item['body']),(identifier,'',source['task_audio_text']))
            self.assertEqual((item['voice_path'],item['sequence_cursor']),('manual.wav',7))
            self.assertFalse(item['requires_title'])
            self.assertEqual(merge_copies(state,[entry_from_record(source,no_title=True)],sheet=True),(0,0))
            self.assertEqual(merge_copies(state,[entry_from_record(source)],sheet=True),(0,1))
            self.assertTrue(item['requires_title']);self.assertEqual(item['title'],'Prvá veta.')

    def test_pairing_quick_export_and_direct_copy_binding_allow_missing_title(self):
        from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
        from app_plugins.builtin.batch_text_video.node_effects import task_snapshot
        with tempfile.TemporaryDirectory() as root:
            state=fresh_state();item=entry_from_record(record(root),no_title=True)
            merge_copies(state,[item],sheet=True);apply_pairs(state,pairing_plan(state['copy_pool'],[]))
            job=state['jobs'][0];self.assertFalse(job['requires_title']);self.assertEqual(job['text_source'],'task_audio_text')
            frozen=task_snapshot(job).fields;self.assertEqual(frozen['text_source'],'task_audio_text')
            layer=dict(id='b',kind='body',source='pool',pool_field='body',sequence_items=[dict(id='other',text='WRONG')],sequence_cursor=0)
            self.assertEqual(bind_task_copy(layer,item)['text'],item['body'])
            self.assertEqual(bind_task_copy(layer,frozen)['text'],item['body'])
            queued,identity=prepare_current_export(state,item,copy_id=item['id'])
            self.assertEqual(next(j for j in queued['jobs'] if j['id']==identity)['body'],item['body'])

    def test_body_only_preview_is_bound_even_when_title_pool_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            state=fresh_state();item=entry_from_record(record(root),no_title=True)
            merge_copies(state,[item],sheet=True)
            layers=componentize_layers(None,state['settings'])
            for layer in layers:layer['source']='pool'
            bound=bind_pool_layers(layers,state)
            resolved,steps=resolve_sources(bound,item['title'],item['body'],job=item)
            self.assertEqual(next(l for l in resolved if l['kind']=='title')['text'],'')
            self.assertEqual(next(l for l in resolved if l['kind']=='body')['text'],item['body'])
            self.assertEqual(steps,[])

    def test_import_does_not_change_title_control_visibility(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root,patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            editor=BatchTextVideoDialog(store=Store(Path(root)/'store'))
            before=editor.layer_panel.values()
            def accept(dialog):
                dialog.no_title.setChecked(True);dialog.accept_selection()
                return QtWidgets.QDialog.DialogCode.Accepted
            try:
                source=record(root);source['task_audio_text']='No title, only body.'
                with patch.object(TaskTextImportDialog,'exec',accept):self.assertTrue(editor.import_task_copy(records=[source]))
                self.assertEqual(editor.layer_panel.values(),before)
                saved=editor.store.load()['copy_pool'][0]
                self.assertEqual((saved['title'],saved['body']),('',source['task_audio_text']))
                screenshot=os.environ.get('LZX_BODY_ONLY_SCREENSHOT')
                if screenshot:
                    dialog=TaskTextImportDialog(records=[source]);dialog.no_title.setChecked(True);dialog.show()
                    self.app.processEvents();dialog.grab().save(screenshot);dialog.close();dialog.deleteLater()
            finally:editor.close();editor.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)


@unittest.skipUnless(FFMPEG,'Real FFmpeg required')
class BodyOnlyExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_body_only_survives_parallel_preparation_and_real_render_without_background(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.engine import probe
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);task=root/'task';task.mkdir()
            source=record(task);source['task_audio_text']='First line stays in body.\n\nSecond paragraph.'
            state=fresh_state();item=entry_from_record(source,no_title=True)
            merge_copies(state,[item],sheet=True);apply_pairs(state,pairing_plan(state['copy_pool'],[]))
            settings=state['settings'];settings.update(ffmpeg_path=FFMPEG,music_enabled=False,use_voice=False,overwrite=True,
                aspect='custom',canvas_width=320,canvas_height=400,unvoiced_duration_seconds=.2,darkness=0,export_workers=2)
            layers=componentize_layers(None,settings)
            for layer in layers:
                layer.update(source='pool')
                if layer['kind']=='title':layer['enabled']=False  # The user controls this switch.
                else:layer.update(x=50,y=50,width=90,height=90,font_min=12,font_max=18)
            settings['layers']=layers;store=Store(root/'store');state=store.save(state,0)
            logs=[];result,saved=execute_batch(store,state,[state['jobs'][0]['id']],threading.Event(),lambda *args:None,logs.append,lambda *args:None)
            self.assertFalse(result.get('error'),result);self.assertEqual(result['completed'],1,result)
            self.assertEqual(result['failed'],0,result)
            output=Path(result['paths'][0]);self.assertEqual(output.parent,task)
            stream=probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))['video']
            self.assertEqual((stream['width'],stream['height']),(320,400))
            self.assertEqual(saved['jobs'][0]['body'],source['task_audio_text'])

    def test_imported_body_only_task_still_requires_its_bound_output_directory(self):
        from app_plugins.builtin.batch_text_video import engine
        with tempfile.TemporaryDirectory() as root:
            state=fresh_state();item=entry_from_record(record(root),no_title=True);item['task_dir']=''
            state['settings'].update(output_dir=root,ffmpeg_path=FFMPEG,music_enabled=False,unvoiced_duration_seconds=.1,
                                     aspect='custom',canvas_width=320,canvas_height=400)
            with self.assertRaisesRegex(ValueError,'缺少绑定的任务目录'):
                engine.render_static_text(item,state,Path(root)/'cache')

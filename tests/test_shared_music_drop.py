from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import shared_music as shared
from app_plugins.builtin.batch_text_video import shared_music_ui as ui
from app_plugins.builtin.batch_text_video.store import Store


class SharedMusicDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'data')
        self.store.save(self.store.load(),0)
        self.owner=QtWidgets.QWidget();self.owner.store=self.store;self.owner.state=self.store.load();self.owner.controls={};self.owner.audio_cards={}
        self.dialog=ui.SharedMusicDialog(self.owner);self.dialog.show();self.app.processEvents()
    def tearDown(self):
        self.dialog.cancel.set();self.dialog.executor.shutdown(wait=True,cancel_futures=True)
        self.dialog.reject();self.dialog.deleteLater();self.owner.deleteLater();self.app.processEvents();self.temp.cleanup()
    def file(self,name):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'path registration only');return path
    def mime(self,paths=(),urls=()):
        mime=QtCore.QMimeData();mime.setUrls([QtCore.QUrl.fromLocalFile(str(p)) for p in paths]+[QtCore.QUrl(v) for v in urls]);return mime
    def enter(self,target,mime,*,actions=None):
        actions=actions or (QtCore.Qt.DropAction.CopyAction|QtCore.Qt.DropAction.MoveAction)
        event=QtGui.QDragEnterEvent(QtCore.QPoint(10,10),actions,mime,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(target,event);return event
    def drop(self,target,mime):
        actions=QtCore.Qt.DropAction.CopyAction|QtCore.Qt.DropAction.MoveAction
        entered=self.enter(target,mime)
        move=QtGui.QDragMoveEvent(QtCore.QPoint(10,10),actions,mime,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(target,move)
        event=QtGui.QDropEvent(QtCore.QPointF(10,10),actions,mime,QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(target,event)
        return entered,move,event
    def wait(self):
        end=time.monotonic()+5
        while self.dialog.future and time.monotonic()<end:self.app.processEvents();time.sleep(.005)
        self.app.processEvents();self.assertIsNone(self.dialog.future,self.dialog.note.text())
    def paths(self,identifier=shared.ID):return [v['path'] for v in shared.get(self.store.load(),identifier)['items']]

    def test_empty_table_viewport_accepts_multiple_files_as_copy_without_moving_sources(self):
        paths=[self.file('中文 空格一.mp3'),self.file('二.wav')]
        events=self.drop(self.dialog.table.viewport(),self.mime(paths))
        self.assertTrue(all(event.isAccepted() for event in events))
        self.assertTrue(all(event.dropAction()==QtCore.Qt.DropAction.CopyAction for event in events))
        self.wait();self.assertEqual(self.paths(),[str(p) for p in paths]);self.assertTrue(all(p.is_file() for p in paths))
        self.assertEqual(self.dialog.model.rowCount(),2)

    def test_drop_onto_existing_rows_appends_deduplicates_and_preserves_playhead(self):
        first=self.file('existing.mp3');self.dialog.import_paths([first]);self.wait()
        pool=shared.get(self.store.load());pool['items'][0]['cursor']=18.;pool['next_id']=pool['items'][0]['id']
        self.store.save_shared_music(pool,pool['revision']);self.dialog.reload()
        another=self.file('second.FLAC')
        events=self.drop(self.dialog.table.viewport(),self.mime([first,another,another]))
        self.assertTrue(events[-1].isAccepted());self.wait()
        pool=shared.get(self.store.load());self.assertEqual(len(pool['items']),2)
        self.assertEqual(pool['items'][0]['cursor'],18.);self.assertEqual(pool['next_id'],pool['items'][0]['id'])

    def test_dialog_blank_area_accepts_recursive_folders_and_ignores_unsupported_files(self):
        paths=[self.file('collection/a.mp3'),self.file('collection/sub/b.ogg')]
        self.file('collection/not-a-song.txt')
        events=self.drop(self.dialog,self.mime([self.root/'collection']))
        self.assertTrue(events[-1].isAccepted());self.wait()
        self.assertEqual(set(self.paths()),{str(p) for p in paths})

    def test_drop_registers_only_in_current_selected_shared_library(self):
        extra=self.store.create_shared_music('第二库');self.dialog.reload()
        self.dialog.libraries.setCurrentIndex(self.dialog.libraries.findData(extra['id']))
        path=self.file('selected.m4a');events=self.drop(self.dialog.table.viewport(),self.mime([path]))
        self.assertTrue(events[-1].isAccepted());self.wait()
        self.assertEqual(self.paths(extra['id']),[str(path)]);self.assertEqual(self.paths(),[])

    def test_drag_hover_does_not_scan_or_decode_media_and_remote_only_urls_are_rejected(self):
        mime=self.mime([self.root/'network-or-private-folder'])
        with patch.object(Path,'is_dir',side_effect=AssertionError('GUI stat')),patch.object(Path,'is_file',side_effect=AssertionError('GUI stat')):
            event=self.enter(self.dialog.table.viewport(),mime)
        self.assertTrue(event.isAccepted());self.assertIsNone(self.dialog.future)
        self.assertFalse(self.enter(self.dialog,self.mime(urls=['https://example.invalid/music.mp3'])).isAccepted())
        text=QtCore.QMimeData();text.setText('C:/song.mp3')
        self.assertFalse(self.enter(self.dialog,text).isAccepted());self.assertIsNone(self.dialog.future)

    def test_mixed_remote_and_local_urls_import_only_local_file(self):
        path=self.file('local.wav')
        event=self.drop(self.dialog,self.mime([path],urls=['https://example.invalid/music.mp3']))[-1]
        self.assertTrue(event.isAccepted());self.wait();self.assertEqual(self.paths(),[str(path)])

    def test_busy_closed_and_unselected_library_do_not_accept_and_silently_lose_drops(self):
        mime=self.mime([self.file('busy.mp3')]);self.dialog.future=Future()
        self.assertFalse(self.enter(self.dialog.table.viewport(),mime).isAccepted())
        self.assertFalse(self.dialog.import_paths([self.root/'busy.mp3']))
        self.dialog.future=None;pool=self.dialog.pool;self.dialog.pool=None
        self.assertFalse(self.enter(self.dialog,mime).isAccepted())
        self.dialog.pool=pool;self.dialog.cancel.set()
        self.assertFalse(self.enter(self.dialog,mime).isAccepted())

    def test_move_only_drag_is_not_reported_as_a_move_of_original_music(self):
        event=self.enter(self.dialog.table.viewport(),self.mime([self.file('move.mp3')]),actions=QtCore.Qt.DropAction.MoveAction)
        self.assertFalse(event.isAccepted());self.assertIsNone(self.dialog.future)

    def test_import_runs_off_gui_thread_through_existing_pipeline(self):
        path=self.file('worker.wav');threads=[];original=ui.prepare_paths
        def prepare(*args):threads.append(threading.get_ident());return original(*args)
        with patch.object(ui,'prepare_paths',side_effect=prepare),patch('subprocess.Popen',side_effect=AssertionError('import decoded media')):
            event=self.drop(self.dialog.table.viewport(),self.mime([path]))[-1]
            self.assertTrue(event.isAccepted());self.wait()
        self.assertTrue(threads);self.assertTrue(all(t!=threading.get_ident() for t in threads))
        self.assertEqual(self.paths(),[str(path)])

import os
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets, QtTest
from model.image_retrieval.encoder import ChineseImageEncoder
from model.image_retrieval.index import ImageSearchIndex
from app_plugins.builtin.smart_image_search.ui import SmartImageSearchDialog, _SearchWorker
from app_plugins.builtin.smart_music_search.encoder import MusicEncoder
from app_plugins.builtin.smart_music_search.index import MusicIndex
from app_plugins.builtin.smart_music_search.ui import SmartMusicSearchDialog, _Worker
from app_plugins.builtin.smart_image_search.plugin import SmartImageSearchPlugin
from app_plugins.builtin.smart_music_search.plugin import SmartMusicSearchPlugin


class _ImageEncoder:
    model_key = "base"
    model_id = "synthetic/image"
    release = None


class _MusicEncoder:
    model_id = "synthetic/music"
    release = None


class SearchShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dialogs = []

    def image(self):
        encoder = _ImageEncoder()
        encoder.release = Mock()
        dialog = SmartImageSearchDialog({}, store=object(),
            index=ImageSearchIndex(Path(self.temp.name) / "image"), encoder=encoder)
        self.dialogs.append(dialog)
        return dialog

    def music(self):
        encoder = _MusicEncoder()
        encoder.release = Mock()
        dialog = SmartMusicSearchDialog({},
            index=MusicIndex(Path(self.temp.name) / "music"), encoder=encoder)
        self.dialogs.append(dialog)
        return dialog

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def test_finished_does_not_unlock_or_release_before_native_join(self):
        for dialog, worker_type in ((self.image(), _SearchWorker), (self.music(), _Worker)):
            with self.subTest(dialog=type(dialog).__name__):
                task = Mock()
                worker = worker_type(task, dialog)
                dialog.worker = worker
                with patch.object(worker, "wait", return_value=False):
                    dialog._finished()
                    self.assertTrue(dialog.is_busy())
                    self.assertTrue(dialog._finish_timer.isActive())
                    self.assertIs(worker.task, task)
                    self.assertFalse(dialog.shutdown())
                    dialog.encoder.release.assert_not_called()
                dialog._finish_timer.stop()
                with patch.object(worker, "wait", return_value=True):
                    dialog._finished()
                self.assertFalse(dialog.is_busy())
                self.assertIsNone(worker.task)
                self.assertTrue(dialog.shutdown())
                dialog.encoder.release.assert_called_once()

    def test_shutdown_refuses_running_search_then_allows_retry(self):
        for dialog in (self.image(), self.music()):
            with self.subTest(dialog=type(dialog).__name__):
                worker = Mock()
                dialog.worker = worker
                self.assertFalse(dialog.shutdown())
                worker.requestInterruption.assert_called_once()
                self.assertFalse(dialog._shutting_down)
                dialog.encoder.release.assert_not_called()
                dialog.worker = None
                self.assertTrue(dialog.shutdown())

    def test_shutdown_blocks_new_searches(self):
        image, music = self.image(), self.music()
        image.shutdown()
        music.shutdown()
        with patch("app_plugins.builtin.smart_image_search.ui._SearchWorker") as image_worker, \
             patch("app_plugins.builtin.smart_music_search.ui._Worker") as music_worker:
            image._start(Mock(), "test")
            music._start("test", Mock())
        image_worker.assert_not_called()
        music_worker.assert_not_called()

    def test_image_delayed_drag_cleanup_is_cancelled_on_exit(self):
        dialog = self.image()
        dialog._finalize_dragged_move = Mock()
        dialog._schedule_drag_cleanup(["nonexistent.jpg"], QtCore.Qt.DropAction.MoveAction)
        self.assertEqual(len(dialog._drag_cleanup_timers), 1)
        dialog.shutdown()
        self.assertEqual(len(dialog._drag_cleanup_timers), 0)
        QtTest.QTest.qWait(550)
        dialog._finalize_dragged_move.assert_not_called()

    def test_music_shutdown_stops_clears_and_detaches_player(self):
        dialog = self.music()
        player, output = Mock(), Mock()
        dialog._player, dialog._audio_output = player, output
        self.assertTrue(dialog.shutdown())
        player.stop.assert_called_once()
        player.blockSignals.assert_called_once_with(True)
        self.assertTrue(player.setSource.call_args.args[0].isEmpty())
        player.setAudioOutput.assert_called_once_with(None)
        player.deleteLater.assert_called_once()
        output.deleteLater.assert_called_once()
        self.assertIsNone(dialog._player)
        self.assertIsNone(dialog._audio_output)
        with patch.object(dialog, "_selected") as selected:
            dialog.preview_selected()
        selected.assert_not_called()

    def test_plugin_stop_uses_full_shutdown(self):
        for plugin_type in (SmartImageSearchPlugin, SmartMusicSearchPlugin):
            plugin = plugin_type()
            plugin.dialog = Mock()
            plugin.dialog.shutdown.return_value = True
            plugin.stop()
            plugin.dialog.shutdown.assert_called_once()
            plugin.dialog.cancel_work.assert_not_called()

    def test_encoder_release_drops_runtime_and_query_caches(self):
        class Runtime:
            pass
        for encoder, fields, cache in (
            (ChineseImageEncoder(), ("_runtime",), "_query_cache"),
            (MusicEncoder(), ("_music", "_translation"), "_text_cache"),
        ):
            runtime = Runtime()
            ref = weakref.ref(runtime)
            for field in fields:
                setattr(encoder, field, runtime)
            getattr(encoder, cache)["query"] = runtime
            del runtime
            encoder.release()
            self.assertIsNone(ref())
            self.assertFalse(getattr(encoder, cache))


class ExitLoggingTests(unittest.TestCase):
    def test_fault_fd_survives_python_stream_close(self):
        from model import AppLogger
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(AppLogger, "_fault_stream", None), \
             patch.object(AppLogger, "_fault_fd", None), \
             patch.object(AppLogger.faulthandler, "enable") as enable:
            AppLogger._enable_fault_handler(Path(directory), Mock())
            stream, fd = AppLogger._fault_stream, AppLogger._fault_fd
            try:
                enable.assert_called_once_with(file=fd, all_threads=True)
                self.assertNotEqual(stream.fileno(), fd)
                stream.close()
                self.assertGreaterEqual(os.fstat(fd).st_size, 1)
            finally:
                if not stream.closed:
                    stream.close()
                os.close(fd)

    def test_shutdown_keeps_fatal_logging_and_does_not_claim_process_exit(self):
        from model import AppLogger
        stream, logger = Mock(), Mock(handlers=[])
        with patch.object(AppLogger, "_fault_stream", stream), \
             patch.object(AppLogger.logging, "getLogger", return_value=logger), \
             patch.object(AppLogger.faulthandler, "disable") as disable:
            AppLogger.shutdown_application_logging()
        disable.assert_not_called()
        stream.close.assert_not_called()
        self.assertNotIn("正常退出", logger.info.call_args.args[0])

    def test_qt_python_callback_is_uninstalled_before_finalization(self):
        from model import AppLogger
        with patch.object(AppLogger, "_qt_message_handler", object()), \
             patch.object(QtCore, "qInstallMessageHandler") as install:
            AppLogger.remove_qt_message_logging()
            install.assert_called_once_with(None)
            self.assertIsNone(AppLogger._qt_message_handler)


if __name__ == "__main__":
    unittest.main()

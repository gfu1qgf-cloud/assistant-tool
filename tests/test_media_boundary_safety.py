"""Regression for the captured GUI stop() hang and modal review boundaries."""
import subprocess
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtWidgets,QtMultimedia
import test_smart_video_player as preview
import test_smart_video_export_blockers_ui as review


class NativeBoundarySafetyTests(unittest.TestCase):
    setUpClass=preview.NativePreviewTests.__dict__['setUpClass']
    setUp=preview.NativePreviewTests.setUp
    tearDown=preview.NativePreviewTests.tearDown
    frame=preview.NativePreviewTests.frame
    ready_for_frames=preview.NativePreviewTests.ready_for_frames

    def seed_frame(self):
        self.ready_for_frames();frame=self.frame(4)
        self.backend._last_frames[0]=frame
        self.backend.video_widgets[0].set_frame(frame)
        with QtCore.QSignalBlocker(self.backend.video_probes[0]):
            self.backend.video_probes[0].setVideoFrame(frame)
        return frame

    def assert_released(self):
        self.assertIsNone(self.backend._last_frames[0])
        self.assertIsNone(self.backend.video_widgets[0].frame)
        self.assertFalse(self.backend.video_probes[0].videoFrame().isValid())

    def test_source_change_releases_frames_and_never_calls_redundant_stop(self):
        self.seed_frame()
        with patch.object(self.backend.players[0],'stop') as stop,patch.object(self.backend.players[0],'setSource',side_effect=lambda *_:self.assert_released()):
            self.backend._set_media(0,'new-synthetic.mp4',0)
            stop.assert_not_called()

    def test_stop_releases_view_cache_and_sink_before_native_stop(self):
        self.seed_frame()
        with patch.object(self.backend.players[0],'stop',side_effect=self.assert_released) as stop:
            self.surface.stop();stop.assert_called_once()

    def test_release_discards_frames_before_source_teardown_without_double_stop(self):
        self.seed_frame()
        with patch.object(self.backend.players[0],'stop') as stop,patch.object(self.backend.players[0],'setSource',side_effect=lambda *_:self.assert_released()):
            self.surface.release();stop.assert_not_called()

    def test_frame_cache_does_not_share_native_frame_state(self):
        self.ready_for_frames();frame=self.frame(4)
        self.backend._frame_probed(0,frame)
        cached=self.backend._last_frames[0]
        frame.setStartTime(7000000)
        self.assertEqual(cached.startTime(),4000000)

    def test_frame_callback_does_not_recursively_play_and_pause_cancels_queued_play(self):
        self.ready_for_frames();self.backend._play_requested=True
        with patch.object(self.backend.players[0],'play') as play:
            self.backend._frame_probed(0,self.frame(4));play.assert_not_called()
            self.backend.pause();self.app.processEvents();play.assert_not_called()

    def test_boundary_pause_is_deferred_outside_frame_callback(self):
        self.ready_for_frames();finished=[]
        self.surface.rangeFinished.connect(lambda:finished.append(True))
        with patch.object(self.backend.players[0],'pause') as pause:
            self.backend._finish_current_range(0);pause.assert_not_called()
            self.app.processEvents();pause.assert_called_once()
            self.assertEqual(finished,[True])

    def test_frame_published_synchronously_during_seek_is_consumed_after_guard(self):
        self.ready_for_frames();self.backend._seek_pending[0]=False
        def publish(position):self.backend.video_probes[0].setVideoFrame(self.frame(position/1000))
        with patch.object(self.backend.players[0],'setPosition',side_effect=publish),patch.object(self.backend.players[0],'pause'):
            self.backend._set_media(0,'synthetic.mp4',2)
        self.assertFalse(self.backend._seek_pending[0])
        self.assertEqual(self.backend._last_frames[0].startTime(),2000000)

    def test_stale_play_callback_cannot_cancel_a_new_range_play_request(self):
        self.ready_for_frames();self.backend._seek_pending[0]=False;self.backend._play_requested=True
        with patch.object(self.backend.players[0],'play') as play:
            self.backend._request_play(0)
            self.backend._display_generation+=1
            self.backend._request_play(0)
            self.app.processEvents();play.assert_called_once()


class ModalReviewBoundaryTests(unittest.TestCase):
    setUpClass=review.ExportBlockersUiTests.__dict__['setUpClass']
    setUp=review.ExportBlockersUiTests.setUp
    tearDown=review.ExportBlockersUiTests.tearDown
    clip=review.ExportBlockersUiTests.clip
    bundle=review.ExportBlockersUiTests.bundle
    dialog=review.ExportBlockersUiTests.dialog

    def test_confirmation_suspends_playback_before_nested_modal_events(self):
        dialog=self.dialog();timeline=dialog.timeline_review
        self.app.processEvents()
        with patch.object(timeline.player,'prepare') as prepare,patch.object(timeline.player,'pause') as pause:
            def question(*_args):
                self.assertTrue(timeline._review_suspended);pause.assert_called()
                timeline._play_next_range();self.app.processEvents()
                prepare.assert_not_called()
                return QtWidgets.QMessageBox.StandardButton.No
            with patch.object(QtWidgets.QMessageBox,'question',side_effect=question):
                dialog._set_missing_decision_for_task(0,'approved')


@unittest.skipUnless(preview.FFMPEG,'Synthetic FFmpeg generator unavailable')
class RealBoundaryStressTests(unittest.TestCase):
    setUpClass=preview.RealNativePreviewTests.__dict__['setUpClass']
    tearDownClass=preview.RealNativePreviewTests.__dict__['tearDownClass']
    setUp=preview.RealNativePreviewTests.setUp
    tearDown=preview.RealNativePreviewTests.tearDown
    surface=preview.RealNativePreviewTests.surface
    wait=preview.RealNativePreviewTests.wait
    seek=preview.RealNativePreviewTests.seek

    def test_real_repeated_source_switches_with_queued_boundary_and_confirmation(self):
        surface=self.surface(preload=True)
        finished=[];surface.rangeFinished.connect(lambda:finished.append(True))
        for index in range(12):
            source=self.root/('portrait.mp4' if index%2 else 'landscape.mp4')
            following=self.root/('landscape.mp4' if index%2 else 'portrait.mp4')
            self.seek(surface,source,1.0,start=.5,end=1.3)
            surface.queue(str(following),1.5,2.5)
            surface.play()
            self.wait(lambda:surface.backend.position>.55)
            # A queued end callback must not switch media inside a review
            # confirmation's nested Qt event loop.
            before=len(finished);surface.backend._finish_current_range(surface.backend._active)
            surface.pause();surface.clear_queue()
            box=QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question,'模拟核对','确认完整？',
                QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No)
            QtCore.QTimer.singleShot(5,box.reject);box.exec();box.deleteLater()
            self.assertEqual(len(finished),before)
            self.assertFalse(surface.is_playing())
        surface.release()
        self.assertTrue(all(player.source().isEmpty() for player in surface.backend.players))

    def test_actual_range_end_callbacks_switch_files_with_preloading(self):
        surface=self.surface(preload=True)
        playlist=[self.root/('portrait.mp4' if index%2 else 'landscape.mp4') for index in range(8)]
        completed=[]
        def play_next():
            completed.append(True)
            if len(completed)>=len(playlist):return
            index=len(completed)
            surface.prepare(str(playlist[index]),.5,1.0)
            surface.play()
            if index+1<len(playlist):surface.queue(str(playlist[index+1]),.5,1.0)
        surface.rangeFinished.connect(play_next)
        self.seek(surface,playlist[0],.5,start=.5,end=1.0)
        surface.queue(str(playlist[1]),.5,1.0);surface.play()
        self.wait(lambda:len(completed)==len(playlist),timeout=15)
        self.assertFalse(surface.is_playing());self.assertEqual(surface.errors,[])

    def test_full_hd_portrait_frames_are_detached_before_switching_to_another_source(self):
        source=self.root/'full-hd.mp4'
        subprocess.run([preview.FFMPEG,'-v','error','-y','-f','lavfi','-i',
            'color=c=blue:s=1080x1920:r=24:d=2','-c:v','libx264','-preset','ultrafast',
            '-pix_fmt','yuv420p',str(source)],check=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        surface=self.surface(preload=True)
        for _ in range(3):
            self.seek(surface,source,.5,start=.25,end=1.5)
            frame=surface.backend._last_frames[surface.backend._active]
            self.assertEqual(frame.handleType(),QtMultimedia.QVideoFrame.HandleType.NoHandle)
            self.assertEqual((frame.width(),frame.height()),(1080,1920))
            self.seek(surface,self.root/'landscape.mp4',1,start=.5,end=1.5)
        self.assertEqual(surface.errors,[])

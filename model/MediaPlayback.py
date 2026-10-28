"""Qt6-native media preview; no embedded external player windows or processes."""

from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets


class _VideoFrameView(QtWidgets.QWidget):
    """Paint the accepted Qt frame into this pane's own logical coordinates."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = None
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def set_frame(self, frame):
        self.frame = frame
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#050608"))
        if self.frame is not None and self.frame.isValid():
            from qt_compat import QtMultimedia
            options = QtMultimedia.QVideoFrame.PaintOptions()
            options.aspectRatioMode = QtCore.Qt.AspectRatioMode.KeepAspectRatio
            options.backgroundColor = QtGui.QColor("#050608")
            self.frame.paint(painter, QtCore.QRectF(self.rect()), options)
        painter.end()


class _QtMediaReviewSurface(QtWidgets.QFrame):
    """PyQt6 multimedia player with optional low-cost standby decoder."""

    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, preload_enabled=True):
        super().__init__(parent)
        from qt_compat import QtMultimedia
        if QtMultimedia is None:
            raise RuntimeError("当前安装缺少 Qt6 Multimedia，请安装完整的 PyQt6 运行库。")
        self._multimedia = QtMultimedia
        self.source = ""
        self.range_start = 0.0
        self.range_end = 0.0
        self.position = 0.0
        self._active = 0
        self._preload_enabled = bool(preload_enabled)
        decoder_count = 2 if self._preload_enabled else 1
        self._sources = [""] * decoder_count
        self._pending_positions = [None] * decoder_count
        self._play_requested = False
        self._finishing_range = False
        self._reported_error = False
        self._queued = None
        self._display_generation = 0
        self._target_positions = [0] * decoder_count
        self._seek_pending = [False] * decoder_count
        self._setting_media = [False] * decoder_count
        self._last_frames = [None] * decoder_count
        self._stopped = True

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#050608; border:1px solid #303746; }"
        )
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QtWidgets.QStackedWidget(self)
        self.stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        layout.addWidget(self.stack)

        self.placeholder = QtWidgets.QLabel(
            "选择时间线片段后在这里预览", self.stack
        )
        self.placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self.placeholder.setStyleSheet(
            "color:#9AA3B8; background:#050608; border:0;"
        )
        self.placeholder.setAutoFillBackground(True)
        self.stack.addWidget(self.placeholder)

        self.players = []
        self.audio_outputs = []
        self.video_widgets = []
        self.video_probes = []
        for index in range(decoder_count):
            video = _VideoFrameView(self.stack)
            self.stack.addWidget(video)
            player = QtMultimedia.QMediaPlayer(self)
            audio_output = QtMultimedia.QAudioOutput(self)
            player.setAudioOutput(audio_output)
            probe = QtMultimedia.QVideoSink(self)
            player.setVideoSink(probe)
            player.positionChanged.connect(
                lambda milliseconds, slot=index: self._position_changed(
                    slot, milliseconds
                )
            )
            player.playbackStateChanged.connect(
                lambda state, slot=index: self._state_changed(slot, state)
            )
            player.mediaStatusChanged.connect(
                lambda status, slot=index: self._media_status_changed(slot, status)
            )
            try:
                player.errorOccurred.connect(
                    lambda _error, _message="", slot=index: self._player_error(slot)
                )
            except (AttributeError, TypeError):
                pass
            try:
                probe.videoFrameChanged.connect(
                    lambda frame, slot=index: self._frame_probed(slot, frame)
                )
            except (AttributeError, RuntimeError, TypeError):
                pass
            self.players.append(player)
            self.audio_outputs.append(audio_output)
            self.video_widgets.append(video)
            self.video_probes.append(probe)

    def is_available(self):
        return bool(self.players)

    def is_playing(self):
        return self._play_requested or (
            self.players[self._active].playbackState()
            == self._multimedia.QMediaPlayer.PlaybackState.PlayingState
        )

    def configure_ffplay(self, _ffmpeg_path=""):
        return None

    def _set_media(self, slot, source, position_seconds):
        player = self.players[slot]
        source = str(source or "")
        position_ms = max(0, int(round(float(position_seconds or 0.0) * 1000)))
        self._target_positions[slot] = position_ms
        self._seek_pending[slot] = True
        self._setting_media[slot] = True
        try:
            if self._sources[slot] != source:
                self._last_frames[slot] = None
                self._sources[slot] = source
                self._pending_positions[slot] = position_ms
                player.stop()
                player.setSource(QtCore.QUrl.fromLocalFile(source))
            elif self._pending_positions[slot] is not None:
                self._pending_positions[slot] = position_ms
            else:
                player.setPosition(position_ms)
                player.pause()
        finally:
            self._setting_media[slot] = False
        # A paused/preloaded player at exactly this target need not decode
        # again. Only reuse a frame stamped on the current source/clock.
        frame = self._last_frames[slot]
        if frame is not None:
            self._frame_probed(slot, frame)

    def prepare(self, source, start, end, position=0.0):
        self._display_generation += 1
        self._stopped = False
        self._play_requested = False
        self._finishing_range = False
        self._reported_error = False
        requested = (
            str(source or ""),
            max(0.0, float(start or 0.0)),
            max(0.0, float(end or 0.0)),
        )
        queued = self._queued
        if queued and queued[:3] == requested:
            self.players[self._active].pause()
            self._active = int(queued[3])
            self._queued = None
        else:
            self.players[self._active].pause()
            self._queued = None

        self.source = requested[0]
        self.range_start = requested[1]
        self.range_end = max(self.range_start + 0.01, requested[2])
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, float(position or 0.0)),
        )
        # Hide the previous frame until the sink confirms the requested PTS.
        # Position signals alone can precede the actual seek/decoded picture.
        for video in self.video_widgets:
            video.hide()
        self.placeholder.setText("正在定位预览画面…")
        self.stack.setCurrentWidget(self.placeholder)
        self.placeholder.raise_()
        self.placeholder.repaint()
        self._set_media(
            self._active, self.source, self.range_start + self.position
        )
        self.positionChanged.emit(self.position)

    def queue(self, source, start, end):
        """Load the following range without disturbing current playback."""
        if not self._preload_enabled or len(self.players) < 2:
            self._queued = None
            return False
        source = str(source or "")
        if not source or not Path(source).is_file():
            self._queued = None
            return False
        slot = 1 - self._active
        self.players[slot].pause()
        self._set_media(slot, source, float(start or 0.0))
        self._queued = (
            source,
            max(0.0, float(start or 0.0)),
            max(float(start or 0.0) + 0.01, float(end or 0.0)),
            slot,
        )
        return True

    def clear_queue(self):
        self._queued = None
        if len(self.players) < 2:
            return
        standby = 1 - self._active
        self.players[standby].pause()

    def play(self):
        if not self.source or not Path(self.source).is_file():
            self.errorOccurred.emit(f"视频文件不存在：{self.source}")
            return False
        if self._stopped or self.position >= self.range_end - self.range_start - 0.02:
            self.position = 0.0
            self._stopped = False
            self._set_media(self._active, self.source, self.range_start)
        self._play_requested = True
        self._finishing_range = False
        if not self._seek_pending[self._active] and self._pending_positions[self._active] is None:
            self.players[self._active].play()
        self.playingChanged.emit(True)
        return True

    def pause(self, show_frame=True):
        del show_frame
        self._display_generation += 1
        self._play_requested = False
        self.players[self._active].pause()
        self.playingChanged.emit(False)

    def reset_source(self):
        self.release()  # Clear both decoded/preloaded QMediaPlayer sources.
        self.source, self.position = "", 0.0

    def stop(self):
        self._stopped = True
        self._display_generation += 1
        self._play_requested = False
        self.position = 0.0
        for player in self.players:
            player.stop()
        for video in self.video_widgets:
            video.hide()
        self.stack.setCurrentWidget(self.placeholder)
        self.positionChanged.emit(0.0)
        self.playingChanged.emit(False)

    def release(self):
        """Release Windows decoder handles so source files can be replaced."""
        self._stopped = True
        self._play_requested = False
        self._display_generation += 1
        self._queued = None
        for index, player in enumerate(self.players):
            self._setting_media[index] = True
            player.stop()
            player.setSource(QtCore.QUrl())
            self._sources[index] = ""
            self._pending_positions[index] = None
            self._seek_pending[index] = False
            self._last_frames[index] = None
            self._setting_media[index] = False
            self.video_widgets[index].hide()
            self.video_widgets[index].set_frame(None)
        self.stack.setCurrentWidget(self.placeholder)
        self.source, self.position = "", 0.0
        self.playingChanged.emit(False)

    def _reveal(self, slot):
        if slot != self._active:
            return
        video = self.video_widgets[slot]
        self.stack.setCurrentWidget(video)
        video.show()

    def _frame_probed(self, slot, frame):
        """Reveal only after the decoder supplied a real frame for this seek."""
        if (self._setting_media[slot] or not self._sources[slot] or not frame.isValid()
                or slot == self._active and self._stopped):
            return
        timestamp = frame.startTime()
        milliseconds = timestamp / 1000.0 if timestamp >= 0 else self.players[slot].position()
        rate = frame.surfaceFormat().frameRate()
        tolerance = max(60.0, min(250.0, 1100.0 / rate)) if rate > 0 else 150.0
        if self._seek_pending[slot] and abs(milliseconds - self._target_positions[slot]) > tolerance:
            return
        if slot == self._active and self._finishing_range:
            return
        if slot == self._active and self._play_requested and milliseconds >= self.range_end * 1000:
            self._finish_current_range(slot)
            return
        self._last_frames[slot] = self._multimedia.QVideoFrame(frame)
        self.video_widgets[slot].set_frame(self._last_frames[slot])
        self._seek_pending[slot] = False
        if slot != self._active:
            return
        self._reveal(slot)
        self._publish_position(slot, milliseconds)
        if self._play_requested and not self._finishing_range:
            self.players[slot].play()

    def _media_status_changed(self, slot, status):
        ready_states = {
            self._multimedia.QMediaPlayer.MediaStatus.LoadedMedia,
            self._multimedia.QMediaPlayer.MediaStatus.BufferedMedia,
        }
        if status in ready_states:
            pending = self._pending_positions[slot]
            if pending is not None:
                self._pending_positions[slot] = None
                self.players[slot].setPosition(pending)
            if self._seek_pending[slot]:
                # Loading leaves QMediaPlayer stopped. Enter paused decoding
                # explicitly so a seek can deliver its still frame silently.
                self.players[slot].pause()
            if self.players[slot].hasAudio() and not self.players[slot].hasVideo():
                self._seek_pending[slot] = False
            if slot == self._active and self._play_requested and not self._seek_pending[slot]:
                self.players[slot].play()
        elif (
            status == self._multimedia.QMediaPlayer.MediaStatus.InvalidMedia
            and slot == self._active
        ):
            self._player_error(slot)
        elif (
            status == self._multimedia.QMediaPlayer.MediaStatus.EndOfMedia
            and slot == self._active
            and self._play_requested
            and not self._seek_pending[slot]
            and not self._finishing_range
        ):
            self._finish_current_range(slot)

    def _position_changed(self, slot, milliseconds):
        # Video progress follows presented frame timestamps. This event can
        # report a new seek target while the old frame is still on screen.
        if slot != self._active or self._setting_media[slot] or self._seek_pending[slot]:
            return
        if not self.players[slot].hasVideo():
            self._publish_position(slot, milliseconds)

    def _publish_position(self, slot, milliseconds):
        absolute = max(0.0, float(milliseconds) / 1000.0)
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, absolute - self.range_start),
        )
        self.positionChanged.emit(self.position)
        if (
            self._play_requested
            and not self._finishing_range
            and absolute >= self.range_end - 0.025
        ):
            self._finish_current_range(slot)

    def _finish_current_range(self, slot):
        if self._finishing_range:
            return
        self._finishing_range = True
        self._play_requested = False
        self.players[slot].pause()
        self.position = self.range_end - self.range_start
        self.positionChanged.emit(self.position)
        generation = self._display_generation
        def finished():
            if generation == self._display_generation and self._finishing_range and slot == self._active:
                self.rangeFinished.emit()
        QtCore.QTimer.singleShot(0, finished)

    def _state_changed(self, slot, state):
        if slot != self._active:
            return
        playing = (
            state == self._multimedia.QMediaPlayer.PlaybackState.PlayingState
        )
        self.playingChanged.emit(playing or self._play_requested)

    def _player_error(self, slot):
        if slot != self._active:
            self._queued = None
            return
        if self._reported_error:
            return
        self._reported_error = True
        self._play_requested = False
        self.players[slot].pause()
        self.placeholder.setText("预览解码失败，可选择其他片段重试。")
        self.stack.setCurrentWidget(self.placeholder)
        self.playingChanged.emit(False)
        message = self.players[slot].errorString() or "未知的媒体解码错误"
        self.errorOccurred.emit(f"Qt 原生播放器无法播放该视频：{message}")

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)


class QtMediaReviewSurface(QtWidgets.QFrame):
    """Shared Qt6 preview with the historical range/queue API kept compatible."""
    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, ffplay_path="", parent=None, preload_enabled=True):
        super().__init__(parent)
        # The positional path argument remains accepted by existing consumers;
        # native Qt playback neither resolves nor starts an external executable.
        self._backend_kind = "qt"
        self._last_prepare = ("", 0.0, 0.01, 0.0)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self.backend = _QtMediaReviewSurface(self, preload_enabled=preload_enabled)
        layout.addWidget(self.backend)
        self.backend.positionChanged.connect(self.positionChanged)
        self.backend.playingChanged.connect(self.playingChanged)
        self.backend.rangeFinished.connect(self.rangeFinished)
        self.backend.errorOccurred.connect(self.errorOccurred)

    def configure_ffplay(self, _ffmpeg_path=""):
        """Compatibility no-op: preview uses Qt6's bundled multimedia backend."""

    def is_playing(self):
        return self.backend.is_playing()

    def prepare(self, source, start, end, position=0.0):
        self._last_prepare = (source, start, end, position)
        return self.backend.prepare(source, start, end, position)

    def queue(self, source, start, end):
        return self.backend.queue(source, start, end)

    def clear_queue(self):
        self.backend.clear_queue()

    def play(self):
        return self.backend.play()

    def pause(self, show_frame=True):
        return self.backend.pause(show_frame=show_frame)

    def reset_source(self):
        self._last_prepare = ("", 0.0, 0.01, 0.0)
        return self.backend.reset_source()

    def stop(self):
        return self.backend.stop()

    def release(self):
        self.backend.release()

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)


# Existing plugin imports and third-party callers retain one shared class.
FfplayReviewSurface = QtMediaReviewSurface

"""Main-thread, parent-owned reminder audio with a repeated beep fallback."""
from pathlib import Path
from qt_compat import QtCore, QtMultimedia, QtWidgets


class ReminderAudio(QtCore.QObject):
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = None
        self.output = None
        self.active = False
        self.beep_timer = QtCore.QTimer(self)
        self.beep_timer.setInterval(4000)
        self.beep_timer.timeout.connect(self.beep)

    def start(self, settings):
        self.stop()
        if not settings.get("sound", True):
            return
        self.active = True
        path = Path(settings.get("sound_file", "")).expanduser()
        if settings.get("sound_file"):
            if not path.is_file():
                self._fallback("提醒歌曲不存在，已改为循环警告音。")
                return
            if QtMultimedia is None:
                self._fallback("音频播放器不可用，已改为循环警告音。")
                return
            try:
                if self.player is None:
                    self.player = QtMultimedia.QMediaPlayer(self)
                    self.output = QtMultimedia.QAudioOutput(self)
                    self.player.setAudioOutput(self.output)
                    self.player.errorOccurred.connect(self._media_error)
                self.output.setVolume(settings.get("sound_volume", 60)/100)
                self.player.setLoops(QtMultimedia.QMediaPlayer.Loops.Infinite)
                self.player.setSource(QtCore.QUrl.fromLocalFile(str(path.resolve())))
                self.player.play()
                return
            except Exception:
                self._fallback("提醒歌曲无法播放，已改为循环警告音。")
                return
        self._fallback()

    def _media_error(self, *_):
        if self.active:
            self._fallback("提醒歌曲解码失败，已改为循环警告音。")

    def _fallback(self, error=""):
        if self.player:
            self.player.stop()
        if error:
            self.failed.emit(error)
        self.beep()
        self.beep_timer.start()

    def beep(self):
        if not self.active:
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError):
            QtWidgets.QApplication.beep()

    def stop(self):
        self.active = False
        self.beep_timer.stop()
        if self.player:
            self.player.stop()
            self.player.setSource(QtCore.QUrl())

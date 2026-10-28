"""Reuse the host playback surface; never render or download to preview a result."""
from pathlib import Path
import math
from qt_compat import QtCore, QtWidgets


class ResultPreview(QtWidgets.QDialog):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        from app_paths import APP_ROOT
        from model.MediaPlayback import FfplayReviewSurface
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        remote = row.get('remote') if isinstance(row.get('remote'), dict) else {}
        media = remote.get('media') if isinstance(remote.get('media'), dict) else {}
        from .suspicious_download import output_path
        self.path = output_path(row)
        if not isinstance(self.path, (str, Path)) or not self.path or not Path(self.path).is_file():
            raise ValueError('成品不存在，请先完成下载归档。')
        self.path = str(self.path)
        self.duration = 10.0
        for value in (media.get('duration'), payload.get('duration')):
            try:
                duration = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if not isinstance(value, bool) and math.isfinite(duration) and duration > 0:
                self.duration = duration
                break
        self.setWindowTitle('Flow 成品预览 · ' + Path(self.path).name); self.resize(600, 780)
        layout = QtWidgets.QVBoxLayout(self)
        self.surface = FfplayReviewSurface(str(APP_ROOT / 'runtime/ffmpeg/ffmpeg.exe'), self, preload_enabled=False)
        layout.addWidget(self.surface, 1)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self); self.slider.setRange(0, 10000)
        layout.addWidget(self.slider)
        row_layout = QtWidgets.QHBoxLayout()
        play = QtWidgets.QPushButton('播放', self); play.clicked.connect(self.surface.play); row_layout.addWidget(play)
        pause = QtWidgets.QPushButton('暂停', self); pause.clicked.connect(lambda: self.surface.pause()); row_layout.addWidget(pause)
        self.note = QtWidgets.QLabel(self); row_layout.addWidget(self.note, 1); layout.addLayout(row_layout)
        self.surface.positionChanged.connect(self.position)
        self.surface.errorOccurred.connect(self.note.setText)
        self.slider.sliderReleased.connect(self.seek)
        self.surface.prepare(self.path, 0, self.duration, 0)

    def position(self, seconds):
        if not self.slider.isSliderDown(): self.slider.setValue(round(seconds / self.duration * 10000))
        self.note.setText(f'{seconds:.1f} / {self.duration:.1f} 秒')

    def seek(self):
        self.surface.prepare(self.path, 0, self.duration, self.slider.value() / 10000 * self.duration)
        self.surface.play()

    def closeEvent(self, event):
        self.surface.release()
        super().closeEvent(event)

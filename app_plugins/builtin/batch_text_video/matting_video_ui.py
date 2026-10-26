"""Video timeline and latest-frame ownership inside the existing matte canvas."""
import copy
import time
from qt_compat import QtCore, QtWidgets
from model.QtThreadLifecycle import connect_thread_finished
from .matting_video import MattingVideoWorker, frame_position


def timestamp(seconds):
    millis = max(0, round(seconds*1000)); minutes, rest = divmod(millis, 60000)
    return f'{minutes:02d}:{rest/1000:06.3f}'


class MattingVideoController:
    def init_video_preview(self, layout):
        self.video_worker, self._pending_video = None, None
        self.video_info, self._video_expected = None, None
        self.video_position, self._video_generation = 0., 0
        self._video_playing, self._video_source = False, False
        self._video_has_frame = False
        self._video_last_tick = None
        self.video_controls = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(self.video_controls); root.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.video_play = QtWidgets.QPushButton('播放抠像预览')
        self.video_previous = QtWidgets.QPushButton('上一帧'); self.video_next = QtWidgets.QPushButton('下一帧')
        for button in (self.video_play, self.video_previous, self.video_next): button.setAutoDefault(False); row.addWidget(button)
        self.video_play.clicked.connect(self.toggle_video)
        self.video_previous.clicked.connect(lambda: self.step_video(-1)); self.video_next.clicked.connect(lambda: self.step_video(1))
        self.video_time = QtWidgets.QLabel('00:00.000 / 00:00.000'); row.addWidget(self.video_time, 1)
        root.addLayout(row)
        self.video_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.video_slider.setRange(0, 0); root.addWidget(self.video_slider)
        self.video_slider.setToolTip('拖动后松开定位并抠像当前帧；方向键微调。播放使用当前参数，暂停后可涂抹。')
        self.video_seek_timer = QtCore.QTimer(self); self.video_seek_timer.setSingleShot(True); self.video_seek_timer.setInterval(160)
        self.video_seek_timer.timeout.connect(lambda: self.seek_video(self.video_slider.value()/1000))
        self.video_slider.sliderPressed.connect(self.pause_video)
        self.video_slider.sliderReleased.connect(lambda: self.seek_video(self.video_slider.value()/1000))
        self.video_slider.valueChanged.connect(self.video_slider_changed)
        layout.addWidget(self.video_controls); self.video_controls.hide()

    def reset_video_source(self, path):
        self.pause_video(); self.video_info, self._video_expected = None, None; self.video_position = 0.
        from .visual_media import is_animated
        self._video_source = is_animated(path)
        self._video_has_frame = False
        self.video_controls.setVisible(self._video_source)
        self.video_slider.setRange(0, 0); self.video_time.setText('00:00.000 / 00:00.000')

    def configure_video(self, info, expected):
        if not self._video_source: return
        self.video_info, self._video_expected = info, expected
        self.video_slider.setRange(0, round(info['duration']*1000))
        self.video_slider.setSingleStep(max(1, round(1000/info['fps']))); self.video_slider.setPageStep(1000)
        self.update_video_position(self.video_position)

    def update_video_position(self, position):
        self.video_position = position
        blocker = QtCore.QSignalBlocker(self.video_slider)
        self.video_slider.setValue(round(position*1000)); del blocker
        self.video_time.setText(f'{timestamp(position)} / {timestamp(self.video_info["duration"] if self.video_info else 0)}')

    def video_slider_changed(self, value):
        if not self.video_info: return
        self.video_time.setText(f'{timestamp(value/1000)} / {timestamp(self.video_info["duration"])}')
        if not self.video_slider.isSliderDown(): self.video_seek_timer.start()

    def pause_video(self, *_):
        was_playing = self._video_playing
        self.video_seek_timer.stop(); self._video_generation += 1
        self._video_playing = False; self._pending_video = None
        if self.video_worker: self.video_worker.stop()
        if was_playing and hasattr(self, 'status') and not self._closing:
            self.status.setText('已暂停视频预览；可涂抹或调整当前帧。')
        if hasattr(self, 'generate'): self.refresh_enabled()

    def toggle_video(self):
        if self._video_playing: self.pause_video(); return
        if not self.video_info: return
        position = self.video_position
        if self._video_has_frame:
            position += 1/self.video_info['fps']
            if position >= (self.video_info['frames']-.5)/self.video_info['fps']: position = 0.
        self.request_video(position, True)

    def step_video(self, offset):
        if self.video_info: self.seek_video(self.video_position+offset/self.video_info['fps'])

    def seek_video(self, seconds): self.request_video(seconds, False)

    def request_video(self, seconds, play):
        if not self.video_info or self.worker or not self._parameters_valid or self._closing: return
        self.pause_video(); self.timer.stop(); self._serial += 1; self._pending = None; self._user_cancelled = False
        self.update_video_position(frame_position(self.video_info, seconds)); self._video_last_tick = None
        if self.base is not None and abs(self.base.get('position', 0)-self.video_position) > .000001: self.base = None
        if play: self.brush.setCurrentIndex(self.brush.findData('view'))
        request = dict(path=self.path, values=copy.deepcopy(self.values), info=copy.deepcopy(self.video_info),
            position=self.video_position, play=play, generation=self._video_generation, expected=self._video_expected,
            models=self.models, ffmpeg=self.ffmpeg, original_only=self.view.currentData() == 'original')
        self._video_playing = play
        if self.video_worker: self._pending_video = request
        else: self.start_video(request)
        self.refresh_enabled()

    def start_video(self, request):
        self.video_worker = MattingVideoWorker(request, self)
        self.video_worker.frameReady.connect(self.video_frame_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.video_worker.failed.connect(self.video_failed, QtCore.Qt.ConnectionType.QueuedConnection)
        self.video_worker.message.connect(self.video_message, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.video_worker, self, 'video_worker', self.video_finished)
        self.status.setText('正在读取原片…' if request.get('original_only') else
            '正在准备AI逐帧预览；速度取决于设备，可随时暂停。' if request['play'] else '正在定位并抠像所选帧…')
        self.video_worker.start()

    def video_frame_ready(self, result):
        worker = self.sender()
        try:
            if (self._closing or result['generation'] != self._video_generation or result['path'] != self.path
                    or result['values'] != self.values): return
            self.base = result['base']; self._video_has_frame = True; self.update_video_position(result['position'])
            self.canvas.set_images(result['original'], result['image'])
            now = time.monotonic(); elapsed = now-self._video_last_tick if self._video_last_tick else 0
            self._video_last_tick = now
            speed = f' · {1/elapsed:.1f}帧/秒' if elapsed > 0 else ''
            title = ('原片预览（无声）' if result['original_only'] else f'AI逐帧预览（{result["base"]["device"]}，无声）') if self._video_playing else '已定位当前帧'
            self.status.setText(title+speed)
            self.refresh_enabled()
        finally:
            if worker: worker.acknowledge()

    def video_failed(self, result):
        if result['generation'] == self._video_generation:
            self._video_playing = False; self.status.setText('视频预览失败：'+result['error'])

    def video_message(self, result):
        if result['generation'] == self._video_generation: self.status.setText(result['text'])

    def video_finished(self):
        ended = (self._video_playing and not self.video_worker.cancel.is_set() and not self.video_worker.error
                 and self.video_worker.request['generation'] == self._video_generation)
        self.video_worker.deleteLater(); self.video_worker = None
        if self._closing: self.close(); return
        pending, self._pending_video = self._pending_video, None
        if pending and pending['generation'] == self._video_generation and pending['path'] == self.path:
            self._video_playing = pending['play']; self.start_video(pending)
        else: self._video_playing = False
        if self._user_cancelled and not self.worker: self.status.setText('已取消视频预览，原素材与已保存参数保留。')
        elif ended and pending is None: self.status.setText('视频预览已结束；可逐帧检查，或点击播放从头重播。')
        self.refresh_enabled()

    def refresh_video_controls(self):
        ready = bool(self._video_source and self.video_info and not self.worker and not self._closing and self._parameters_valid)
        self.video_play.setText('暂停' if self._video_playing else '播放原片' if self.view.currentData() == 'original' else '播放抠像预览')
        self.video_play.setEnabled(ready)
        self.video_slider.setEnabled(ready)
        self.video_previous.setEnabled(ready and not self._video_playing)
        self.video_next.setEnabled(ready and not self._video_playing)

    def video_view_changed(self):
        if not self._video_source or not self.video_info: return
        original_only = self.view.currentData() == 'original'
        if self.video_worker and original_only != self.video_worker.request.get('original_only', False):
            self.request_video(self.video_position, self._video_playing)
        elif not original_only and self.base is None and self.video_worker is None:
            self.seek_video(self.video_position)
        self.refresh_video_controls()

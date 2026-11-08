"""An explicit A/B editor: original source time, frame steps, bounded playback."""
import copy
from collections import deque
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from model.QtThreadLifecycle import connect_thread_finished
from .visual_loop_region import KEY, identity, sources, stamp, normalize, validate
from .matting_video import MattingVideoWorker, describe, frame_position
from .matting_video_ui import timestamp


class FrameView(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.image = QtGui.QImage(); self.setMinimumSize(140, 85)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_image(self, image): self.image = image; self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#303238'))
        if self.image.isNull(): return
        size = self.image.size().scaled(self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        rect = QtCore.QRect(QtCore.QPoint((self.width()-size.width())//2, (self.height()-size.height())//2), size)
        painter.save(); painter.setClipRect(rect)
        for y in range(rect.top(), rect.bottom()+1, 16):
            for x in range(rect.left(), rect.right()+1, 16):
                color = '#797979' if ((x-rect.left())//16+(y-rect.top())//16)%2 else '#a0a0a0'
                painter.fillRect(x, y, 16, 16, QtGui.QColor(color))
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, self.image); painter.restore()


class SourceWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, path, generation, parent):
        super().__init__(parent); self.path, self.generation = path, generation

    def run(self):
        result = dict(path=self.path, generation=self.generation)
        try:
            from .engine import resolve_tools, probe
            before = stamp(self.path); ffmpeg, ffprobe = resolve_tools()
            info = describe(probe(self.path, ffprobe))
            if stamp(self.path) != before: raise ValueError('素材正在改变，请稍后重试。')
            result.update(info=info, stamp=before, ffmpeg=ffmpeg)
        except Exception as error: result['error'] = str(error)
        self.ready.emit(result)


class LoopRegionDialog(QtWidgets.QDialog):
    def __init__(self, layer, parent=None, *, trim_mode=False):
        super().__init__(parent)
        self.trim_mode = trim_mode
        self.setWindowTitle('裁切视频' if trim_mode else '视频循环区间 · 首遍保留开头，后续循环 A–B')
        self.resize(860, 720)
        self.rules = copy.deepcopy(layer.get(KEY, []))
        self.worker = self.loader = None
        self.pending = deque(); self.generation = 0
        self.info = self.source_stamp = None; self.path = ''; self.position = 0.
        self.playing = self.closing = self.loading = False; self.accept_after_stop = False
        self.source_index = -1; self.next_source = None
        root = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout(); root.addLayout(form)
        self.source = QtWidgets.QComboBox()
        for path in sources(layer):
            self.source.addItem(Path(path).name, path)
            self.source.setItemData(self.source.count()-1, path, QtCore.Qt.ItemDataRole.ToolTipRole)
        self.source.setToolTip('区间按这个控件的每个素材分别保存；不会改动资源池的生成顺序。')
        form.addRow('素材', self.source)
        self.enabled = QtWidgets.QCheckBox('使用裁切区间' if trim_mode else '为此素材指定后续循环区间')
        form.addRow(self.enabled)
        self.start = QtWidgets.QDoubleSpinBox(); self.end = QtWidgets.QDoubleSpinBox()
        for box in (self.start, self.end):
            box.setDecimals(6); box.setRange(0, 86400); box.setSuffix(' 秒'); box.setKeyboardTracking(False)
            box.installEventFilter(self)
        form.addRow('起点 A（包含）' if trim_mode else '循环起点 A（包含）', self.start); form.addRow('终点 B（不包含）' if trim_mode else '循环终点 B（不包含）', self.end)
        self.summary = QtWidgets.QLabel('正在读取素材…'); self.summary.setWordWrap(True); root.addWidget(self.summary)
        cards = QtWidgets.QHBoxLayout(); root.addLayout(cards)
        self.start_view, self.end_view = FrameView(), FrameView()
        for title, view, box in (('A · 后续每次从这里开始', self.start_view, self.start),
                                 ('B · 到这里之前结束（显示最后一帧）', self.end_view, self.end)):
            column = QtWidgets.QVBoxLayout(); cards.addLayout(column)
            label = QtWidgets.QLabel('A · 起始画面' if trim_mode and box is self.start else title); column.addWidget(label); column.addWidget(view, 1)
            jump = QtWidgets.QPushButton('定位到'+('起点 A' if box is self.start else '终点 B 前一帧'))
            jump.setAutoDefault(False); column.addWidget(jump)
            jump.clicked.connect(lambda checked=False, target=box: self.seek_boundary(target))
        self.view = FrameView(); self.view.setMinimumHeight(190); root.addWidget(self.view, 2)
        self.time = QtWidgets.QLabel('原片时间'); root.addWidget(self.time)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.slider.setRange(0, 0)
        root.addWidget(self.slider)
        row = QtWidgets.QHBoxLayout(); root.addLayout(row)
        actions = [('上一帧', lambda: self.seek(self.position-1/self.info['fps']) if self.info else None),
                   ('下一帧', lambda: self.seek(self.position+1/self.info['fps']) if self.info else None),
                   ('当前帧设为 A', self.mark_start), ('当前帧之后设为 B', self.mark_end)]
        for text, slot in actions:
            button = QtWidgets.QPushButton(text); button.setAutoDefault(False); button.clicked.connect(slot); row.addWidget(button)
        row = QtWidgets.QHBoxLayout(); root.addLayout(row)
        self.play = QtWidgets.QPushButton('试看后续循环 A→B')
        self.join = QtWidgets.QPushButton('试看首次接头 B→A')
        self.pause = QtWidgets.QPushButton('暂停')
        if trim_mode:
            self.play.setText('播放所选区间'); self.join.hide()
        for button in (self.play, self.join, self.pause): button.setAutoDefault(False); row.addWidget(button)
        self.play.clicked.connect(lambda: self.play_segment(False)); self.join.clicked.connect(lambda: self.play_segment(True))
        self.pause.clicked.connect(self.stop)
        note = QtWidgets.QLabel('首遍：从0秒播放到B；之后：只重复A–B。未设置区间的素材仍循环整段。\n'
            '这里无声试看原片动作；抠像、叠化和合成效果使用主窗口的播放预览。取消不会保存任何调整。')
        note.setWordWrap(True); root.addWidget(note)
        if trim_mode: note.hide()
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True); root.addWidget(self.status)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('保存区间')
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.save); self.buttons.rejected.connect(self.reject); root.addWidget(self.buttons)
        self.timer = QtCore.QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(220)
        self.timer.timeout.connect(self.preview_boundaries)
        self.seek_timer = QtCore.QTimer(self); self.seek_timer.setSingleShot(True); self.seek_timer.setInterval(140)
        self.seek_timer.timeout.connect(lambda: self.seek(self.slider.value()/self.info['fps']) if self.info else None)
        self.slider.valueChanged.connect(lambda: self.seek_timer.start() if not self.loading else None)
        self.start.valueChanged.connect(self.changed); self.end.valueChanged.connect(self.changed)
        self.enabled.toggled.connect(self.changed); self.source.currentIndexChanged.connect(self.choose_source)
        items = layer.get('sequence_items', []) if layer.get('source') in {'pool','sequence'} else []
        initial = items[layer.get('sequence_cursor',0)%len(items)].get('path','') if items else (layer.get('source_file') or layer.get('path',''))
        index = next((i for i in range(self.source.count()) if identity(self.source.itemData(i)) == identity(initial)),0)
        blocker = QtCore.QSignalBlocker(self.source); self.source.setCurrentIndex(index); del blocker
        self.choose_source(index)

    def eventFilter(self, watched, event):
        if isinstance(watched, QtWidgets.QAbstractSpinBox) and event.type() == QtCore.QEvent.Type.Wheel:
            event.ignore(); return True
        return super().eventFilter(watched, event)

    def choose_source(self, index):
        if index < 0: self.status.setText('此控件没有视频素材。'); self.refresh(); return
        if self.source_index >= 0 and self.info and not self.capture():
            blocker = QtCore.QSignalBlocker(self.source); self.source.setCurrentIndex(self.source_index); del blocker
            return
        self.stop(); self.source_index = index; self.path = self.source.itemData(index)
        self.info = None; self.source_stamp = None
        for view in (self.start_view, self.end_view, self.view): view.set_image(QtGui.QImage())
        self.status.setText('正在读取：'+self.path)
        if self.loader: self.next_source = (self.path, self.generation)
        else: self.load_source(self.path, self.generation)
        self.refresh()

    def load_source(self, path, generation):
        self.loader = SourceWorker(path, generation, self)
        self.loader.ready.connect(self.source_ready)
        connect_thread_finished(self.loader, self, 'loader', self.loaded)
        self.loader.start()

    def loaded(self):
        self.loader = None
        if self.closing: self.finish_close(); return
        pending, self.next_source = self.next_source, None
        if pending: self.load_source(*pending)

    def source_ready(self, result):
        if self.closing or result['generation'] != self.generation or result['path'] != self.path: return
        if result.get('error'): self.status.setText(result['error']); self.refresh(); return
        self.info, self.source_stamp, self.ffmpeg = result['info'], result['stamp'], result['ffmpeg']
        rule = next((r for r in self.rules if identity(r['path']) == identity(self.path)), None)
        self.loading = True
        self.enabled.setChecked(rule is not None or self.trim_mode)
        self.start.setRange(0, max(0., self.info['duration']-1/self.info['fps']))
        self.end.setRange(1/self.info['fps'], self.info['duration'])
        for box in (self.start, self.end): box.setSingleStep(1/self.info['fps'])
        self.start.setValue(min(rule['start'], self.start.maximum()) if rule else 0.)
        self.end.setValue(min(rule['end'], self.info['duration']) if rule else self.info['duration'])
        self.slider.setRange(0, self.info['frames']-1); self.position = 0.
        self.loading = False
        stale = bool(rule and self.source_stamp != {k:rule[k] for k in ('size','mtime_ns')})
        self.status.setText('原片已改变，请重新检查区间后保存。' if stale else '调整A、B，或拖动下方时间条逐帧选点。')
        self.changed()

    def refresh(self):
        ready = bool(self.info and not self.closing)
        for control in (self.enabled, self.slider, self.play, self.join, self.pause): control.setEnabled(ready)
        for control in (self.start, self.end): control.setEnabled(ready and self.enabled.isChecked())
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(ready)

    def changed(self):
        if self.loading or not self.info: return
        self.stop()
        a,b = self.start.value(), self.end.value()
        self.summary.setText(f'首遍 0 → {b:.3f} 秒；后续 {a:.3f} → {b:.3f} 秒（每次 {b-a:.3f} 秒）'
            if self.enabled.isChecked() else f'循环整段：0 → {self.info["duration"]:.3f} 秒')
        if self.trim_mode:
            self.summary.setText(f'仅使用 {a:.3f} → {b:.3f} 秒（{b-a:.3f} 秒）' if self.enabled.isChecked() else '使用整段视频')
        self.refresh(); self.timer.start()

    def capture(self):
        if not self.info: return False
        token = identity(self.path)
        rules = [r for r in self.rules if identity(r['path']) != token]
        if self.enabled.isChecked():
            rate = self.info['fps']
            a = round(self.start.value()*rate)/rate
            b = min(self.info['duration'], round(self.end.value()*rate)/rate)
            rule = dict(path=self.path, start=a, end=b,
                        duration=self.info['duration'], **self.source_stamp)
            try: normalize([rule]); validate(rule, self.info)
            except (ValueError, OSError) as error: self.status.setText(str(error)); return False
            rules.append(rule)
        try: rules = normalize(rules)
        except ValueError as error: self.status.setText(str(error)); return False
        self.rules = rules
        return True

    def stop(self):
        self.timer.stop(); self.seek_timer.stop(); self.generation += 1
        self.playing = False; self.pending.clear()
        if self.worker: self.worker.stop()

    def request(self, position, play=False, target='view'):
        if not self.info or self.closing: return
        self.pending.append(dict(path=self.path, values={}, info=self.info, position=frame_position(self.info, position),
            play=play, generation=self.generation, source_stamp=self.source_stamp, ffmpeg=self.ffmpeg,
            original_only=True, target=target))
        self.start_pending()

    def start_pending(self):
        if self.worker or self.closing or not self.pending: return
        request = self.pending.popleft()
        if request['generation'] != self.generation: self.start_pending(); return
        self.worker = MattingVideoWorker(request, self)
        self.worker.frameReady.connect(self.frame_ready)
        self.worker.failed.connect(self.failed)
        connect_thread_finished(self.worker, self, 'worker', self.finished)
        self.worker.start()

    def finished(self):
        self.worker = None
        if self.closing: self.finish_close(); return
        self.start_pending()

    def failed(self, result):
        if result['generation'] == self.generation:
            self.playing = False; self.pending.clear(); self.status.setText('预览失败：'+result['error'])

    def frame_ready(self, result):
        worker = self.sender()
        try:
            if self.closing or result['generation'] != self.generation or result['path'] != self.path: return
            target = worker.request.get('target', 'view')
            view = self.start_view if target == 'start' else self.end_view if target == 'end' else self.view
            view.set_image(result['original'])
            if target == 'view':
                self.position = result['position']; self.time.setText('原片时间 '+timestamp(self.position))
                self.loading = True; self.slider.setValue(round(self.position*self.info['fps'])); self.loading = False
            if self.playing and result['position']+1/self.info['fps'] >= self.play_end-1e-6:
                worker.stop()
                self.request(self.play_start, True)
        finally:
            if worker: worker.acknowledge()

    def preview_boundaries(self):
        if not self.info: return
        self.stop()
        self.request(self.start.value(), target='start')
        self.request(max(self.start.value(), self.end.value()-1/self.info['fps']), target='end')
        self.request(self.start.value())

    def seek(self, seconds):
        if not self.info: return
        self.stop(); self.request(seconds)

    def seek_boundary(self, box):
        self.seek(box.value() if box is self.start else box.value()-1/self.info['fps']) if self.info else None

    def mark_start(self): self.enabled.setChecked(True); self.start.setValue(self.position)

    def mark_end(self):
        if self.info:
            self.enabled.setChecked(True); self.end.setValue(min(self.info['duration'], self.position+1/self.info['fps']))

    def play_segment(self, join):
        if not self.info: return
        a,b = (self.start.value(), self.end.value()) if self.enabled.isChecked() else (0., self.info['duration'])
        if b-a < 1/self.info['fps']-1e-6: self.status.setText('循环区间至少需要一帧。'); return
        self.stop(); self.playing = True; self.play_start, self.play_end = a,b
        self.status.setText('正在无声试看原片动作，原片时间在接头处跳回A。')
        self.request(max(0., b-.8) if join else a, True)

    def save(self):
        if self.capture(): self.accept_after_stop = True; self.close()

    def reject(self): self.accept_after_stop = False; self.close()

    def finish_close(self):
        if self.closing and self.worker is None and self.loader is None:
            self.done(QtWidgets.QDialog.DialogCode.Accepted if self.accept_after_stop else QtWidgets.QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.closing = True; self.next_source = None; self.stop()
        if self.worker or self.loader: event.ignore(); self.status.setText('正在释放预览…'); return
        event.ignore(); self.finish_close()

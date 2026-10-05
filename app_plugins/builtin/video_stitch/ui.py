"""A non-modal two-file plugin UI; decoding/probing/export only run in a worker."""
import logging
from fractions import Fraction
from pathlib import Path
import threading
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from .engine import Canceled, export_pair, make_plan, probe, resolve_tools, thumbnail

logger = logging.getLogger(__name__)


class FileEdit(QtWidgets.QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("拖入一个本地视频，或点击选择…")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.setText(urls[0].toLocalFile())
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class FrameLabel(QtWidgets.QLabel):
    def __init__(self, parent=None):
        super().__init__("暂无视频", parent)
        self.image = QtGui.QImage()
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(120, 150)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background:#20242a;color:#dddddd;border-radius:4px;")

    def set_image(self, data):
        self.image = QtGui.QImage.fromData(data) if data else QtGui.QImage()
        self._refresh()

    def _refresh(self):
        if self.image.isNull():
            self.setText("暂无视频")
        else:
            self.setPixmap(QtGui.QPixmap.fromImage(self.image).scaled(
                self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()


class StitchWorker(QtCore.QThread):
    metadata = QtCore.pyqtSignal(object)
    progress = QtCore.pyqtSignal(int)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str, str)
    canceled = QtCore.pyqtSignal()

    def __init__(self, payload, render=False, parent=None):
        super().__init__(parent)
        self.payload = dict(payload)
        self.render = render
        self.cancel = threading.Event()

    def run(self):
        try:
            p = self.payload
            if self.render:
                result = export_pair(
                    p["left"], p["right"], p["output"], basis=p["basis"], audio=p["audio"],
                    compatible=p["compatible"], overwrite=p.get("overwrite", False), cancel=self.cancel,
                    progress=self.progress.emit,
                    report=lambda media, plan: self.metadata.emit({"media": media, "plan": plan}),
                )
                self.completed.emit(result)
            else:
                ffmpeg, ffprobe = resolve_tools()
                media = [probe(p[key], ffprobe, self.cancel) for key in ("left", "right")]
                plan = make_plan(*media, p["basis"], p["audio"], p["compatible"])
                images = [thumbnail(item, ffmpeg, self.cancel) for item in media]
                if self.cancel.is_set():
                    raise Canceled()
                self.metadata.emit({"media": media, "plan": plan, "images": images})
        except Canceled:
            self.canceled.emit()
        except Exception as exc:
            logger.exception("视频左右拼接%s失败", "导出" if self.render else "读取")
            self.failed.emit(f"{type(exc).__name__}: {exc}", traceback.format_exc())


class VideoStitchDialog(QtWidgets.QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self._worker = None
        self._pending_inspect = False
        self._close_after = False
        self._last_output = ""
        self._auto_output = ""
        self.setWindowTitle("视频拼接 · 左右")
        self.setWindowFlags(QtCore.Qt.WindowType.Window | QtCore.Qt.WindowType.WindowMinMaxButtonsHint
                            | QtCore.Qt.WindowType.WindowCloseButtonHint)
        self.setModal(False)
        self.resize(880, 600)
        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel("原尺寸左右拼接；较矮的一边居中补黑边。以选定视频的时长和帧率为准，另一边短则循环、长则裁掉。", self)
        tip.setWordWrap(True)
        layout.addWidget(tip)
        columns = QtWidgets.QHBoxLayout()
        self.paths, self.frames, self.info, self.browse_buttons = [], [], [], []
        for index, title in enumerate(("左侧视频", "右侧视频")):
            box = QtWidgets.QGroupBox(title, self)
            column = QtWidgets.QVBoxLayout(box)
            row = QtWidgets.QHBoxLayout()
            path = FileEdit(box)
            path.textChanged.connect(self._changed)
            button = QtWidgets.QPushButton("选择…", box)
            button.clicked.connect(lambda _checked=False, i=index: self._browse(i))
            row.addWidget(path, 1)
            row.addWidget(button)
            column.addLayout(row)
            frame = FrameLabel(box)
            column.addWidget(frame, 1)
            info = QtWidgets.QLabel("待选择视频", box)
            info.setWordWrap(True)
            column.addWidget(info)
            columns.addWidget(box, 1)
            self.paths.append(path)
            self.frames.append(frame)
            self.info.append(info)
            self.browse_buttons.append(button)
        layout.addLayout(columns, 1)
        options = QtWidgets.QHBoxLayout()
        self.swap = QtWidgets.QPushButton("交换左右", self)
        self.swap.clicked.connect(self._swap)
        options.addWidget(self.swap)
        options.addWidget(QtWidgets.QLabel("时长基准", self))
        self.basis = QtWidgets.QComboBox(self)
        self.basis.addItems(["左侧视频", "右侧视频"])
        self.basis.currentIndexChanged.connect(self._changed)
        options.addWidget(self.basis)
        options.addWidget(QtWidgets.QLabel("声音", self))
        self.audio = QtWidgets.QComboBox(self)
        for title, value in (("跟随时长基准", "basis"), ("左侧声音", "left"), ("右侧声音", "right"),
                             ("两侧混音", "mix"), ("静音", "mute")):
            self.audio.addItem(title, value)
        self.audio.currentIndexChanged.connect(self._changed)
        options.addWidget(self.audio)
        options.addStretch(1)
        layout.addLayout(options)
        self.compatible = QtWidgets.QCheckBox("兼容常用播放器（奇数宽高最多补 1 像素）", self)
        self.compatible.setChecked(True)
        self.compatible.setToolTip("关闭后严格保留拼接尺寸；奇数尺寸使用 H.264 4:4:4，部分硬件播放器可能不支持。")
        self.compatible.toggled.connect(self._changed)
        layout.addWidget(self.compatible)
        self.plan_label = QtWidgets.QLabel("选择两个视频后自动显示首帧和预计成品尺寸。", self)
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)
        output_row = QtWidgets.QHBoxLayout()
        output_row.addWidget(QtWidgets.QLabel("成品", self))
        self.output = QtWidgets.QLineEdit(self)
        output_row.addWidget(self.output, 1)
        self.output_button = QtWidgets.QPushButton("另存为…", self)
        self.output_button.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_button)
        layout.addLayout(output_row)
        self.progress = QtWidgets.QProgressBar(self)
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        bottom = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("等待视频。", self)
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.export = QtWidgets.QPushButton("开始拼接", self)
        self.export.clicked.connect(self._export)
        bottom.addWidget(self.export)
        self.cancel_button = QtWidgets.QPushButton("取消", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        bottom.addWidget(self.cancel_button)
        self.open_button = QtWidgets.QPushButton("打开成品目录", self)
        self.open_button.clicked.connect(self._open_folder)
        bottom.addWidget(self.open_button)
        layout.addLayout(bottom)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self._inspect)

    def is_busy(self):
        # Retain the guard until finished has been handled on the GUI thread.
        return self._worker is not None

    def _payload(self):
        return {"left": self.paths[0].text().strip(), "right": self.paths[1].text().strip(),
                "basis": self.basis.currentIndex(), "audio": self.audio.currentData(),
                "compatible": self.compatible.isChecked(), "output": self.output.text().strip()}

    def _browse(self, index):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择视频", self.paths[index].text(),
                                                     "视频 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.m2ts);;所有文件 (*)")
        if path:
            self.paths[index].setText(path)

    def _changed(self, *_args):
        if not hasattr(self, "timer"):
            return
        left = self.paths[0].text().strip()
        if left and self.output.text() in ("", self._auto_output):
            self._auto_output = str(Path(left).with_name(Path(left).stem + "_左右拼接.mp4"))
            self.output.setText(self._auto_output)
        self.plan_label.setText("正在更新视频信息…")
        self.timer.start()

    def _swap(self):
        widgets = self.paths + [self.basis, self.audio]
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        left, right = (p.text() for p in self.paths)
        self.paths[0].setText(right)
        self.paths[1].setText(left)
        self.basis.setCurrentIndex(1 - self.basis.currentIndex())
        if self.audio.currentData() in {"left", "right"}:
            self.audio.setCurrentIndex(3 - self.audio.currentIndex())
        del blockers
        self._changed()

    def _choose_output(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存拼接视频", self.output.text(), "MP4 视频 (*.mp4)")
        if path:
            self.output.setText(path if Path(path).suffix else path + ".mp4")

    def _inspect(self):
        p = self._payload()
        if not p["left"] or not p["right"]:
            self.plan_label.setText("请为左右两侧分别选择一个视频。")
            return
        if self.is_busy():
            if not self._worker.render:
                self._pending_inspect = True
                self._worker.cancel.set()
            return
        self._start(p, False)

    def _export(self):
        if self.is_busy():
            self.status.setText("正在读取或导出，请稍候。")
            return
        p = self._payload()
        if not all(p[k] for k in ("left", "right", "output")):
            self.status.setText("请先选择两个视频和成品保存路径。")
            return
        p["overwrite"] = False
        if Path(p["output"]).exists():
            answer = QtWidgets.QMessageBox.question(self, "覆盖成品？", "已有同名文件，是否覆盖？源视频永远不会被覆盖。",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            p["overwrite"] = True
        self.timer.stop()
        self._pending_inspect = False
        self._start(p, True)

    def _start(self, payload, render):
        worker = StitchWorker(payload, render, self)
        self._worker = worker
        worker.metadata.connect(self._metadata)
        worker.progress.connect(self.progress.setValue)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.canceled.connect(self._canceled)
        worker.finished.connect(self._finished)
        self.export.setEnabled(False)
        self.cancel_button.setEnabled(True)
        if render:
            self.progress.setValue(0)
            for widget in self.paths + self.browse_buttons + [self.swap, self.basis, self.audio,
                                                             self.compatible, self.output, self.output_button]:
                widget.setEnabled(False)
            self.status.setText("正在后台拼接…")
            self.context.log("视频拼接开始；成品：" + payload["output"])
        else:
            self.status.setText("正在后台读取首帧和视频信息…")
        worker.start()

    def _metadata(self, data):
        if self._worker is None:
            return
        current = self._payload()
        if any(current[key] != self._worker.payload[key] for key in ("left", "right", "basis", "audio", "compatible")):
            return
        for index, media in enumerate(data["media"]):
            self.info[index].setText(f"{media.width} × {media.height} · {media.duration:.3f} 秒 · "
                                    f"{float(media.fps):.3f} fps · {'有声音' if media.audio_index >= 0 else '无声音'}")
        for index, image in enumerate(data.get("images", [])):
            self.frames[index].set_image(image)
        p = data["plan"]
        action = "循环播放" if p["loop"] else "按基准时长截取"
        self.plan_label.setText(f"成品：{p['width']} × {p['height']} · {p['duration']:.3f} 秒 · "
                               f"{float(Fraction(p['fps'])):.3f} fps；另一侧{action}。"
                               + (" 所选声源没有声音，成品为静音。" if p["missing_audio"] else ""))
        if not self._worker.render:
            self.status.setText("首帧预览已更新，可以开始拼接。")

    def _completed(self, result):
        self._last_output = result["output"]
        self.progress.setValue(100)
        self.status.setText("完成：" + Path(self._last_output).name)
        self.context.log("视频拼接完成：" + self._last_output)

    def _failed(self, message, detail):
        self.status.setText(message)
        self.context.log(detail)
        if self._worker is not None and self._worker.render:
            QtWidgets.QMessageBox.warning(self, "视频拼接失败", message)

    def _canceled(self):
        self.status.setText("已取消，源视频与已有成品未改动。")

    def _finished(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        for widget in self.paths + self.browse_buttons + [self.swap, self.basis, self.audio, self.compatible,
                                                         self.output, self.output_button, self.export]:
            widget.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self._close_after:
            self._close_after = False
            self._pending_inspect = False
            self.timer.stop()
            self.hide()
        elif self._pending_inspect:
            self._pending_inspect = False
            self.timer.start(0)

    def cancel_work(self):
        self.timer.stop()
        self._pending_inspect = False
        if self._worker is not None:
            self._worker.cancel.set()
            self.status.setText("正在取消…")

    def _open_folder(self):
        raw = self._last_output or self.output.text().strip()
        if not raw or not Path(raw).parent.is_dir():
            self.status.setText("成品目录尚不存在，请先选择路径或导出。")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(raw).resolve().parent)))

    def closeEvent(self, event):
        self.timer.stop()
        if self.is_busy():
            self._close_after = True
            self.cancel_work()
            event.ignore()
        else:
            event.accept()

"""Non-destructive composite playback. Previewing never commits rotations."""
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import traceback

from qt_compat import QtCore, QtWidgets
from .engine import Cancelled, probe, render_static_text, resolve_tools


def preview_key(job, state):
    settings = copy.deepcopy(state["settings"])
    for field in ("output_dir", "overwrite", "geometry_units", "show_layer_bounds"):
        settings.pop(field, None)
    paths = [job.get("path"), job.get("voice_path")]
    paths += [item.get("path") for item in state["music"]]
    for layer in settings["layers"]:
        if layer["kind"] == "image":
            paths += [layer.get("path")] + [item.get("path") for item in layer.get("sequence_items", [])]
    # Missing explicit voice paths are resolved by the renderer just like exports.
    if settings.get("use_voice") and not job.get("voice_path"):
        from .matching import folder_entry
        paths.append(folder_entry(job.get("task_dir") or Path(job["path"]).parent).get("voice_path"))
    fingerprints = []
    for value in paths:
        if not value:
            continue
        path = Path(value).resolve()
        try:
            info = path.stat()
            fingerprints.append((str(path), info.st_size, info.st_mtime_ns))
        except OSError:
            fingerprints.append((str(path), None, None))
    values = ["playback-v1", settings, job.get("title", ""), job.get("body", ""),
              job.get("requires_title", False), fingerprints, state["music"], state["next_music_id"]]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf8")).hexdigest()


class PlaybackWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int)

    def __init__(self, job, state, directory, key, parent=None):
        super().__init__(parent)
        self.job, self.state = copy.deepcopy(job), copy.deepcopy(state)
        self.directory, self.key = Path(directory), key
        self.cancel = threading.Event()

    def run(self):
        result = {"key": self.key}
        try:
            cache = self.directory / "playback-cache"
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / (self.key + ".mp4")
            _, ffprobe = resolve_tools(self.state["settings"]["ffmpeg_path"])
            duration = None
            if target.is_file():
                try:
                    duration = probe(target, ffprobe)["duration"]
                except (OSError, ValueError, RuntimeError):
                    pass
            if self.cancel.is_set():
                raise Cancelled("预览已取消。")
            if duration is None:
                if self.state["settings"]["use_voice"] and not self.job.get("voice_path"):
                    from .matching import folder_entry
                    match = folder_entry(self.job.get("task_dir") or Path(self.job["path"]).parent)
                    if match["match_error"]:
                        raise ValueError(match["match_error"])
                    self.job["voice_path"] = match["voice_path"]
                self.job.update(name=self.key, task_dir=str(cache))
                self.state["settings"]["overwrite"] = True
                target, _ = render_static_text(self.job, self.state, self.directory,
                    self.cancel, self.progress.emit, self.message.emit, preview=True)
                duration = probe(target, ffprobe)["duration"]
            else:
                self.message.emit("复用播放预览缓存，不重复渲染。")
                target.touch()
            if self.cancel.is_set():
                raise Cancelled("预览已取消。")
            # Only generated cache files; never source files or task outputs.
            files = [path for path in cache.glob("*.mp4") if re.fullmatch(r"[0-9a-f]{64}\.mp4", path.name)]
            files.sort(key=lambda path: path.stat().st_mtime)
            total = sum(path.stat().st_size for path in files)
            count = len(files)
            for path in files:
                if total <= 256 * 1024**2 and count <= 12:
                    break
                if path == target:
                    continue
                size = path.stat().st_size
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
                count -= 1
            result.update(path=str(target), duration=duration)
        except Cancelled:
            result["cancelled"] = True
        except Exception as error:
            result.update(error=str(error), traceback=traceback.format_exc())
        self.ready.emit(result)


class PlaybackController:
    def init_playback(self, layout):
        self.playback_worker = None
        self.playback_surface = None
        self._playback_key = None
        self._playback_file = ""
        self._playback_duration = 0.0
        self._batch_after_preview = None
        self.preview_stack = QtWidgets.QStackedWidget()
        self.preview_stack.addWidget(self.preview)
        layout.addWidget(self.preview_stack, 1)
        row = QtWidgets.QHBoxLayout()
        self.play_preview_button = QtWidgets.QPushButton("播放预览")
        self.play_preview_button.setAutoDefault(False)
        self.play_preview_button.setToolTip("首次后台生成低分辨率完整预览，包含当前图层、人声和音乐；不推进任何轮换记录。再次播放复用缓存。")
        self.play_preview_button.clicked.connect(self.play_preview)
        self.edit_preview_button = QtWidgets.QPushButton("返回编辑")
        self.edit_preview_button.setAutoDefault(False)
        self.edit_preview_button.clicked.connect(self.return_to_editor)
        self.quick_export_button = QtWidgets.QPushButton("快速导出")
        self.quick_export_button.setAutoDefault(False)
        self.quick_export_button.setToolTip("Ctrl+E：只将当前预览的搭配导出为正式成品，不生成其他队列任务；沿用成品分辨率、任务目录及覆盖设置。成功后保存轮换进度。")
        self.quick_export_button.clicked.connect(self.quick_export_current)
        self.playback_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.playback_slider.setRange(0, 0)
        self.playback_slider.setEnabled(False)
        self.playback_slider.sliderReleased.connect(self.seek_preview)
        self.playback_time = QtWidgets.QLabel("00:00 / 00:00")
        for widget in (self.play_preview_button, self.edit_preview_button, self.quick_export_button):
            row.addWidget(widget)
        row.addWidget(self.playback_slider, 1)
        row.addWidget(self.playback_time)
        layout.addLayout(row)
        self.playback_status = QtWidgets.QLabel("首帧用于拖动排版；点击“播放预览”查看完整视频。")
        self.playback_status.setWordWrap(True)
        layout.addWidget(self.playback_status)

    def invalidate_playback(self):
        self._playback_key = None
        self._playback_file = ""
        if self.playback_worker:
            self.playback_worker.cancel.set()
        if self.playback_surface:
            self.playback_surface.release()
            self.preview_stack.removeWidget(self.playback_surface)
            self.playback_surface.deleteLater()
            self.playback_surface = None
        self.preview_stack.setCurrentWidget(self.preview)
        self.playback_slider.setEnabled(False)
        self.playback_slider.setRange(0, 0)
        self.playback_time.setText("00:00 / 00:00")
        self.play_preview_button.setText("播放预览")

    def play_preview(self):
        if self.is_batch_busy():
            self.playback_status.setText("正在批量处理，请稍后播放预览。")
            return
        if self.playback_worker:
            self.playback_worker.cancel.set()
            self._playback_key = None
            self.playback_status.setText("正在取消预览生成…")
            return
        try:
            job = copy.deepcopy(self.active_entry())
            if not job.get("path") or not Path(job["path"]).is_file():
                raise ValueError("请先选择一个有效的背景视频。")
            job.update(title=self.title.toPlainText(), body=self.body.toPlainText())
            state = copy.deepcopy(self.state)
            state["settings"] = self.read_settings()
            key = preview_key(job, state)
            if key == self._playback_key and self._playback_file and Path(self._playback_file).is_file():
                self.preview_stack.setCurrentWidget(self.playback_surface)
                if self.playback_surface.is_playing():
                    self.playback_surface.pause()
                else:
                    self.playback_surface.play()
                return
            self.invalidate_playback()
            self._playback_key = key
            self.playback_status.setText("正在后台准备完整播放预览；可以取消，不会生成正式成品。")
            self.play_preview_button.setText("取消准备")
            self.playback_worker = PlaybackWorker(job, state, self.store.directory, key, self)
            self.playback_worker.ready.connect(self._playback_ready, QtCore.Qt.ConnectionType.QueuedConnection)
            self.playback_worker.message.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
            self.playback_worker.progress.connect(self._playback_progress, QtCore.Qt.ConnectionType.QueuedConnection)
            self.playback_worker.finished.connect(self._playback_finished)
            self.playback_worker.start()
        except Exception as error:
            self.playback_status.setText("预览无法播放：" + str(error))
            self._log("播放预览失败：" + traceback.format_exc())

    def _playback_progress(self, value):
        self.playback_status.setText(f"正在准备完整播放预览：{value}%（可取消）")

    def _playback_ready(self, result):
        if result["key"] != self._playback_key:
            return
        if result.get("cancelled"):
            self.playback_status.setText("已取消播放预览；资源和轮换位置未改变。")
            return
        if result.get("error"):
            self.playback_status.setText("播放预览失败：" + result["error"])
            self._log("播放预览失败：" + result.get("traceback", result["error"]))
            return
        try:
            if self.playback_surface is None:
                from app_plugins.builtin.smart_video_editor.player import FfplayReviewSurface
                self.playback_surface = FfplayReviewSurface(parent=self.preview_stack, preload_enabled=False)
                self.playback_surface.positionChanged.connect(self._playback_position)
                self.playback_surface.playingChanged.connect(self._playback_playing)
                self.playback_surface.errorOccurred.connect(self._playback_error)
                self.preview_stack.addWidget(self.playback_surface)
            self._playback_file, self._playback_duration = result["path"], result["duration"]
            self.playback_slider.setRange(0, round(self._playback_duration*1000))
            self.playback_slider.setEnabled(True)
            self.preview_stack.setCurrentWidget(self.playback_surface)
            self.playback_surface.prepare(self._playback_file, 0, self._playback_duration, 0)
            self.playback_surface.play()
            self.playback_status.setText("播放的是低分辨率合成预览；正式导出仍使用所选画布分辨率。")
        except Exception:
            self._log("播放器初始化失败：" + traceback.format_exc())
            self._playback_error("播放器初始化失败，请查看程序日志。")

    def _playback_finished(self):
        self.playback_worker.deleteLater()
        self.playback_worker = None
        self.play_preview_button.setText("暂停" if self.playback_surface and self.playback_surface.is_playing() else "播放预览")
        if self._batch_after_preview is not None:
            QtCore.QTimer.singleShot(0,self._start_pending_batch)

    def _start_pending_batch(self):
        selected, self._batch_after_preview = self._batch_after_preview, None
        if isinstance(selected, dict):
            self._start_quick_export(selected)
        elif selected is not None:
            self.start_batch(selected)

    def _playback_playing(self, playing):
        if self.sender() is not None and self.sender() is not self.playback_surface:
            return
        self.play_preview_button.setText("暂停" if playing else "播放预览")

    def _playback_position(self, value):
        if self.sender() is not None and self.sender() is not self.playback_surface:
            return
        if not self.playback_slider.isSliderDown():
            self.playback_slider.setValue(round(value*1000))
        def text(seconds):
            value = max(0, int(seconds))
            return f"{value//60:02}:{value%60:02}"
        self.playback_time.setText(text(value) + " / " + text(self._playback_duration))

    def seek_preview(self):
        if not self.playback_surface or not self._playback_file:
            return
        playing = self.playback_surface.is_playing()
        value = self.playback_slider.value()/1000
        self.playback_surface.prepare(self._playback_file, 0, self._playback_duration, value)
        if playing:
            self.playback_surface.play()

    def return_to_editor(self):
        if self.playback_surface:
            self.playback_surface.pause()
        self.preview_stack.setCurrentWidget(self.preview)

    def _playback_error(self, message):
        self.return_to_editor()
        self.playback_status.setText("播放器错误：" + str(message))
        self._log("播放器错误：" + str(message))

    def stop_playback(self):
        self.invalidate_playback()

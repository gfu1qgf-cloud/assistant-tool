"""Non-destructive composite playback. Previewing never commits rotations."""
from model.AppDataStorage import data_directory, data_file
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import traceback

from qt_compat import QtCore, QtWidgets
from .engine import Cancelled, probe, render_static_text, resolve_tools
from .audio_sources import resolve_voice


def preview_key(job, state, node_bundle=None, *, preview_limit=None):
    settings = copy.deepcopy(state["settings"])
    from .components import resolve_sources, walk_components
    from .pools import bind_pool_layers
    original_layers = settings["layers"]
    settings["layers"], _ = resolve_sources(bind_pool_layers(original_layers, state), job.get("title", ""), job.get("body", ""), job=job)
    for field in ("output_dir", "overwrite", "geometry_units", "show_layer_bounds", "export_quality"):
        settings.pop(field, None)
    paths = [job.get("path"), resolve_voice(job, state, discover=True)]
    if paths[1] and Path(paths[1]).is_file():
        from model.ElevenLabsTimings import artifact_files
        for metadata in artifact_files(paths[1]): paths.extend((str(metadata), str(metadata.with_suffix('.srt'))))
    from .node_effects import active_effects, subtitle_for
    if active_effects(settings):
        paths.append(subtitle_for(job, paths[1]))
    from .captions import active_captions, caption_subtitle_path
    uses_model = False
    for caption in active_captions(settings["layers"]):
        try:
            subtitle = caption_subtitle_path(job,paths[1],caption)
        except FileNotFoundError:
            # A verified dynamic word record can satisfy task timing even if
            # no ordinary sentence SRT was ever written. The worker validates
            # the record and reports the missing-SRT error if it cannot use it.
            if caption.get('caption_timing') != 'task' or not paths[1] or not Path(paths[1]).is_file():
                raise
            from model.ElevenLabsTimings import artifact_files
            if not artifact_files(paths[1]):
                raise
            subtitle = None
        if subtitle:
            paths.append(str(subtitle))
        elif caption["caption_timing"] in {'auto', 'model'}:
            uses_model = True
    from .custom_pools import music_source
    music, next_id, _pool = music_source(state) if settings.get("music_enabled", True) else ([], "", None)
    paths += [item.get("path") for item in music]
    for layer in walk_components(original_layers):
        paths.append(layer.get("source_file"))
        if layer["kind"] == "image":
            paths += [layer.get("path")] + [item.get("path") for item in layer.get("sequence_items", [])]
    paths += [layer["path"] for layer in walk_components(settings["layers"]) if layer.get("kind") == "image" and layer.get("path")]
    from .chroma_key import active_mask
    paths += [active_mask(layer) for layer in walk_components(settings["layers"]) if layer.get("kind") == "image"]
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
    from .text_colors import job_color_scope
    values = ["playback-v28-voice-space-v1", preview_limit, job_color_scope(job), settings, job.get("title", ""), job.get("body", ""),
              job.get("requires_title", False), job.get("voice_override", False), fingerprints, music, next_id]
    from .node_effects import cache_snapshot
    values.append(cache_snapshot(node_bundle, job.get('id')))
    if uses_model:
        from globalValue import globalValue
        values.append(globalValue.whisper_model_name())
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf8")).hexdigest()


class PlaybackWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int)

    def __init__(self, job, state, directory, key, parent=None, *, node_bundle=None, node_registry=None, preview_limit=None, elevenlabs_binding=None):
        super().__init__(parent)
        self.job, self.state = copy.deepcopy(job), copy.deepcopy(state)
        self.directory, self.key = Path(directory), key
        self.cancel = threading.Event()
        self.node_bundle, self.node_registry = node_bundle, node_registry
        self.preview_limit = preview_limit
        self.elevenlabs_binding = elevenlabs_binding

    def run(self):
        result = {"key": self.key, "preview_limit": self.preview_limit}
        try:
            from model.MediaStorage import media_cache_root
            cache = media_cache_root(self.directory, anchors=(self.job.get('task_dir'), self.state['settings'].get('output_dir'),
                Path(self.job['path']).parent if self.job.get('path') else None),
                                     namespace='batch-text-video') / "playback-cache"
            data_directory(cache).ensure(parents=True, exist_ok=True)
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
                from .text_colors import job_color_scope
                self.job["_color_scope"] = job_color_scope(self.job)
                # Freeze the source before changing the task directory to a
                # generated preview cache (and before changing its output name).
                self.job.update(voice_path=resolve_voice(self.job, self.state, discover=True), voice_override=True)
                from .components import resolve_sources
                from .pools import bind_pool_layers
                self.state["settings"]["layers"], _ = resolve_sources(
                    bind_pool_layers(self.state["settings"]["layers"], self.state), self.job.get("title", ""), self.job.get("body", ""), job=self.job)
                from .captions import active_captions, caption_subtitle_path
                frozen_subtitles = {}
                for layer in active_captions(self.state["settings"]["layers"]):
                    try:
                        path = caption_subtitle_path(self.job, self.job['voice_path'], layer)
                    except FileNotFoundError:
                        from model.ElevenLabsTimings import artifact_files
                        voice = self.job['voice_path']
                        if layer.get('caption_timing') != 'task' or not voice or not Path(voice).is_file() or not artifact_files(voice):
                            raise
                        path = None
                    if path is not None:
                        frozen_subtitles[layer['id']] = str(path)
                self.job['caption_subtitles'] = frozen_subtitles
                self.job.update(name=self.key, task_dir=str(cache))
                self.state["settings"]["overwrite"] = True
                kwargs = {}
                if self.preview_limit is not None:
                    kwargs['preview_limit'] = self.preview_limit
                if self.node_bundle:
                    from .node_effects import EffectRuntime
                    kwargs['node_effects'] = EffectRuntime(self.node_bundle, self.node_registry, self.cancel,
                        self.message.emit, self.message.emit, preview=True)
                from model.ElevenLabsService import alignment_session
                with alignment_session(self.elevenlabs_binding):
                    target, _ = render_static_text(self.job, self.state, self.directory,
                        self.cancel, self.progress.emit, self.message.emit, preview=True, **kwargs)
                duration = probe(target, ffprobe)["duration"]
            else:
                self.message.emit("复用播放预览缓存，不重复渲染。")
                data_file(target).touch()
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
                    data_file(path).remove()
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
        self._playback_preview_limit = None
        self._batch_after_preview = None
        self.preview_stack = QtWidgets.QStackedWidget()
        self.preview_stack.addWidget(self.preview)
        layout.addWidget(self.preview_stack, 1)
        row = QtWidgets.QHBoxLayout()
        self.play_preview_button = QtWidgets.QPushButton("播放预览")
        self.play_preview_button.setAutoDefault(False)
        self.play_preview_button.setToolTip("首次后台生成低分辨率完整预览，包含当前图层、人声和音乐；不推进任何轮换记录。再次播放复用缓存。")
        self.play_preview_button.clicked.connect(self.play_preview)
        self.quick_preview_button = QtWidgets.QPushButton('快速预览 · 前10秒')
        self.quick_preview_button.setAutoDefault(False)
        self.quick_preview_button.setToolTip('只编码前10秒，保留完整成品的排版和动画速度；短视频按实际时长。首次字幕对齐仍需等待，取消不会推进资源轮换。')
        self.quick_preview_button.clicked.connect(lambda _checked=False: self.play_preview(preview_limit=10))
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
        for widget in (self.quick_preview_button, self.play_preview_button, self.edit_preview_button, self.quick_export_button):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        self.playback_timeline = QtWidgets.QWidget()
        timeline = QtWidgets.QHBoxLayout(self.playback_timeline)
        timeline.setContentsMargins(0, 0, 0, 0)
        timeline.addWidget(self.playback_slider, 1)
        timeline.addWidget(self.playback_time)
        layout.addWidget(self.playback_timeline)
        self.playback_timeline.hide()
        from .pool_preview import PoolPreview
        self.pool_preview = PoolPreview(self)
        self.pool_preview.selectionChanged.connect(self._pool_preview_changed)
        from .preview_timeline import AnimationTimeline
        self.animation_timeline = AnimationTimeline(self)
        self.animation_timeline.timeChanged.connect(self._animation_preview_changed)
        layout.addWidget(self.animation_timeline)
        self.playback_status = QtWidgets.QLabel("拖动动画快览可检查滚动文字的中途／结尾，不渲染整片；播放预览用于完整合成。")
        self.playback_status.setWordWrap(True)
        layout.addWidget(self.playback_status)

    def invalidate_playback(self):
        if hasattr(self, 'stop_voice_effect_preview'): self.stop_voice_effect_preview()
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
        self.animation_timeline.show()
        self.playback_timeline.hide()
        self.playback_slider.setEnabled(False)
        self.playback_slider.setRange(0, 0)
        self.playback_time.setText("00:00 / 00:00")
        self._refresh_playback_buttons()

    def play_preview(self, _checked=False, *, preview_limit=None):
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
            if job.get("path") and not Path(job["path"]).is_file():
                raise ValueError("请先选择一个有效的背景视频或图片。")
            job.update(title=self.title.toPlainText(), body=self.body.toPlainText())
            state = copy.deepcopy(self.state)
            state["settings"] = self.read_settings()
            if getattr(self, 'pool_preview', None) is not None:
                state["settings"] = self.pool_preview.apply(state["settings"])
            from .cloud_alignment import prepare_binding
            elevenlabs_binding = prepare_binding(state['settings'], getattr(self, 'context', None))
            from .node_effects import prepare_preview_bundle
            bundle, registry = prepare_preview_bundle(state, job, getattr(self, 'context', None))
            key = preview_key(job, state, bundle, preview_limit=preview_limit)
            if key == self._playback_key and self._playback_file and Path(self._playback_file).is_file():
                self.preview_stack.setCurrentWidget(self.playback_surface)
                self.animation_timeline.hide()
                self.playback_timeline.show()
                if self.playback_surface.is_playing():
                    self.playback_surface.pause()
                else:
                    self.playback_surface.play()
                return
            self.invalidate_playback()
            self._playback_key = key
            self._playback_preview_limit = preview_limit
            self.playback_status.setText('正在后台准备前10秒快速预览；首次字幕对齐仍需等待，可取消。' if preview_limit else
                "正在后台准备完整播放预览；可以取消，不会生成正式成品。")
            self.playback_worker = PlaybackWorker(job, state, self.store.directory, key, self,
                node_bundle=bundle, node_registry=registry, preview_limit=preview_limit, elevenlabs_binding=elevenlabs_binding)
            self._refresh_playback_buttons()
            self.playback_worker.ready.connect(self._playback_ready, QtCore.Qt.ConnectionType.QueuedConnection)
            self.playback_worker.message.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
            self.playback_worker.progress.connect(self._playback_progress, QtCore.Qt.ConnectionType.QueuedConnection)
            connect_thread_finished(self.playback_worker, self, 'playback_worker', self._playback_finished)
            self.playback_worker.start()
        except Exception as error:
            self.playback_status.setText("预览无法播放：" + str(error))
            self._log("播放预览失败：" + traceback.format_exc())

    def _playback_progress(self, value):
        title = '前10秒快速预览' if self._playback_preview_limit else '完整播放预览'
        self.playback_status.setText(f"正在准备{title}：{value}%（可取消）")

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
                from model.MediaPlayback import FfplayReviewSurface
                self.playback_surface = FfplayReviewSurface(parent=self.preview_stack, preload_enabled=False)
                self.playback_surface.positionChanged.connect(self._playback_position)
                self.playback_surface.playingChanged.connect(self._playback_playing)
                self.playback_surface.errorOccurred.connect(self._playback_error)
                self.preview_stack.addWidget(self.playback_surface)
            self._playback_file, self._playback_duration = result["path"], result["duration"]
            self.playback_slider.setRange(0, round(self._playback_duration*1000))
            self.playback_slider.setEnabled(True)
            self.preview_stack.setCurrentWidget(self.playback_surface)
            self.animation_timeline.hide()
            self.playback_timeline.show()
            self.playback_surface.prepare(self._playback_file, 0, self._playback_duration, 0)
            self.playback_surface.play()
            self.playback_status.setText('播放的是开头最多10秒的快速预览；正式导出仍是完整时长。' if self._playback_preview_limit else
                "播放的是低分辨率合成预览；正式导出仍使用所选画布分辨率。")
        except Exception:
            self._log("播放器初始化失败：" + traceback.format_exc())
            self._playback_error("播放器初始化失败，请查看程序日志。")

    def _playback_finished(self):
        self.playback_worker.deleteLater()
        self.playback_worker = None
        self._refresh_playback_buttons()
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
        self._refresh_playback_buttons(playing)

    def _refresh_playback_buttons(self, playing=None):
        if playing is None:
            playing = bool(self.playback_surface and self.playback_surface.is_playing())
        quick = self._playback_preview_limit is not None
        self.play_preview_button.setText('暂停' if playing and not quick else '播放预览')
        self.quick_preview_button.setText('暂停快览' if playing and quick else '快速预览 · 前10秒')
        if self.playback_worker:
            (self.quick_preview_button if quick else self.play_preview_button).setText('取消准备')

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
        self.animation_timeline.show()
        self.playback_timeline.hide()

    def _playback_error(self, message):
        self.return_to_editor()
        self.playback_status.setText("播放器错误：" + str(message))
        self._log("播放器错误：" + str(message))

    def stop_playback(self):
        self.invalidate_playback()

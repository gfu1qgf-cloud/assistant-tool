"""Timeline review playback with mpv, Qt and FFplay backends.

The original review surface launched and embedded a new ``ffplay.exe`` process
for every source range.  That made clip boundaries visibly freeze and, on
Windows, could leave the old child window painted over the next frame. A pinned
mpv runtime is now preferred because it embeds into the existing Qt widget,
keeps one decoder process alive between clips and supports the source codecs
used by the editor more reliably.

QtMultimedia and the existing FFplay implementation remain automatic fallbacks
when mpv is unavailable or cannot initialize on a particular machine.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtNetwork, QtWidgets


_USER32 = None
# Qt 5 delegates video decoding to the Windows multimedia stack.  Some
# otherwise valid H.264 files are rejected there even though FFmpeg can play
# them.  Once that happens, retrying QMediaPlayer for every newly opened
# review dialog only repeats the same modal error and delays the fallback.
_QT_MEDIA_DISABLED_FOR_SESSION = False


def _windows_user32():
    """Return user32 with pointer-safe signatures on 32/64-bit Windows."""
    global _USER32
    if os.name != "nt":
        return None
    if _USER32 is not None:
        return _USER32

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    user32._lzx_enum_proc_type = enum_proc
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.LONG,
    ]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    user32.MoveWindow.restype = wintypes.BOOL
    _USER32 = user32
    return user32


def resolve_ffplay(ffmpeg_path=""):
    configured = str(ffmpeg_path or "").strip()
    if configured:
        sibling = Path(configured).with_name("ffplay.exe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffplay") or ""


def resolve_mpv():
    """Return the pinned bundled mpv, or an explicitly installed one."""
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return ""
    configured = str(os.environ.get("LZX_MPV_PATH") or "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parents[3]
    candidates.extend((
        root / "runtime" / "mpv" / "mpv.exe",
        root / "mpv.exe",
    ))
    system_mpv = shutil.which("mpv")
    if system_mpv:
        candidates.append(Path(system_mpv))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


class _EmbeddedFfplayReviewSurface(QtWidgets.QFrame):
    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, ffplay_path="", parent=None):
        super().__init__(parent)
        self.ffplay_path = resolve_ffplay(ffplay_path)
        self.source = ""
        self.range_start = 0.0
        self.range_end = 0.0
        self.position = 0.0
        self._playing = False
        self._base_position = 0.0
        self._play_clock = 0.0
        self._window_title = ""
        self._child_hwnd = 0
        self._embed_attempts = 0
        self._intentional_stop = False

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet("QFrame { background:#050608; border:1px solid #303746; }")
        self.setMinimumHeight(190)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QtWidgets.QLabel("选择时间线片段后在这里预览", self)
        self.placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self.placeholder.setStyleSheet("color:#9AA3B8; background:#050608; border:0;")
        self.placeholder.setScaledContents(False)
        layout.addWidget(self.placeholder)

        self.process = QtCore.QProcess(self)
        self.process.started.connect(self._on_started)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)
        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.setInterval(40)
        self.clock_timer.timeout.connect(self._tick)
        self.embed_timer = QtCore.QTimer(self)
        self.embed_timer.setInterval(60)
        self.embed_timer.timeout.connect(self._try_embed)

    def is_playing(self):
        return self._playing

    def configure_ffplay(self, ffmpeg_path=""):
        self.ffplay_path = resolve_ffplay(ffmpeg_path)

    def prepare(self, source, start, end, position=0.0):
        self.pause(show_frame=False)
        self.source = str(source or "")
        self.range_start = max(0.0, float(start or 0.0))
        self.range_end = max(self.range_start + 0.01, float(end or 0.0))
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, float(position or 0.0)),
        )
        self._show_still_frame(self.range_start + self.position)
        self.positionChanged.emit(self.position)

    def play(self):
        if not self.source or not Path(self.source).is_file():
            self.errorOccurred.emit(f"视频文件不存在：{self.source}")
            return False
        if not self.ffplay_path:
            self.errorOccurred.emit("没有找到 ffplay.exe，无法在时间线中播放。")
            return False
        if self.position >= self.range_end - self.range_start - 0.02:
            self.position = 0.0
        self._stop_process()
        self._window_title = f"LZX-SmartReview-{uuid.uuid4().hex}"
        remaining = max(0.05, self.range_end - self.range_start - self.position)
        args = [
            "-loglevel", "error",
            "-nostats",
            "-autoexit",
            "-noborder",
            "-window_title", self._window_title,
            "-ss", f"{self.range_start + self.position:.6f}",
            "-t", f"{remaining:.6f}",
            self.source,
        ]
        self.process.setProgram(self.ffplay_path)
        self.process.setArguments(args)
        self._base_position = self.position
        self._play_clock = time.monotonic()
        self._intentional_stop = False
        self._child_hwnd = 0
        self._embed_attempts = 0
        self.placeholder.setText("正在启动预览…")
        self.placeholder.show()
        self.process.start()
        return True

    def pause(self, show_frame=True):
        if self._playing:
            self._tick()
        self._playing = False
        self.clock_timer.stop()
        self.embed_timer.stop()
        self._stop_process()
        if show_frame and self.source:
            self._show_still_frame(self.range_start + self.position)
        self.playingChanged.emit(False)

    def stop(self):
        self.position = 0.0
        self.pause(show_frame=bool(self.source))
        self.positionChanged.emit(self.position)

    def _stop_process(self):
        if self.process.state() == QtCore.QProcess.NotRunning:
            self._child_hwnd = 0
            return
        self._intentional_stop = True
        self.process.kill()
        self.process.waitForFinished(300)
        self._child_hwnd = 0

    def _on_started(self):
        self._playing = True
        self._play_clock = time.monotonic()
        self.clock_timer.start()
        self.playingChanged.emit(True)
        if os.name == "nt":
            self.embed_timer.start()
        else:
            self.placeholder.setText("当前系统不支持嵌入 ffplay，视频已在独立窗口播放。")

    def _on_finished(self, _exit_code, _status):
        intentional = self._intentional_stop
        self._intentional_stop = False
        self.embed_timer.stop()
        self.clock_timer.stop()
        self._child_hwnd = 0
        if intentional:
            return
        self._tick(force_end=True)
        self._playing = False
        self.playingChanged.emit(False)
        self._show_still_frame(self.range_end)
        self.rangeFinished.emit()

    def _on_process_error(self, _error):
        if self._intentional_stop:
            return
        self._playing = False
        self.clock_timer.stop()
        self.errorOccurred.emit(
            f"ffplay 启动失败：{self.process.errorString()}"
        )

    def _tick(self, force_end=False):
        if force_end:
            value = self.range_end - self.range_start
        elif not self._playing:
            value = self.position
        else:
            value = self._base_position + time.monotonic() - self._play_clock
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, value),
        )
        self.positionChanged.emit(self.position)

    def _show_still_frame(self, source_time):
        self.placeholder.show()
        if not self.source or not Path(self.source).is_file():
            self.placeholder.setText("选择时间线片段后在这里预览")
            self.placeholder.setPixmap(QtGui.QPixmap())
            return
        if Path(self.source).stat().st_size < 32:
            self.placeholder.setPixmap(QtGui.QPixmap())
            self.placeholder.setText("视频文件无有效内容，无法读取预览画面")
            return
        try:
            import cv2

            capture = cv2.VideoCapture(self.source)
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, source_time) * 1000.0)
            ok, frame = capture.read()
            capture.release()
            if not ok or frame is None:
                raise ValueError("无法读取该时间的画面")
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width, channels = frame.shape
            image = QtGui.QImage(
                frame.data,
                width,
                height,
                channels * width,
                QtGui.QImage.Format_RGB888,
            ).copy()
            pixmap = QtGui.QPixmap.fromImage(image).scaled(
                self.placeholder.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            )
            self.placeholder.setText("")
            self.placeholder.setPixmap(pixmap)
        except Exception as error:
            self.placeholder.setPixmap(QtGui.QPixmap())
            self.placeholder.setText(
                f"暂停位置：{source_time:.2f} 秒\n{error}"
            )

    @staticmethod
    def _window_with_title(title):
        if os.name != "nt" or not title:
            return 0
        user32 = _windows_user32()
        result = {"hwnd": 0}
        callback_type = user32._lzx_enum_proc_type

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == title:
                result["hwnd"] = int(hwnd)
                return False
            return True

        callback_ref = callback_type(callback)
        user32.EnumWindows(callback_ref, 0)
        return result["hwnd"]

    def _try_embed(self):
        self._embed_attempts += 1
        hwnd = self._window_with_title(self._window_title)
        if hwnd:
            user32 = _windows_user32()
            get_window_long = user32.GetWindowLongW
            set_window_long = user32.SetWindowLongW
            style = get_window_long(hwnd, -16)
            style &= ~(0x80000000 | 0x00C00000 | 0x00040000)
            style |= 0x40000000 | 0x10000000
            set_window_long(hwnd, -16, style)
            user32.SetParent(hwnd, int(self.winId()))
            self._child_hwnd = hwnd
            self.placeholder.hide()
            self._resize_child()
            self.embed_timer.stop()
            return
        if self._embed_attempts >= 35:
            self.embed_timer.stop()
            self.placeholder.setText(
                "预览未能嵌入，ffplay 已在独立窗口继续播放。"
            )

    def _resize_child(self):
        if self._child_hwnd and os.name == "nt":
            _windows_user32().MoveWindow(
                self._child_hwnd,
                0,
                0,
                max(1, self.width()),
                max(1, self.height()),
                True,
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_child()
        if not self._playing and self.source:
            self._show_still_frame(self.range_start + self.position)

    def closeEvent(self, event):
        self._stop_process()
        super().closeEvent(event)

    def release(self):
        self._stop_process()


class _EmbeddedMpvReviewSurface(QtWidgets.QFrame):
    """Persistent mpv process embedded in a native Qt widget via ``--wid``."""

    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, mpv_path, parent=None):
        super().__init__(parent)
        self.mpv_path = str(mpv_path or "")
        self.source = ""
        self.loaded_source = ""
        self.range_start = 0.0
        self.range_end = 0.01
        self.position = 0.0
        self._play_requested = False
        self._playing = False
        self._waiting_for_file = False
        self._finishing_range = False
        self._releasing = False
        self._connected = False
        self._connect_attempts = 0
        self._read_buffer = bytearray()
        self._request_id = 10
        self._path_request_id = None
        self._pipe_name = "lzx_mpv_" + uuid.uuid4().hex
        self._pipe_path = "\\\\.\\pipe\\" + self._pipe_name

        self.setAttribute(QtCore.Qt.WA_NativeWindow, True)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#050608; border:1px solid #303746; }"
        )
        self.setMinimumHeight(190)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QtWidgets.QLabel(
            "选择时间线片段后在这里预览", self
        )
        self.placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self.placeholder.setStyleSheet(
            "color:#9AA3B8; background:#050608; border:0;"
        )
        layout.addWidget(self.placeholder)

        self.process = QtCore.QProcess(self)
        self.process.started.connect(self._process_started)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self.socket = QtNetwork.QLocalSocket(self)
        self.socket.connected.connect(self._socket_connected)
        self.socket.readyRead.connect(self._socket_ready_read)
        self.connect_timer = QtCore.QTimer(self)
        self.connect_timer.setInterval(80)
        self.connect_timer.timeout.connect(self._connect_socket)

    def is_playing(self):
        return self._playing

    def configure_ffplay(self, _ffmpeg_path=""):
        return None

    def _ensure_started(self):
        if self.process.state() != QtCore.QProcess.NotRunning:
            return
        if not self.mpv_path or not Path(self.mpv_path).is_file():
            self.errorOccurred.emit("没有找到内置 mpv 播放器。")
            return
        self._releasing = False
        self._connect_attempts = 0
        self.process.setProgram(self.mpv_path)
        self.process.setArguments([
            "--no-config",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--osc=no",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--terminal=no",
            "--really-quiet",
            "--hwdec=auto-safe",
            "--vo=gpu-next",
            "--gpu-api=d3d11",
            "--wid=" + str(int(self.winId())),
            "--input-ipc-server=" + self._pipe_path,
        ])
        self.process.start()

    def _process_started(self):
        self.connect_timer.start()
        self._connect_socket()

    def _connect_socket(self):
        if self._connected or self._releasing:
            return
        if self.socket.state() != QtNetwork.QLocalSocket.UnconnectedState:
            return
        self._connect_attempts += 1
        if self._connect_attempts > 75:
            self.connect_timer.stop()
            self.errorOccurred.emit("mpv 已启动，但控制通道连接失败。")
            return
        self.socket.abort()
        self.socket.connectToServer(
            self._pipe_path, QtCore.QIODevice.ReadWrite
        )

    def _socket_connected(self):
        self._connected = True
        self.connect_timer.stop()
        self._send(["observe_property", 1, "time-pos"])
        self._send(["observe_property", 2, "pause"])
        self._apply_prepare()

    def _send(self, command):
        if not self._connected:
            return None
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps(
            {"command": command, "request_id": request_id},
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        if self.socket.write(payload.encode("utf-8")) < 0:
            return None
        self.socket.flush()
        return request_id

    def _socket_ready_read(self):
        self._read_buffer.extend(bytes(self.socket.readAll()))
        while b"\n" in self._read_buffer:
            raw, _, remainder = self._read_buffer.partition(b"\n")
            self._read_buffer = bytearray(remainder)
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except (TypeError, ValueError):
                continue
            self._handle_message(message)

    def _handle_message(self, message):
        if (
            self._path_request_id is not None
            and message.get("request_id") == self._path_request_id
        ):
            self._path_request_id = None
            if str(message.get("error") or "success") != "success":
                self.errorOccurred.emit("mpv 无法确认当前视频文件。")
                return
            loaded_source = str(message.get("data") or "")
            self.loaded_source = loaded_source
            if not self._same_source(loaded_source, self.source):
                self._apply_prepare()
                return
            self._waiting_for_file = False
            self.placeholder.hide()
            self._seek_absolute(self.range_start + self.position)
            self._send(["set_property", "pause", not self._play_requested])
            return
        event = str(message.get("event") or "")
        if event == "file-loaded":
            self._path_request_id = self._send(["get_property", "path"])
            return
        if event == "property-change":
            name = str(message.get("name") or "")
            data = message.get("data")
            if name == "time-pos" and not self._waiting_for_file:
                self._update_absolute_position(data)
            elif name == "pause" and isinstance(data, bool):
                self._set_playing(not data and bool(self.loaded_source))
            return
        if event == "end-file":
            reason = str(message.get("reason") or "")
            if reason == "eof" and self._play_requested:
                self._finish_range()
            elif reason == "error":
                detail = str(
                    message.get("file_error")
                    or message.get("error")
                    or "未知解码错误"
                )
                self.errorOccurred.emit(f"mpv 无法播放该视频：{detail}")

    @staticmethod
    def _same_source(left, right):
        if not left or not right:
            return False
        try:
            return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
                os.path.abspath(right)
            )
        except (OSError, TypeError, ValueError):
            return str(left) == str(right)

    def _update_absolute_position(self, value):
        try:
            absolute = float(value)
        except (TypeError, ValueError):
            return
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
            self._finish_range()

    def _set_playing(self, playing):
        playing = bool(playing)
        if self._playing == playing:
            return
        self._playing = playing
        self.playingChanged.emit(playing)

    def _seek_absolute(self, seconds):
        self._send([
            "seek", max(0.0, float(seconds or 0.0)), "absolute+exact"
        ])

    def prepare(self, source, start, end, position=0.0):
        self.source = str(source or "")
        self.range_start = max(0.0, float(start or 0.0))
        self.range_end = max(self.range_start + 0.01, float(end or 0.0))
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, float(position or 0.0)),
        )
        self._play_requested = False
        self._finishing_range = False
        self.positionChanged.emit(self.position)
        self._ensure_started()
        if self._connected:
            self._apply_prepare()

    def _apply_prepare(self):
        if not self.source or not Path(self.source).is_file():
            return
        self._send(["set_property", "pause", True])
        if self.loaded_source != self.source:
            self._waiting_for_file = True
            self.placeholder.setText("正在加载预览…")
            self.placeholder.show()
            self._send(["loadfile", self.source, "replace"])
        else:
            self._waiting_for_file = False
            self._seek_absolute(self.range_start + self.position)

    def play(self):
        if not self.source or not Path(self.source).is_file():
            self.errorOccurred.emit(f"视频文件不存在：{self.source}")
            return False
        if self.position >= self.range_end - self.range_start - 0.02:
            self.position = 0.0
        self._play_requested = True
        self._finishing_range = False
        self._ensure_started()
        if self._connected:
            if self.loaded_source != self.source:
                self._apply_prepare()
            else:
                self._seek_absolute(self.range_start + self.position)
                self._send(["set_property", "pause", False])
        return True

    def pause(self, show_frame=True):
        del show_frame
        self._play_requested = False
        self._send(["set_property", "pause", True])
        self._set_playing(False)

    def stop(self):
        self.pause()
        self.position = 0.0
        self._seek_absolute(self.range_start)
        self.positionChanged.emit(0.0)

    def queue(self, _source, _start, _end):
        # mpv stays alive between files, so the expensive process start is
        # already avoided.  Native playlist preloading cannot retain a unique
        # start/end range for every entry.
        return False

    def clear_queue(self):
        return None

    def _finish_range(self):
        if self._finishing_range:
            return
        self._finishing_range = True
        self._play_requested = False
        self._send(["set_property", "pause", True])
        self.position = self.range_end - self.range_start
        self.positionChanged.emit(self.position)
        self._set_playing(False)
        QtCore.QTimer.singleShot(0, self.rangeFinished.emit)

    def _process_error(self, _error):
        if not self._releasing:
            self.errorOccurred.emit(
                f"mpv 播放器启动失败：{self.process.errorString()}"
            )

    def _process_finished(self, exit_code, _status):
        self._connected = False
        self.connect_timer.stop()
        self.socket.abort()
        self._set_playing(False)
        if not self._releasing:
            self.errorOccurred.emit(f"mpv 播放器意外退出：{exit_code}")

    def release(self):
        self._releasing = True
        self.connect_timer.stop()
        if self._connected:
            self._send(["quit"])
        self._connected = False
        self.socket.abort()
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(300):
                self.process.kill()
        self._set_playing(False)

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)


class _QtMediaReviewSurface(QtWidgets.QFrame):
    """PyQt5 multimedia player with optional low-cost standby decoder."""

    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, preload_enabled=True):
        super().__init__(parent)
        from PyQt5 import QtMultimedia, QtMultimediaWidgets

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

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#050608; border:1px solid #303746; }"
        )
        self.setMinimumHeight(190)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QtWidgets.QStackedWidget(self)
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
        self.video_widgets = []
        self.video_probes = []
        for index in range(decoder_count):
            video = QtMultimediaWidgets.QVideoWidget(self.stack)
            video.setStyleSheet("background:#050608; border:0;")
            video.setAspectRatioMode(QtCore.Qt.KeepAspectRatio)
            video.setAutoFillBackground(True)
            palette = video.palette()
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#050608"))
            video.setPalette(palette)
            video.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Expanding,
            )
            self.stack.addWidget(video)
            player = QtMultimedia.QMediaPlayer(
                self, QtMultimedia.QMediaPlayer.VideoSurface
            )
            player.setVideoOutput(video)
            # 15-17 UI updates per second are enough for a review playhead and
            # avoid repainting a long waveform 30+ times per second.
            player.setNotifyInterval(60)
            player.positionChanged.connect(
                lambda milliseconds, slot=index: self._position_changed(
                    slot, milliseconds
                )
            )
            player.stateChanged.connect(
                lambda state, slot=index: self._state_changed(slot, state)
            )
            player.mediaStatusChanged.connect(
                lambda status, slot=index: self._media_status_changed(slot, status)
            )
            try:
                player.error.connect(
                    lambda _error, slot=index: self._player_error(slot)
                )
            except (AttributeError, TypeError):
                pass
            probe = QtMultimedia.QVideoProbe(self)
            try:
                if probe.setSource(player):
                    probe.videoFrameProbed.connect(
                        lambda frame, slot=index: self._frame_probed(slot, frame)
                    )
            except (AttributeError, RuntimeError, TypeError):
                pass
            self.players.append(player)
            self.video_widgets.append(video)
            self.video_probes.append(probe)

    def is_available(self):
        return bool(self.players and self.players[0].isAvailable())

    def is_playing(self):
        return (
            self.players[self._active].state()
            == self._multimedia.QMediaPlayer.PlayingState
        )

    def configure_ffplay(self, _ffmpeg_path=""):
        return None

    def _set_media(self, slot, source, position_seconds):
        player = self.players[slot]
        source = str(source or "")
        position_ms = max(0, int(round(float(position_seconds or 0.0) * 1000)))
        self._target_positions[slot] = position_ms
        if self._sources[slot] != source:
            player.stop()
            self._sources[slot] = source
            self._pending_positions[slot] = position_ms
            content = self._multimedia.QMediaContent(
                QtCore.QUrl.fromLocalFile(source)
            )
            player.setMedia(content)
        else:
            self._pending_positions[slot] = None
            player.setPosition(position_ms)

    def prepare(self, source, start, end, position=0.0):
        self._display_generation += 1
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
        # QMediaPlayer emits positionChanged before QVideoWidget has painted the
        # corresponding frame.  Keep every native video surface hidden until a
        # short settle pass, otherwise Windows can expose the previous frame as
        # a ghost image while seeking or changing clips.
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
        if self.position >= self.range_end - self.range_start - 0.02:
            self.position = 0.0
            self.players[self._active].setPosition(
                int(round(self.range_start * 1000))
            )
        self._play_requested = True
        self._finishing_range = False
        self.players[self._active].play()
        return True

    def pause(self, show_frame=True):
        del show_frame
        self._play_requested = False
        self.players[self._active].pause()
        self.playingChanged.emit(False)

    def stop(self):
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
        self._play_requested = False
        self._display_generation += 1
        self._queued = None
        empty_media = self._multimedia.QMediaContent()
        for index, player in enumerate(self.players):
            player.stop()
            player.setMedia(empty_media)
            self._sources[index] = ""
            self._pending_positions[index] = None
            self.video_widgets[index].hide()
        self.stack.setCurrentWidget(self.placeholder)

    def _schedule_reveal(self, slot, delay=55):
        if slot != self._active:
            return
        generation = self._display_generation

        def reveal():
            if generation != self._display_generation or slot != self._active:
                return
            video = self.video_widgets[slot]
            video.setAspectRatioMode(QtCore.Qt.KeepAspectRatio)
            self.stack.setCurrentWidget(video)
            video.show()
            video.raise_()
            video.update()

        QtCore.QTimer.singleShot(max(0, int(delay)), reveal)

    def _frame_probed(self, slot, frame):
        """Reveal only after the decoder supplied a real frame for this seek."""
        if slot != self._active or not frame.isValid():
            return
        player_position = int(self.players[slot].position())
        if abs(player_position - int(self._target_positions[slot])) <= 350:
            self._schedule_reveal(slot, delay=0)

    def _media_status_changed(self, slot, status):
        ready_states = {
            self._multimedia.QMediaPlayer.LoadedMedia,
            self._multimedia.QMediaPlayer.BufferedMedia,
        }
        if status in ready_states:
            pending = self._pending_positions[slot]
            if pending is not None:
                self._pending_positions[slot] = None
                self.players[slot].setPosition(pending)
            if slot == self._active:
                self._schedule_reveal(slot, delay=120)
                if self._play_requested:
                    self.players[slot].play()
        elif (
            status == self._multimedia.QMediaPlayer.InvalidMedia
            and slot == self._active
        ):
            self._player_error(slot)
        elif (
            status == self._multimedia.QMediaPlayer.EndOfMedia
            and slot == self._active
            and self._play_requested
            and not self._finishing_range
        ):
            self._finish_current_range(slot)

    def _position_changed(self, slot, milliseconds):
        if slot != self._active:
            return
        absolute = max(0.0, float(milliseconds) / 1000.0)
        self.position = max(
            0.0,
            min(self.range_end - self.range_start, absolute - self.range_start),
        )
        target = self._target_positions[slot]
        if abs(int(milliseconds) - int(target)) <= 250:
            self._schedule_reveal(slot, delay=120)
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
        QtCore.QTimer.singleShot(0, self.rangeFinished.emit)

    def _state_changed(self, slot, state):
        if slot != self._active:
            return
        playing = state == self._multimedia.QMediaPlayer.PlayingState
        if playing:
            self._schedule_reveal(slot, delay=100)
        self.playingChanged.emit(playing)

    def _player_error(self, slot):
        if slot != self._active:
            self._queued = None
            return
        if self._reported_error:
            return
        self._reported_error = True
        message = self.players[slot].errorString() or "未知的媒体解码错误"
        self.errorOccurred.emit(f"Qt 原生播放器无法播放该视频：{message}")

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)


class FfplayReviewSurface(QtWidgets.QFrame):
    """Stable public surface selecting mpv/Qt playback with FFplay fallback."""

    positionChanged = QtCore.pyqtSignal(float)
    playingChanged = QtCore.pyqtSignal(bool)
    rangeFinished = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, ffplay_path="", parent=None, preload_enabled=True):
        global _QT_MEDIA_DISABLED_FOR_SESSION
        super().__init__(parent)
        self.ffplay_path = resolve_ffplay(ffplay_path)
        self.mpv_path = resolve_mpv()
        self._using_fallback = False
        self._backend_kind = ""
        self._last_prepare = ("", 0.0, 0.01, 0.0)
        self.setMinimumHeight(190)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        backend = None
        if self.mpv_path:
            backend = _EmbeddedMpvReviewSurface(self.mpv_path, self)
            self._backend_kind = "mpv"
        elif not _QT_MEDIA_DISABLED_FOR_SESSION:
            try:
                candidate = _QtMediaReviewSurface(
                    self,
                    preload_enabled=preload_enabled,
                )
                if candidate.is_available():
                    backend = candidate
                    self._backend_kind = "qt"
                else:
                    candidate.deleteLater()
            except (ImportError, RuntimeError, OSError):
                backend = None
        if backend is None:
            backend = _EmbeddedFfplayReviewSurface(self.ffplay_path, self)
            self._using_fallback = True
            self._backend_kind = "ffplay"
        self._install_backend(backend)

    def _install_backend(self, backend):
        self.backend = backend
        self._layout.addWidget(backend)
        backend.positionChanged.connect(self.positionChanged)
        backend.playingChanged.connect(self.playingChanged)
        backend.rangeFinished.connect(self.rangeFinished)
        backend.errorOccurred.connect(self._backend_error)

    def _backend_error(self, message):
        global _QT_MEDIA_DISABLED_FOR_SESSION
        if self._backend_kind != "ffplay" and self.ffplay_path:
            if self._backend_kind == "qt":
                _QT_MEDIA_DISABLED_FOR_SESSION = True
            was_playing = self.backend.is_playing()
            old_backend = self.backend
            if hasattr(old_backend, "release"):
                old_backend.release()
            old_backend.setParent(None)
            old_backend.deleteLater()
            self._using_fallback = True
            self._backend_kind = "ffplay"
            self._install_backend(
                _EmbeddedFfplayReviewSurface(self.ffplay_path, self)
            )
            self.backend.prepare(*self._last_prepare)
            if was_playing:
                self.backend.play()
            # The compatible backend is already ready.  Do not interrupt the
            # review with a modal warning for a failure that was recovered.
            return
        self.errorOccurred.emit(str(message))

    def is_playing(self):
        return self.backend.is_playing()

    def configure_ffplay(self, ffmpeg_path=""):
        self.ffplay_path = resolve_ffplay(ffmpeg_path)
        if hasattr(self.backend, "configure_ffplay"):
            self.backend.configure_ffplay(ffmpeg_path)

    def prepare(self, source, start, end, position=0.0):
        self._last_prepare = (source, start, end, position)
        return self.backend.prepare(source, start, end, position)

    def queue(self, source, start, end):
        if hasattr(self.backend, "queue"):
            return self.backend.queue(source, start, end)
        return False

    def clear_queue(self):
        if hasattr(self.backend, "clear_queue"):
            self.backend.clear_queue()

    def play(self):
        return self.backend.play()

    def pause(self, show_frame=True):
        return self.backend.pause(show_frame=show_frame)

    def stop(self):
        return self.backend.stop()

    def release(self):
        if hasattr(self.backend, "release"):
            self.backend.release()
        else:
            self.backend.stop()

    def closeEvent(self, event):
        self.release()
        self.backend.close()
        super().closeEvent(event)

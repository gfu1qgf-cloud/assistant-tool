"""Asynchronous, dependency-free waveform extraction through FFmpeg."""

from __future__ import annotations

import math
import os
import re
import shutil
from array import array
from pathlib import Path

from qt_compat import QtCore


_WAVEFORM_CACHE = {}


def resolve_ffmpeg(value=""):
    configured = str(value or "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        found = shutil.which(configured)
        if found:
            return found
    return shutil.which("ffmpeg") or ""


def _cache_key(path):
    source = Path(path)
    try:
        stat = source.stat()
        identity = (int(stat.st_dev), int(stat.st_ino))
        if not identity[1]:
            identity = str(source.resolve())
        return identity, int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return str(source), 0, 0


def pcm_waveform(raw, sample_rate=1000, maximum_points=700):
    samples = array("h")
    usable = len(raw) - len(raw) % samples.itemsize
    if usable:
        samples.frombytes(raw[:usable])
    if os.sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return []
    window = max(1, math.ceil(len(samples) / max(1, maximum_points)))
    peaks = [
        max(abs(value) for value in samples[index:index + window])
        for index in range(0, len(samples), window)
    ]
    largest = max(1, max(peaks))
    return [
        (
            min(len(samples), index * window + window / 2.0) / sample_rate,
            math.sqrt(peak / largest),
        )
        for index, peak in enumerate(peaks)
    ]


class WaveformLoader(QtCore.QObject):
    waveformReady = QtCore.pyqtSignal(int, object)
    loadingChanged = QtCore.pyqtSignal(bool)
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, ffmpeg_path="", parent=None):
        super().__init__(parent)
        self.ffmpeg_path = resolve_ffmpeg(ffmpeg_path)
        self._queue = []
        self._raw = bytearray()
        self._current = None
        self._generation = 0
        self._cancelling = False
        self.process = QtCore.QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)

    def configure(self, ffmpeg_path=""):
        self.ffmpeg_path = resolve_ffmpeg(ffmpeg_path)

    def load_task(self, task):
        self.cancel()
        self._generation += 1
        token = self._generation
        self._queue = [
            (token, clip_index, str(clip.get("source") or ""))
            for clip_index, clip in enumerate(task.get("clips", []) or [])
        ]
        self.loadingChanged.emit(bool(self._queue))
        QtCore.QTimer.singleShot(0, self._start_next)

    def cancel(self):
        self._generation += 1
        self._queue = []
        self._current = None
        self._raw.clear()
        if self.process.state() != QtCore.QProcess.NotRunning:
            self._cancelling = True
            self.process.kill()
            self.process.waitForFinished(200)
            self._cancelling = False

    def _start_next(self):
        if self.process.state() != QtCore.QProcess.NotRunning:
            return
        while self._queue:
            token, clip_index, source = self._queue.pop(0)
            if token != self._generation:
                continue
            if not Path(source).is_file():
                continue
            key = _cache_key(source)
            cached = _WAVEFORM_CACHE.get(key)
            if cached is not None:
                self.waveformReady.emit(clip_index, cached)
                continue
            if not self.ffmpeg_path:
                self.errorOccurred.emit("没有找到 FFmpeg，音频波形暂时无法加载。")
                self._queue.clear()
                break
            self._current = (token, clip_index, source, key)
            self._raw.clear()
            self.process.setProgram(self.ffmpeg_path)
            self.process.setArguments([
                "-hide_banner", "-loglevel", "error", "-i", source,
                "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "1000",
                "-f", "s16le", "pipe:1",
            ])
            self.process.start()
            return
        self.loadingChanged.emit(False)

    def _read_output(self):
        self._raw.extend(bytes(self.process.readAllStandardOutput()))

    def _finished(self, exit_code, _status):
        self._read_output()
        current = self._current
        self._current = None
        if self._cancelling or current is None:
            return
        token, clip_index, source, key = current
        if token == self._generation and exit_code == 0 and self._raw:
            values = pcm_waveform(bytes(self._raw))
            _WAVEFORM_CACHE[key] = values
            self.waveformReady.emit(clip_index, values)
        elif token == self._generation and exit_code != 0:
            detail = bytes(self.process.readAllStandardError()).decode(
                "utf-8", errors="replace"
            ).strip().splitlines()
            self.errorOccurred.emit(
                f"{Path(source).name} 的音频波形读取失败"
                + (f"：{detail[-1]}" if detail else "")
            )
        self._raw.clear()
        QtCore.QTimer.singleShot(0, self._start_next)

    def _process_error(self, _error):
        if self._cancelling:
            return
        if self._current is not None:
            self.errorOccurred.emit(
                f"音频波形加载失败：{self.process.errorString()}"
            )
            self._current = None
            self._raw.clear()
            QtCore.QTimer.singleShot(0, self._start_next)


def parse_silence_output(value, duration):
    pattern = re.compile(r"silence_(start|end):\s*(-?[0-9.]+)")
    ranges = []
    current_start = None
    for match in pattern.finditer(str(value or "")):
        point = max(0.0, float(match.group(2)))
        if match.group(1) == "start":
            current_start = point
        elif current_start is not None:
            end = min(max(current_start, point), max(0.0, float(duration)))
            if end > current_start:
                ranges.append([round(current_start, 3), round(end, 3)])
            current_start = None
    if current_start is not None and duration > current_start:
        ranges.append([round(current_start, 3), round(float(duration), 3)])
    return ranges


class SilenceBatchLoader(QtCore.QObject):
    """Re-run FFmpeg silence detection without blocking the review window."""

    completed = QtCore.pyqtSignal(object)
    progressChanged = QtCore.pyqtSignal(str)

    def __init__(self, ffmpeg_path="", parent=None):
        super().__init__(parent)
        self.ffmpeg_path = resolve_ffmpeg(ffmpeg_path)
        self._queue = []
        self._results = []
        self._stderr = bytearray()
        self._current = None
        self._cancelling = False
        self.process = QtCore.QProcess(self)
        self.process.readyReadStandardError.connect(self._read_error)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)

    def start(self, task, threshold_db, minimum_ms):
        self.cancel()
        self._results = []
        self._queue = [
            (
                clip_index,
                str(clip.get("source") or ""),
                float(clip.get("original_duration") or 0.0),
            )
            for clip_index, clip in enumerate(task.get("clips", []) or [])
        ]
        self._threshold_db = int(threshold_db)
        self._minimum_ms = int(minimum_ms)
        if not self.ffmpeg_path:
            self.completed.emit([
                {
                    "clip_index": index,
                    "ranges": [],
                    "error": "没有找到 FFmpeg",
                }
                for index, _source, _duration in self._queue
            ])
            self._queue = []
            return
        self._start_next()

    def cancel(self):
        self._queue = []
        self._current = None
        self._stderr.clear()
        if self.process.state() != QtCore.QProcess.NotRunning:
            self._cancelling = True
            self.process.kill()
            self.process.waitForFinished(200)
            self._cancelling = False

    def _start_next(self):
        if not self._queue:
            self.progressChanged.emit("")
            self.completed.emit(self._results)
            return
        clip_index, source, duration = self._queue.pop(0)
        if not Path(source).is_file():
            self._results.append({
                "clip_index": clip_index,
                "ranges": [],
                "error": "视频文件不存在",
            })
            QtCore.QTimer.singleShot(0, self._start_next)
            return
        self._current = (clip_index, source, duration)
        self._stderr.clear()
        self.progressChanged.emit(f"正在检测 {Path(source).name} 的静音区间…")
        self.process.setProgram(self.ffmpeg_path)
        self.process.setArguments([
            "-hide_banner", "-nostats", "-i", source, "-vn", "-af",
            f"silencedetect=noise={self._threshold_db}dB:d={self._minimum_ms / 1000.0:.3f}",
            "-f", "null", "-",
        ])
        self.process.start()

    def _read_error(self):
        self._stderr.extend(bytes(self.process.readAllStandardError()))

    def _finished(self, exit_code, _status):
        self._read_error()
        current = self._current
        self._current = None
        if self._cancelling or current is None:
            return
        clip_index, _source, duration = current
        text = self._stderr.decode("utf-8", errors="replace")
        ranges = parse_silence_output(text, duration)
        error = ""
        if exit_code not in {0, 255} and not ranges:
            lines = text.strip().splitlines()
            error = lines[-1] if lines else f"FFmpeg 退出代码 {exit_code}"
        self._results.append({
            "clip_index": clip_index,
            "ranges": ranges,
            "error": error,
        })
        self._stderr.clear()
        QtCore.QTimer.singleShot(0, self._start_next)

    def _process_error(self, _error):
        if self._cancelling or self._current is None:
            return
        clip_index, _source, _duration = self._current
        self._results.append({
            "clip_index": clip_index,
            "ranges": [],
            "error": self.process.errorString(),
        })
        self._current = None
        self._stderr.clear()
        QtCore.QTimer.singleShot(0, self._start_next)


class _VoiceActivitySignals(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, str)
    completed = QtCore.pyqtSignal(int, object)


class _VoiceActivityJob(QtCore.QRunnable):
    """Decode audio and run Silero VAD outside the Qt GUI thread."""

    def __init__(self, generation, items, settings):
        super().__init__()
        self.generation = int(generation)
        self.items = list(items)
        self.settings = dict(settings)
        self.signals = _VoiceActivitySignals()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    @QtCore.pyqtSlot()
    def run(self):
        # Lazy import avoids creating a circular module dependency at startup.
        from .engine import detect_voice_absence_ranges

        results = []
        for clip_index, source, duration in self.items:
            if self.cancelled:
                break
            self.signals.progress.emit(
                self.generation,
                f"正在检测 {Path(source).name} 的无人声区间…",
            )
            if not Path(source).is_file():
                ranges, error = [], "视频文件不存在"
            else:
                ranges, error = detect_voice_absence_ranges(
                    source, duration, self.settings
                )
            results.append({
                "clip_index": clip_index,
                "ranges": ranges,
                "error": error,
            })
        self.signals.completed.emit(self.generation, results)


class VoiceActivityBatchLoader(QtCore.QObject):
    """Re-run voice activity detection without freezing the review window."""

    completed = QtCore.pyqtSignal(object)
    progressChanged = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0
        self._job = None

    def start(self, task, settings):
        self.cancel()
        generation = self._generation
        items = [
            (
                clip_index,
                str(clip.get("source") or ""),
                float(clip.get("original_duration") or 0.0),
            )
            for clip_index, clip in enumerate(task.get("clips", []) or [])
        ]
        if not items:
            QtCore.QTimer.singleShot(0, lambda: self.completed.emit([]))
            return
        job = _VoiceActivityJob(generation, items, settings)
        job.signals.progress.connect(self._progress)
        job.signals.completed.connect(self._completed)
        self._job = job
        QtCore.QThreadPool.globalInstance().start(job)

    def cancel(self):
        self._generation += 1
        if self._job is not None:
            self._job.cancel()
            self._job = None

    def _progress(self, generation, message):
        if int(generation) == self._generation:
            self.progressChanged.emit(str(message))

    def _completed(self, generation, results):
        if int(generation) != self._generation:
            return
        self._job = None
        self.progressChanged.emit("")
        self.completed.emit(list(results or []))

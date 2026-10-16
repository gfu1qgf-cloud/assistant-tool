"""Latest-request-only, bounded media thumbnails without GUI-thread decoding.

FFmpeg does source IO/decoding in a child process. Jobs belong to QApplication,
not disposable panels; cancellation never waits for a process on the GUI thread.
No disk cache, private-data copy, OpenCV codec or plugin dependency is involved.
"""
import shutil
import sys
from pathlib import Path

from app_paths import APP_ROOT
from qt_compat import QtCore, QtGui, QtWidgets


def resolve_thumbnail_ffmpeg(configured=''):
    value = str(configured or '').strip()
    if value:
        path = Path(value)
        if path.stem.casefold() == 'ffplay':
            path = path.with_name('ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
        return str(path)  # QProcess reports invalid explicit paths asynchronously.
    name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    for root in (APP_ROOT, Path(getattr(sys, '_MEIPASS', APP_ROOT / '_internal'))):
        candidate = root / 'runtime' / 'ffmpeg' / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which('ffmpeg') or ''


class _FrameJob(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)
    OUTPUT_LIMIT = 4 * 1024 * 1024

    def __init__(self, generation, source, seconds, ffmpeg, timeout_ms):
        super().__init__(QtWidgets.QApplication.instance())
        self.generation, self.source = generation, source
        self._done, self._error = False, ''
        self._output, self._stderr = bytearray(), bytearray()
        self.process = QtCore.QProcess(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._read_error)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.setProgram(ffmpeg)
        self.process.setArguments([
            '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
            '-filter_threads', '1', '-protocol_whitelist', 'file,pipe',
            '-ss', f'{max(0.0, seconds):.6f}', '-i', source,
            '-map', '0:v:0', '-frames:v', '1', '-an', '-sn', '-dn',
            '-vf', 'scale=960:960:force_original_aspect_ratio=decrease:reset_sar=1',
            '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1',
        ])
        self.timeout_ms = timeout_ms

    def start(self):
        self.timer.start(self.timeout_ms)
        if not self.process.program():
            QtCore.QTimer.singleShot(0, lambda: self._complete('找不到FFmpeg，无法读取预览画面'))
        else:
            self.process.start()

    def _read_output(self):
        self._output.extend(bytes(self.process.readAllStandardOutput()))
        if len(self._output) > self.OUTPUT_LIMIT:
            self.abort('预览画面输出超出安全大小')

    def _read_error(self):
        chunk = bytes(self.process.readAllStandardError())
        self._stderr.extend(chunk[-8192:])
        if len(self._stderr) > 8192:
            del self._stderr[:-8192]

    def _timeout(self):
        self.abort('预览取帧超时；可重新选择素材，不影响其他操作')

    @QtCore.pyqtSlot()
    def cancel(self):
        self.abort('已取消预览取帧')

    def abort(self, error):
        if self._done:
            return
        self._error = error
        self.timer.stop()
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.process.kill()  # finished() reaps the process; never wait here.
        else:
            self._complete(error)

    def _process_error(self, error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._complete(self._error or '预览解码器启动失败：' + self.process.errorString())

    def _finished(self, code, _status):
        self._read_output()
        self._read_error()
        error = self._error
        if not error and code != 0:
            error = '无法读取预览画面：' + (self._stderr.decode('utf-8', 'replace').strip()[-300:] or f'解码器退出 {code}')
        self._complete(error)

    def _complete(self, error=''):
        if self._done:
            return
        self._done = True
        self.timer.stop()
        image = QtGui.QImage()
        if not error:
            image = QtGui.QImage.fromData(bytes(self._output), 'PNG')
            if image.isNull():
                error = '无法读取该时间的画面'
        self.finished.emit({'job': self, 'generation': self.generation, 'image': image, 'error': error})
        self.deleteLater()


class LatestMediaThumbnail(QtCore.QObject):
    ready = QtCore.pyqtSignal(int, object, str)

    def __init__(self, parent=None, *, timeout_ms=8000):
        super().__init__(parent)
        self.timeout_ms = max(100, int(timeout_ms))
        self.generation = 0
        self._active = None
        self._pending = None

    def request(self, source, seconds=0.0, *, ffmpeg_path=''):
        self.generation += 1
        self._pending = (self.generation, str(source), float(seconds), resolve_thumbnail_ffmpeg(ffmpeg_path))
        if self._active is not None:
            self._active.cancel()
        else:
            self._start_pending()
        return self.generation

    def cancel(self):
        self.generation += 1
        self._pending = None
        if self._active is not None:
            self._active.cancel()

    def _start_pending(self):
        if self._pending is None:
            return
        args, self._pending = self._pending, None
        job = _FrameJob(*args, self.timeout_ms)
        self._active = job
        job.finished.connect(self._completed)
        self.destroyed.connect(job.cancel)
        job.start()

    @QtCore.pyqtSlot(object)
    def _completed(self, result):
        if result['job'] is not self._active:
            return
        self._active = None
        if result['generation'] == self.generation:
            self.ready.emit(self.generation, result['image'], result['error'])
        self._start_pending()

"""Bounded, cancellable in-memory video frames for the matting workbench."""
import copy
from fractions import Fraction
import math
import subprocess
import threading
import time
from qt_compat import QtCore
from .matting_models import check_cancel, MattingCancelled


def describe(media):
    stream = media['video']
    if not stream: raise ValueError('素材没有视频画面。')
    width, height = stream['width'], stream['height']
    rotation = stream.get('tags', {}).get('rotate', 0)
    rotation = next((entry.get('rotation', rotation) for entry in stream.get('side_data_list', []) if 'rotation' in entry), rotation)
    if abs(float(rotation)) % 180 == 90: width, height = height, width
    if max(width, height) > 8192 or width*height > 40_000_000: raise ValueError('视频画幅过大。')
    try: rate = Fraction(stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '25/1')
    except (ValueError, ZeroDivisionError): rate = Fraction(25)
    if rate <= 0 or rate > 120: rate = Fraction(25)
    duration = float(media['duration'])
    if not math.isfinite(duration) or not 0 < duration <= 86400: raise ValueError('视频时长无效。')
    frames = max(1, math.ceil(duration*float(rate)-.000001))
    try: counted = int(stream.get('nb_frames') or 0)
    except (ValueError, TypeError): counted = 0
    if counted > 0 and abs(counted-duration*float(rate)) < 2: frames = counted
    return dict(width=width, height=height, fps=float(rate), rate=str(rate), duration=duration, frames=frames)


def frame_position(info, seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds): raise ValueError('预览时间必须是有限秒数。')
    index = min(info['frames']-1, max(0, round(seconds*info['fps'])))
    return index/info['fps']


def seek_args(info, seconds):
    # Seek just before the selected frame so timestamp rounding cannot discard
    # that frame at fractional rates. fps normalizes the first output to zero.
    position = frame_position(info, seconds)
    return ['-ss', f'{max(0, position-.25/info["fps"]):.9f}'] if position else []


class MattingVideoWorker(QtCore.QThread):
    frameReady = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(object)

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = copy.deepcopy(request)
        self.cancel, self._received = threading.Event(), threading.Event()
        self._process, self._lock = None, threading.Lock()
        self.error = None

    def acknowledge(self): self._received.set()

    def stop(self):
        self.cancel.set(); self._received.set()
        # Never wait on the UI thread. The owning worker reaps its decoder.
        with self._lock:
            if self._process and self._process.poll() is None:
                try: self._process.terminate()
                except OSError: pass

    def run(self):
        import numpy as np
        from . import matting_render as render
        from .matting_assets import fingerprint
        from .engine import resolve_tools
        request = self.request; process = None
        try:
            info, path, values = request['info'], request['path'], request['values']
            expected = fingerprint(path)
            if request.get('expected') and expected['sha256'] != request['expected']['sha256']:
                raise ValueError('素材已改变，请重新选择后预览。')
            check_cancel(self.cancel)
            executable, _ = resolve_tools(request.get('ffmpeg', ''))
            start = frame_position(info, request['position']); first = round(start*info['fps'])
            original_only = request.get('original_only', False)
            report = lambda text: self.message.emit(dict(generation=request['generation'], text=text))
            predictor = None if original_only else render.Predictor(values['model'], values['device'], values['quality'], request.get('models'), self.cancel, report)
            check_cancel(self.cancel)
            args = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                    *seek_args(info, start), '-i', path, '-vf', 'fps='+info['rate'], '-an', '-sn',
                    '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1']
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self._lock: self._process = process
            check_cancel(self.cancel)
            size = info['width']*info['height']*4; emitted = 0; eof = False
            for index in range(first, info['frames']):
                tick = time.monotonic(); check_cancel(self.cancel)
                data = bytearray()
                while len(data) < size:
                    part = process.stdout.read(size-len(data))
                    if not part: break
                    data.extend(part); check_cancel(self.cancel)
                if not data: eof = True; break
                if len(data) != size: raise ValueError('视频预览帧解码不完整。')
                source = np.frombuffer(data, np.uint8).reshape(info['height'], info['width'], 4)
                position = index/info['fps']
                base = None
                if predictor:
                    rgb, alpha = predictor.predict(source[:, :, :3]); check_cancel(self.cancel)
                    base = dict(source=source, rgb=rgb, alpha=alpha, expected=expected, position=position,
                                model=values['model'], quality=values['quality'], device=predictor.device, video=info)
                result = dict(generation=request['generation'], path=path, position=position, values=values,
                              base=base, original_only=original_only, original=render.qimage(source),
                              image=render.qimage(render.refined(base, values)) if base is not None else None)
                check_cancel(self.cancel); self._received.clear(); self.frameReady.emit(result); emitted += 1
                # At most one queued frame. A stalled/hidden UI cannot grow an
                # unbounded QImage queue while an AI producer runs ahead.
                while not self._received.wait(.05): check_cancel(self.cancel)
                check_cancel(self.cancel)
                if not request['play']: break
                remaining = 1/info['fps']-(time.monotonic()-tick)
                if remaining > 0 and self.cancel.wait(remaining): check_cancel(self.cancel)
            check_cancel(self.cancel)
            if not emitted: raise ValueError('无法读取目标视频帧。')
            if eof and process.wait(timeout=5): raise ValueError('视频解码中断，请检查素材。')
            if fingerprint(path)['sha256'] != expected['sha256']: raise ValueError('素材在预览期间已改变，请重新读取。')
        except MattingCancelled: pass
        except Exception as error:
            if not self.cancel.is_set():
                self.error = str(error); self.failed.emit(dict(generation=request['generation'], error=self.error))
        finally:
            render._stop(process)
            if process and process.stdout: process.stdout.close()
            with self._lock: self._process = None

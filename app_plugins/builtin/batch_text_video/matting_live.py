"""Frame-at-a-time AI input for both playback and final FFmpeg composition."""
from collections import OrderedDict
import copy
from fractions import Fraction
import json
import math
import subprocess
import threading

from .matting_assets import verify_file
from .matting_models import MattingCancelled, check_cancel
from .matting_settings import normalize, refine

_POSTERS, _LOCK = OrderedDict(), threading.RLock()
POSTER_LIMIT = 32*1024**2
_BASES = OrderedDict()
BASE_LIMIT = 32*1024**2


def dynamic(layer):
    return layer.get('_matting_binding', {}).get('mode') == 'dynamic'


def poster(binding, bounds, *, cancel=None, ffmpeg=''):
    from .matting_render import infer, refined, qimage
    from qt_compat import QtCore
    path = verify_file(binding['source'], '原素材')
    p = normalize(binding['parameters'])
    key = (str(path), binding['source']['sha256'], json.dumps(p, sort_keys=True), bounds.width(), bounds.height())
    with _LOCK:
        image = _POSTERS.get(key)
        if image is not None:
            _POSTERS.move_to_end(key); return image.copy()
    inference_key = (str(path), binding['source']['sha256'], p['model'], p['quality'], p['device'])
    with _LOCK:
        base = _BASES.get(inference_key)
        if base is not None: _BASES.move_to_end(inference_key)
    if base is None:
        base = infer(path, p, cancel=cancel, ffmpeg=ffmpeg)
        check_cancel(cancel)
        size = sum(getattr(value, 'nbytes', 0) for value in base.values())
        if size <= BASE_LIMIT:
            with _LOCK:
                _BASES[inference_key] = base
                while sum(sum(getattr(value, 'nbytes', 0) for value in item.values()) for item in _BASES.values()) > BASE_LIMIT:
                    _BASES.popitem(last=False)
    image = qimage(refined(base, p))
    image = image.scaled(bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                         QtCore.Qt.TransformationMode.SmoothTransformation)
    check_cancel(cancel)
    with _LOCK:
        _POSTERS[key] = image
        size = sum(v.sizeInBytes() for v in _POSTERS.values())
        while size > POSTER_LIMIT and _POSTERS:
            _, old = _POSTERS.popitem(last=False); size -= old.sizeInBytes()
    return image.copy()


class StreamInput(str):
    """A deferred input: preparation never opens a pipe or launches inference."""
    def __new__(cls, binding, duration, loop, fps, ffmpeg):
        value = super().__new__(cls, binding['source']['path'])
        value.binding = copy.deepcopy(binding)
        value.duration, value.loop, value.ffmpeg = duration, loop, ffmpeg
        from .visual_media import is_animated
        value.animated = is_animated(value)
        from .matting_render import load_source
        if value.animated:
            from .engine import resolve_tools, probe
            _, ffprobe = resolve_tools(ffmpeg)
            info = probe(value, ffprobe); stream = info['video']
            if not stream: raise ValueError('AI抠像素材没有视频画面。')
            from .video_geometry import decoded_size
            w, h = decoded_size(stream)
            try: fps = Fraction(stream.get('avg_frame_rate', '25/1'))
            except (ValueError, ZeroDivisionError): fps = Fraction(25)
            if fps <= 0 or fps > 120: fps = Fraction(25)
        else:
            source = load_source(value, ffmpeg=ffmpeg)['source']; h, w = source.shape[:2]
        if max(w, h) > 8192 or w*h > 40_000_000: raise ValueError('AI抠像素材画幅过大。')
        value.width, value.height, value.fps = w, h, Fraction(fps)
        return value

    def __deepcopy__(self, memo):
        return self  # Spec is immutable after construction; every run gets a new producer.

    def arguments(self):
        # The host fragment validator accepts local paths only. Keep the source
        # path here; expand this trusted spec immediately before Popen.
        return ['-i', self]

    def options(self):
        return ['-thread_queue_size', '2', '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{self.width}x{self.height}',
                '-framerate', str(self.fps)]


class BoundSource(str):
    """Carry the frozen matte through a source-node text output to its layer."""
    def __new__(cls, path, binding):
        value = super().__new__(cls, path); value.binding = copy.deepcopy(binding)
        return value

    def __deepcopy__(self, memo): return BoundSource(str(self), self.binding)


class Feed:
    def __init__(self, spec):
        from .matting_pipe import MemoryPipe
        self.spec, self.cancel, self.error = spec, threading.Event(), None
        self.pipe = MemoryPipe(self.cancel)
        self.decoder, self._process_lock = None, threading.Lock()
        self.thread = threading.Thread(target=self._run, name='matting-frame-feed', daemon=True)

    def start(self): self.thread.start()

    def _run(self):
        try:
            self.pipe.connect()
            iterator = self.frames()
            try:
                for frame in iterator: self.pipe.write(frame)
            finally: iterator.close()
        except (MattingCancelled, BrokenPipeError): pass
        except Exception as error: self.error = error
        finally: self.pipe.close()

    def frames(self):
        if hasattr(self.spec, 'loop_layer'):
            from .visual_loop import frames
            yield from frames(self)
            return
        import numpy as np
        from . import matting_render as render
        spec = self.spec
        path = verify_file(spec.binding['source'], '原素材')
        p = normalize(spec.binding['parameters'])
        count = max(1, math.ceil(spec.duration*float(spec.fps)))
        if not spec.animated:
            base = render.infer(path, p, cancel=self.cancel, ffmpeg=spec.ffmpeg)
            frame = render.refined(base, p).tobytes()
            del base
            for _ in range(count):
                check_cancel(self.cancel); yield frame
        else:
            written = 0
            while written < count:
                check_cancel(self.cancel)
                # Temporal state belongs to this material and one loop only.
                predictor = render.Predictor(p['model'], p['device'], p['quality'], cancel=self.cancel)
                process = None
                try:
                    with self._process_lock:
                        check_cancel(self.cancel)
                        process = subprocess.Popen([spec.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                            '-threads', '2', '-i', str(path), '-vf', f'fps={float(spec.fps):.9f}', '-an', '-sn', '-f', 'rawvideo',
                            '-threads', '1', '-pix_fmt', 'rgba', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.decoder = process
                    frames = 0; needed = spec.width*spec.height*4
                    while written < count:
                        check_cancel(self.cancel)
                        data = process.stdout.read(needed)
                        if not data: break
                        if len(data) != needed: raise ValueError('AI抠像素材帧解码不完整。')
                        source = np.frombuffer(data, np.uint8).reshape(spec.height, spec.width, 4)
                        rgb, alpha = predictor.predict(source[:, :, :3])
                        result = refine(rgb, alpha, source[:, :, 3], p, source_rgb=source[:, :, :3]).tobytes()
                        frames += 1; written += 1
                        yield result
                    check_cancel(self.cancel)
                    if written < count and process.wait(timeout=15): raise RuntimeError('AI抠像素材解码失败。')
                    if not frames: raise ValueError('AI抠像素材没有可解码的帧。')
                finally:
                    with self._process_lock:
                        render._stop(process)
                        if process and process.stdout: process.stdout.close()
                        self.decoder = None
                if not spec.loop: break
        verify_file(spec.binding['source'], '原素材')

    def close(self):
        self.cancel.set()
        with self._process_lock:
            from .matting_render import _stop
            _stop(self.decoder)
        if self.thread.ident is not None: self.thread.join(timeout=30)
        else: self.pipe.close()
        if self.thread.is_alive(): raise RuntimeError('AI抠像线程尚未释放，请稍后重试。')


class Inputs:
    """Fresh, bounded feeds for one encoder attempt (including CPU retries)."""
    def __init__(self, args):
        self.args, self.feeds = list(args), []

    def __enter__(self):
        try:
            expanded = []
            for value in self.args:
                if isinstance(value, StreamInput):
                    if not expanded or expanded.pop() != '-i': raise ValueError('内存抠像输入参数无效。')
                    feed = Feed(value); self.feeds.append(feed)
                    expanded += value.options() + ['-i', feed.pipe.name]
                    feed.start()
                else: expanded.append(value)
            self.args = expanded
            return self
        except BaseException:
            self.__exit__(None, None, None); raise

    def check(self):
        for feed in self.feeds:
            if feed.error: raise RuntimeError('内存AI抠像失败：'+str(feed.error)) from feed.error

    def __exit__(self, kind, error, traceback):
        for feed in self.feeds: feed.cancel.set()
        for feed in self.feeds: feed.close()
        if kind is None:
            self.check()
            for feed in self.feeds:
                verify_file(feed.spec.binding['source'], '原素材')
                for source in getattr(feed.spec, 'extra_sources', []): verify_file(source, '辅助遮罩')

"""Alpha-correct loop joins; only overlap heads/tails are held in RAM."""
from collections import deque
from contextlib import closing
import math
import subprocess

from .matting_assets import fingerprint, verify_file
from .matting_live import StreamInput, dynamic
from .matting_models import check_cancel

BUFFER_LIMIT = 256*1024**2  # Per input, head + tail, independent of clip length.


def enabled(layer):
    from .visual_media import is_animated
    return (layer.get('media_loop', True) and layer.get('media_loop_transition', 'none') == 'fade'
            and layer.get('media_loop_fade', .3) > 0 and is_animated(layer.get('path', '')))


class LoopInput(StreamInput):
    def __new__(cls, layer, duration, fps, ffmpeg, box):
        binding = layer['_matting_binding'] if dynamic(layer) else dict(mode='visual_loop', source=fingerprint(layer['path']))
        value = super().__new__(cls, binding, duration, True, fps, ffmpeg)
        value.source_width, value.source_height = value.width, value.height
        # Key/AI at original resolution, then keep overlap frames at the size
        # actually needed by the composition. Never upscale the RAM buffers.
        ratio = min(1., box[0]/value.width, box[1]/value.height)
        value.width, value.height = max(1, round(value.width*ratio)), max(1, round(value.height*ratio))
        from .engine import resolve_tools, probe
        _, ffprobe = resolve_tools(ffmpeg)
        value.clip_duration = probe(value, ffprobe)['duration']
        clip_frames = max(1, round(value.clip_duration*float(value.fps)))
        requested = max(1, round(layer['media_loop_fade']*float(value.fps))) if layer['media_loop_fade'] > 0 else 0
        limit = BUFFER_LIMIT//(2*value.width*value.height*4)
        value.overlap = min(requested, clip_frames//3, limit)
        if duration <= value.clip_duration + .5/float(value.fps): value.overlap = 0
        value.loop_layer = dict(layer)
        value.extra_sources = []
        if not dynamic(layer):
            from .chroma_key import active_mask
            mask = active_mask(layer)
            if mask: value.extra_sources.append(fingerprint(mask))
        return value


def blend(tail, head, weight, width, height):
    """Interpolate premultiplied RGB and alpha, return straight RGBA bytes."""
    import numpy as np
    a = np.frombuffer(tail, np.uint8).reshape(height, width, 4).astype(np.float32)
    b = np.frombuffer(head, np.uint8).reshape(height, width, 4).astype(np.float32)
    alpha_a, alpha_b = a[:, :, 3:4]*(1-weight), b[:, :, 3:4]*weight
    alpha = alpha_a + alpha_b
    rgb = a[:, :, :3]*alpha_a + b[:, :, :3]*alpha_b
    np.divide(rgb, alpha, out=rgb, where=alpha > 0)
    rgb[alpha[:, :, 0] == 0] = 0
    result = np.empty_like(a, dtype=np.uint8)
    result[:, :, :3] = np.rint(np.clip(rgb, 0, 255)).astype(np.uint8)
    result[:, :, 3] = np.rint(np.clip(alpha[:, :, 0], 0, 255)).astype(np.uint8)
    return result.tobytes()


def resize_rgba(data, width, height):
    """Downsize AI output without mixing hidden background RGB into hair edges."""
    import cv2
    import numpy as np
    rgba = data.astype(np.float32)
    rgba[:, :, :3] *= rgba[:, :, 3:4]/255
    small = cv2.resize(rgba, (width, height), interpolation=cv2.INTER_AREA)
    alpha = np.clip(small[:, :, 3:4], 0, 255)
    rgb = np.zeros_like(small[:, :, :3])
    np.divide(small[:, :, :3]*255, alpha, out=rgb, where=alpha > 0)
    result = np.empty((height, width, 4), np.uint8)
    result[:, :, :3] = np.rint(np.clip(rgb, 0, 255)).astype(np.uint8)
    result[:, :, 3] = np.rint(alpha[:, :, 0]).astype(np.uint8)
    return result


def cycle(feed):
    """One source pass, same key/AI operations as ordinary visual composition."""
    import numpy as np
    from . import matting_render as render
    from .matting_settings import normalize, refine
    spec = feed.spec; check_cancel(feed.cancel)
    ai = spec.binding.get('mode') == 'dynamic'
    predictor = None
    if ai:
        p = normalize(spec.binding['parameters'])
        predictor = render.Predictor(p['model'], p['device'], p['quality'], cancel=feed.cancel)
    args = [spec.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '1',
            '-threads', '2', '-i', str(spec)]
    filters, label = [], '0:v'
    if not ai and spec.loop_layer.get('key_mode', 'none') != 'none':
        from .chroma_key import add_keyed_source
        from .visual_media import is_animated
        label, _ = add_keyed_source(args, filters, spec.loop_layer, 0, 'loop_matte', spec.fps,
                                    is_animated, duration=spec.clip_duration)
    if ai:
        w, h = spec.source_width, spec.source_height
        filters.append(f'[{label}]fps={float(spec.fps):.9f},format=rgba[decoded]')
    else:
        w, h = spec.width, spec.height
        filters.append(f'[{label}]fps={float(spec.fps):.9f},format=rgba,scale={w}:{h},setsar=1[decoded]')
    args += ['-filter_complex', ';'.join(filters), '-map', '[decoded]', '-an', '-sn', '-f', 'rawvideo',
             '-threads', '1', '-pix_fmt', 'rgba', 'pipe:1']
    process = None; frames = 0
    try:
        with feed._process_lock:
            check_cancel(feed.cancel)
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            feed.decoder = process
        while True:
            check_cancel(feed.cancel)
            data = process.stdout.read(w*h*4)
            if not data: break
            if len(data) != w*h*4: raise ValueError('循环素材帧解码不完整。')
            if ai:
                source = np.frombuffer(data, np.uint8).reshape(h, w, 4)
                rgb, alpha = predictor.predict(source[:, :, :3])
                result = refine(rgb, alpha, source[:, :, 3], p)
                if (w, h) != (spec.width, spec.height):
                    result = resize_rgba(result, spec.width, spec.height)
                data = result.tobytes()
            frames += 1; yield data
        check_cancel(feed.cancel)
        if process.wait(timeout=15): raise RuntimeError('循环素材解码／抠像失败，请检查素材和辅助遮罩。')
        if not frames: raise ValueError('循环素材没有可解码的画面。')
    finally:
        with feed._process_lock:
            render._stop(process)
            if process and process.stdout: process.stdout.close()
            feed.decoder = None


def frames(feed):
    spec = feed.spec
    verify_file(spec.binding['source'], '原素材')
    for source in spec.extra_sources: verify_file(source, '辅助遮罩')
    count = max(1, math.ceil(spec.duration*float(spec.fps)))
    written, head, overlap = 0, [], spec.overlap
    first = True
    while written < count:
        check_cancel(feed.cancel)
        tail = deque()
        with closing(cycle(feed)) as source:
            skipped = 0
            for frame in source:
                check_cancel(feed.cancel)
                if first and len(head) < overlap:
                    head.append(frame); written += 1; yield frame
                elif not first and skipped < len(head):
                    skipped += 1
                else:
                    tail.append(frame)
                    if len(tail) > overlap:
                        written += 1; yield tail.popleft()
                if written >= count: return
        if not overlap or len(tail) != overlap or len(head) != overlap:
            # A short/damaged duration estimate must not silently lose frames.
            for frame in tail:
                written += 1; yield frame
                if written >= count: return
            overlap = 0; head.clear()
        else:
            for index, frame in enumerate(tail):
                check_cancel(feed.cancel)
                weight = (index+1)/(overlap+1)
                written += 1
                yield blend(frame, head[index], weight, spec.width, spec.height)
                if written >= count: return
        first = False

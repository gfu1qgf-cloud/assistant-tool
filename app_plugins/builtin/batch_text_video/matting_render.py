"""Cancelable one-time material processing; batch exports only reuse results."""
from fractions import Fraction
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import time

from model.AppDataStorage import data_file, data_directory, data_io
from .matting_assets import Catalogue, fingerprint
from .matting_models import Predictor, check_cancel, MattingCancelled
from .matting_settings import normalize, refine


def image_array(image):
    import numpy as np
    from qt_compat import QtGui
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()


def qimage(data):
    from qt_compat import QtGui
    h, w = data.shape[:2]
    return QtGui.QImage(data.tobytes(), w, h, w*4, QtGui.QImage.Format.Format_RGBA8888).copy()


def load_source(path, cancel=None, ffmpeg=''):
    from qt_compat import QtCore, QtGui
    from .layers import read_image
    from .visual_media import is_animated
    check_cancel(cancel)
    before = fingerprint(path)
    if is_animated(path):
        from .engine import resolve_tools, probe
        executable, ffprobe = resolve_tools(ffmpeg)
        stream = probe(path, ffprobe)['video']
        if not stream or stream['width']*stream['height'] > 40_000_000 or max(stream['width'], stream['height']) > 8192:
            raise ValueError('素材画幅过大或没有视频画面。')
        # Decode at native dimensions. The general poster helper scales small
        # video up to its requested bounds, unsuitable for AI source pixels.
        decoded = subprocess.run([executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path),
            '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], capture_output=True,
            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        source = QtGui.QImage.fromData(decoded.stdout)
        if decoded.returncode or source.isNull(): raise ValueError('无法读取素材首帧。')
    else: source = read_image(path, QtCore.QSize(8192, 8192))
    check_cancel(cancel)
    return dict(source=image_array(source), expected=before)


def infer(path, parameters, *, models=None, cancel=None, report=None, ffmpeg=''):
    p = normalize(parameters)
    base = load_source(path, cancel, ffmpeg)
    predictor = Predictor(p['model'], p['device'], p['quality'], models, cancel, report)
    rgb, alpha = predictor.predict(base['source'][:, :, :3])
    base.update(rgb=rgb, alpha=alpha, device=predictor.device, model=p['model'], quality=p['quality'])
    return base


def refined(base, parameters):
    return refine(base['rgb'], base['alpha'], base['source'][:, :, 3], parameters)


def _temporary(catalogue, suffix):
    root = data_directory(catalogue.root/'assets').path
    data_directory(root).ensure()
    handle, path = data_io.mkstemp(prefix='render-', suffix=suffix, dir=root)
    os.close(handle)
    return Path(path)


def save(path, parameters, catalogue=None, *, base=None, models=None, ffmpeg='', cancel=None, report=None, progress=None):
    from .visual_media import is_animated
    p = normalize(parameters); catalogue = catalogue or Catalogue()
    temporary = _temporary(catalogue, '.mov' if is_animated(path) else '.png')
    try:
        if is_animated(path):
            expected, info = _video(path, temporary, p, models, ffmpeg, cancel, report, progress)
        else:
            if base and (base['expected']['path'] != str(Path(path).resolve()) or
                         base['model'] != p['model'] or base['quality'] != p['quality']):
                base = None
            base = base or infer(path, p, models=models, cancel=cancel, report=report)
            expected = base['expected']
            check_cancel(cancel)
            from PIL import Image
            with data_file(temporary).open('wb') as output:
                Image.fromarray(refined(base, p), 'RGBA').save(output, format='PNG')
            info = dict(model=p['model'], device=base['device'])
        check_cancel(cancel)
        return catalogue.publish(path, temporary, p, expected=expected, model_info=info)
    finally:
        if temporary.exists(): data_file(temporary).remove()


def save_dynamic(path, parameters, catalogue=None, *, base=None, models=None, ffmpeg='', cancel=None, report=None, progress=None):
    """Validate the visible first frame, then publish only frozen parameters."""
    p = normalize(parameters)
    if (not base or base['expected']['path'] != str(Path(path).resolve()) or
            base['model'] != p['model'] or base['quality'] != p['quality']):
        base = infer(path, p, models=models, cancel=cancel, report=report, ffmpeg=ffmpeg)
    refined(base, p)  # Validate the exact refinement before committing metadata.
    check_cancel(cancel)
    return (catalogue or Catalogue()).publish_dynamic(path, p, expected=base['expected'])


def _wait(process, cancel):
    while process.poll() is None:
        check_cancel(cancel); time.sleep(.05)
    return process.returncode


def _stop(process):
    if process is None: return
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def _video(path, target, p, models, ffmpeg, cancel, report, progress):
    import numpy as np
    from .engine import resolve_tools, probe
    report, progress = report or (lambda _text: None), progress or (lambda _number: None)
    expected = fingerprint(path)
    ffmpeg, ffprobe = resolve_tools(ffmpeg)
    info = probe(path, ffprobe); stream = info['video']
    if not stream: raise ValueError('素材没有视频画面。')
    w, h = stream.get('display_width', stream['width']), stream.get('display_height', stream['height'])
    rotation = stream.get('tags', {}).get('rotate', 0)
    rotation = next((v.get('rotation', rotation) for v in stream.get('side_data_list', []) if 'rotation' in v), rotation)
    if abs(float(rotation)) % 180 == 90: w, h = h, w
    if max(w, h) > 8192 or w*h > 40_000_000: raise ValueError('素材画幅过大，请先缩小后抠像。')
    try: fps = Fraction(stream.get('avg_frame_rate', '25/1'))
    except (ValueError, ZeroDivisionError): fps = Fraction(25)
    if fps <= 0 or fps > 120: fps = Fraction(25)
    predictor = Predictor(p['model'], p['device'], p['quality'], models, cancel, report)
    report('逐帧处理并保存透明MOV（无音轨）；结果可反复使用，无需每次导出重新推理。')
    decode = encode = None
    logs, stack = [], ExitStack()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        for label in ('decode', 'encode'):
            handle, filename = data_io.mkstemp(prefix=label+'-', suffix='.log', dir=target.parent)
            os.close(handle); logs.append(Path(filename))
        decode_log, encode_log = [stack.enter_context(data_file(filename).open('w+b')) for filename in logs]
        decode = subprocess.Popen([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path),
            '-vf', f'fps={float(fps):.9f}', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=decode_log, creationflags=flags)
        encode = subprocess.Popen([ffmpeg, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', str(fps), '-i', 'pipe:0',
            '-an', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(target)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=encode_log, creationflags=flags)
        total = max(1, round(info['duration']*float(fps))); frames = 0
        while True:
            check_cancel(cancel)
            chunks, length, needed = [], 0, w*h*4
            while length < needed:
                piece = decode.stdout.read(needed-length)
                if not piece: break
                chunks.append(piece); length += len(piece)
            if not length: break
            if length != needed: raise ValueError('素材帧解码不完整。')
            source = np.frombuffer(b''.join(chunks), np.uint8).reshape(h, w, 4)
            rgb, alpha = predictor.predict(source[:, :, :3])
            data = refine(rgb, alpha, source[:, :, 3], p)
            data_file(target)._check(write=True)
            encode.stdin.write(data.tobytes())
            frames += 1; progress(min(99, round(frames/total*100)))
        encode.stdin.close()
        decode_code, encode_code = _wait(decode, cancel), _wait(encode, cancel)
        if decode_code or encode_code or not frames:
            error = []
            for stream in (decode_log, encode_log):
                stream.flush(); stream.seek(0); error.append(stream.read()[-3000:].decode('utf-8', 'replace'))
            raise RuntimeError('透明视频生成失败：'+' '.join(error))
        progress(100)
        return expected, dict(model=p['model'], device=predictor.device, frames=frames, fps=str(fps))
    except BrokenPipeError as error:
        raise RuntimeError('透明视频编码中断，未登记抠像结果。') from error
    finally:
        _stop(decode); _stop(encode)
        for process in (decode, encode):
            if process:
                for stream in (process.stdout, process.stdin):
                    if stream and not stream.closed: stream.close()
        stack.close()
        for filename in logs:
            if filename.exists(): data_file(filename).remove()

"""First-frame face localization; one cached point, no per-frame inference.

Layer x/y remain canvas percentages, referring to the face in this mode.
Only render copies carry the normalized source anchor. No source is rewritten.
"""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import threading

VERSION = 'first-face-1'
_MEMORY, _LAYOUTS, _LOCK = OrderedDict(), OrderedDict(), threading.RLock()
_MODELS = threading.local()


def enabled(layer):
    return layer.get('kind') == 'image' and layer.get('position_reference', 'canvas') == 'face'


def source_path(layer):
    binding = layer.get('_matting_binding', {}).get('source', {}).get('path')
    return binding or (layer.get('source_file') or layer.get('path', '') if layer.get('source') == 'file' else layer.get('path', ''))


def _layout_key(layer):
    from .visual_loop_region import identity
    return (identity(source_path(layer)), bool(layer.get('media_crop_alpha', False)),
            layer.get('media_crop_alpha_threshold', 1), layer.get('media_crop_alpha_padding', 2))


def _first_frame(path, ffmpeg='', cancel=None):
    from qt_compat import QtCore, QtGui
    from .visual_media import is_animated
    if not is_animated(path):
        from .layers import read_image
        return read_image(path, QtCore.QSize(1024, 1024))
    from .dnx_alpha import describe, poster, command
    from .engine import resolve_tools
    ffmpeg, _ = resolve_tools(ffmpeg)
    native = describe(path, ffmpeg, cancel)
    if native:
        return poster(path, QtCore.QSize(min(1024, native['width']), min(1024, native['height'])), ffmpeg=ffmpeg, cancel=cancel)
    output = command([ffmpeg, '-nostdin', '-v', 'error', '-threads', '1', '-i', str(path),
        '-vf', "scale=w='min(iw,1024)':h='min(ih,1024)':force_original_aspect_ratio=decrease",
        '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'], cancel)
    image = QtGui.QImage.fromData(output)
    if image.isNull(): raise ValueError('无法读取人脸定位所需的视频首帧。')
    return image


def _models():
    import cv2
    if not hasattr(_MODELS, 'models'):
        models = []
        for name in ('haarcascade_frontalface_default.xml', 'haarcascade_profileface.xml'):
            path = Path(cv2.data.haarcascades)/name
            # OpenCV's narrow Windows filename API cannot open a Chinese
            # installation path. Read with Python, then parse XML in memory.
            try:
                storage = cv2.FileStorage(path.read_text(encoding='utf8'), cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
                try:
                    model = cv2.CascadeClassifier(); model.read(storage.getFirstTopLevelNode())
                finally: storage.release()
            except (OSError, cv2.error) as error:
                raise ValueError('无法读取本地人脸定位模型，请修复软件安装：'+name) from error
            if model.empty(): raise ValueError('缺少本地人脸定位模型，请修复软件安装：'+name)
            models.append(model)
        _MODELS.models = models
    return _MODELS.models


def detect(image, cancel=None):
    """Prefer frontal faces; use both profile directions only as fallback."""
    from .dnx_alpha import check
    from qt_compat import QtGui
    import cv2
    import numpy as np
    check(cancel)
    if image.isNull(): raise ValueError('人脸定位需要有效的首帧图像。')
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4)
    alpha = data[:, :, 3:4].astype(np.float32)/255
    rgb = np.rint(data[:, :, :3]*alpha+128*(1-alpha)).astype(np.uint8)
    gray = cv2.equalizeHist(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    frontal, profile = _models()
    options = dict(scaleFactor=1.1, minNeighbors=5, minSize=(24,24))
    faces = [tuple(map(int, box)) for box in frontal.detectMultiScale(gray, **options)]
    check(cancel)
    if not faces:
        faces = [tuple(map(int, box)) for box in profile.detectMultiScale(gray, **options)]
        check(cancel)
        for x,y,w,h in profile.detectMultiScale(cv2.flip(gray, 1), **options):
            faces.append((gray.shape[1]-int(x)-int(w), int(y), int(w), int(h)))
    check(cancel)
    # Stable principal-face choice: largest, then nearest frame centre.
    faces.sort(key=lambda b: (-b[2]*b[3], (b[0]+b[2]/2-image.width()/2)**2+
                (b[1]+b[3]/2-image.height()/2)**2, b[0], b[1]))
    unique = []
    for item in faces:
        x,y,w,h = item
        duplicate = False
        for a,b,c,d in unique:
            overlap = max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
            if overlap > .4*(w*h+c*d-overlap): duplicate = True; break
        if not duplicate: unique.append(item)
    faces = unique
    found = len(faces)
    x,y,w,h = faces[0] if found else (0,0,image.width(),image.height())
    return dict(found=found, ax=(x+w/2)/image.width(), ay=(y+h/2)/image.height(),
                aspect=image.width()/image.height())


def _valid(value):
    return (type(value.get('found')) is int and value['found'] >= 0 and
            all(type(value.get(k)) in (int,float) and math.isfinite(value[k]) for k in ('ax','ay','aspect')) and
            0 <= value['ax'] <= 1 and 0 <= value['ay'] <= 1 and .001 <= value['aspect'] <= 1000)


def locate(path, ffmpeg='', cancel=None):
    from .dnx_alpha import signature, check, file_lock
    from .preview_cache import cache_root
    from model.AppDataStorage import data_directory, data_file, data_io
    stamp = signature(path)
    key = hashlib.sha256(json.dumps([VERSION, stamp], ensure_ascii=False).encode()).hexdigest()
    check(cancel)
    with _LOCK:
        if key in _MEMORY: _MEMORY.move_to_end(key); return dict(_MEMORY[key])
    root = cache_root('face-anchors'); data_directory(root).ensure()
    target = root/(key+'.json')
    with file_lock(root/(key+'.lock'), cancel):
        try:
            value = json.loads(data_file(target).read_text(encoding='utf8'))
            if not isinstance(value, dict) or value.get('key') != key or not _valid(value): raise ValueError('旧人脸定位缓存。')
        except (OSError, ValueError, TypeError):
            value = detect(_first_frame(path, ffmpeg, cancel), cancel)
            if signature(path) != stamp: raise ValueError('定位人脸期间素材发生变化，请重新预览。')
            value['key'] = key
            with data_io.TemporaryDirectory(prefix='face-', dir=root) as directory:
                pending = Path(directory)/'point.json'
                data_file(pending).write_text(json.dumps(value), encoding='utf8'); data_io.replace(pending, target)
        check(cancel)
        if signature(path) != stamp: raise ValueError('人脸定位素材已改变，请重新预览。')
    with _LOCK:
        _MEMORY[key] = dict(value)
        while len(_MEMORY) > 128: _MEMORY.popitem(last=False)
    return value


def for_layer(layer, *, alpha_box=None, ffmpeg='', cancel=None):
    if not enabled(layer): return None
    from .alpha_bounds import current_cancel
    value = dict(locate(source_path(layer), ffmpeg, cancel if cancel is not None else current_cancel()))
    # Detection is on the original frame. Native transparent trimming may have
    # removed borders; map that original point into the surviving source box.
    if alpha_box:
        if value['found']:
            value['ax'] = (value['ax']*alpha_box['width']-alpha_box['x'])/alpha_box['w']
            value['ay'] = (value['ay']*alpha_box['height']-alpha_box['y'])/alpha_box['h']
        value['aspect'] = alpha_box['w']/alpha_box['h']
        if not (0 <= value['ax'] <= 1 and 0 <= value['ay'] <= 1): value.update(found=0, reason='crop')
    if not value['found']: value.update(ax=.5, ay=.5)
    with _LOCK:
        key = _layout_key(layer); _LAYOUTS[key] = dict(value); _LAYOUTS.move_to_end(key)
        while len(_LAYOUTS) > 256: _LAYOUTS.popitem(last=False)
    return value


def cached(layer):
    """GUI callers only consult worker-published geometry: never stat/decode."""
    if not enabled(layer): return None
    with _LOCK:
        value = _LAYOUTS.get(_layout_key(layer))
        return dict(value) if value else None


def status(value):
    if value is None: return '首次预览时检测首帧人脸。'
    if value.get('reason') == 'crop': return '人脸不在当前保留范围内，按素材中心定位。'
    if not value['found']: return '首帧未检测到人脸，按素材中心定位。'
    return '已定位首帧人脸。' if value['found'] == 1 else f"已定位首帧主要人脸（共{value['found']}张，使用最大）。"


def center_for(layer, width, height, value):
    """Object centre in canvas pixels at base size, not the face target."""
    cx,cy = width*layer['x']/100, height*layer['y']/100
    if value is None: return cx,cy
    box_w,box_h = width*layer['width']/100, height*layer['height']/100
    w = min(box_w, box_h*value['aspect']); h = w/value['aspect']
    return cx+(.5-value['ax'])*w, cy+(.5-value['ay'])*h

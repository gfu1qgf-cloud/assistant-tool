"""Still backgrounds share one finite clock and centered zoom in both viewers."""
from pathlib import Path

from qt_compat import QtCore, QtGui
from .layers import IMAGE_SUFFIXES, read_image

FPS = 30
DEFAULTS = {'background_pool_id': '', 'background_visual': {'visual_animation': 'none'}}


def is_image(path):
    return Path(path).suffix.casefold() in IMAGE_SUFFIXES


def normalized(settings):
    from .background_visual import control, values
    identifier = settings.get('background_pool_id', '')
    if not isinstance(identifier, str): raise ValueError('背景池设置无效。')
    return {'background_pool_id': identifier, 'background_visual': values(control(settings))}


def duration(settings, voice_duration=None):
    from .adaptation import normalize_duration
    values = dict(settings)
    normalize_duration(values)
    return float(voice_duration) if voice_duration is not None else values['unvoiced_duration_seconds']


def probe_background(path, ffprobe, settings=None):
    if not path:
        from .engine import output_size
        width, height = output_size({'aspect': 'portrait', **(settings or {})})
        return {'empty_background': True, 'duration': duration(settings or {}), 'audio': False,
                'video': {'width': width, 'height': height,
                          'avg_frame_rate': f'{FPS}/1', 'r_frame_rate': f'{FPS}/1'}}
    if not is_image(path):
        from .engine import probe
        return probe(path, ffprobe)
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('背景图片不存在：' + path.name)
    if path.stat().st_size > 200 * 1024**2:
        raise ValueError('背景图片超过200 MB：' + path.name)
    reader = QtGui.QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 40_000_000:
        raise ValueError('背景图片损坏、不支持或超过4000万像素：' + path.name)
    transform = reader.transformation().value
    if transform & 4:  # EXIF rotations that swap the image axes.
        size.transpose()
    return {'still_image': True, 'duration': duration(settings or {}), 'audio': False,
            'video': {'width': size.width(), 'height': size.height(),
                      'avg_frame_rate': f'{FPS}/1', 'r_frame_rate': f'{FPS}/1'}}


def materialize(path, target):
    """Decode once with EXIF orientation, flatten alpha; never create a video cache."""
    image = read_image(path)
    if image.colorSpace().isValid():
        image = image.convertedToColorSpace(QtGui.QColorSpace(QtGui.QColorSpace.NamedColorSpace.SRgb))
    flattened = QtGui.QImage(image.size(), QtGui.QImage.Format.Format_RGB888)
    flattened.fill(QtCore.Qt.GlobalColor.black)
    painter = QtGui.QPainter(flattened)
    try:
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    if not flattened.save(str(target), 'PNG'):
        raise RuntimeError('无法准备背景图片。')
    return str(target)


def scale_at(settings, time, full_duration=None):
    from .background_visual import control
    from .visual_animation import motion_at
    layer = control(settings)
    if layer['visual_animation'] == 'none' or layer['animation_strength'] == 0: return 1.0
    return motion_at(layer, full_duration or duration(settings), time)[0]


def motion_filter(width, height, settings, full_duration=None, fps=FPS):
    import re
    from .background_visual import control
    from .visual_animation import expressions
    layer = control(settings)
    if layer['visual_animation'] == 'none' or layer['animation_strength'] == 0:
        return f'fps={fps:.9f},setsar=1'
    scale, _ = expressions(layer, full_duration or duration(settings))
    zoom = re.sub(r'\bt\b', f'(on/{fps:.9f})', scale)
    return (f"zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
            f'd=1:s={width}x{height}:fps={fps:.9f},setsar=1')

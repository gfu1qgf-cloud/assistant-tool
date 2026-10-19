"""Read-only media facts for conditional workflows."""
import math
from fractions import Fraction

from .contracts import NodeDefinition, NodePort


def build_video_probe(inputs, _params, context):
    from .render import probe_resource

    context.check_canceled()
    info = probe_resource(inputs['video'], context, getattr(context, 'ffmpeg', ''))
    video = info['video']
    duration = float(info['duration'])
    rate = video.get('avg_frame_rate') or video.get('r_frame_rate') or '0'
    try:
        fps = float(Fraction(str(rate)))
    except (ValueError, ZeroDivisionError, OverflowError):
        fps = 0.0
    if not math.isfinite(fps) or fps < 0:
        fps = 0.0
    rotations = [item.get('rotation') for item in (video.get('side_data_list') or ())
                 if isinstance(item, dict) and 'rotation' in item]
    raw_rotation = rotations[0] if rotations else (video.get('tags') or {}).get('rotate', 0)
    try:
        rotation = float(raw_rotation) % 360
    except (TypeError, ValueError, OverflowError):
        rotation = 0.0
    if not math.isfinite(rotation):
        rotation = 0.0
    width = int(video.get('display_width') or video['width'])
    height = int(video.get('display_height') or video['height'])
    sar = video.get('sample_aspect_ratio')
    if sar:
        try:
            aspect = float(Fraction(str(sar).replace(':', '/')))
        except (ValueError, ZeroDivisionError, OverflowError):
            aspect = 1.0
        if math.isfinite(aspect) and 0 < aspect <= 100:
            display_width = max(1, round(int(video['width']) * aspect))
            display_height = int(video['height'])
            width, height = ((display_height, display_width)
                             if round(rotation / 90) % 2 else (display_width, display_height))
    return {'duration': duration, 'width': width, 'height': height, 'fps': fps,
            'has_audio': bool(info['audio']), 'rotate': rotation}


def register_video_probe(registry):
    registry.register('core', NodeDefinition('core.video_probe', '视频属性 · 探针',
        inputs=(NodePort('video', '视频资源', 'video'),),
        outputs=(NodePort('duration', '时长 · 秒', 'number'),
                 NodePort('width', '显示宽度 · 像素', 'number'),
                 NodePort('height', '显示高度 · 像素', 'number'),
                 NodePort('fps', '帧率 · 未知时为 0', 'number'),
                 NodePort('has_audio', '是否有音频', 'boolean'),
                 NodePort('rotate', '旋转角度 · 度', 'number')),
        build=build_video_probe, execution='compute', category='视频/信息',
        keywords=('ffprobe', '时长', '分辨率', '帧率'),
        description='读取输入视频的实际属性，不重新编码。显示尺寸考虑视频旋转和像素宽高比；'
                    '未知帧率明确输出 0。连接文件时在后台调用 FFprobe。'))

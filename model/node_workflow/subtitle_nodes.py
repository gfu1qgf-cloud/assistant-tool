"""Pure, validated transformations of in-memory subtitle tracks."""
import math

from .contracts import NodeDefinition, NodePort, SubtitleCue, SubtitleTrack


def track(value):
    if not isinstance(value, SubtitleTrack):
        raise TypeError('此节点需要词级／轨道字幕；SRT 文件引用请先转换为字幕轨道。')
    return value.verify()


def seconds(value, title):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 86400:
        raise ValueError(title + '须是有限的秒数，绝对值不超过 86400。')
    return float(value)


def shift(inputs, _params, _context):
    source = track(inputs['subtitles'])
    offset = seconds(inputs['offset'], '偏移')
    cues = tuple(SubtitleCue(max(0.0, cue.start + offset), cue.end + offset, cue.text)
                 for cue in source.cues if cue.end + offset > 0)
    if not cues:
        raise ValueError('字幕移位后没有可见内容。')
    # Cues are ordered by start, not end: an earlier overlapping cue can
    # extend beyond the last cue. Preserve the full shifted track.
    duration = max(source.duration, max(cue.end for cue in cues))
    return {'subtitles': SubtitleTrack(cues, source.task_key, duration, source.timing).verify()}


def trim(inputs, _params, _context):
    source = track(inputs['subtitles'])
    start, end = seconds(inputs['start'], '裁剪起点'), seconds(inputs['end'], '裁剪终点')
    if not 0 <= start < end <= source.duration:
        raise ValueError('字幕裁剪范围须在原轨道时长内，且终点晚于起点。')
    cues = tuple(SubtitleCue(max(start, cue.start) - start, min(end, cue.end) - start, cue.text)
                 for cue in source.cues if cue.start < end and cue.end > start)
    if not cues:
        raise ValueError('裁剪范围内没有字幕，不会输出空轨道。')
    return {'subtitles': SubtitleTrack(cues, source.task_key, end - start, source.timing).verify()}


def merge(inputs, _params, _context):
    sources = tuple(track(item) for item in inputs['subtitles'])
    if not sources:
        raise ValueError('请至少连接一条字幕轨道。')
    keys = {source.task_key for source in sources} - {''}
    if len(keys) > 1:
        raise ValueError('不能合并不同任务绑定的字幕轨道。')
    cues = tuple(sorted((cue for source in sources for cue in source.cues), key=lambda cue: cue.start))
    return {'subtitles': SubtitleTrack(cues, next(iter(keys), ''),
                                       max(source.duration for source in sources), 'merged').verify()}


def register_subtitle_nodes(registry):
    category = '字幕/轨道处理'
    incoming = (NodePort('subtitles', '字幕轨道', 'subtitles'),)
    outgoing = (NodePort('subtitles', '处理后的字幕轨道', 'subtitles'),)
    registry.register('core', NodeDefinition('core.subtitle_shift', '字幕时间偏移',
        inputs=incoming + (NodePort('offset', '偏移秒数 · 可为负', 'number'),),
        outputs=outgoing, build=shift, category=category,
        description='整体移动轨道时间；移到 0 秒之前的片段会截断，完全移出则报错。'
                    '若尾部超出原时长，输出轨道会延长；最终仍须与视频时长匹配。'))
    registry.register('core', NodeDefinition('core.subtitle_trim', '字幕时间裁剪',
        inputs=incoming + (NodePort('start', '起点 · 秒', 'number'),
                           NodePort('end', '终点 · 秒', 'number')),
        outputs=outgoing, build=trim, category=category,
        description='只保留指定时间范围；跨边界的字幕按边界裁剪，输出时间从 0 开始。'
                    '视频也应裁到相同长度，空结果会报错。'))
    registry.register('core', NodeDefinition('core.subtitle_merge', '合并字幕轨道',
        inputs=(NodePort('subtitles', '字幕轨道 · 可连接多条', 'subtitles', multiple=True),),
        outputs=outgoing, build=merge, category=category,
        description='按起始时间稳定合并多条字幕，不改文本或写 SRT；不同任务的轨道不能混用。'))

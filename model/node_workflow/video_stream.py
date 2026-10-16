"""One video contract for file/memory/pipe sources and deferred composition."""
from dataclasses import dataclass, replace

from .contracts import ResourceRef, VideoTimeline, VideoComposition, FilterOperation, RenderPlan

PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')


@dataclass(frozen=True)
class EncodingSettings:
    codec: str = 'libx264'
    preset: str = 'medium'
    crf: int = 23
    audio: str = 'aac'
    audio_kbps: int = 192

    def __post_init__(self):
        if (self.codec not in {'libx264', 'libx265'} or self.preset not in PRESETS
                or type(self.crf) is not int or not 0 <= self.crf <= 51
                or self.audio not in {'aac', 'copy'} or type(self.audio_kbps) is not int
                or not 32 <= self.audio_kbps <= 512):
            raise ValueError('视频编码参数无效；不接受原始命令或表达式。')


@dataclass(frozen=True)
class VideoStream:
    source: object
    operations: tuple = ()
    encoding: object = None
    data_type: str = 'video'

    @property
    def path(self):
        return self.source.path

    @property
    def storage(self):
        return 'composition'

    @property
    def task_key(self):
        return getattr(self.source, 'task_key', '')

    @property
    def capabilities(self):
        return self.source.capabilities

    def verify(self):
        if (not isinstance(self.source, (ResourceRef, VideoTimeline, VideoComposition))
                or self.source.data_type != 'video' or not isinstance(self.operations, tuple)
                or len(self.operations) > 512 or any(not isinstance(op, FilterOperation) for op in self.operations)
                or self.encoding is not None and not isinstance(self.encoding, EncodingSettings)):
            raise TypeError('视频流对象的来源、处理操作或编码设置无效。')
        self.source.verify()
        return self


def stream(value):
    if isinstance(value, VideoStream):
        return value.verify()
    return VideoStream(value).verify()


def same_source(first, second, context):
    """Transport may change storage, never the captured source identity."""
    if isinstance(first, ResourceRef) and isinstance(second, ResourceRef):
        for ref in (first, second):
            ref.verify()
            if ref.storage != 'file':
                context.media.verify(ref)
        return replace(first, storage=second.storage, resource_id=second.resource_id) == second
    return first == second


def unchanged_source(value, original, session):
    """Prove passthrough, not visual similarity; never discard an actual effect."""
    value = stream(value)
    if value.encoding is not None or any(
            op.name != 'null' or op.options or op.assets
            or op.video not in (None, value.source) for op in value.operations):
        return False
    if not isinstance(value.source, ResourceRef):
        return False  # A timeline/composition is meaningful even if it looks similar.
    try:
        recovered = session.source_file(value.source)
    except ValueError:
        return False  # Generated streams cannot be proven to be the original.
    original.verify()
    return recovered == original


def transport(value, session, mode, limit_mb):
    """Cache/remux the compressed source; defer all effects until final encoding."""
    if not isinstance(value, VideoStream):
        return session.convert(value, mode, limit_mb)
    value.verify()
    if isinstance(value.source, (VideoTimeline, VideoComposition)):
        raise ValueError('剪辑／多视频合成计划尚未编码；可直接接预览或结算器，不需要中间缓存。')
    converted = session.convert(value.source, mode, limit_mb)
    return replace(value, source=converted, operations=tuple(
        replace(op, video=converted) if op.video == value.source else op for op in value.operations))


def compose(video, groups, context):
    from .operations import bind_operations
    value = stream(video)
    added = bind_operations(value.source, groups, context)
    return replace(value, operations=value.operations + added).verify()


def normalize_plan(plan):
    """Unwrap once; both normal exports and plugin sinks use this function."""
    if not isinstance(plan, RenderPlan):
        raise TypeError('需要视频渲染计划。')
    if not isinstance(plan.video, VideoStream):
        return plan
    value = plan.video.verify()
    operations = tuple(replace(op, video=value.source) if op.video == plan.video else op
                       for op in plan.operations)
    return replace(plan, video=value.source, operations=value.operations + operations,
                   encoding=plan.encoding or value.encoding)


def video_encoder_args(settings=None):
    if settings is None:
        return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p']
    if not isinstance(settings, EncodingSettings):
        raise TypeError('编码设置必须来自压缩节点。')
    return ['-c:v', settings.codec, '-preset', settings.preset, '-crf', str(settings.crf),
            '-pix_fmt', 'yuv420p']


def audio_encoder_args(settings=None, *, timeline=False):
    if settings is None:
        return ['-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2'] if timeline else ['-c:a', 'copy']
    # Timeline audio has already been filtered and cannot use stream-copy.
    if settings.audio == 'copy' and not timeline:
        return ['-c:a', 'copy']
    return ['-c:a', 'aac', '-b:a', str(settings.audio_kbps) + 'k']


def register_video_nodes(registry):
    from .contracts import NodeDefinition, NodeParameter, NodePort
    def compress(inputs, params, _context):
        value = stream(inputs['video'])
        enabled = inputs.get('enabled')
        if enabled is not None and type(enabled) is not bool:
            raise TypeError('是否压缩输入只接受真／假。')
        if enabled is False:
            return {'video': value}
        settings = EncodingSettings(params['codec'], params['preset'], params['crf'],
                                    params['audio'], params['audio_kbps'])
        return {'video': replace(value, encoding=settings)}
    registry.register('core', NodeDefinition('core.compress', '视频压缩 · 编码设置',
        inputs=(NodePort('video', '视频', 'video'), NodePort('enabled', '是否压缩（不接时开启）', 'boolean', required=False)),
        outputs=(NodePort('video', '视频', 'video'),),
        parameters=(NodeParameter('codec', '编码器', 'choice', 'libx264', choices=(('libx264', 'H.264'), ('libx265', 'H.265'))),
            NodeParameter('preset', '编码速度', 'choice', 'medium', choices=tuple((p, p) for p in PRESETS)),
            NodeParameter('crf', 'CRF · 越小越清晰', 'integer', 23, 0, 51),
            NodeParameter('audio', '音频', 'choice', 'aac', choices=(('aac', 'AAC · 兼容 MP4'), ('copy', '保留原编码'))),
            NodeParameter('audio_kbps', 'AAC 码率 kbps', 'integer', 192, 32, 512)), build=compress,
        description='可连接路径／文本判断到“是否压缩”：真时应用本节点编码设置，假时原样传递上游视频及已有设置；不接时开启。只记录设置，不产生中间视频；其他效果仍在最终保存时一次编码。多个启用的压缩节点以后面的设置为准。'))

"""One video contract for file/memory/pipe sources and deferred composition."""
from dataclasses import dataclass, replace

from .contracts import ResourceRef, VideoTimeline, VideoComposition, FilterOperation, RenderPlan

from .encoding import (EncodingSettings, PRESETS, PARAMETERS, from_parameters,
                       video_encoder_args, audio_encoder_args)


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


def compression_inspector(params, changed, parent):
    from PYUI.node_compression_ui import CompressionInspector
    return CompressionInspector(params, changed, parent)


def register_video_nodes(registry):
    from .contracts import NodeDefinition, NodeParameter, NodePort
    def compress(inputs, params, _context):
        value = stream(inputs['video'])
        enabled = inputs.get('enabled')
        if enabled is not None and type(enabled) is not bool:
            raise TypeError('是否压缩输入只接受真／假。')
        if enabled is False:
            return {'video': value}
        settings = from_parameters(params)
        return {'video': replace(value, encoding=settings)}
    registry.register('core', NodeDefinition('core.compress', '视频压缩 · 编码设置',
        inputs=(NodePort('video', '视频', 'video'), NodePort('enabled', '是否压缩（不接时开启）', 'boolean', required=False)),
        outputs=(NodePort('video', '视频', 'video'),),
        parameters=PARAMETERS, build=compress, inspector_factory=compression_inspector,
        validate_parameters=from_parameters,
        description='默认：MP4／H.264 质量30／MP3 128kbps／立体声44100Hz。只记录设置，在最终保存时一次编码，不生成中间视频；质量模式不保证文件一定更小，可选目标码率控制体积。后面的启用压缩节点覆盖前面的设置；是否压缩为假则原样传递。'))

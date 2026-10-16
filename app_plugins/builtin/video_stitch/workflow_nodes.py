"""The existing stitch engine exposed as a deferred, two-input video node."""
from dataclasses import asdict, replace
from fractions import Fraction
import json

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from model.node_workflow.contracts import ResourceRef, VideoComposition, VideoTimeline, VideoSection
from model.node_workflow.video_stream import VideoStream, stream
from model.node_workflow.source_composition import SourceStage
from . import engine


class CancelCheck:
    def __init__(self, context): self.context = context
    def is_set(self):
        self.context.check_canceled()
        return False


def source_media(value, context):
    from model.node_workflow.render import probe_resource, resolve_tools
    source = value.source
    if isinstance(source, ResourceRef):
        ref = source if source.storage == 'file' else context.media.source_file(source)
        _, ffprobe = resolve_tools(getattr(context, 'ffmpeg', ''))
        return engine.probe(ref.path, ffprobe, CancelCheck(context))
    info = probe_resource(value, context, getattr(context, 'ffmpeg', ''))
    video = info['video']
    return engine.Media('', int(video['width']), int(video['height']), info['duration'],
        Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate') or '25'), 0,
        0 if info['audio'] else -1, info['duration'] if info['audio'] else 0)


def build_stitch(inputs, params, context):
    context.check_canceled()
    left, right = stream(inputs['left']), stream(inputs['right'])
    media = [source_media(left, context), source_media(right, context)]
    basis = 0 if params['basis'] == 'left' else 1
    plan = engine.make_plan(*media, basis, params['audio'], params['dimensions'] == 'compatible')
    records = [{**asdict(value), 'fps': str(value.fps)} for value in media]
    info = {'duration': plan['duration'], 'video': {'width': plan['width'], 'height': plan['height'],
             'avg_frame_rate': plan['fps']}, 'audio': bool(plan['audio_indices'])}
    settings = {'basis': basis, 'audio': params['audio'], 'compatible': params['dimensions'] == 'compatible', 'media': records}
    source = VideoComposition('video_stitch.horizontal', (left, right),
        json.dumps(settings, ensure_ascii=False, allow_nan=False), json.dumps(info, allow_nan=False)).verify()
    if plan['missing_audio']:
        context.log('视频拼接：选定的声音来源没有音轨，将输出静音，不生成额外音频。')
    return {'video': VideoStream(source, encoding=(left, right)[basis].encoding).verify()}


def compile_stitch(source, builder, *, need_audio=True):
    settings = source.parameters
    media = [engine.Media(**{**record, 'fps': Fraction(record['fps'])}) for record in settings['media']]
    plan = engine.make_plan(*media, settings['basis'], settings['audio'], settings['compatible'])
    if not need_audio:
        plan = {**plan, 'audio_indices': []}
    stages = []
    for index, value in enumerate(source.inputs):
        repeat = index != plan['basis'] and plan['loop']
        # A long audio tail must not delay repetition of the actual picture.
        # Reuse the existing deferred timeline for this uncommon file case.
        if repeat and media[index].audio_duration > media[index].duration + 2 / float(media[index].fps):
            base = value.source
            if isinstance(base, ResourceRef):
                ref = base if base.storage == 'file' else builder.context.media.source_file(base)
                timeline = VideoTimeline((VideoSection(ref, 0, media[index].duration),),
                    media[index].width, media[index].height, str(media[index].fps), value.task_key,
                    media[index].audio_index >= 0)
                operations = tuple(replace(op, video=timeline) if op.video == base else op for op in value.operations)
                value = VideoStream(timeline, operations, value.encoding)
        stages.append(builder.build(value, plan['duration'], repeat=repeat,
                                    need_audio=index in plan['audio_indices'], source_metadata=settings['media'][index]))
    prefix = builder.namespace()
    filters, video, audio = engine.composition_filters(media, plan,
        [stage.video for stage in stages], [stage.audio for stage in stages], prefix)
    builder.filters.extend(filters)
    return SourceStage(video, audio, {**source.info, 'audio': bool(audio)})


def stitch_node():
    return NodeDefinition('video_stitch.horizontal', '视频拼接 · 左右',
        inputs=(NodePort('left', '左侧视频', 'video'), NodePort('right', '右侧视频', 'video')),
        outputs=(NodePort('video', '拼接后视频流', 'video'),),
        parameters=(NodeParameter('basis', '时长／帧率基准', 'choice', 'left', choices=(('left', '左侧视频'), ('right', '右侧视频'))),
            NodeParameter('audio', '声音来源', 'choice', 'basis', choices=(('basis', '跟随基准视频'), ('left', '仅左侧'),
                ('right', '仅右侧'), ('mix', '两侧混音'), ('mute', '静音'))),
            NodeParameter('dimensions', '尺寸方式', 'choice', 'compatible', choices=(('compatible', '兼容 MP4 · 奇数边补1像素'),
                ('exact', '严格原尺寸 · 可为奇数')))),
        build=build_stitch, compile_source=compile_stitch, execution='compute',
        category='插件处理/视频拼接', keywords=('hstack', '拼接', '左右', '合并'),
        description='两个必填视频端口；原尺寸左右拼接，宽度相加、高度取较大值，较矮一侧居中补黑边。'
            '选定视频决定时长和帧率，另一侧短则循环、长则裁掉。可接文件、无损内存传输、智能剪辑计划或已有图层计划。'
            '复用独立拼接插件的尺寸、画面和声音算法，只构建计划，不生成中间MP4；输出可继续接字幕／图层、预览、压缩或导出。')

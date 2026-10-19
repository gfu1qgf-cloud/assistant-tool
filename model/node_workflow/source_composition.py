"""Compile registered multi-video recipes into the existing final FFmpeg job."""

from model.AppDataStorage import data_io, data_file, data_directory
import copy
from dataclasses import dataclass, replace
import math
from pathlib import Path
import re

from .contracts import ResourceRef, RenderPlan, VideoTimeline, VideoComposition
from .filter_graph import append_chain
from .video_stream import VideoStream
from .encoding import mux_args, video_encoder_args, audio_encoder_args


@dataclass(frozen=True)
class SourceStage:
    video: str
    audio: object
    info: dict


class SourceBuilder:
    def __init__(self, context, directory, ffprobe):
        self.context, self.directory, self.ffprobe = context, Path(directory), ffprobe
        self.command, self.filters, self.serial, self.depth = [], [], 0, 0

    def namespace(self):
        self.serial += 1
        return f'src{self.serial}_'

    def input_count(self):
        return self.command.count('-i')

    def build(self, value, length=None, *, repeat=False, need_audio=True, source_metadata=None):
        from .render import probe_resource, verify_resource, compile_filters, timeline_source
        self.context.check_canceled()
        verify_resource(value, self.context)
        self.depth += 1
        try:
            if self.depth > 16 or self.input_count() > 128:
                raise ValueError('多视频计划超过16层或128个媒体输入的安全上限。')
            info = probe_resource(value, self.context, getattr(self.context, 'ffmpeg', ''))
            base = value.source if isinstance(value, VideoStream) else value
            if source_metadata is not None and isinstance(base, ResourceRef):
                info = {'duration': source_metadata['duration'], 'video': {
                    'width': source_metadata['width'], 'height': source_metadata['height'],
                    'avg_frame_rate': str(source_metadata['fps'])}, 'audio': source_metadata['audio_index'] >= 0}
            target = info['duration'] if length is None else length
            if not math.isfinite(target) or not 0 < target <= 86400:
                raise ValueError('多视频计划目标时长无效。')
            processed = isinstance(value, VideoStream) and bool(value.operations)
            # Repeat the *processed* clip, not only its raw footage. Repeating
            # recipes/timelines is a filter plan, never an intermediate MP4.
            if repeat and target > info['duration'] + 1e-6 and (processed or not isinstance(base, ResourceRef)):
                count = math.ceil(target / info['duration'])
                if count > 256:
                    raise ValueError('处理后视频需循环超过256次，请调整视频或时长基准。')
                parts = [self.build(value, need_audio=need_audio, source_metadata=source_metadata) for _ in range(count)]
                prefix = self.namespace()
                audible = bool(parts[0].audio)
                tags = ''.join(f'[{part.video}]' + (f'[{part.audio}]' if audible else '') for part in parts)
                self.filters.append(tags + f'concat=n={count}:v=1:a={int(audible)}[{prefix}v]' +
                                    (f'[{prefix}a]' if audible else ''))
                return self.trim(SourceStage(prefix+'v', prefix+'a' if audible else None,
                                             {**info, 'audio': audible}), target)
            prefix = self.namespace()
            if isinstance(base, VideoComposition):
                registry = getattr(self.context, 'workflow_registry', None)
                definition = registry.definitions.get(base.kind) if registry else None
                if definition is None or definition.compile_source is None:
                    raise ValueError('多视频计划的插件已禁用或未提供源编译器：' + base.kind)
                stage = definition.compile_source(base, self, need_audio=need_audio)
                if not isinstance(stage, SourceStage):
                    raise TypeError('源编译器必须返回 SourceStage。')
            elif isinstance(base, VideoTimeline):
                timeline = replace(base, has_audio=base.has_audio and need_audio)
                inputs, lines, _count = timeline_source(timeline, self.ffprobe)
                offset = self.input_count()
                self.command.extend(inputs)
                def rename(match):
                    key = match.group(1)
                    port = re.fullmatch(r'(\d+):(v|a):0', key)
                    return f'[{offset+int(port[1])}:{port[2]}:0]' if port else f'[{prefix}{key}]'
                self.filters.extend(re.sub(r'\[([^\]]+)\]', rename, line) for line in lines)
                stage = SourceStage(prefix+'joined', prefix+'audio' if timeline.has_audio else None,
                                    {**info, 'audio': timeline.has_audio})
            elif isinstance(base, ResourceRef):
                ref = base if base.storage == 'file' else self.context.media.source_file(base)
                index = self.input_count()
                if repeat and target > info['duration'] + 1e-6:
                    self.command += ['-stream_loop', '-1']
                self.command += ['-threads', '2', '-i', ref.path]
                tag = f"{index}:{source_metadata['video_index']}" if source_metadata else f'{index}:v:0'
                scale = f"scale={source_metadata['width']}:{source_metadata['height']},setsar=1," if source_metadata else ''
                self.filters.append(f'[{tag}]setpts=PTS-STARTPTS,{scale}null[{prefix}v]')
                audible = need_audio and info['audio']
                if audible:
                    tag = f"{index}:{source_metadata['audio_index']}" if source_metadata else f'{index}:a:0'
                    self.filters.append(f'[{tag}]asetpts=PTS-STARTPTS[{prefix}a]')
                stage = SourceStage(prefix+'v', prefix+'a' if audible else None, {**info, 'audio': audible})
            else:
                raise TypeError('多视频输入只能是视频资源、剪辑计划或已注册的合成计划。')
            if processed:
                plan = RenderPlan(base, value.operations, '', value.encoding)
                chain = compile_filters(plan, self.directory, self.context, video_info=info, asset_prefix=prefix)
                result = prefix+'processed'
                append_chain(chain, self.command, self.filters, stage.video, self.input_count(), result, prefix=prefix)
                stage = SourceStage(result, stage.audio, stage.info)
            return self.trim(stage, target)
        finally:
            self.depth -= 1

    def trim(self, stage, duration):
        prefix = self.namespace()
        self.filters.append(f'[{stage.video}]trim=duration={duration:.9f},setpts=PTS-STARTPTS[{prefix}v]')
        audio = None
        if stage.audio:
            audio = prefix+'a'
            self.filters.append(f'[{stage.audio}]aresample=48000:async=1:first_pts=0,'
                f'aformat=channel_layouts=stereo,apad,atrim=duration={duration:.9f},'
                f'asetpts=PTS-STARTPTS[{audio}]')
        return SourceStage(prefix+'v', audio, {**copy.deepcopy(stage.info), 'duration': duration, 'audio': bool(audio)})


def composition_command(video, ffmpeg, ffprobe, chain, target, context, encoding=None, *, include_audio=True):
    builder = SourceBuilder(context, Path(target).parent, ffprobe)
    stage = builder.build(video, need_audio=include_audio)
    command = [ffmpeg, '-n', '-nostdin', '-hide_banner', '-loglevel', 'error', '-progress', 'pipe:1'] + builder.command
    append_chain(chain, command, builder.filters, stage.video, builder.input_count())
    if builder.input_count() > 128 or len(builder.filters) > 10000 or len(';'.join(builder.filters)) > 1024*1024:
        raise ValueError('多视频过滤器计划过大；未启动渲染。')
    script = Path(target).parent / 'filters.txt'
    data_file(script).write_text_direct(';\n'.join(builder.filters), encoding='utf-8')
    command += ['-filter_complex_threads', '2', '-/filter_complex', str(script), '-map', '[final]', '-threads', '2']
    video_args = video_encoder_args(encoding)
    spec = stage.info['video']
    if spec['width'] % 2 or spec['height'] % 2:
        video_args[video_args.index('-pix_fmt')+1] = 'yuv444p'
    command += video_args
    if stage.audio:
        command += ['-map', '['+stage.audio+']'] + audio_encoder_args(encoding, timeline=True)
    else:
        command += ['-an']
    return command + ['-t', f"{stage.info['duration']:.9f}"] + mux_args(encoding) + [str(target)]

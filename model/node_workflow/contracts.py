"""Plain, copyable values only. Resource references are not live OS handles."""
from dataclasses import dataclass
import copy
import json
import math
from pathlib import Path
import re
from typing import Callable, Tuple


@dataclass(frozen=True)
class NodePort:
    key: str
    title: str
    data_type: str
    required: bool = True
    multiple: bool = False
    capabilities: Tuple[str, ...] = ()
    # data_type="any" ports sharing a group infer ONE concrete type from the graph.
    type_group: str = ""


def validate_ports(ports):
    if not isinstance(ports, tuple) or len(ports) > 128 or any(
            not isinstance(p, NodePort) or not p.key or not p.data_type for p in ports):
        raise ValueError("节点端口定义无效。")
    if len({p.key for p in ports}) != len(ports):
        raise ValueError("节点端口名称重复。")
    if any(not isinstance(p.capabilities, tuple) or
           any(c not in {"seek", "replay"} for c in p.capabilities) for p in ports):
        raise ValueError("端口能力必须使用 seek / replay 声明。")
    if any(not isinstance(p.type_group, str) or
           p.data_type == "any" and not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", p.type_group) or
           p.data_type != "any" and bool(p.type_group) for p in ports):
        raise ValueError("多态端口必须声明 data_type=any 和同类型组 type_group；固定类型端口不能声明类型组。")
    return ports


@dataclass(frozen=True)
class NodeParameter:
    key: str
    title: str
    kind: str = "text"
    default: object = ""
    minimum: int = 0
    maximum: int = 10000
    choices: Tuple = ()
    required: bool = True


@dataclass(frozen=True)
class NodeDefinition:
    kind: str
    title: str
    inputs: Tuple[NodePort, ...] = ()
    outputs: Tuple[NodePort, ...] = ()
    parameters: Tuple[NodeParameter, ...] = ()
    build: Callable = None
    version: int = 1
    description: str = ""
    prepare: Callable = None
    execution: str = "plan"
    # Plugins may restrict a node to one workflow scope; "any" works in both.
    execution_scope: str = "any"
    # GUI-only properties editor and headless deferred-operation compiler.
    # Trusted callbacks stay in the registry, never in saved graphs/payloads.
    inspector_factory: Callable = None
    compile_operation: Callable = None
    validate_parameters: Callable = None
    # Presentation metadata belongs to the registered implementation, not the graph.
    category: str = ""
    keywords: Tuple[str, ...] = ()
    # A sink returns this typed value to the executor, not to downstream nodes.
    terminal_result: NodePort = None
    # Trusted registry callbacks, not serialized code. Input ports may depend on
    # ordinary parameters; a selector evaluates conditions and returns one key.
    input_factory: Callable = None
    select_input: Callable = None
    # Trusted plugin compiler for a deferred, multi-video source recipe.
    compile_source: Callable = None
    output_factory: Callable = None

    def input_ports(self, params=None):
        if self.input_factory is None:
            return self.inputs
        return validate_ports(self.input_factory(copy.deepcopy(params or {})))

    def output_ports(self, params=None):
        if self.output_factory is None:
            return self.outputs
        return validate_ports(self.output_factory(copy.deepcopy(params or {})))

    def result_ports(self, params=None):
        return self.output_ports(params) + ((self.terminal_result,) if self.terminal_result is not None else ())

    @property
    def result_fields(self):
        return self.outputs + ((self.terminal_result,) if self.terminal_result is not None else ())


@dataclass(frozen=True)
class ColorValue:
    """Explicit display-referred sRGB RGBA; never a QColor or an untyped hex string."""
    red: float
    green: float
    blue: float
    alpha: float = 1.0
    space: str = "srgb"

    def __post_init__(self):
        if self.space != "srgb" or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in (self.red, self.green, self.blue, self.alpha)):
            raise ValueError("颜色须为 sRGB，RGBA 均为 0–1 的有限数值。")

    @property
    def hex_rgb(self):
        return "#" + "".join(f"{round(v * 255):02x}" for v in (self.red, self.green, self.blue))

    @classmethod
    def from_hex(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
            raise ValueError("颜色文字须为 #RRGGBB 或 #RRGGBBAA。")
        channels = [int(value[i:i+2], 16) / 255 for i in range(1, len(value), 2)]
        return cls(*channels)


@dataclass(frozen=True)
class ResourceRef:
    path: str
    data_type: str
    size: int
    modified_ns: int
    storage: str = "file"
    resource_id: str = ""
    task_key: str = ""

    @property
    def capabilities(self):
        # Stream resources in this prototype are reopenable recipes, not one-shot generators.
        return ("replay", "seek") if self.storage in {"file", "memory"} else ("replay",)

    @classmethod
    def capture(cls, path, data_type, expected=None):
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError("资源文件不存在：" + str(source))
        stat = source.stat()
        stamp = [stat.st_size, stat.st_mtime_ns]
        if expected is not None and stamp != expected:
            raise ValueError("排队期间源文件发生变化，请重新提交：" + source.name)
        return cls(str(source), data_type, *stamp)

    def verify(self):
        if self.storage not in {"file", "memory", "stream"}:
            raise ValueError("未知媒体存储方式。")
        if self.storage != "file" and not self.resource_id:
            raise ValueError("媒体资源引用缺少会话 ID。")
        if self.storage == "file" or self.path:
            self.capture(self.path, self.data_type, [self.size, self.modified_ns])
        return self


@dataclass(frozen=True)
class FilterOperation:
    """Deferred filter. video=None is reusable; the settler binds its input video.

    A non-None video retains the source restriction of legacy/bound operations.
    No render, live callback, or hidden plugin settings are carried here.
    """
    title: str
    video: object
    name: str
    options: Tuple[Tuple[str, str], ...] = ()
    assets: Tuple[Tuple[str, object], ...] = ()


@dataclass(frozen=True)
class RenderPlan:
    video: object
    operations: Tuple[FilterOperation, ...]
    output: str
    encoding: object = None


@dataclass(frozen=True)
class VideoFilterFragment:
    """Trusted compiler output only; never accepted from saved node parameters.

    Local input labels start at 0; `input`/`output` are the canvas pads.
    The host rebases every input and private pad before one final encoding.
    """
    video: object
    inputs: Tuple[Tuple[str, ...], ...]
    filters: Tuple[str, ...]


@dataclass(frozen=True)
class ResourcePool:
    """Immutable, read-only pool snapshot; no Store, widgets or mutable cursor."""
    name: str
    kind: str
    data_json: str
    files: Tuple[ResourceRef, ...] = ()

    @property
    def data(self):
        return json.loads(self.data_json)


@dataclass(frozen=True)
class TaskSnapshot:
    """All row data, copied at submission. JSON keeps nested values immutable."""
    key: str
    label: str
    directory: str
    table: str
    source_row: object
    fields_json: str
    language: str = ""

    @property
    def fields(self):
        return json.loads(self.fields_json)


@dataclass(frozen=True)
class DirectoryRef:
    """A directory value, not a task packet. Ownership is optional metadata."""
    path: str
    task_key: str = ""

    def verify(self):
        if not Path(self.path).is_dir():
            raise FileNotFoundError("目录不存在：" + self.path)
        return self


@dataclass(frozen=True)
class FileList:
    """Ordered file snapshots produced by an explicit filesystem node."""
    directory: DirectoryRef
    files: Tuple[ResourceRef, ...]

    def verify(self, canceled=None):
        self.directory.verify()
        for resource in self.files:
            if canceled:
                canceled()
            if not isinstance(resource, ResourceRef) or resource.storage != "file":
                raise TypeError("文件列表仅接受本地文件资源。")
            resource.verify()
        return self


@dataclass(frozen=True)
class VideoSection:
    source: ResourceRef
    start: float
    end: float


@dataclass(frozen=True)
class VideoComposition:
    """A registered source recipe, not an encoded file or executable callback."""
    kind: str
    inputs: tuple
    parameters_json: str
    info_json: str
    path: str = ""
    storage: str = "composition"
    data_type: str = "video"

    @property
    def parameters(self):
        from .python_values import parse_json
        return parse_json(self.parameters_json)

    @property
    def info(self):
        from .python_values import parse_json
        return parse_json(self.info_json)

    @property
    def task_key(self):
        keys = {getattr(value, 'task_key', '') for value in self.inputs} - {''}
        if len(keys) > 1:
            raise ValueError('不能拼接属于不同已绑定任务的视频。')
        return next(iter(keys), '')

    @property
    def capabilities(self):
        return ('replay', 'seek')

    def verify(self):
        from .video_stream import VideoStream
        if (not re.fullmatch(r'[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*', self.kind)
                or not isinstance(self.inputs, tuple) or not 0 <= len(self.inputs) <= 16
                or any(not isinstance(value, (ResourceRef, VideoTimeline, VideoStream)) for value in self.inputs)
                or len(self.parameters_json.encode('utf-8')) > 256000
                or len(self.info_json.encode('utf-8')) > 65536):
            raise ValueError('多视频处理计划格式无效。')
        info = self.info
        video = info.get('video', {}) if isinstance(info, dict) else {}
        if (not isinstance(self.parameters, dict) or type(info.get('duration')) not in (int, float)
                or not 0 < info['duration'] <= 86400 or type(info.get('audio')) is not bool
                or any(type(video.get(key)) is not int or not 0 < video[key] <= 16384 for key in ('width', 'height'))):
            raise ValueError('多视频处理计划的尺寸或时长无效。')
        self.task_key
        for value in self.inputs:
            value.verify()
        return self


@dataclass(frozen=True)
class VideoTimeline:
    """Deferred cuts. No decoded frames, intermediate videos, or live handles."""
    sections: Tuple[VideoSection, ...]
    width: int
    height: int
    fps: str
    task_key: str
    has_audio: bool = True
    path: str = ""
    storage: str = "timeline"
    data_type: str = "video"

    @property
    def duration(self):
        return sum(item.end - item.start for item in self.sections)

    @property
    def capabilities(self):
        return ("replay", "seek")

    def verify(self):
        from fractions import Fraction
        rate = float(Fraction(self.fps))
        if not self.sections or not 1 <= rate <= 240 or not (0 < self.width <= 16384 and 0 < self.height <= 16384):
            raise ValueError("剪辑计划的规格或片段无效。")
        for item in self.sections:
            item.source.verify()
            if (item.source.data_type != "video" or item.source.storage != "file"
                    or not all(math.isfinite(value) for value in (item.start, item.end))
                    or not 0 <= item.start < item.end):
                raise ValueError("剪辑计划的源资源或时间区间无效。")
        return self


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SubtitleTrack:
    cues: Tuple[SubtitleCue, ...]
    task_key: str
    duration: float
    timing: str = "source_words_mapped_to_edit"
    data_type: str = "subtitles"

    def verify(self):
        if not math.isfinite(self.duration) or self.duration <= 0 or not self.cues:
            raise ValueError("没有可用字幕，未生成假字幕。")
        previous = -1.0
        for cue in self.cues:
            if (not all(math.isfinite(value) for value in (cue.start, cue.end))
                    or not 0 <= cue.start < cue.end <= self.duration + .05
                    or cue.start < previous or not cue.text.strip()):
                raise ValueError("字幕的顺序、内容或时间范围无效。")
            previous = cue.start
        return self

    def to_srt(self):
        def stamp(value):
            milliseconds = int(round(value * 1000))
            hours, rest = divmod(milliseconds, 3600000)
            minutes, rest = divmod(rest, 60000)
            seconds, milliseconds = divmod(rest, 1000)
            return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"
        self.verify()
        return "\n\n".join(f"{index}\n{stamp(cue.start)} --> {stamp(cue.end)}\n{cue.text}"
                            for index, cue in enumerate(self.cues, 1)) + "\n"


"""Validate deferred operations and bind them to the settler's video, not a GUI."""

from model.AppDataStorage import data_io, data_file, data_directory
from dataclasses import replace
import math
from pathlib import Path
import re

from .contracts import FilterOperation, ResourceRef, SubtitleTrack


def validate_subtitle(subtitles, duration=None):
    """Check SRT/track integrity; optionally check against the final video clock."""
    if not isinstance(subtitles, (ResourceRef, SubtitleTrack)):
        raise TypeError("字幕输入必须是 SRT 文件引用或字幕轨道。")
    subtitles.verify()
    if isinstance(subtitles, SubtitleTrack):
        if duration is not None and abs(subtitles.duration - duration) > .1:
            raise ValueError("字幕不是当前剪辑时间线的字幕。")
        return
    if (subtitles.data_type != "subtitles" or subtitles.storage != "file"
            or Path(subtitles.path).suffix.lower() != ".srt" or subtitles.size > 2 * 1024 * 1024):
        raise ValueError("请使用不超过 2MB 的本地 SRT 字幕。")
    with data_io.open(Path(subtitles.path), "rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("SRT 字幕超过 2MB。")
    try:
        text = raw.decode("utf16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError:
        raise ValueError("字幕编码无法识别，请保存为 UTF-8 或 UTF-16 SRT。") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    clock = r"(\d+:\d{2}:\d{2}[,.]\d{3})"
    pattern = re.compile(r"^\s*(?:\d+\s*\n)?" + clock + r"\s*-->\s*" + clock + r"[^\n]*\n(.*)$", re.S)

    def seconds(value):
        hour, minute, last = value.replace(",", ".").split(":")
        if int(minute) >= 60 or float(last) >= 60:
            raise ValueError("SRT 时间格式无效。")
        return int(hour) * 3600 + int(minute) * 60 + float(last)

    previous, word_count = -1.0, 0
    for index, block in enumerate(re.split(r"\n[ \t]*\n", text), 1):
        match = pattern.fullmatch(block)
        if not match:
            raise ValueError(f"SRT 第{index}个字幕块格式不正确，未忽略任何字幕。")
        start, end, content = match.groups()
        start, end = seconds(start), seconds(end)
        content = re.sub(r"</?(?:i|b|u|font)(?:\s[^>]*)?>", "", content, flags=re.I)
        words = re.findall(r"\S+", content)
        word_count += len(words)
        if (not words or any(len(word) > 2000 for word in words) or word_count > 6000
                or not all(math.isfinite(value) for value in (start, end))
                or not 0 <= start < end <= 86400 or start < previous):
            raise ValueError(f"SRT 第{index}个字幕块的内容或时间戳无效。")
        if duration is not None and (start >= duration or end > duration + .25):
            raise ValueError(f"SRT 第{index}个字幕块超出视频时长，请核对字幕与视频。")
        previous = start
    subtitles.verify()


def flatten_operations(values):
    """Flatten incoming operation groups without changing their wire order."""
    result = []
    for value in values or ():
        group = (value,) if isinstance(value, FilterOperation) else value
        if not isinstance(group, (tuple, list)) or not all(isinstance(op, FilterOperation) for op in group):
            raise TypeError("操作输入必须来自操作节点，不能传入任意函数或程序代码。")
        result.extend(group)
    return tuple(result)


def bind_operations(video, values, context, *, video_info=None):
    from .render import probe_resource
    from .video_stream import VideoStream
    if isinstance(video, VideoStream):
        video = video.source
    operations = flatten_operations(values)
    result = []
    for operation in operations:
        if operation.video is not None and operation.video != video:
            raise ValueError("操作来自另一个视频，请连接相同的视频输出。")
        for _, asset in operation.assets:
            if isinstance(asset, SubtitleTrack) or getattr(asset, "data_type", "") == "subtitles":
                if (getattr(asset, "task_key", "") and getattr(video, "task_key", "")
                        and asset.task_key != video.task_key):
                    raise ValueError("视频与字幕属于不同任务。")
                if video_info is None:
                    video_info = probe_resource(video, context, getattr(context, "ffmpeg", ""))
                validate_subtitle(asset, video_info["duration"])
        result.append(replace(operation, video=video))
    return tuple(result)

"""Generic composition of trusted plugin fragments, independent of business UI."""

from model.AppDataStorage import data_io, data_file, data_directory
from dataclasses import dataclass
from pathlib import Path
import re

from .contracts import VideoFilterFragment


@dataclass(frozen=True)
class VideoFilterChain:
    steps: tuple
    before: str = ""
    after: str = ""


def wrap_filters(chain, before, after):
    if isinstance(chain, VideoFilterChain):
        return VideoFilterChain(chain.steps, before, after)
    return before + "," + chain + "," + after


def check_fragment(value, video):
    if (not isinstance(value, VideoFilterFragment) or value.video != video
            or not isinstance(value.inputs, tuple) or not 0 <= len(value.inputs) <= 32
            or not isinstance(value.filters, tuple) or not 1 <= len(value.filters) <= 256):
        raise ValueError("插件必须提供当前视频的有界过滤器片段。")
    for args in value.inputs:
        if (not isinstance(args, tuple) or len(args) < 2 or args[-2] != "-i"
                or any(not isinstance(arg, str) or "\x00" in arg for arg in args)
                or not Path(args[-1]).is_file()):
            raise ValueError("过滤器片段只接受本地媒体输入。")
        options = args[:-2]
        if options not in {(), ("-loop", "1"), ("-stream_loop", "-1")}:
            raise ValueError("过滤器片段的媒体输入选项无效。")
    if any(not isinstance(line, str) or "\x00" in line for line in value.filters):
        raise ValueError("过滤器片段过大或无效。")
    text = ";".join(value.filters)
    if len(text) > 128000:
        raise ValueError("过滤器片段过大或无效。")
    if "[input]" not in text or "[output]" not in text:
        raise ValueError("过滤器片段必须提供画面输入与输出。")


def append_chain(chain, command, filters, background, next_input, final="final", prefix=""):
    """Append inputs and uniquely rename fragment pads; preserve wire order."""
    if isinstance(chain, str):
        filters.append(f"[{background}]{chain}[{final}]")
        return next_input
    if not isinstance(chain, VideoFilterChain):
        raise TypeError("无效的已编译过滤器链。")
    if chain.before:
        filters.append(f"[{background}]{chain.before}[{prefix}wf_before]")
        background = prefix + "wf_before"
    for index, step in enumerate(chain.steps):
        output = f"{prefix}wf_{index}_result"
        if isinstance(step, str):
            filters.append(f"[{background}]{step}[{output}]")
        else:
            for args in step.inputs:
                command.extend(args)
            def rename(match):
                key = match.group(1)
                if key == "input": return "[" + background + "]"
                if key == "output": return "[" + output + "]"
                source = re.fullmatch(r"(\d+):v(?::0)?", key)
                if source:
                    number = int(source.group(1))
                    if number >= len(step.inputs): raise ValueError("过滤器引用了不存在的素材输入。")
                    return f"[{next_input + number}:v:0]"
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", key):
                    raise ValueError("过滤器片段的私有端口无效。")
                return f"[{prefix}wf_{index}_{key}]"
            filters.extend(re.sub(r"\[([^\]]+)\]", rename, line) for line in step.filters)
            next_input += len(step.inputs)
        background = output
    filters.append(f"[{background}]{chain.after or 'null'}[{final}]")
    return next_input


def file_filter_options(chain, command, directory):
    """Single-file/memory source is input 0; appended media start at input 1."""
    if isinstance(chain, str):
        return ["-map", "0:v:0", "-vf", chain]
    filters = []
    append_chain(chain, command, filters, "0:v:0", 1)
    script = Path(directory) / "filters.txt"
    data_file(script).write_text_direct(";\n".join(filters), encoding="utf-8")
    return ["-filter_complex_threads", "2", "-/filter_complex", str(script), "-map", "[final]"]

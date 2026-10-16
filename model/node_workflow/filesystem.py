"""Explicit filesystem nodes. No task lookup, media discovery, or plugin imports."""
import fnmatch
import os
from pathlib import Path
import re
import unicodedata

from .contracts import DirectoryRef, FileList, ResourceRef, NodeDefinition, NodeParameter, NodePort


def _fold(value):
    return unicodedata.normalize("NFC", str(value)).casefold()


def _rules(value):
    return [_fold(item.strip()) for item in re.split(r"[;,；，\r\n]+", str(value or "")) if item.strip()]


def _matches(value, patterns):
    return any(fnmatch.fnmatchcase(_fold(value), pattern) for pattern in patterns)


def _natural(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"(\d+)", _fold(value)))


def _kind(path):
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts"}:
        return "video"
    if suffix in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}:
        return "audio"
    if suffix in {".srt", ".ass", ".vtt"}:
        return "subtitles"
    return "file"


def build_directory(_inputs, params, _context):
    path = Path(params["path"]).expanduser().resolve()
    return {"directory": DirectoryRef(str(path)).verify()}


def build_listdir(inputs, params, context):
    directory = inputs["directory"].verify()
    root = Path(directory.path).resolve()
    include = _rules(params.get("include", "*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.mts;*.m2ts"))
    exclude = _rules(params.get("exclude", ""))
    keywords = _rules(params.get("exclude_keywords", "-口播;-reels"))
    folders = _rules(params.get("exclude_dirs", "智能剪辑结果;节点工作流结果;剪辑气口结果;.smart_edit_cache;.node-*"))
    recursive = params.get("recursive", "no") == "yes"
    hidden = params.get("hidden", "no") == "no"
    limit = int(params.get("limit", 50000))
    selected, visited = [], set()

    def fail(error):
        raise error

    context.report("列出文件：" + str(root), None)
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=fail):
        context.check_canceled()
        base = Path(base)
        resolved = base.resolve()
        if resolved in visited or not resolved.is_relative_to(root):
            dirs[:] = []
            continue
        visited.add(resolved)
        dirs[:] = sorted((name for name in dirs if recursive and not _matches(name, folders)
                          and not (hidden and name.startswith("."))
                          and not (base / name).is_symlink()
                          and (base / name).resolve().is_relative_to(root)), key=_natural)
        for name in names:
            context.check_canceled()
            path = base / name
            if ((hidden and name.startswith(".")) or not _matches(name, include or ["*"])
                    or _matches(name, exclude) or any(word in _fold(path.stem) for word in keywords)
                    or path.is_symlink() or not path.resolve().is_relative_to(root)):
                continue
            if not path.is_file():
                continue
            selected.append(ResourceRef.capture(path, _kind(path)))
            if len(selected) > limit:
                raise ValueError(f"文件数超过 {limit}，请收紧枚举规则；未截断文件列表。")
        if not recursive:
            break
    sort = params.get("sort", "natural")
    if sort == "modified":
        selected.sort(key=lambda item: (-item.modified_ns, _natural(Path(item.path).relative_to(root))))
    elif sort == "name":
        selected.sort(key=lambda item: _fold(Path(item.path).relative_to(root)))
    else:
        selected.sort(key=lambda item: _natural(Path(item.path).relative_to(root)))
    context.report(f"列出文件完成：{len(selected)} 个", None)
    return {"files": FileList(directory, tuple(selected))}


def register_filesystem(registry):
    registry.register("core", NodeDefinition("core.directory", "目录路径",
        outputs=(NodePort("directory", "目录", "directory"),),
        parameters=(NodeParameter("path", "目录路径", "directory_file"),), build=build_directory,
        description="手动指定一个目录。也可直接用任务入口的任务路径连接 listdir，不需要这个节点。"))
    registry.register("core", NodeDefinition("core.listdir", "列出文件 · listdir",
        inputs=(NodePort("directory", "目录路径", "directory"),),
        outputs=(NodePort("files", "文件列表", "files"),), build=build_listdir, execution="compute",
        parameters=(NodeParameter("recursive", "遍历子目录", "choice", "no", choices=(("no", "否：仅当前目录"), ("yes", "是：递归"))),
                    NodeParameter("include", "包含文件（通配符；分号分隔）", default="*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.mts;*.m2ts"),
                    NodeParameter("exclude_keywords", "排除文件名关键词（分号分隔）", default="-口播;-reels"),
                    NodeParameter("exclude", "排除文件（通配符；可留空）", default="", required=False),
                    NodeParameter("exclude_dirs", "递归时跳过目录（通配符）", default="智能剪辑结果;节点工作流结果;剪辑气口结果;.smart_edit_cache;.node-*", required=False),
                    NodeParameter("hidden", "包含隐藏名称（以点开头）", "choice", "no", choices=(("no", "不包含"), ("yes", "包含"))),
                    NodeParameter("sort", "文件顺序", "choice", "natural", choices=(("natural", "自然序号：1、2、10"), ("name", "文件名"), ("modified", "修改时间：最新优先"))),
                    NodeParameter("limit", "最多文件数（超出报错，不截断）", "integer", 50000, 1, 100000)),
        description="仅按这里的规则读取目录，输出有顺序、带文件签名的列表；下游不会再次搜文件。空列表也如实输出。执行时枚举，不阻塞主界面。"))

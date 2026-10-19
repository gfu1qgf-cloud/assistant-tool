"""Path text manipulation is separate from filesystem reads and video work."""
import fnmatch
import ntpath
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import posixpath
import re
import unicodedata
from datetime import date

from .contracts import DirectoryRef, ResourceRef, VideoTimeline, NodeDefinition, NodeParameter, NodePort
from .text_nodes import literal_test, text_value, TEST_CHOICES

STYLE = NodeParameter("style", "路径格式", "choice", "auto", choices=(
    ("auto", "自动识别"), ("windows", "Windows"), ("posix", "POSIX · /")))


def path_value(value):
    value = text_value(value)
    if not value.strip() or "\x00" in value:
        raise ValueError("路径不能为空或包含空字符。")
    return value


def path_class(values, style="auto"):
    if style == "windows" or style == "auto" and any("\\" in value or value.startswith("//") or re.match(r"^[A-Za-z]:", value) for value in values):
        return PureWindowsPath
    if style == "posix" or style == "auto" and any(value.startswith("/") for value in values):
        return PurePosixPath
    if style != "auto":
        raise ValueError("未知的路径格式。")
    return PureWindowsPath if os.name == "nt" else PurePosixPath


def parsed(inputs, params):
    value = path_value(inputs["path"])
    return path_class([value], params.get("style", "auto"))(value)


def build_path_text(inputs, _params, _context):
    from .video_stream import VideoStream
    value = inputs["value"]
    if isinstance(value, str):
        path = value
    elif isinstance(value, (DirectoryRef, ResourceRef, VideoTimeline, VideoStream)):
        path = value.path
    else:
        raise TypeError("只接受路径文本、目录或媒体资源；数值、文件列表等不能当作路径。")
    return {"path": path_value(path)}


def build_parts(inputs, params, _context):
    path = parsed(inputs, params)
    return {"name": path.name, "stem": path.stem, "extension": path.suffix,
            "parent": str(path.parent), "drive": path.drive}


def build_parent(inputs, params, _context):
    path = parsed(inputs, params)
    for _ in range(params["levels"]):
        path = path.parent
    return {"path": str(path)}


def build_join(inputs, params, _context):
    values = [path_value(value) for value in inputs["parts"]]
    if not values:
        raise ValueError("请连接至少一段路径。")
    return {"path": str(path_class(values, params["style"])(*values))}


def build_suffix(inputs, params, _context):
    extension = inputs.get("extension")
    extension = text_value(params["extension"] if extension is None else extension)
    if extension and not extension.startswith("."):
        extension = "." + extension
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', extension) or extension.endswith((".", " ")):
        raise ValueError("扩展名无效；请填写 mp4、.mp4 等，留空表示移除扩展名。")
    return {"path": str(parsed(inputs, params).with_suffix(extension))}


def build_relative(inputs, params, _context):
    value = path_value(inputs["path"])
    base = path_value(inputs.get("base") if inputs.get("base") is not None else params["base"])
    cls = path_class([value, base], params["style"])
    normalizer = ntpath if cls is PureWindowsPath else posixpath
    try:
        relative = cls(normalizer.normpath(value)).relative_to(cls(normalizer.normpath(base)))
        if ".." in relative.parts:
            raise ValueError("相对路径越界")
    except ValueError as error:
        raise ValueError("输入路径不在基准目录内，或两者盘符不一致。不会生成越界的 ../ 路径。") from error
    return {"path": str(relative)}


def build_normalize(inputs, params, _context):
    value = path_value(inputs["path"])
    module = ntpath if path_class([value], params["style"]) is PureWindowsPath else posixpath
    return {"path": module.normpath(value)}


def build_match(inputs, params, _context):
    path = parsed(inputs, params)
    value = {"path": str(path), "name": path.name, "stem": path.stem, "parent": str(path.parent)}[params["subject"]]
    pattern = inputs.get("pattern")
    pattern = text_value(params["pattern"] if pattern is None else pattern)
    if not pattern:
        raise ValueError("请填写路径匹配文字，避免空关键词匹配所有文件。")
    if isinstance(path, PureWindowsPath):
        pattern = pattern.replace("/", "\\")
    ignore = params["case"] == "ignore"
    if params["operation"] == "glob":
        value, pattern = (unicodedata.normalize("NFC", text) for text in (value, pattern))
        if ignore:
            value, pattern = value.casefold(), pattern.casefold()
        matched = fnmatch.fnmatchcase(value, pattern)
    else:
        matched = literal_test(value, pattern, params["operation"], ignore)
    return {"boolean": matched}


def build_exists(inputs, _params, context):
    context.check_canceled()
    path = Path(path_value(inputs["path"])).expanduser()
    exists = path.exists()
    return {"exists": exists, "is_file": exists and path.is_file(), "is_directory": exists and path.is_dir()}


_OUTPUT_FIELD = re.compile(r'\{(stem|name|ext|parent|date|iteration|index(?::0[1-6]d)?)\}')


def build_output_path(inputs, params, context):
    """Resolve a bounded filename template; no Python attribute access or writes."""
    source = build_path_text({'value': inputs['source']}, {}, context)['path']
    directory = inputs.get('directory')
    directory = path_value(directory) if directory is not None else ''
    cls = path_class([source, directory], params['style'])
    path = cls(source)
    globals_value = getattr(context, 'workflow_globals', None)
    values = globals_value.values() if globals_value is not None else {}
    index = values.get('iteration', 1)
    if type(index) is not int or index < 1:
        raise ValueError('当前循环序号无效。')
    fields = {'stem': path.stem, 'name': path.name, 'ext': path.suffix,
              'parent': path.parent.name, 'date': values.get('run_date') or date.today().isoformat(),
              'iteration': str(index)}
    template = str(params['template'])
    if not 1 <= len(template) <= 180:
        raise ValueError('输出文件名模板长度须为 1～180 字符。')
    if '{' in _OUTPUT_FIELD.sub('', template) or '}' in _OUTPUT_FIELD.sub('', template):
        raise ValueError('输出文件名模板包含未知变量。')

    def replace(match):
        field = match.group(1)
        if field.startswith('index:'):
            return f'{index:0{int(field[7])}d}'
        if field == 'index':
            return str(index)
        return fields[field]

    filename = _OUTPUT_FIELD.sub(replace, template)
    if (not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]{1,180}', filename) or
            filename.endswith((' ', '.')) or filename in {'.', '..'}):
        raise ValueError('输出文件名模板包含未知变量或无效文件名；仅能生成单个文件名。')
    base = cls(directory) if directory else path.parent
    return {'path': str(base / filename)}


def register_path_nodes(registry):
    path_in = (NodePort("path", "路径文本", "text"),)
    path_out = (NodePort("path", "路径文本", "text"),)
    def add(kind, title, *, inputs=path_in, outputs=path_out, parameters=(STYLE,), build, description, execution="plan"):
        registry.register("core", NodeDefinition("core." + kind, title, inputs=inputs, outputs=outputs,
            parameters=parameters, build=build, description=description, execution=execution))
    add("path_text", "路径转文本 · 目录／媒体资源", inputs=(NodePort("value", "路径／目录／媒体", "any", type_group="resource"),),
        parameters=(), build=build_path_text,
        description="从目录或媒体资源提取路径文字，也接受已有路径文本。不复制文件；内存剪辑没有文件路径时会明确报错。")
    # Preserve the existing node kind, output name and part parameter.
    add("path_name", "文件名 · basename", outputs=(NodePort("name", "文件名", "text"),),
        parameters=(NodeParameter("part", "提取部分", "choice", "name", choices=(
            ("name", "文件名 · 含扩展名"), ("stem", "文件名 · 不含扩展名"))), STYLE),
        build=lambda i, p, _c: {"name": getattr(parsed(i, p), p["part"])},
        description="原“路径提取文件名”节点。只处理路径文字，不要求文件存在；支持 Windows、UNC 和 POSIX 路径。")
    add("path_parent", "父目录 · dirname", parameters=(NodeParameter("levels", "向上层数", "integer", 1, 1, 99), STYLE),
        build=build_parent, description="提取一层或多层父目录，不读取文件；到达根目录后保持根目录。")
    add("path_parts", "拆分路径 · 文件名／扩展名／父目录",
        outputs=(NodePort("name", "文件名", "text"), NodePort("stem", "不含扩展名", "text"),
                 NodePort("extension", "扩展名 · 含点", "text"), NodePort("parent", "父目录", "text"),
                 NodePort("drive", "盘符／UNC 共享", "text")), build=build_parts,
        description="每一部分单独输出。扩展名取最后一段，如 archive.tar.gz 输出 .gz。只处理文字，不读取文件。")
    add("path_join", "拼接路径 · join", inputs=(NodePort("parts", "路径片段（按接线顺序）", "text", multiple=True),),
        build=build_join, description="按接线顺序拼接目录和文件名，右键连线可调整顺序。遵循标准路径规则：后面的绝对路径会替换前面的目录。不创建目录或文件。")
    add("path_suffix", "修改／移除扩展名", inputs=path_in + (NodePort("extension", "扩展名（可覆盖设置）", "text", required=False),),
        parameters=(NodeParameter("extension", "新扩展名 · 留空移除", default=".mp4", required=False), STYLE),
        build=build_suffix, description="仅修改输出路径文字，不重命名或转换原文件。支持 mp4 或 .mp4；只替换最后一段扩展名。")
    add("path_relative", "相对路径 · relative", inputs=path_in + (NodePort("base", "基准目录（可覆盖设置）", "text", required=False),),
        parameters=(NodeParameter("base", "基准目录", required=False), STYLE), build=build_relative,
        description="提取相对基准目录的路径。不同盘符或范围外路径报错；只作文字运算，不验证真实链接位置。")
    add("path_normalize", "规范路径 · normpath", build=build_normalize,
        description="整理重复分隔符、. 和 ..；只作文字运算，不访问磁盘，不展开环境变量，也不改变真实文件。")
    add("path_match", "路径判断 · 包含／通配符", inputs=path_in + (NodePort("pattern", "匹配文字（可覆盖设置）", "text", required=False),),
        outputs=(NodePort("boolean", "匹配结果 · 真／假", "boolean"),),
        parameters=(NodeParameter("subject", "判断范围", "choice", "path", choices=(
            ("path", "完整路径"), ("name", "文件名 · 含扩展名"), ("stem", "文件名 · 不含扩展名"), ("parent", "父目录"))),
            NodeParameter("operation", "判断方式", "choice", "contains", choices=TEST_CHOICES + (("glob", "通配符 · * 和 ?"),)),
            NodeParameter("pattern", "匹配文字／人名", required=False),
            NodeParameter("case", "大小写", "choice", "ignore", choices=(("ignore", "忽略大小写"), ("sensitive", "区分大小写"))), STYLE),
        build=build_match, description="例如完整路径包含“某管理员”，输出真／假，直接接压缩节点的“是否压缩”。不读取文件，不执行正则或代码。")
    add("path_exists", "路径状态 · 是否存在", outputs=(NodePort("exists", "是否存在", "boolean"),
        NodePort("is_file", "是否文件", "boolean"), NodePort("is_directory", "是否目录", "boolean")),
        parameters=(), build=build_exists, execution="compute",
        description="执行时读取本机路径状态，不读取文件内容，不创建或修改任何文件。")
    add("path_directory", "路径文本转目录", outputs=(NodePort("directory", "目录", "directory"),), parameters=(),
        build=lambda i, _p, _c: {"directory": DirectoryRef(str(Path(path_value(i["path"])).expanduser().resolve())).verify()},
        execution="compute", description="把路径文字变成目录引用，可接 listdir。目录不存在则报错，不偷偷创建目录。")
    add('output_path', '输出路径模板',
        inputs=(NodePort('source', '源文件路径／媒体', 'any', type_group='source'),
                NodePort('directory', '输出目录 · 可留空', 'text', required=False)),
        outputs=path_out,
        parameters=(NodeParameter('template', '文件名模板', default='{stem}_字幕{ext}'), STYLE),
        build=build_output_path,
        description='例如 {stem}_字幕{ext}；可用 {name}、{parent}（父目录名）、{date}、'
                    '{iteration}、{index:02d}。只生成路径文字，不创建目录或文件；'
                    '未知变量、路径分隔符和非法文件名直接报错。')

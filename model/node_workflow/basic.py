"""Small typed value nodes. No eval, shell, dynamic code, or hidden task reads."""
import math
import os
from pathlib import Path
import re

from .contracts import NodeDefinition, NodeParameter, NodePort, RenderPlan, FileList
from .operations import bind_operations


def finite_number(value):
    if type(value) not in (int, float) or abs(value) > 1e100 or not math.isfinite(value):
        raise ValueError("请输入有限数值（绝对值不超过 1e100），布尔值不是数值。")
    return value


def parse_number(text):
    text = str(text).strip()
    if len(text) > 128 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        raise ValueError("不是完整的数字；不会截取编号中的部分数字，也不会将无效值改成零。")
    return finite_number(float(text) if any(ch in text.lower() for ch in ".e") else int(text))


def arithmetic(inputs, params, _context):
    a, b = finite_number(inputs["a"]), finite_number(inputs["b"])
    op = params["operation"]
    if op in {"divide", "modulo"} and b == 0:
        raise ValueError("除数不能为零。")
    functions = {"add": lambda: a + b, "subtract": lambda: a - b, "multiply": lambda: a * b,
                 "divide": lambda: a / b, "modulo": lambda: a % b, "min": lambda: min(a, b), "max": lambda: max(a, b)}
    return {"number": finite_number(functions[op]())}


def compare(inputs, params, _context):
    a, b = finite_number(inputs["a"]), finite_number(inputs["b"])
    return {"boolean": {"eq": a == b, "ne": a != b, "gt": a > b, "ge": a >= b,
                         "lt": a < b, "le": a <= b}[params["operation"]]}


def boolean_operation(inputs, params, _context):
    first = inputs.get('a', ())
    # A is now a multi-wire port. Scalar A/B callers and existing graph port
    # keys keep exactly their original two-condition behavior.
    values = list(first) if isinstance(first, tuple) else [first]
    if not values:
        raise ValueError('布尔运算至少需要连接一个条件；未把空输入当成真。')
    if inputs.get('b') is not None:
        values.append(inputs['b'])
    if any(type(value) is not bool for value in values):
        raise TypeError("布尔运算只接受真／假。")
    return {"boolean": {'and': all(values), 'or': any(values),
                        'xor': sum(values) % 2 == 1}[params['operation']]}


def file_sets(inputs, params, context):
    a, b = inputs["a"], inputs["b"]
    key = lambda resource: os.path.normcase(str(Path(resource.path).resolve()))
    if os.path.normcase(a.directory.path) != os.path.normcase(b.directory.path) or a.directory.task_key != b.directory.task_key:
        raise ValueError("文件集合运算需来自同一目录／任务，不能混入另一任务的素材。")
    a.verify(context.check_canceled)
    b.verify(context.check_canceled)
    first, second = {}, {}
    for group, lookup in ((a.files, first), (b.files, second)):
        for item in group:
            context.check_canceled()
            identity = key(item)
            if identity in first and first[identity] != item:
                raise ValueError("同一文件的两个快照不一致，请重新运行目录节点。")
            lookup.setdefault(identity, item)
    operation = params["operation"]
    if operation == "union":
        result = tuple(first.values()) + tuple(item for identity, item in second.items() if identity not in first)
    elif operation == "intersection":
        result = tuple(item for identity, item in first.items() if identity in second)
    elif operation == "difference":
        result = tuple(item for identity, item in first.items() if identity not in second)
    else:
        result = tuple(item for identity, item in first.items() if identity not in second) + tuple(item for identity, item in second.items() if identity not in first)
    if len(result) > 100000:
        raise ValueError("文件集合超过 100000 项，未截断数据。")
    return {"files": FileList(a.directory, result)}


def list_video(inputs, params, context):
    files = inputs["files"]
    index = finite_number(inputs["index"] if inputs.get("index") is not None else params["index"])
    if index != int(index) or not 1 <= index <= len(files.files):
        raise ValueError(f"视频序号需为 1–{len(files.files)} 的整数（从 1 开始）。")
    context.check_canceled()
    value = files.files[int(index) - 1].verify()
    if value.data_type != "video":
        raise ValueError("该条目不是视频，请先调整 listdir 的过滤规则。")
    return {"video": value}


def number_text(inputs, _params, _context):
    value = finite_number(inputs["number"])
    return {"text": str(int(value)) if type(value) is float and value.is_integer() else str(value)}


def number_format(inputs, params, _context):
    value = finite_number(inputs['number'])
    mode, digits = params['format'], params['digits']
    if mode == 'zero_pad':
        if value != int(value):
            raise ValueError('补零格式需要整数，不会截掉小数部分。')
        result = f'{int(value):0{digits}d}'
    elif mode == 'fixed':
        result = f'{value:.{digits}f}'
    elif mode == 'percent':
        result = f'{value * 100:.{digits}f}%'
    elif mode in {'mmss', 'hhmmss'}:
        if value < 0:
            raise ValueError('时长格式需要非负秒数。')
        total = round(value)
        minutes, seconds = divmod(total, 60)
        if mode == 'mmss':
            result = f'{minutes}:{seconds:02d}'
        else:
            hours, minutes = divmod(minutes, 60)
            result = f'{hours}:{minutes:02d}:{seconds:02d}'
    else:
        raise ValueError('未知数值格式。')
    return {'text': result}


def settle(inputs, _params, context):
    from .video_stream import compose
    video = compose(inputs['video'], inputs.get('operations'), context)
    return {'plan': RenderPlan(video.source, video.operations, '', video.encoding), 'video': video}


def export(inputs, params, context):
    plan = inputs["plan"]
    if params["destination"] == "task":
        if inputs.get('output') is not None:
            raise ValueError('已连接输出路径时，请把导出位置改成“指定路径”。')
        from .graph import _task_render
        if getattr(context, "task", None) is None:
            raise ValueError("导出到任务目录需要先载入任务；独立文件流程请选择“指定路径”。")
        from dataclasses import replace
        result = _task_render({"video": plan.video, "operations": plan.operations}, params, context)
        result['result'] = replace(result['result'], encoding=plan.encoding)
        return result
    output = str(inputs.get('output') if inputs.get('output') is not None else params.get('output') or '').strip()
    if not output:
        raise ValueError("请填写导出文件路径。")
    path = Path(output).expanduser().resolve()
    if path.suffix.lower() != ".mp4":
        raise ValueError("导出成品目前使用 MP4。")
    if path.exists() or plan.video.path and str(path).casefold() == plan.video.path.casefold():
        raise ValueError("成品路径已存在或与源文件相同，不会覆盖。")
    task = getattr(context, "task", None)
    if task is not None and not path.is_relative_to(Path(task.directory).resolve()):
        raise ValueError("任务工作流成品必须输出到该任务目录内。")
    if task is not None and getattr(plan.video, "task_key", "") not in {"", task.key}:
        raise ValueError("视频来自另一项任务，不能写入当前任务目录。")
    return {"result": RenderPlan(plan.video, plan.operations, str(path), plan.encoding)}


def register_basic(registry):
    from .selection import select_boolean
    categories = {
        **{kind: "数据与转换" for kind in ("number", "boolean", "text_number", "number_text", "number_format")},
        **{kind: "逻辑与判断" for kind in ("compare", "boolean_math", "not")},
        **{kind: "字符串" for kind in ("text_join", "text_replace")},
        "arithmetic": "数学", "switch": "流程控制/条件分支",
    }
    def add(kind, title, inputs=(), outputs=(), parameters=(), build=None, description="", terminal_result=None, select_input=None):
        registry.register("core", NodeDefinition("core." + kind, title, inputs=inputs, outputs=outputs,
            parameters=parameters, build=build, description=description, terminal_result=terminal_result,
            category="Python 原生/" + categories[kind] if kind in categories else "",
            select_input=select_input))
    number = (NodePort("number", "数值", "number"),)
    boolean = (NodePort("boolean", "真／假", "boolean"),)
    pair = (NodePort("a", "A", "number"), NodePort("b", "B", "number"))
    add("number", "数值", outputs=number,
        parameters=(NodeParameter("value", "数值", "number", 0, -1e12, 1e12),),
        build=lambda _i, p, _c: {"number": finite_number(p["value"])})
    add("boolean", "布尔值 · 真／假", outputs=boolean,
        parameters=(NodeParameter("value", "值", "choice", "true", choices=(("true", "真"), ("false", "假"))),),
        build=lambda _i, p, _c: {"boolean": p["value"] == "true"})
    add("arithmetic", "数学运算", inputs=pair, outputs=number,
        parameters=(NodeParameter("operation", "运算", "choice", "add", choices=(
            ("add", "加 A+B"), ("subtract", "减 A−B"), ("multiply", "乘 A×B"), ("divide", "除 A÷B"),
            ("modulo", "取余"), ("min", "较小值"), ("max", "较大值"))),), build=arithmetic)
    add("compare", "数值比较", inputs=pair, outputs=boolean,
        parameters=(NodeParameter("operation", "比较", "choice", "ge", choices=(
            ("eq", "等于"), ("ne", "不等于"), ("gt", "大于"), ("ge", "大于等于"), ("lt", "小于"), ("le", "小于等于"))),), build=compare)
    add("boolean_math", "布尔运算 · 与／或／异或",
        inputs=(NodePort("a", "条件（可接多个）", "boolean", multiple=True),
                NodePort("b", "附加条件（可选）", "boolean", required=False)), outputs=boolean,
        parameters=(NodeParameter("operation", "运算", "choice", "and", choices=(
            ("and", "与 AND · 全部为真"), ("or", "或 OR · 任意为真"),
            ("xor", "异或 XOR · 奇数个为真"))),), build=boolean_operation,
        description='将多路比较结果直接接到“条件（可接多个）”竖长端口，无需先组列表或串联多个AND。AND全部为真、OR任一为真、XOR真值数量为奇数；只有一路时原样输出。至少连接一个主条件，只接受布尔，不把文字或数值自动当作真。附加条件与主端所有条件一同运算，原A/B接线继续有效。所有已连接的上游都会求值，不做短路；跳过耗时分支请用条件选择。')
    add("not", "布尔取反 · NOT", inputs=(NodePort("boolean", "真／假", "boolean"),), outputs=boolean,
        build=lambda i, _p, _c: {"boolean": not i["boolean"]})
    add("text_number", "文本转数值", inputs=(NodePort("text", "数字文本", "text"),), outputs=number,
        build=lambda i, _p, _c: {"number": parse_number(i["text"])},
        description="严格转换完整数值；不会丢掉夹杂在任务编号中的字母，不会把错误值当成零。")
    add("number_text", "数值转文本", inputs=number, outputs=(NodePort("text", "文本", "text"),),
        build=number_text)
    add('number_format', '数值格式化', inputs=number, outputs=(NodePort('text', '格式化文字', 'text'),),
        parameters=(NodeParameter('format', '显示格式', 'choice', 'zero_pad', choices=(
            ('zero_pad', '整数补零 · 01'), ('fixed', '固定小数位 · 1.2'),
            ('percent', '比例转百分比 · 87.3%'), ('mmss', '时长 · 分:秒'),
            ('hhmmss', '时长 · 时:分:秒'))),
            NodeParameter('digits', '补零宽度／小数位数', 'integer', 2, 0, 8)),
        build=number_format,
        description='补零要求整数；百分比输入是 0～1 的比例，例如 0.873 → 87.3%（小数位设为 1）。'
                    '时长按秒四舍五入；格式化仅改变显示文本，不改变数值。')
    add("text_join", "文本拼接", inputs=(NodePort("texts", "文本（按接线顺序）", "text", multiple=True),),
        outputs=(NodePort("text", "文本", "text"),), parameters=(NodeParameter("separator", "间隔符", default="\n", required=False),),
        build=lambda i, p, _c: {"text": str(p["separator"]).join(i["texts"])})
    add("text_replace", "文本替换", inputs=(NodePort("text", "文本", "text"),),
        outputs=(NodePort("text", "文本", "text"),), parameters=(
            NodeParameter("find", "查找文字"), NodeParameter("replacement", "替换为", required=False)),
        build=lambda i, p, _c: {"text": i["text"].replace(p["find"], p["replacement"]) if p["find"] else i["text"]},
        description="只替换普通文字，不执行正则表达式或程序代码。")
    add("file_count", "文件列表计数", inputs=(NodePort("files", "文件列表", "files"),),
        outputs=(NodePort("count", "数量", "number"), NodePort("empty", "是否为空", "boolean")),
        build=lambda i, _p, _c: {"count": len(i["files"].files), "empty": not i["files"].files})
    add("file_sets", "文件集合 · 并／交／差集", inputs=(NodePort("a", "列表 A", "files"), NodePort("b", "列表 B", "files")),
        outputs=(NodePort("files", "文件列表", "files"),), parameters=(NodeParameter("operation", "集合运算", "choice", "union",
            choices=(("union", "并集 A∪B · 去重"), ("intersection", "交集 A∩B"), ("difference", "差集 A−B"), ("xor", "对称差集"))),),
        build=file_sets, description="两个列表来自同一目录／任务；按路径去重并保留顺序。不移动、删除或复制原文件。")
    add("list_video", "文件列表取视频", inputs=(NodePort("files", "文件列表", "files"),
        NodePort("index", "序号（可覆盖参数）", "number", required=False)), outputs=(NodePort("video", "视频", "video"),),
        parameters=(NodeParameter("index", "序号 · 从 1 开始", "integer", 1, 1, 100000),), build=list_video,
        description="从列表中选取一个视频引用，不复制素材。可用数值节点提供动态序号。")
    add("switch", "条件选择", inputs=(NodePort("condition", "1 · 条件", "boolean"),
        NodePort("yes", "2 · 为真时的值", "any", type_group="value"),
        NodePort("no", "3 · 为假时的值", "any", type_group="value")),
        outputs=(NodePort("value", "所选值", "any", type_group="value"),),
        build=lambda i, _p, _c: {"value": next(iter(i.values()))}, select_input=select_boolean,
        description="第 1 路接布尔条件；第 2、3 路及输出按接线自动推断类型，必须一致。执行时只计算选中的结果分支；两侧接线和参数仍须通过静态预检。不自动转换数字与文本。")
    # Retain registered legacy kinds for saved workflows, but hide them in the menu.
    for kind, title in (("text", "文本"), ("number", "数值"), ("boolean", "布尔值"), ("files", "文件列表")):
        add("switch_" + kind, "条件选择 · " + title,
            inputs=(NodePort("condition", "条件", "boolean"), NodePort("yes", "为真时的值", kind), NodePort("no", "为假时的值", kind)),
            outputs=(NodePort("value", title, kind),), build=lambda i, _p, _c: {"value": i["yes"] if i["condition"] else i["no"]},
            description="选择一个输入值；两侧上游都正常执行。这是数据选择，不是跳过耗时任务的惰性分支。")
    add("settle", "视频结算 · 编译计划", inputs=(NodePort("video", "视频", "video"),
        NodePort("operations", "处理操作（可接多个）", "operations", required=False, multiple=True)),
        outputs=(NodePort("plan", "视频计划", "render_plan"), NodePort('video', '视频流', 'video')), build=settle,
        description="视频只接这里；可直接连接多个操作节点，按接线顺序应用（右键连线可调整）。检查字幕与视频的时长／任务归属，不编码、不创建中间视频。接预览查看画面，接导出生成成品。")
    add("export", "导出成品", inputs=(NodePort("plan", "视频计划", "render_plan"),
        NodePort('output', '输出路径 · 可覆盖参数', 'text', required=False)),
        terminal_result=NodePort("result", "内部导出计划", "render_plan"), parameters=(
            NodeParameter("destination", "导出位置", "choice", "task", choices=(("task", "当前任务目录 · 自动命名"), ("file", "指定路径"))),
            NodeParameter("output", "指定路径（仅指定路径模式）", "output_file", required=False),
            NodeParameter("suffix", "任务模式成品名称后缀", default="字幕成品")), build=export,
        description="工作流终点，无可连线输出端；最终编码一次。任务模式自动命名并避免覆盖，独立流程可连接“输出路径模板”或在参数中指定路径。成品发布成功后，由执行器向Python调用方返回文件路径、字幕路径、报告和时长，不把渲染计划当成已生成文件。")

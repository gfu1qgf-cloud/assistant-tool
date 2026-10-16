"""Literal text operations; never evaluate expressions or user-written code."""
import unicodedata

from .contracts import NodeDefinition, NodeParameter, NodePort

TEST_CHOICES = (("contains", "包含"), ("not_contains", "不包含"),
                ("starts_with", "以此开头"), ("ends_with", "以此结尾"),
                ("equals", "完全相等"), ("not_equals", "不相等"))


def text_value(value):
    if not isinstance(value, str):
        raise TypeError("需要文本，目录或媒体资源请先接“路径转文本”节点。")
    if len(value) > 1_000_000:
        raise ValueError("文本超过 100 万字符，未截断数据。")
    return value


def literal_test(text, needle, operation, ignore_case=False):
    text, needle = text_value(text), text_value(needle)
    if not needle and operation not in {"equals", "not_equals"}:
        raise ValueError("请填写非空的查找文字，或连接查找文字输入端，避免空关键词匹配所有文件。")
    text, needle = (unicodedata.normalize("NFC", value) for value in (text, needle))
    if ignore_case:
        text, needle = text.casefold(), needle.casefold()
    if operation == "contains":
        return needle in text
    if operation == "not_contains":
        return needle not in text
    if operation == "starts_with":
        return text.startswith(needle)
    if operation == "ends_with":
        return text.endswith(needle)
    if operation == "equals":
        return text == needle
    if operation == "not_equals":
        return text != needle
    raise ValueError("未知的文本判断方式。")


def build_test(inputs, params, _context):
    needle = inputs.get("needle")
    if needle is None:
        needle = params["needle"]
    return {"boolean": literal_test(inputs["text"], needle, params["operation"],
                                     params["case"] == "ignore")}


def build_transform(inputs, params, _context):
    value = text_value(inputs["text"])
    transforms = {"trim": str.strip, "lower": str.lower, "upper": str.upper,
                  "casefold": str.casefold, "normalize_spaces": lambda text: " ".join(text.split())}
    return {"text": transforms[params["operation"]](value)}


def build_slice(inputs, params, _context):
    from .basic import finite_number
    value = text_value(inputs["text"])
    options = {}
    for key in ("start", "length"):
        number = finite_number(inputs.get(key) if inputs.get(key) is not None else params[key])
        if number != int(number) or not -1_000_000 <= number <= 1_000_000:
            raise ValueError("起点和长度需为范围内的整数。")
        options[key] = int(number)
    start, length = options["start"], options["length"]
    if length < -1:
        raise ValueError("长度不能小于 -1；-1 表示直到末尾。")
    start = max(0, len(value) + start) if start < 0 else start
    return {"text": value[start:] if length == -1 else value[start:start + length]}


def register_text_nodes(registry):
    text = (NodePort("text", "文本", "text"),)
    registry.register("core", NodeDefinition("core.text_test", "文本判断 · 包含／开头／结尾",
        inputs=text + (NodePort("needle", "查找文字（可覆盖设置）", "text", required=False),),
        outputs=(NodePort("boolean", "判断结果 · 真／假", "boolean"),),
        parameters=(NodeParameter("operation", "判断方式", "choice", "contains", choices=TEST_CHOICES),
            NodeParameter("needle", "查找文字", required=False),
            NodeParameter("case", "大小写", "choice", "sensitive",
                          choices=(("sensitive", "区分大小写"), ("ignore", "忽略大小写")))),
        build=build_test, category="Python 原生/逻辑与判断", description="输入文本，输出真／假，可接条件选择或压缩节点的是否压缩端口。普通文字匹配，不执行正则或代码；空包含关键词会报错。"))
    registry.register("core", NodeDefinition("core.text_transform", "文本整理 · 空白／大小写",
        inputs=text, outputs=text,
        parameters=(NodeParameter("operation", "处理方式", "choice", "trim", choices=(
            ("trim", "去除首尾空白"), ("lower", "小写"), ("upper", "大写"),
            ("casefold", "无大小写形式"), ("normalize_spaces", "连续空白合并为空格"))),),
        build=build_transform, category="Python 原生/字符串", description="显式整理文本；默认只去掉首尾空白，不更改正文内部的换行。"))
    registry.register("core", NodeDefinition("core.text_slice", "文本截取",
        inputs=text + (NodePort("start", "起点（可覆盖设置）", "number", required=False),
                       NodePort("length", "长度（可覆盖设置）", "number", required=False)), outputs=text,
        parameters=(NodeParameter("start", "起点 · 从 0 开始，负数从末尾计算", "integer", 0, -1_000_000, 1_000_000),
                    NodeParameter("length", "长度 · -1 表示直到末尾", "integer", -1, -1, 1_000_000)),
        build=build_slice, category="Python 原生/字符串", description="按字符截取，支持动态起点和长度；超出文本末尾自然结束，不执行程序表达式。"))
    registry.register("core", NodeDefinition("core.text_length", "文本长度",
        inputs=text, outputs=(NodePort("number", "字符数", "number"),),
        build=lambda i, _p, _c: {"number": len(text_value(i["text"]))}, category="Python 原生/字符串",
        description="返回字符数量，可接数值比较或文本截取，不按字节计算。"))

"""Useful Python builtins/stdlib as pure, typed nodes (no eval or code strings)."""
from datetime import date, timedelta
import json
import math
import os
import statistics
from urllib.parse import quote, unquote, urlsplit

from .basic import finite_number
from .contracts import NodeParameter, NodePort, FileList
from .function_library import node_function
from .python_values import PythonValue, MAX_DATA_ITEMS, parse_json, unpack, validate_data


def integer(value):
    value = finite_number(value)
    if value != int(value):
        raise ValueError("此参数必须是整数。")
    return int(value)


def sequence(value):
    value = unpack(value)
    if type(value) is not list:
        raise TypeError("需要 Python 列表；请先使用 list() 转换或输入 JSON 列表。")
    return value


def mapping(value):
    value = unpack(value)
    if type(value) is not dict:
        raise TypeError("需要文本键字典；请先使用 dict() 转换或输入 JSON 字典。")
    return value


def checked_list(values):
    result = []
    for value in values:
        if len(result) >= MAX_DATA_ITEMS:
            raise ValueError("Python 列表超过 100000 项，未截断。")
        result.append(value)
    validate_data(result)
    return result


def list_get(items, index, position):
    items = sequence(items)
    at = integer(index if index is not None else position)
    if not -len(items) <= at < len(items):
        raise IndexError(f"列表索引 {at} 越界，共 {len(items)} 项；Python 索引从 0 开始。")
    return PythonValue.capture(items[at])


def list_slice(items, start, stop, step):
    items = sequence(items)
    if not step:
        raise ValueError("切片步长不能为 0。")
    end = None if stop == "" else int(stop)
    return items[slice(integer(start), end, integer(step))]


def list_sort(items, mode):
    items = sequence(items)
    if mode == "reverse":
        return list(reversed(items))
    if (not all(type(v) is str for v in items)
            and not all(type(v) in (int, float) for v in items)):
        raise TypeError("排序需要全部是文本或全部是数值；不会偷偷转换混合数据。")
    return sorted(items, reverse=mode == "descending")


def unique(items):
    items, seen, result = sequence(items), set(), []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def matches(items, value, count=False):
    target = unpack(value)
    values = (type(v) is type(target) and v == target for v in sequence(items))
    return sum(values) if count else any(values)


def list_filter(items, target, operation, field, case):
    items, target, result = sequence(items), unpack(target), []
    for item in items:
        value = item
        if field:
            if type(item) is not dict or field not in item:
                continue
            value = item[field]
        a, b = value, target
        if operation in {"contains", "starts", "ends"}:
            if type(a) is not str or type(b) is not str:
                raise TypeError("文字筛选需要文本项和文本条件。")
            if case == "ignore":
                a, b = a.casefold(), b.casefold()
            selected = b in a if operation == "contains" else a.startswith(b) if operation == "starts" else a.endswith(b)
        elif operation in {"gt", "ge", "lt", "le"}:
            finite_number(a); finite_number(b)
            selected = {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[operation]
        else:
            selected = type(a) is type(b) and a == b
            if operation == "ne":
                selected = not selected
        if selected:
            result.append(item)
    return result


def make_range(stop, start, end, step):
    value = range(integer(start), integer(stop if stop is not None else end), integer(step))
    if len(value) > MAX_DATA_ITEMS:
        raise ValueError("range 超过 100000 项；请减少范围，未截断。")
    return checked_list(value)


def dict_get(items, key, name, fallback):
    items, key = mapping(items), name if key is None else key
    found = key in items
    return {"value": PythonValue.capture(items[key] if found else parse_json(fallback)), "found": found}


def dict_set(items, value, key, name):
    result = dict(mapping(items))
    result[name if key is None else key] = unpack(value)
    return validate_data(result)


def dict_merge(dictionaries, conflicts):
    result = {}
    for item in dictionaries:
        for key, value in mapping(item).items():
            if key in result and conflicts == "error":
                raise ValueError("字典重复键：" + key[:100])
            if key not in result or conflicts == "last":
                result[key] = value
    return validate_data(result)


def dict_items(items, mode):
    items = mapping(items)
    return list(items) if mode == "keys" else list(items.values()) if mode == "values" else [[k, v] for k, v in items.items()]


def split(text, separator, mode, count):
    if mode == "lines":
        return checked_list(text.splitlines())
    return checked_list((text.rsplit if mode == "right" else text.split)(separator or None, count))


def partition(text, separator, direction):
    if not separator:
        raise ValueError("partition 分隔符不能为空。")
    a, b, c = (text.rpartition if direction == "right" else text.partition)(separator)
    return {"head": a, "separator": b, "tail": c}


def join(items, separator):
    items = sequence(items)
    if not all(type(v) is str for v in items):
        raise TypeError("join() 只拼接文本列表；不会把字典或数值偷偷变成文本。")
    if sum(len(v) for v in items) + len(separator) * max(0, len(items) - 1) > 1024 * 1024:
        raise ValueError("拼接文本超过 1 MB，未截断。")
    return separator.join(items)


def convert(value, kind):
    value = unpack(value)
    if kind == "text":
        return json.dumps(value, ensure_ascii=False, allow_nan=False) if type(value) in (list, dict) else str(value)
    if kind == "boolean":
        return bool(value)
    if kind == "integer":
        if type(value) is str and len(value) > 128:
            raise ValueError("整数文本过长。")
        return finite_number(int(value))
    if kind == "number":
        if type(value) is str and len(value) > 128:
            raise ValueError("数值文本过长。")
        return finite_number(float(value))
    if kind == "list":
        if type(value) not in (list, dict, str):
            raise TypeError("list() 需要列表、字典或文本；数值不能直接转换。")
        return checked_list(value)
    return mapping(value)


def math_unary(value, operation, digits):
    value = finite_number(value)
    functions = {"abs": abs, "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
        "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "sin": math.sin,
        "cos": math.cos, "tan": math.tan, "radians": math.radians, "degrees": math.degrees,
        "round": lambda v: round(v, digits)}
    return finite_number(functions[operation](value))


def aggregate(items, operation):
    items = sequence(items)
    for value in items:
        finite_number(value)
    if not items and operation != "sum":
        raise ValueError("空列表不能计算最值、平均数或中位数。")
    return finite_number({"sum": sum, "min": min, "max": max,
        "mean": statistics.fmean, "median": statistics.median}[operation](items))


def date_shift(text, days, offset):
    amount = integer(days if days is not None else offset)
    return (date.fromisoformat(text) + timedelta(days=amount)).isoformat()


def url_parts(text):
    parts = urlsplit(text)
    return {"scheme": parts.scheme, "host": parts.hostname or "", "path": parts.path,
            "query": parts.query, "fragment": parts.fragment}


def paths_to_files(files, items):
    """Reorder/subset only; never invent resource refs from untrusted paths."""
    items = sequence(items)
    lookup = {os.path.normcase(ref.path): ref for ref in files.files}
    selected, seen = [], set()
    for path in items:
        if type(path) is not str:
            raise TypeError("文件路径列表必须全部是文本。")
        key = os.path.normcase(path)
        if key not in lookup:
            raise ValueError("只能选择原文件列表中的路径，不能注入其他任务的文件：" + path[:200])
        if key in seen:
            raise ValueError("选择路径重复，不能重复加入同一视频：" + path[:200])
        seen.add(key)
        selected.append(lookup[key])
    return FileList(files.directory, tuple(selected))


def register_python_nodes(registry):
    def p(key, title, dtype, optional=False, multiple=False):
        return NodePort(key, title, dtype, required=not optional, multiple=multiple,
                        type_group=key if dtype == "any" else "")
    def choice(key, title, default, choices):
        return NodeParameter(key, title, "choice", default, choices=tuple(choices))
    def number(key, title, default=0):
        return NodeParameter(key, title, "integer", default, -100000, 100000)
    def text(key, title, default="", required=False):
        return NodeParameter(key, title, default=default, required=required)
    def add(name, title, function, inputs=(), outputs=(), parameters=(), section="数据与转换", description="", keywords=(), scope="any"):
        function = node_function(kind="core.py_" + name, title=title, inputs=tuple(inputs), outputs=tuple(outputs),
            parameters=tuple(parameters), category="Python 原生/" + section,
            keywords=("python", "stdlib", name) + tuple(keywords), description=description, execution_scope=scope)(function)
        registry.register_function("core", function)
    data = (p("value", "Python 数据", "data"),)
    lists = (p("items", "列表", "list"),)
    dictionaries = (p("items", "字典", "mapping"),)
    txt = (p("text", "文本", "text"),)
    num = (p("number", "数值", "number"),)
    boolean = (p("boolean", "真／假", "boolean"),)
    any_value = (p("value", "值（自动识别输入类型）", "any"),)

    add("json", "Python 数据 · JSON 输入", lambda content: PythonValue.capture(parse_json(content)), outputs=data,
        parameters=(NodeParameter("content", "JSON 内容（不是 Python 代码）", "multiline", "null"),),
        description="输入任意 JSON 数据；列表、字典、文本、数值、布尔或 null。不执行代码。")
    add("json_parse", "json.loads · 解析 JSON", lambda text: PythonValue.capture(parse_json(text)), txt, data,
        description="解析严格 JSON；重复键、NaN、过大或过深的数据会报错。")
    add("json_dump", "json.dumps · 转 JSON 文本", lambda value, pretty: json.dumps(unpack(value), ensure_ascii=False, allow_nan=False,
        indent=2 if pretty == "yes" else None), any_value, txt,
        (choice("pretty", "易读缩进", "yes", (("yes", "开启"), ("no", "紧凑"))),))
    add("data", "Python 数据 · 包装值", lambda value: PythonValue.capture(unpack(value)), any_value, data,
        description="把普通值包装成通用 Python 数据；取列表项／字典值后用 str/int/float/bool/list/dict 显式转换。")
    add("output", "数据输出 · 查看结果", lambda value: value, data, data, scope="detail",
        description="Detail 数据流程的终点；执行后显示数据结果，不渲染视频、不写文件。普通值先用 Python 数据包装节点连接。")
    for kind, name, result in (("text", "str", txt), ("integer", "int", num), ("number", "float", num),
                              ("boolean", "bool", boolean), ("list", "list", lists), ("dict", "dict", dictionaries)):
        def conversion(value, kind=kind):
            return convert(value, kind)
        add(name, name + "() · 类型转换", conversion, any_value, result,
            description="显式 Python 类型转换，转换失败报错。bool() 使用 Python 规则：非空字符串（包括 'false'）为真。" if name == "bool" else "显式转换；不会自动转换其他节点的端口类型。")
    add("type", "type() · 值的类型", lambda value: type(unpack(value)).__name__, any_value, txt)
    add("len", "len() · 长度", lambda value: len(unpack(value)), any_value, num,
        description="文本的字符数、列表项数或字典键数；数值没有长度。")

    add("list_create", "列表 · 收集输入", lambda values: checked_list(unpack(v) for v in values),
        (p("values", "值（按连线顺序，同一类型）", "any", multiple=True),), lists, section="列表",
        description="按连线顺序收集同类型输入；混合类型可先包装成 Python 数据。")
    add("list_get", "列表 · 取项 [index]", list_get, lists + (p("index", "索引（覆盖参数）", "number", True),), data,
        (number("position", "索引 · 从 0 开始，可为负数"),), "列表")
    add("list_slice", "列表 · 切片 [start:stop:step]", list_slice, lists, lists,
        (number("start", "开始索引", 0), text("stop", "结束索引（不含；空表示到末尾）"), number("step", "步长", 1)), "列表")
    add("list_concat", "列表 · 拼接", lambda groups: checked_list(v for group in groups for v in sequence(group)),
        (p("groups", "列表（按连线顺序）", "list", multiple=True),), lists, section="列表")
    add("list_sort", "列表 · 排序／倒序", list_sort, lists, lists,
        (choice("mode", "方式", "ascending", (("ascending", "升序 sorted"), ("descending", "降序 sorted"), ("reverse", "反转 reversed · 不排序"))),), "列表")
    add("list_unique", "列表 · 去重保留顺序", unique, lists, lists, section="列表",
        description="按实际数据去重，区分数值／文本／布尔；不移动或删除源文件。")
    add("list_contains", "列表 · 是否含值", lambda items, value: matches(items, value),
        lists + data, boolean, section="列表")
    add("list_count", "列表 · 值出现次数", lambda items, value: matches(items, value, True),
        lists + data, num, section="列表")
    add("list_filter", "列表 · 筛选", list_filter, lists + (p("target", "条件值（Python 数据）", "data"),), lists,
        (choice("operation", "条件", "contains", (("contains", "包含文字"), ("starts", "开头为"), ("ends", "结尾为"),
            ("eq", "等于（类型也一致）"), ("ne", "不等于"), ("gt", "大于"), ("ge", "大于等于"), ("lt", "小于"), ("le", "小于等于"))),
         text("field", "字典字段（空表示整个列表项）"), choice("case", "文字大小写", "ignore", (("ignore", "忽略"), ("sensitive", "区分")))), "列表")
    add("enumerate", "enumerate() · 加序号", lambda items, start: checked_list([i, v] for i, v in enumerate(sequence(items), integer(start))),
        lists, lists, (number("start", "起始编号", 0),), "列表")
    add("zip", "zip() · 对齐多列表", lambda groups: checked_list(list(row) for row in zip(*(sequence(v) for v in groups))),
        (p("groups", "列表（按连线顺序）", "list", multiple=True),), lists, section="列表",
        description="按索引对齐，像 Python zip 一样以最短列表为准。")
    add("range", "range() · 数值序列", make_range, (p("stop", "结束值（覆盖参数）", "number", True),), lists,
        (number("start", "起始值", 0), number("end", "结束值（不含）", 10), number("step", "步长", 1)), "列表")
    add("files_paths", "文件列表 → Python 路径列表", lambda files: checked_list(ref.path for ref in files.files),
        (p("files", "源文件列表", "files"),), lists, section="列表",
        description="把 listdir 的路径提取为普通列表，可用 Python 列表筛选、排序和切片。只读，不更改资源文件。")
    add("paths_files", "Python 路径列表 → 文件列表", paths_to_files,
        (p("files", "原文件列表（保留文件签名）", "files"), p("items", "筛选／重排后的路径列表", "list")),
        (p("files", "文件列表", "files"),), section="列表",
        description="将 Python 处理结果接回智能剪辑等插件；只能从原列表选取，不接受其他文件或重复项。保留任务归属与原文件签名。")

    add("dict_input", "字典 · JSON 输入", lambda content: mapping(parse_json(content)), outputs=dictionaries,
        parameters=(NodeParameter("content", "JSON 字典", "multiline", "{}"),), section="字典")
    add("dict_get", "字典 · 取值 get()", dict_get, dictionaries + (p("key", "键（覆盖参数）", "text", True),),
        data + (p("found", "键是否存在", "boolean"),),
        (text("name", "键名"), NodeParameter("fallback", "键不存在时的 JSON 默认值", "multiline", "null")), "字典")
    add("dict_set", "字典 · 设置键值", dict_set, dictionaries + data + (p("key", "键（覆盖参数）", "text", True),), dictionaries,
        (text("name", "键名"),), "字典", "返回新字典，不修改上游数据或其他分支。")
    add("dict_remove", "字典 · 移除键", lambda items, key: {k: v for k, v in mapping(items).items() if k != key}, dictionaries, dictionaries,
        (text("key", "要移除的键"),), "字典", "不存在的键会忽略；返回新字典，不改上游。")
    add("dict_merge", "字典 · 合并", dict_merge, (p("dictionaries", "字典（按连线顺序）", "mapping", multiple=True),), dictionaries,
        (choice("conflicts", "重复键", "last", (("last", "后面的值覆盖前面的值"), ("first", "保留第一个值"), ("error", "报错，不覆盖"))),), "字典")
    add("dict_items", "字典 · 键／值／键值对", dict_items, dictionaries, lists,
        (choice("mode", "输出", "keys", (("keys", "keys · 键列表"), ("values", "values · 值列表"), ("items", "items · 键值对列表"))),), "字典")

    add("split", "str.split · 拆分文字", split, txt, lists,
        (text("separator", "分隔符（空表示按空白拆分）"), choice("mode", "方式", "left", (("left", "从左拆分 split"), ("right", "从右拆分 rsplit"), ("lines", "按行 splitlines"))),
         NodeParameter("count", "最多拆分次数 · -1 不限", "integer", -1, -1, 100000)), "字符串")
    add("join", "str.join · 拼接列表文字", join, lists, txt, (text("separator", "分隔符", "\n"),), "字符串")
    add("partition", "str.partition · 三段拆分", partition, txt,
        (p("head", "分隔符之前", "text"), p("separator", "实际分隔符", "text"), p("tail", "分隔符之后", "text")),
        (text("separator", "分隔符", "/", True), choice("direction", "方向", "left", (("left", "第一个 partition"), ("right", "最后一个 rpartition")))), "字符串")
    add("find", "str.find · 查找位置", lambda text, keyword, direction: (text.rfind if direction == "right" else text.find)(keyword), txt, num,
        (text("keyword", "查找文字", required=True), choice("direction", "方向", "left", (("left", "从左 find"), ("right", "从右 rfind")))), "字符串",
        "返回从 0 开始的位置；没找到返回 -1。")
    add("text_count", "str.count · 文字出现次数", lambda text, keyword: text.count(keyword), txt, num,
        (text("keyword", "查找文字", required=True),), "字符串")
    add("padding", "str · 补齐宽度", lambda text, width, fill, mode: {"left": text.ljust, "right": text.rjust, "center": text.center}[mode](width, fill), txt, txt,
        (number("width", "目标字符宽度", 10), text("fill", "填充字符（一个字符）", " ", True),
         choice("mode", "对齐", "left", (("left", "左对齐 ljust"), ("right", "右对齐 rjust"), ("center", "居中 center")))), "字符串")
    add("zfill", "str.zfill · 数字补零", lambda text, width: text.zfill(width), txt, txt,
        (number("width", "目标字符宽度", 3),), "字符串", "例如 7 → 007，适合生成有序文件名。")

    add("math", "math · 单值数学函数", math_unary, (p("value", "数值", "number"),), num,
        (choice("operation", "函数", "abs", tuple((v, v) for v in ("abs", "round", "floor", "ceil", "trunc", "sqrt", "log", "log10", "sin", "cos", "tan", "radians", "degrees"))),
         NodeParameter("digits", "round 小数位数", "integer", 2, -12, 12)), "数学")
    add("power", "math.pow · 幂", lambda a, b: finite_number(math.pow(finite_number(a), finite_number(b))),
        (p("a", "底数", "number"), p("b", "指数", "number")), num, section="数学")
    add("aggregate", "statistics · 列表统计", aggregate, lists, num,
        (choice("operation", "统计", "sum", (("sum", "求和 sum"), ("min", "最小 min"), ("max", "最大 max"), ("mean", "平均 mean"), ("median", "中位数 median"))),), "数学")
    add("clamp", "数值 · 限制范围 clamp", lambda value, minimum, maximum: finite_number(max(minimum, min(value, maximum))) if minimum <= maximum else (_raise("最小值不能大于最大值。")),
        (p("value", "数值", "number"), p("minimum", "最小值", "number"), p("maximum", "最大值", "number")), num, section="数学")
    add("date_shift", "datetime · 日期加减", date_shift, txt + (p("days", "天数（覆盖参数）", "number", True),), txt,
        (number("offset", "偏移天数", 1),), "日期与网址", "输入 ISO 日期 YYYY-MM-DD；负数表示提前。不查询网络。")
    add("date_diff", "datetime · 日期间隔", lambda a, b: (date.fromisoformat(b) - date.fromisoformat(a)).days,
        (p("a", "开始日期 YYYY-MM-DD", "text"), p("b", "结束日期 YYYY-MM-DD", "text")), num, section="日期与网址")
    add("url_parts", "urllib.parse · 拆解网址", url_parts, txt,
        tuple(p(v, t, "text") for v, t in (("scheme", "协议"), ("host", "主机名"), ("path", "路径"), ("query", "查询参数"), ("fragment", "片段"))),
        section="日期与网址", description="只解析字符串，不访问网址，也不下载文件。")
    add("url_quote", "urllib.parse.quote · URL 编码", lambda text, safe: quote(text, safe=safe), txt, txt,
        (text("safe", "保留字符", "/"),), "日期与网址")
    add("url_unquote", "urllib.parse.unquote · URL 解码", lambda text: unquote(text), txt, txt, section="日期与网址")


def _raise(message):
    raise ValueError(message)


def python_example_graph(registry):
    from .graph import empty_graph, new_node
    kinds = ("core.py_json", "core.py_list", "core.py_list_filter", "core.py_data", "core.py_output")
    nodes = [new_node(registry.definitions[kind], (i * 270, 40)) for i, kind in enumerate(kinds)]
    target = new_node(registry.definitions["core.py_json"], (270, 280))
    nodes[0]["params"]["content"] = '["Maria_001.mp4", "John_002.mp4", "Maria_003.mp4"]'
    target["params"]["content"] = '"Maria"'
    graph = {**empty_graph(), "nodes": nodes + [target], "output_node": nodes[-1]["id"]}
    graph["connections"] = [[nodes[0]["id"], "value", nodes[1]["id"], "value"],
        [nodes[1]["id"], "items", nodes[2]["id"], "items"], [target["id"], "value", nodes[2]["id"], "target"],
        [nodes[2]["id"], "items", nodes[3]["id"], "value"], [nodes[3]["id"], "value", nodes[4]["id"], "value"]]
    return graph

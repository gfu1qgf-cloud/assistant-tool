"""Bounded stdlib data tools, registered on the existing node evaluator."""
from string import Template

from .contracts import NodeDefinition, NodeParameter, NodePort
from .function_library import node_function
from .python_nodes import checked_list, integer, mapping, sequence
from .python_values import PythonValue, MAX_DATA_BYTES, parse_json, unpack, validate_data


def same_value(a, b):
    # Number ports intentionally accept both int and float, never bool as 1.
    if type(a) in (int, float) and type(b) in (int, float):
        return a == b
    if type(a) is not type(b):
        return False
    if type(a) is list:
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    if type(a) is dict:
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    return a == b


def compare_values(a, b, operation):
    a, b = unpack(a), unpack(b)
    numeric = type(a) in (int, float) and type(b) in (int, float)
    if type(a) is not type(b) and not numeric:
        raise TypeError("两路值必须是同一类型；数值与文字不能混用，布尔也不当作数值。")
    if operation in {"eq", "ne"}:
        equal = same_value(a, b)
        return equal if operation == "eq" else not equal
    if not numeric and not (type(a) is str and type(b) is str):
        raise TypeError("大小比较只支持两路数值或两路文字；列表、字典、布尔和 null 只支持相等判断。")
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[operation]


def is_empty(value):
    value = unpack(value)
    return value is None or type(value) in (str, list, dict) and len(value) == 0


def truth_reduce(items, operation, truth):
    items = sequence(items)
    if truth == "strict" and any(type(v) is not bool for v in items):
        raise TypeError("严格模式要求列表内全部是真／假；或主动选择 Python 真假规则。")
    return (all if operation == "all" else any)(items)


def list_edit(items, value, index, position, operation):
    result, value = list(sequence(items)), unpack(value)
    if operation == "append":
        result.append(value)
    else:
        at = integer(position if index is None else index)
        maximum = len(result) if operation == "insert" else len(result) - 1
        if not -len(result) <= at <= maximum:
            raise IndexError("列表索引越界；不会自动改到其他位置。")
        if operation == "insert":
            result.insert(at, value)
        else:
            result[at] = value
    return validate_data(result)


def list_pop(items, index, position):
    result = list(sequence(items))
    at = integer(position if index is None else index)
    if not -len(result) <= at < len(result):
        raise IndexError("移除项的索引越界或列表为空。")
    removed = result.pop(at)
    return {"items": result, "removed": PythonValue.capture(removed)}


def chunks(items, size, count):
    items, size = sequence(items), integer(count if size is None else size)
    if not 1 <= size <= 100000:
        raise ValueError("每批数量必须是 1～100000 的整数。")
    return checked_list(items[i:i + size] for i in range(0, len(items), size))


def flatten(items, depth):
    if type(depth) is not int or not 1 <= depth <= 16:
        raise ValueError("展开层数必须是 1～16 的整数。")
    def walk(values, left):
        for value in values:
            if type(value) is list and left:
                yield from walk(value, left - 1)
            else:
                yield value
    return checked_list(walk(sequence(items), depth))


def pluck(items, field, missing, fallback):
    default = parse_json(fallback)
    def values():
        for row in sequence(items):
            row = mapping(row)
            if field in row:
                yield row[field]
            elif missing == "default":
                yield default
            elif missing == "error":
                raise KeyError("列表中的字典缺少字段：" + field)
    return checked_list(values())


def group_by(items, field, missing):
    groups = {}
    for row in sequence(items):
        row = mapping(row)
        if field not in row:
            if missing == "skip":
                continue
            raise KeyError("分组条目缺少字段：" + field)
        key = row[field]
        if type(key) is not str:
            raise TypeError("分组字段必须是文字，避免把数值、布尔和文字合并成同一组。")
        groups.setdefault(key, []).append(row)
    return validate_data(groups)


def sort_by(items, field, direction):
    items = sequence(items)
    keys = []
    for row in items:
        row = mapping(row)
        if field not in row:
            raise KeyError("排序条目缺少字段：" + field)
        keys.append(row[field])
    if not all(type(k) is str for k in keys) and not all(type(k) in (int, float) for k in keys):
        raise TypeError("排序字段必须全部是文字或全部是数值，不能混合类型。")
    return sorted(items, key=lambda row: row[field], reverse=direction == "descending")


def from_pairs(items, conflicts):
    result = {}
    for pair in sequence(items):
        if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
            raise TypeError('需要 [["键", 值], ...] 形式，每组恰好两项，键必须是文字。')
        key, value = pair
        if key in result and conflicts == "error":
            raise ValueError("键值对中出现重复键：" + key[:100])
        if key not in result or conflicts == "last":
            result[key] = value
    return validate_data(result)


def nested_get(value, path, route, fallback):
    value = unpack(value)
    keys = sequence(parse_json(route) if path is None else path)
    if len(keys) > 32 or any(type(k) not in (str, int) for k in keys):
        raise ValueError('字段路径最多 32 层，使用 ["字段", 0, "子字段"]；索引必须是整数，不接受布尔。')
    default = parse_json(fallback)
    for key in keys:
        if type(value) is dict and type(key) is str and key in value:
            value = value[key]
        elif type(value) is list and type(key) is int and -len(value) <= key < len(value):
            value = value[key]
        else:
            return {"value": PythonValue.capture(default), "found": False}
    return {"value": PythonValue.capture(value), "found": True}


def template_text(text, items):
    text, items = unpack(text), mapping(items)
    # Bound expansion *before* joining. str.format attribute/index traversal is
    # deliberately not exposed; Template supports plain $name / ${name} only.
    result, size, last = [], 0, 0
    def emit(value):
        nonlocal size
        size += len(value.encode("utf-8"))
        if size > MAX_DATA_BYTES:
            raise ValueError("模板展开超过 1 MB；未生成或截断文本。")
        result.append(value)
    for match in Template.pattern.finditer(text):
        emit(text[last:match.start()])
        key = match.group("named") or match.group("braced")
        if key is not None:
            if key not in items:
                raise KeyError("模板缺少字段：" + key)
            value = items[key]
            if type(value) in (list, dict):
                raise TypeError("模板字段只接受文字、数值、布尔或 null；容器请先转换成文字。")
            emit(str(value))
        elif match.group("escaped") is not None:
            emit("$")
        else:
            raise ValueError("模板中的 $ 格式无效；字段写作 $name 或 ${name}，文字 $ 写作 $$。")
        last = match.end()
    emit(text[last:])
    return "".join(result)


def register_extended_nodes(registry):
    def port(key, title, dtype, optional=False, group=""):
        return NodePort(key, title, dtype, required=not optional, type_group=group)
    def choice(key, title, default, values):
        return NodeParameter(key, title, "choice", default, choices=tuple(values))
    def text(key, title, default="", required=True, multiline=False):
        return NodeParameter(key, title, "multiline" if multiline else "text", default, required=required)
    def integer_param(key, title, default=0, minimum=-100000, maximum=100000):
        return NodeParameter(key, title, "integer", default, minimum, maximum)
    def add(name, title, function, inputs, outputs, parameters=(), section="列表", description="", keywords=()):
        function = node_function(kind="core.py_" + name, title=title, inputs=inputs, outputs=outputs,
            parameters=parameters, category="Python 原生/" + section,
            description=description, keywords=("python", name) + tuple(keywords))(function)
        registry.register_function("core", function)
    lists = (port("items", "列表", "list"),)
    data = (port("value", "Python 数据", "data"),)
    boolean = (port("boolean", "真／假", "boolean"),)
    generic = (port("value", "值", "any", group="value"),)
    field = text("field", "字段名")
    optional_index = port("index", "索引（覆盖参数）", "number", True)
    index_setting = integer_param("position", "索引 · 从 0 开始，可为负数")
    conflicts = choice("conflicts", "重复键处理", "error", (("error", "报错"), ("first", "保留第一个"), ("last", "保留最后一个")))

    add("compare", "通用比较 · 同类型值", compare_values,
        (port("a", "A", "any", group="operand"), port("b", "B", "any", group="operand")), boolean,
        (choice("operation", "比较方式", "eq", (("eq", "等于"), ("ne", "不等于"), ("gt", "大于"), ("ge", "大于等于"), ("lt", "小于"), ("le", "小于等于"))),),
        "逻辑与判断", "自动推断两路同类型输入；不偷偷转换数字与文字。大小比较只支持数值或文字；列表／字典可比较内容。布尔不等于数值 1。", ("==", "!=", "comparison"))
    add("is_null", "空值判断 · is None", lambda value: unpack(value) is None, generic, boolean,
        section="逻辑与判断", description="只判断 JSON null；空文字、空列表、0 和 False 不属于 null。")
    add("is_empty", "是否为空 · null／空文字／空容器", is_empty, generic, boolean,
        section="逻辑与判断", description="null、空文字、空列表和空字典为真；0、False 或只含空格的文字不算空。不检测媒体文件。")
    add("truth_reduce", "all／any · 列表真假判断", truth_reduce, lists, boolean,
        (choice("operation", "条件", "all", (("all", "all · 全部为真"), ("any", "any · 至少一个为真"))),
         choice("truth", "真假规则", "strict", (("strict", "严格 · 列表内必须全为布尔"), ("python", "Python 规则 · 非空文字等视为真")))),
        "逻辑与判断", "遵循 all/any 空列表规则：all([])=True，any([])=False。默认严格布尔；可主动选择 Python 真假规则。")
    add("list_edit", "列表 · 追加／插入／替换项", list_edit,
        lists + data + (optional_index,), lists,
        (choice("operation", "操作", "append", (("append", "追加到末尾"), ("insert", "插入指定位置"), ("set", "替换指定位置"))), index_setting),
        description="输出新列表，不改上游数据。插入可用 len 位置追加；越界报错，不自动移到末尾。")
    add("list_pop", "列表 · 移除项并取出", list_pop, lists + (optional_index,),
        lists + (port("removed", "取出的项 · Python 数据", "data"),), (index_setting,),
        description="输出移除后的新列表和取出的数据，不删除文件、不修改原列表。")
    add("chunks", "列表 · 按数量分批", chunks, lists + (port("size", "每批数量（覆盖参数）", "number", True),), lists,
        (integer_param("count", "每批数量", 10, 1),), description="例如 23 项按 10 分批，得到 10、10、3 项的三个子列表；保留余项和原顺序。", keywords=("batch", "分块"))
    add("flatten", "列表 · 展开嵌套列表", flatten, lists, lists,
        (integer_param("depth", "展开层数", 1, 1, 16),), description="只展开列表，不展开文字或字典。默认展开一层；结果超限报错，不截断。")
    add("pluck", "列表 · 提取字典字段", pluck, lists, lists,
        (field, choice("missing", "缺少字段时", "error", (("error", "报错"), ("skip", "跳过该项"), ("default", "使用默认值"))), text("fallback", "JSON 默认值", "null", multiline=True)),
        description="从字典列表提取指定字段为列表；默认缺字段时报错，避免漏掉任务。", keywords=("field", "pluck"))
    add("group_by", "列表 · 按字段分组", group_by, lists, (port("items", "分组字典", "mapping"),),
        (field, choice("missing", "缺少字段时", "error", (("error", "报错"), ("skip", "跳过")))),
        description="按文字字段（如 admin）分组；分组字典保留首次出现的组序与每组原顺序，不合并不同数据类型。", keywords=("groupby", "管理员"))
    add("sort_by", "列表 · 按字段排序", sort_by, lists, lists,
        (field, choice("direction", "方向", "ascending", (("ascending", "升序"), ("descending", "降序")))),
        description="稳定排序：相同字段值保持原顺序。字段必须全部是文字或全部是数值；缺字段或混合类型报错。")
    add("dict_from_pairs", "dict() · 从键值对建立字典", from_pairs, lists, (port("items", "字典", "mapping"),),
        (conflicts,), "字典", '输入 [["admin", "Maria"], ["count", 3]]。默认重复键报错，可选择保留第一个／最后一个。', ("pairs",))
    add("nested_get", "数据 · 读取嵌套字段", nested_get,
        data + (port("path", "字段路径列表（覆盖参数）", "list", True),), data + (port("found", "是否找到", "boolean"),),
        (text("route", '字段路径 · JSON，例如 ["items", 0, "name"]', '[]', multiline=True), text("fallback", "未找到时的 JSON 默认值", "null", multiline=True)),
        "字典", "支持字典和列表混合路径、负索引；实际 null 的 found=True，与没找到区别。只读取显式键／索引，不访问对象属性。")
    add("template", "string.Template · 填入字段", template_text,
        (port("text", "模板文字", "text"), port("items", "字段字典", "mapping")), (port("text", "填好后的文字", "text"),),
        section="字符串", description="写成 ${date}-${name}.mp4；$$ 表示文字 $。只替换字典字段；缺字段报错，不执行表达式或访问对象属性。", keywords=("template", "文件命名"))

    def select_fallback(_params, get):
        return "fallback" if unpack(get("value")) is None else "value"
    registry.register("core", NodeDefinition("core.py_coalesce", "空值回退 · null 时用备用值",
        inputs=data + (port("fallback", "备用值 · Python 数据", "data"),), outputs=data,
        build=lambda i, _p, _c: {"value": next(iter(i.values()))}, select_input=select_fallback,
        category="Python 原生/流程控制/条件分支", execution="compute", keywords=("python", "coalesce", "fallback", "空值"),
        description="输入 JSON null 时才计算备用分支；0、False、空文字和空容器保留原值。两路必须接 Python 数据，不用它代替未连接的必填端。所有分支仍须静态预检。"))


def batch_example_graph(registry):
    from .graph import empty_graph, new_node
    graph = empty_graph()
    kinds = ("core.py_json", "core.py_list", "core.py_sort_by", "core.py_group_by", "core.py_data", "core.py_output")
    nodes = [new_node(registry.definitions[kind], (i * 290, 60)) for i, kind in enumerate(kinds)]
    nodes[0]["params"]["content"] = '[{"admin":"Maria","id":3},{"admin":"John","id":2},{"admin":"Maria","id":1}]'
    nodes[2]["params"]["field"] = "id"
    nodes[3]["params"]["field"] = "admin"
    graph["nodes"], graph["output_node"] = nodes, nodes[-1]["id"]
    graph["connections"] = [[nodes[a]["id"], output, nodes[b]["id"], input_] for a, output, b, input_ in (
        (0, "value", 1, "value"), (1, "items", 2, "items"), (2, "items", 3, "items"),
        (3, "items", 4, "value"), (4, "value", 5, "value"))]
    return graph

"""Bounded, copyable Python data. No live objects or executable serialization."""
from dataclasses import dataclass
import json
import math

MAX_DATA_BYTES = 1024 * 1024
MAX_DATA_ITEMS = 100000
MAX_DATA_DEPTH = 32


def validate_data(value):
    """Only JSON-shaped builtins; reject objects, cycles, non-finite numbers."""
    count, size, active = 0, 0, set()

    def visit(item, depth):
        nonlocal count, size
        count += 1
        if count > MAX_DATA_ITEMS or depth > MAX_DATA_DEPTH:
            raise ValueError("Python 数据过多或嵌套过深（最多 100000 项／32 层），未截断。")
        if item is None or type(item) is bool:
            size += 5
        elif type(item) in (int, float):
            if abs(item) > 1e100 or not math.isfinite(item):
                raise ValueError("Python 数值必须有限，绝对值不超过 1e100。")
            size += 128
        elif type(item) is str:
            size += len(item.encode("utf-8")) + 2
        elif type(item) in (list, dict):
            if id(item) in active:
                raise ValueError("Python 数据不能含循环引用。")
            active.add(id(item))
            size += len(item) + 2
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise TypeError("字典键必须是文本。")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(id(item))
        else:
            raise TypeError("只接受文本、有限数值、布尔、空值、列表和文本键字典；不接受函数、文件句柄或任意对象。")
        if size > MAX_DATA_BYTES:
            raise ValueError("Python 数据超过 1 MB 上限，未截断。")
    visit(value, 0)
    return value


def parse_json(text):
    if type(text) is not str or len(text.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError("JSON 文本超过 1 MB 或不是文本。")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 中有重复的字典键：" + key[:100])
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("JSON 不能包含 NaN 或 Infinity。")
    def number(text):
        if len(text) > 128:
            raise ValueError("JSON 数值文本过长。")
        return float(text) if any(ch in text.lower() for ch in ".e") else int(text)
    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_int=number, parse_float=number)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("JSON 格式或嵌套层数无效。") from error
    return validate_data(result)


@dataclass(frozen=True)
class PythonValue:
    """Immutable boxed value, so JSON null is not mistaken for a missing input."""
    data_json: str

    @classmethod
    def capture(cls, value):
        validate_data(value)
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_DATA_BYTES:
            raise ValueError("JSON 编码后超过 1 MB，未截断。")
        return cls(text)

    @property
    def value(self):
        return parse_json(self.data_json)


def unpack(value):
    return value.value if isinstance(value, PythonValue) else validate_data(value)

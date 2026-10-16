"""Formatting-only rules verified against the supplied specification screenshots."""
import math

DEFAULT_RULES = {
    "page_width_cm": 21.0, "page_height_cm": 29.7,
    "margin_top_cm": 1.8, "margin_bottom_cm": 1.8,
    "margin_left_cm": 1.5, "margin_right_cm": 1.5,
    "indent_left_cm": 0.0, "indent_right_cm": 0.0, "first_line_cm": 0.84,
    "title_pt": 14.0, "body_pt": 12.0, "line_spacing": 1.5,
    "first_title": True, "reset_spacing": False,
}
ROLES = {"title": "标题", "body": "正文", "keep": "保留"}
MAX_PARAGRAPHS = 1500


def normalize_rules(value=None):
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_RULES)
    for key, default in DEFAULT_RULES.items():
        if isinstance(default, bool):
            result[key] = source.get(key, default) is True
            continue
        try:
            number = float(source.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f"格式参数无效：{key}") from None
        minimum, maximum = (0, 20) if "margin" in key or "indent" in key or key == "first_line_cm" else (1, 60)
        if key == "line_spacing": minimum, maximum = 0.5, 5
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f"格式参数超出范围：{key}")
        result[key] = number
    if result["page_width_cm"] - result["margin_left_cm"] - result["margin_right_cm"] < 2:
        raise ValueError("左右边距过大，页面正文宽度不足2厘米。")
    if result["page_height_cm"] - result["margin_top_cm"] - result["margin_bottom_cm"] < 3:
        raise ValueError("上下边距过大，页面正文高度不足3厘米。")
    return result


def paragraph_roles(paragraphs, rules, overrides=None):
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise ValueError(f"段落超过{MAX_PARAGRAPHS}段，请拆分文档后处理。")
    supplied = overrides or {}
    known = {item["id"] for item in paragraphs}
    if set(supplied) - known or any(value not in ROLES for value in supplied.values()):
        raise ValueError("段落类型与当前文档不一致，请重新读取。")
    result = {item["id"]: ("keep" if item.get("structured") or not item["text"].strip()
                           else "title" if item.get("heading") else "body") for item in paragraphs}
    if rules["first_title"] and not any(role == "title" for role in result.values()):
        first = next((item for item in paragraphs if result[item["id"]] == "body"), None)
        if first: result[first["id"]] = "title"
    result.update(supplied)
    return result


def summary(rules):
    r = normalize_rules(rules)
    return (f"页面 {r['page_width_cm']:g}×{r['page_height_cm']:g} cm；"
            f"上下 {r['margin_top_cm']:g}/{r['margin_bottom_cm']:g} cm，左右 {r['margin_left_cm']:g}/{r['margin_right_cm']:g} cm；"
            f"首行 {r['first_line_cm']:g} cm；标题 {r['title_pt']:g} 磅加粗，正文 {r['body_pt']:g} 磅；"
            f"{r['line_spacing']:g} 倍行距。保留字体、对齐、文字、链接与图片。")

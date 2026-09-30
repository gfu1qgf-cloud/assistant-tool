"""Timeline naming helpers that do not require the Resolve scripting SDK."""

import re


def get_timeline_task_name(timeline):
    """从当前时间线名称提取日期并生成 MMDD；没有日期时使用完整名称。"""
    try:
        timeline_name = str(timeline.GetName() or "").strip()
    except Exception:
        return ""

    date_patterns = (
        r"(?<!\d)\d{4}\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(?<!\d)\d{4}[-./_](\d{1,2})[-./_](\d{1,2})(?!\d)",
        r"(?<!\d)(?:19|20)\d{2}(\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    )
    for pattern in date_patterns:
        match = re.search(pattern, timeline_name)
        if not match:
            continue
        month = int(match.group(1))
        day = int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return "{:02d}{:02d}".format(month, day)

    return timeline_name

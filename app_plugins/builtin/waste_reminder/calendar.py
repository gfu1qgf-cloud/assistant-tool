"""Calendar data and scheduling rules. No Qt, networking or weekday guesses."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path


class CalendarError(ValueError):
    pass


def clock(value):
    try:
        if not isinstance(value, str) or len(value) != 5:
            raise ValueError()
        return time.fromisoformat(value)
    except ValueError as error:
        raise CalendarError(f"时间必须是 HH:MM：{value}") from error


class AnnualCalendar:
    def __init__(self, document, path=None):
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise CalendarError("不支持的日历格式，需要 schema_version=1。")
        self.path = Path(path) if path else None
        self.year = document.get("year")
        if not isinstance(self.year, int) or isinstance(self.year, bool) or not 2000 <= self.year <= 2200:
            raise CalendarError("年度无效。")
        self.commune = str(document.get("commune") or "").strip()
        self.timezone = str(document.get("timezone") or "")
        self.types = document.get("types")
        self.dates = document.get("dates")
        if not self.commune or not self.timezone or not isinstance(self.types, dict) or not self.types:
            raise CalendarError("缺少 Comune、时区或垃圾类型。")
        for key, value in self.types.items():
            if not isinstance(key, str) or not isinstance(value, dict) or not value.get("it"):
                raise CalendarError("垃圾类型需要意大利语名称。")
        if not isinstance(self.dates, dict):
            raise CalendarError("dates 必须是逐日字典；空数组表示不收运，缺少日期表示未知。")
        for key, values in self.dates.items():
            try:
                day = date.fromisoformat(key)
            except (TypeError, ValueError) as error:
                raise CalendarError(f"日期无效：{key}") from error
            if day.year != self.year or day.isoformat() != key:
                raise CalendarError(f"日期不属于本年度：{key}")
            if not isinstance(values, list) or any(not isinstance(x, str) or x not in self.types for x in values):
                raise CalendarError(f"{key} 包含未知的垃圾类型。")
            if len(set(values)) != len(values):
                raise CalendarError(f"{key} 存在重复类型。")
        exposure = document.get("exposure", {})
        self.start = clock(exposure.get("from", "22:00"))
        self.end = clock(exposure.get("until", "06:00"))
        if self.start <= self.end:
            raise CalendarError("第一版支持前晚至次晨的跨夜投放窗口。")
        self.document = document

    @classmethod
    def load(cls, path):
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")), path)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CalendarError(f"无法读取年度日历：{path}；{error}") from error

    def collections(self, day, enabled_types=None):
        values = self.dates.get(day.isoformat()) if day.year == self.year else None
        if values is None:
            return None
        return tuple(x for x in values if enabled_types is None or x in enabled_types)

    def label(self, key, language="bilingual"):
        value = self.types[key]
        name = value["it"] if language == "it" else (
            value.get("zh") or value["it"] if language == "zh" else
            f"{value.get('zh') or value['it']}（{value['it']}）")
        return f"{value.get('emoji', '♻️')} {name}"

    def event_key(self, day, values):
        identity = json.dumps([self.commune, self.timezone, day.isoformat(), sorted(values)], ensure_ascii=False)
        return day.isoformat() + ":" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ReminderEvent:
    day: date
    types: tuple
    key: str
    deadline: datetime


def target_day(now, settings):
    """Use local Comune time, including a post-midnight catch-up before cutoff."""
    morning_end = clock(settings.get("exposure_end", "06:00"))
    if now.time().replace(tzinfo=None) < morning_end:
        return now.date()
    return now.date() + timedelta(days=1)


def due_event(now, settings, calendar, state):
    if not settings.get("enabled", True):
        return None
    local_clock = now.time().replace(tzinfo=None)
    remind_at = clock(settings["reminder_time"])
    if settings.get("mode") == "morning":
        if not remind_at <= local_clock < calendar.end:
            return None
        day = now.date()
    else:
        if local_clock < calendar.end:
            day = now.date()  # restart/sleep catch-up; never after the pickup cutoff
        elif local_clock >= max(remind_at, calendar.start):
            day = now.date() + timedelta(days=1)
        else:
            return None
    values = calendar.collections(day, settings.get("enabled_types"))
    if values is None:
        raise CalendarError(f"{day.isoformat()} 没有日历数据，请补充该年度文件；不能视作不收运。")
    if not values and not settings.get("notify_empty", False):
        return None
    key = calendar.event_key(day, values)
    record = state.get("events", {}).get(key, {})
    if record.get("confirmed") or float(record.get("snooze_until", 0)) > now.timestamp():
        return None
    deadline = datetime.combine(day, calendar.end, now.tzinfo)
    return ReminderEvent(day, values, key, deadline)


def message(calendar, event, now, language="bilingual"):
    italian = language == "it"
    tomorrow = event.day > now.date()
    weekday = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")[event.day.weekday()]
    when = ("Domani" if tomorrow else "Oggi") if italian else ("明天" if tomorrow else "今天")
    if not event.types:
        return (f"{when} ({weekday}, {event.day}) non è prevista una raccolta attiva."
                if italian else f"{when}（{weekday}，{event.day}）没有已启用的垃圾收运。")
    heading = f"♻️ {when} {weekday} · {event.day}"
    lines = [heading, "", *(calendar.label(x, language) for x in event.types), ""]
    if italian:
        lines.append(f"Esporre dopo le {calendar.start.strftime('%H:%M')} della sera precedente, entro le {calendar.end.strftime('%H:%M')} del giorno di raccolta.")
    else:
        lines.append(f"请在收运日前一晚 {calendar.start.strftime('%H:%M')} 后、收运当天 {calendar.end.strftime('%H:%M')} 前放到指定位置。")
    return "\n".join(lines)

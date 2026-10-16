"""City-level Open-Meteo queries. No application files or credentials are sent."""
import json
import math
import os
from pathlib import Path
from model.AppDataStorage import data_file
import tempfile
import time

import requests
from qt_compat import QtCore
from model.OfflineMode import require_online

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_FORECAST_DAYS = 16
CHART_SPANS = ((24, "24小时"), (72, "3天"), (120, "5天"), (168, "7天"),
               (336, "2周"), (720, "1个月（30天视图）"))
DEFAULT_LOCATION = {"name": "Valle Lomellina", "latitude": 45.15, "longitude": 8.67,
                    "timezone": "Europe/Rome", "country_code": "IT", "admin1": "Lombardia", "admin2": "Pavia"}
PRIVACY = ("仅向 Open-Meteo 发送城市名（搜索时）、城市级坐标、时区和天气参数；不自动定位，"
           "不上传图片、视频、任务表、聊天、歌曲或密钥。服务端仍能看到IP，并可能记录坐标。"
           "免费接口仅限个人/非商业用途，数据来源：Open-Meteo（CC BY 4.0）。")
FIELDS = {"current": ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "weather_code", "wind_speed_10m"),
          "hourly": ("temperature_2m", "precipitation_probability", "precipitation", "weather_code", "wind_speed_10m"),
          "daily": ("weather_code", "temperature_2m_min", "temperature_2m_max", "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max")}
LABELS = {0: "晴", 1: "大致晴朗", 2: "晴间多云", 3: "阴", 45: "雾", 48: "冻雾",
          51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨", 56: "小冻毛毛雨", 57: "冻毛毛雨",
          61: "小雨", 63: "中雨", 65: "大雨", 66: "小冻雨", 67: "冻雨",
          71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "小阵雨", 81: "阵雨", 82: "强阵雨",
          85: "小阵雪", 86: "强阵雪", 95: "雷雨", 96: "雷雨伴小冰雹", 97: "强雷雨", 99: "雷雨伴冰雹"}


class WeatherError(ValueError):
    pass


def location(value):
    if not isinstance(value, dict):
        raise WeatherError("请先选择天气城市。")
    result = {}
    for key, limit in (("latitude", 90), ("longitude", 180)):
        number = float(value[key])
        if not math.isfinite(number) or abs(number) > limit:
            raise WeatherError("城市坐标无效。")
        result[key] = round(number, 2)  # town-level precision, not a home address
    for key in ("name", "timezone", "country_code", "admin1", "admin2"):
        result[key] = str(value.get(key) or "").replace("\n", " ").replace("\r", " ")[:100]
    if not result["name"] or not QtCore.QTimeZone(result["timezone"].encode()).isValid():
        raise WeatherError("城市名称或时区无效。")
    return result


def options(value=None, commune="Valle Lomellina (PV)"):
    value = value if isinstance(value, dict) else {}
    try:
        selected = location(value.get("location") or (DEFAULT_LOCATION if commune.casefold() == "valle lomellina (pv)" else None))
    except (ValueError, TypeError, KeyError):
        selected = None
    try:
        minutes = max(30, min(720, int(value.get("refresh_minutes", 120))))
    except (ValueError, TypeError):
        minutes = 120
    try:
        chart_hours = int(value.get("chart_hours", 24))
    except (TypeError, ValueError):
        chart_hours = 24
    if chart_hours not in dict(CHART_SPANS):
        chart_hours = 24
    return {"enabled": bool(value.get("enabled", False)), "auto_refresh": bool(value.get("auto_refresh", True)),
            "refresh_minutes": minutes, "location": selected, "chart_hours": chart_hours}


def location_key(value):
    selected = location(value)
    return tuple(selected[key] for key in ("latitude", "longitude", "timezone"))


def get_json(url, params, canceled=lambda: False):
    require_online("查询城市天气")
    if canceled():
        raise WeatherError("天气查询已取消。")
    if url not in {FORECAST_URL, GEOCODE_URL}:
        raise WeatherError("不允许的天气服务地址。")
    try:
        with requests.get(url, params=params, timeout=(5, 15), allow_redirects=False, stream=True,
                          headers={"Accept": "application/json", "User-Agent": "AssistantTool-Weather/1.0"}) as response:
            if response.status_code != 200:
                raise WeatherError(f"天气服务 HTTP {response.status_code}；请稍后重试，旧缓存仍保留。")
            data = bytearray()
            for chunk in response.iter_content(16384):
                if canceled():
                    raise WeatherError("天气查询已取消。")
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise WeatherError("天气响应过大，已拒绝。")
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("error"):
            raise WeatherError("天气服务返回无效数据。")
        return result
    except requests.RequestException:
        raise WeatherError("天气服务连接失败或超时；请检查网络，旧缓存仍保留。") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WeatherError("天气服务没有返回有效JSON，旧缓存仍保留。") from None


def search_cities(query, canceled=lambda: False):
    query = str(query).strip()
    if not 2 <= len(query) <= 80:
        raise WeatherError("请输入2至80字的城市名，不要输入详细住址。")
    raw = get_json(GEOCODE_URL, {"name": query, "count": 10, "language": "it", "countryCode": "IT", "format": "json"}, canceled)
    cities = []
    for row in raw.get("results", [])[:10]:
        try:
            if row.get("country_code") == "IT":
                cities.append(location(row))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return cities


def _row(value, keys):
    if not isinstance(value, dict):
        raise WeatherError("天气数据格式无效。")
    timestamp = value.get("time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not 0 < timestamp < 2**40:
        raise WeatherError("天气日期无效。")
    result = {"time": int(timestamp)}
    for key in keys:
        number = value.get(key)
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)):
            raise WeatherError("天气数值无效。")
        result[key] = number
    return result


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise WeatherError("天气缓存格式无效。")
    checked = float(value["checked_at"])
    if not math.isfinite(checked) or checked <= 0:
        raise WeatherError("天气缓存时间无效。")
    result = {"schema": 1, "checked_at": checked, "location": location(value["location"]),
              "current": _row(value["current"], FIELDS["current"])}
    # Keep old seven-day caches valid; allow a DST day with 25 hourly entries.
    for key, maximum in (("hourly", MAX_FORECAST_DAYS * 25), ("daily", MAX_FORECAST_DAYS)):
        rows = value[key]
        if not isinstance(rows, list) or not 1 <= len(rows) <= maximum:
            raise WeatherError("天气预报范围无效。")
        result[key] = [_row(row, FIELDS[key]) for row in rows]
    return result


def forecast(selected, canceled=lambda: False):
    selected = location(selected)
    params = {key: ",".join(value) for key, value in FIELDS.items()}
    params.update(latitude=selected["latitude"], longitude=selected["longitude"], timezone=selected["timezone"],
                  forecast_days=MAX_FORECAST_DAYS, timeformat="unixtime", temperature_unit="celsius", wind_speed_unit="kmh", precipitation_unit="mm")
    raw = get_json(FORECAST_URL, params, canceled)
    result = {"schema": 1, "checked_at": time.time(), "location": selected, "current": raw.get("current")}
    for key in ("daily", "hourly"):
        block = raw.get(key) or {}
        times = block.get("time") or []
        if not times or any(not isinstance(block.get(field), list) or len(block[field]) != len(times) for field in FIELDS[key]):
            raise WeatherError("天气响应缺少完整预报，旧缓存仍保留。")
        result[key] = [{"time": stamp, **{field: block[field][index] for field in FIELDS[key]}} for index, stamp in enumerate(times)]
    return validate_snapshot(result)


def load_cache(path):
    try:
        path = Path(path)
        if path.stat().st_size > 1024 * 1024:
            return None
        return validate_snapshot(data_file(path).read_json())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def save_cache(path, data):
    data = validate_snapshot(data)
    path = Path(path)
    data_file(path).write_json(data, allow_nan=False)


def local_time(stamp, zone):
    return QtCore.QDateTime.fromSecsSinceEpoch(int(stamp), QtCore.QTimeZone(zone.encode()))


def number(value, suffix=""):
    return "—" if value is None else f"{value:g}{suffix}"


def tonight_summary(data, now):
    zone = data["location"]["timezone"]
    today = local_time(now.timestamp(), zone).date()
    begin = QtCore.QDateTime(today, QtCore.QTime(22, 0), QtCore.QTimeZone(zone.encode())).toSecsSinceEpoch()
    end = QtCore.QDateTime(today.addDays(1), QtCore.QTime(6, 0), QtCore.QTimeZone(zone.encode())).toSecsSinceEpoch()
    # At 23:00, show only remaining hours of the exposure window.
    # Precipitation is the preceding hour's value: 23:00 describes 22–23,
    # and the 06:00 value belongs to the final exposure hour 05–06.
    rows = [row for row in data["hourly"] if max(begin, int(now.timestamp())) < row["time"] <= end]
    if not rows:
        return "今晚22:00—次日06:00：预报数据不足，不能判断是否下雨。"
    chances = [row["precipitation_probability"] for row in rows if row["precipitation_probability"] is not None]
    amounts = [row["precipitation"] for row in rows if row["precipitation"] is not None]
    if not chances and not amounts:
        return "今晚22:00—次日06:00：降水数据不足，不能判断是否下雨。"
    incomplete = "部分降水数据缺失；" if len(chances) != len(rows) or len(amounts) != len(rows) else ""
    hint = "出门投放请备雨具，纸板尽量避免淋湿。" if (chances and max(chances) >= 40) or (amounts and sum(amounts) > .2) else "预报仅供参考；投放时间和收运日期仍以垃圾日历为准。"
    return f"今晚投放时段：最高降水概率 {number(max(chances) if chances else None, '%')}；{incomplete}{hint}"


class WeatherWorker(QtCore.QThread):
    result = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, mode, payload, parent=None):
        super().__init__(parent)
        self.mode, self.payload = mode, payload.copy() if isinstance(payload, dict) else payload

    def run(self):
        try:
            result = search_cities(self.payload, self.isInterruptionRequested) if self.mode == "search" else forecast(self.payload, self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.result.emit({"mode": self.mode, "data": result})
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error) if isinstance(error, WeatherError) else "天气数据处理失败；请重试，旧缓存仍保留。")

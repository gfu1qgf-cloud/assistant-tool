from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from model.AppDataStorage import data_file, data_directory
import shutil
import tempfile
import time
import weakref

from qt_compat import QtCore, QtWidgets
from app_paths import DATA_ROOT as APP_ROOT
from model.OfflineMode import is_offline
from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage, PluginMainWidget
from app_plugins.builtin.cooking_assistant.plugin import CookingAssistantPlugin
from .calendar import AnnualCalendar, CalendarError, ReminderEvent, due_event, target_day, clock
from .settings import CONFIG_KEY, WasteSettingsPage, normalize_settings
from .state import ReminderState
from . import weather

BUNDLED_DATA = Path(__file__).parent / "data"


class _DetailsLifecycle(QtCore.QObject):
    """Release detail windows while alive, without a destroyed-signal cycle."""
    def __init__(self, plugin, parent):
        super().__init__(parent)
        self.plugin = weakref.ref(plugin)

    @QtCore.pyqtSlot(int)
    def finished(self, _result):
        plugin, dialog = self.plugin(), self.sender()
        if plugin is not None:
            plugin.details_dialogs[:] = [item for item in plugin.details_dialogs if item is not dialog]


class WasteReminderPlugin:
    plugin_id = "waste_reminder"
    display_name = "生活助手"
    version = "1.3"
    required_api_version = 1

    def __init__(self, data_root=None):
        self.context = None
        self.root = Path(data_root) if data_root else Path(APP_ROOT)
        self.cooking = CookingAssistantPlugin(self.root)
        self.settings = normalize_settings()
        self.state = None
        self.dialog = None
        self.reminder = None
        self.current_event = None
        self.worker = None
        self.timer = None
        self.status = "尚未启动"
        self._calendars = {}
        self._reported_errors = set()
        self.notices = {"items": [], "checked_at": None}
        self.main_panel = None
        self.details_dialogs = []
        self._details_lifecycle = None
        self.weather_worker = None
        self.weather_data = None
        self.weather_cities = []
        self.weather_status = "天气联网默认关闭；在天气分页明确同意后开启，无需Key。"
        self.weather_next_attempt = 0

    def register(self, context):
        self.context = context
        self.settings = normalize_settings(context.load_config().get(CONFIG_KEY))
        self.cooking.bind_context(context)
        context.register_command(PluginCommand("open", "生活助手…", lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), tooltip="垃圾日历、天气、做饭建议和食材库存", order=19))
        from .life_settings import LifeSettingsPage
        context.register_settings_page(PluginSettingsPage("settings", "生活助手",
            lambda parent=None: LifeSettingsPage(context, parent), order=219))
        context.register_main_widget(PluginMainWidget("tonight", self.create_main_panel, order=60, title="生活助手 · 今晚投放 / 天气"))

    def create_main_panel(self, parent=None):
        from .ui import TonightPanel
        self.main_panel = TonightPanel(self, parent)
        return self.main_panel

    def refresh_main_panel(self):
        if self.main_panel is not None:
            self.main_panel.refresh()

    def show_category_details(self, calendar, key):
        from .details import CategoryDetailsDialog
        dialog = CategoryDetailsDialog(self, calendar, key, self.context.parent_widget)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.details_dialogs.append(dialog)
        if self._details_lifecycle is None:
            self._details_lifecycle = _DetailsLifecycle(self, self.context.parent_widget)
        dialog.finished.connect(self._details_lifecycle.finished)
        dialog.show()
        dialog.raise_()
        return dialog

    @property
    def folder(self):
        path = Path(self.settings["calendar_folder"]).expanduser()
        return path if path.is_absolute() else self.root/path

    def calendar_path(self, year):
        pattern = self.settings["calendar_pattern"]
        if Path(pattern).name != pattern or "/" in pattern or "\\" in pattern or "{year}" not in pattern:
            raise CalendarError("年度日历文件名规则无效，应类似 schedule_{year}.json。")
        try:
            return self.folder / pattern.format(year=year)
        except (KeyError, ValueError) as error:
            raise CalendarError("年度文件名只允许使用 {year}。") from error

    def zone(self):
        zone = QtCore.QTimeZone(self.settings["timezone"].encode("utf-8"))
        if not zone.isValid():
            raise CalendarError("Comune 时区无效。")
        return zone

    def now(self):
        return self._aware(QtCore.QDateTime.currentDateTimeUtc().toTimeZone(self.zone()))

    @staticmethod
    def _aware(value):
        # PyQt's toPyDateTime() drops timezone information; keep the actual offset.
        return datetime.fromtimestamp(value.toMSecsSinceEpoch()/1000,
                                      timezone(timedelta(seconds=value.offsetFromUtc())))

    def _deadline(self, event, calendar):
        day = QtCore.QDate(event.day.year, event.day.month, event.day.day)
        at = QtCore.QTime(calendar.end.hour, calendar.end.minute)
        # Qt's timezone database handles the DST offset at the target morning.
        return replace(event, deadline=self._aware(QtCore.QDateTime(day, at, self.zone())))

    def _prepare(self):
        data_directory(self.folder).ensure()
        if self.settings["commune"].casefold() == "valle lomellina (pv)":
            for path in BUNDLED_DATA.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(BUNDLED_DATA)
                    target = self.folder/relative
                    if not target.exists():
                        data_file(target).copy_from(path)
        self.state = ReminderState(self.folder/"state.json")
        notices_path = self.folder/"notices.json"
        if notices_path.is_file():
            self.notices = data_file(notices_path).read_json()
        self.weather_data = weather.load_cache(self.folder/"weather-open-meteo.json")

    def calendar_for(self, year):
        path = self.calendar_path(year)
        stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        cached = self._calendars.get(year)
        if cached and cached[0] == stamp:
            return cached[1]
        calendar = AnnualCalendar.load(path)
        if calendar.commune.casefold() != self.settings["commune"].casefold() or calendar.timezone != self.settings["timezone"]:
            raise CalendarError("日历 Comune/时区与设置不一致，请同时更换对应的年度日历。")
        if calendar.year != year:
            raise CalendarError("年度文件名与文件内年份不一致。")
        self._calendars[year] = stamp, calendar
        return calendar

    def active_types(self, calendar):
        selected = self.settings["enabled_types"]
        return tuple(x for x, v in calendar.types.items() if x in selected) if selected is not None else tuple(
            x for x, v in calendar.types.items() if not v.get("optional", False))

    def start(self):
        try:
            self._prepare()
            self.zone()
            self.status = "本地日历已就绪；下一次提醒按 Comune 当地时间执行。"
            self.context.log(f"垃圾提醒已就绪，时间 {self.settings['reminder_time']}；日历目录：{self.folder}。")
        except Exception as error:
            self._error(error)
        if self.timer is None:
            self.timer = QtCore.QTimer(self.context.parent_widget)
            self.timer.setInterval(20000)
            self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.cooking.start()
        self.refresh_main_panel()
        QtCore.QTimer.singleShot(5000, self.tick)

    def tick(self):
        if self.timer is None or not self.timer.isActive():
            return
        try:
            now = self.now()
            self.refresh_main_panel()
            if self.settings["weather"]["enabled"] and self.settings["weather"]["auto_refresh"]:
                self.refresh_weather()
            if not is_offline() and self.settings.get("translate_news", True) and not self.is_updating() and self.state:
                from .translation import needs_translation
                if any(needs_translation(x) for x in self.notices.get("items", [])[:20]):
                    if self.state.notice("translate-attempt:"+now.date().isoformat()):
                        self.check_official(translation_only=True)
            if not is_offline() and self.settings["auto_update"] and not self.is_updating() and self.state:
                if self.state.notice("auto-check:"+now.date().isoformat()):
                    self.check_official()
            if not self.settings["enabled"]:
                return
            if self.reminder is not None and self.reminder.isVisible():
                if self.current_event and now >= self.current_event.deadline:
                    self.context.log("本次投放窗口已结束，未确认投放；请勿超时投放，查看官方下一次日期。", level=logging.WARNING)
                    self.reminder.finish_safely()
                    self.reminder.deleteLater()
                    self.reminder = None
                    self.current_event = None
                return
            if self.state is None:
                raise CalendarError("提醒记录不可用，请查看程序日志。")
            local = now.time().replace(tzinfo=None)
            # Do not raise a missing-calendar alert every daytime poll.
            at = clock(self.settings["reminder_time"])
            if self.settings["mode"] == "morning":
                if not at <= local < clock("06:00"):
                    return
            elif not (local >= at or local < clock("06:00")):
                return
            day = target_day(now, self.settings)
            calendar = self.calendar_for(day.year)
            options = dict(self.settings, enabled_types=self.active_types(calendar))
            event = due_event(now, options, calendar, self.state.snapshot())
            if event:
                self._show_reminder(calendar, self._deadline(event, calendar), now)
            if now.month >= 11 and not self.calendar_path(now.year+1).exists():
                if self.state.notice(f"next-year:{now.year}"):
                    self.context.notify("垃圾日历尚缺下一年度", "程序会继续检查官网；新年日期未确认时不会假设无需投放。")
        except Exception as error:
            self._error(error)

    def _error(self, error):
        self.status = f"⚠️ {error}"
        key = str(error)
        if key not in self._reported_errors:
            self._reported_errors.add(key)
            self.context.log(self.status, level=logging.ERROR)
            self.context.notify("垃圾提醒需要检查", str(error), critical=True)
        if self.dialog is not None:
            self.dialog.refresh()
        self.refresh_main_panel()

    def _show_reminder(self, calendar, event, now, preview=False):
        from .ui import ReminderDialog
        if self.reminder is not None:
            return
        self.current_event = event
        self.reminder = ReminderDialog(calendar, event, now, self.settings, self.context.parent_widget, preview)
        if self.notices.get("items"):
            latest = self.notices["items"][0]
            title = latest.get("title_zh") or latest.get("zh_subject") or latest['title']
            self.reminder.add_notice(f"官方公告 · {latest['date']} · {title}", latest.get("url"))
        self.reminder.audio.failed.connect(lambda text: self.context.log(text, level=logging.WARNING))
        self.reminder.actionRequested.connect(lambda confirmed: self._complete(confirmed, preview))
        self.reminder.open()
        self.reminder.raise_()
        self.reminder.activateWindow()
        QtWidgets.QApplication.alert(self.reminder, 0)
        self.reminder.play_warning()
        self.context.log(f"{'测试' if preview else '提醒'}：{event.day}，{' + '.join(event.types) or '无已启用收运'}。")

    def _complete(self, confirmed, preview=False):
        if self.reminder is None:
            return
        if confirmed and not self.reminder.confirm.isEnabled():
            return
        try:
            if not preview:
                if not self.state:
                    raise CalendarError("提醒记录未加载，不能保存完成状态。")
                self.state.record(self.current_event, self.now(), confirmed, self.settings["snooze_minutes"])
                self.context.log(f"{self.current_event.day}：{'已确认投放' if confirmed else '已推迟提醒'}。")
            self.reminder.finish_safely()
            self.reminder.deleteLater()
            self.reminder = None
            self.current_event = None
            if self.dialog is not None:
                self.dialog.refresh()
            self.refresh_main_panel()
        except Exception as error:
            self.reminder.error.setText(f"未能保存，请勿关闭：{error}")
            self.context.log(f"保存垃圾提醒状态失败：{error}", level=logging.ERROR)

    def preview_reminder(self, day):
        try:
            calendar = self.calendar_for(day.year)
            values = calendar.collections(day, self.active_types(calendar))
            if values is None:
                raise CalendarError("所选日期没有数据。")
            event = ReminderEvent(day, values, "preview", self.now()+timedelta(minutes=20))
            self._show_reminder(calendar, event, self.now(), preview=True)
        except Exception as error:
            self._error(error)

    def open_dialog(self):
        if self.state is None:
            try:
                self._prepare()
            except Exception as error:
                self._error(error)
        if self.dialog is None:
            from .ui import WasteDashboard
            self.dialog = WasteDashboard(self, self.context.parent_widget)
            self.dialog._tomorrow()
        self.dialog.refresh()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def toggle_type(self, key, checked):
        try:
            calendar = self.calendar_for(self.now().year)
            values = set(self.active_types(calendar))
            values.add(key) if checked else values.discard(key)
            self.settings["enabled_types"] = sorted(values)
            if not self.context.save_config():
                raise OSError("主程序未能保存设置")
            self.refresh_main_panel()
        except Exception as error:
            self._error(error)

    def is_updating(self):
        return self.worker is not None and thread_is_active(self.worker)

    def check_official(self, manual=False, translation_only=False):
        if is_offline():
            self.context.log('离线模式：官网更新和联网翻译暂停，本地日历与提醒仍可使用。')
            return
        if self.is_updating():
            return
        if not translation_only and self.settings["commune"].casefold() != "valle lomellina (pv)":
            self._error(CalendarError("自动解析目前适配 Valle Lomellina；其他 Comune 可使用逐日 JSON 日历。"))
            return
        from .update import OfficialUpdateWorker
        now = self.now()
        self.worker = OfficialUpdateWorker(self.folder, self.settings, (now.year, now.year+1),
                                           self.context.parent_widget, translation_only=translation_only)
        self.worker.progress.connect(self._progress)
        self.worker.failed.connect(self._update_failed)
        self.worker.result.connect(lambda payload: self._updated(payload, manual))
        connect_thread_finished(self.worker, self, 'worker', self._worker_finished)
        self.worker.start()
        self._progress("正在后台翻译已保存的公告；不重复下载日历。" if translation_only else "正在后台检查官方日历；本地提醒正常运行。")

    def _progress(self, text):
        self.status = text
        self.context.log(text)
        if self.dialog is not None:
            self.dialog.refresh()

    def _update_failed(self, text):
        self._error(CalendarError("官网更新失败，已有日历未改动。\n"+text))

    def _updated(self, payload, manual=False):
        try:
            changed_years = []
            for document in payload["documents"]:
                calendar = AnnualCalendar(document)
                path = self.calendar_path(calendar.year)
                previous = AnnualCalendar.load(path) if path.exists() else None
                if previous and previous.dates == calendar.dates:
                    continue
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                if previous:
                    backup = self.folder/"calendar_backups"/f"schedule_{calendar.year}_{stamp}.json"
                    data_file(backup).copy_from(path)
                    changed = [day for day in calendar.dates if previous.dates.get(day) != calendar.dates[day]]
                    self.context.log(f"{calendar.year} 年官方日历变更日期：{', '.join(changed)}；旧版本已备份。")
                # Future years do not silently inherit unverified old sorting rules.
                document.setdefault("guidance", {"rules": []})
                data_file(path).write_json(document, indent=2)
                self._calendars.pop(calendar.year, None)
                changed_years.append(calendar.year)
            self.status = ("已完成已保存公告的中文翻译检查。" if payload.get("translation_only") else
                           f"已解析并更新年度：{changed_years}。原始 PDF 和旧版本均已保存。" if changed_years else
                           f"官网检查完成：已发布年度 {payload['available_years']}；本地日期没有变化。")
            self.context.log(self.status)
            if changed_years or manual:
                self.context.notify("垃圾日历官网检查", self.status)
            self._reported_errors.clear()
            if payload.get("notices") is not None:
                previous = {x["id"] for x in self.notices.get("items", [])}
                new_items = [x for x in payload["notices"] if x["id"] not in previous]
                # Keep older items as well: a website listing becoming shorter isn't a retraction.
                merged = {x["id"]: x for x in self.notices.get("items", [])}
                merged.update({x["id"]: x for x in payload["notices"]})
                self.notices = {"checked_at": payload["notices_checked_at"],
                                "items": sorted(merged.values(), key=lambda x: x["date"], reverse=True)}
                self._write_notices()
                if new_items:
                    self.context.notify("Valle Lomellina 官方新消息", "\n".join(x.get("title_zh") or x.get("zh_subject") or x["title"] for x in new_items[:3]),
                                        critical=any(x["alert"] for x in new_items))
                    self.context.log("官方新公告："+" / ".join(x["title"] for x in new_items))
                translation_errors = [x for x in payload["notices"] if x.get("translation_error")]
                if translation_errors:
                    self.status += f" {len(translation_errors)} 条公告翻译未完成，可在最新消息中重试。"
                    self.context.log(self.status, level=logging.WARNING)
            self.refresh_main_panel()
            if payload.get("errors"):
                self._error(CalendarError("\n".join(payload["errors"])))
        except Exception as error:
            self._error(error)

    def _write_notices(self):
        data_file(self.folder/"notices.json").write_json(self.notices, indent=2)

    def _worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        if self.dialog:
            self.dialog.refresh()
        self.refresh_main_panel()

    def apply_settings(self, config):
        self.cooking.apply_settings(config)
        if self.is_updating() or self.is_weather_updating():
            self.context.log("官网/天气查询正在进行，完成前保留当前助手设置。")
            return False
        self.settings = normalize_settings(config.get(CONFIG_KEY))
        self._calendars.clear()
        self._prepare()
        self.zone()
        if not self.settings["enabled"] and self.reminder:
            self.reminder.finish_safely()
            self.reminder = None
            self.current_event = None
        if self.dialog:
            self.dialog.refresh()
        self.refresh_main_panel()
        return True

    def update_config(self, config):
        config[CONFIG_KEY] = normalize_settings(self.settings)
        return config

    def is_weather_updating(self):
        # Retain the worker until native cleanup and queued results finish.
        return self.weather_worker is not None

    def refresh_weather_views(self):
        if self.dialog is not None:
            self.dialog.weather.refresh()
        self.refresh_main_panel()

    def change_weather_options(self, **updates):
        original = self.settings["weather"]
        value = weather.options({**original, **updates}, self.settings["commune"])
        self.settings["weather"] = value
        try:
            if not self.context.save_config():
                raise OSError("天气设置未保存，请重试。")
        except Exception:
            self.settings["weather"] = original
            raise
        if set(updates) <= {"chart_hours"}:
            # Changing the view is local: no extra request or refresh backoff reset.
            self.refresh_weather_views()
            return
        if not value["enabled"] and self.weather_worker:
            self.weather_worker.requestInterruption()
        if value != original:
            self.weather_next_attempt = 0
        self.weather_status = "天气联网已关闭，已有缓存仍可查看。" if not value["enabled"] else (
            "天气已启用；点击刷新查询当前城市。" if value["location"] else "请先搜索并选择城市，不要填写详细住址。")
        self.refresh_weather_views()

    def _start_weather(self, mode, payload):
        if self.is_weather_updating() or is_offline() or not self.settings["weather"]["enabled"]:
            return False
        self.weather_status = "正在搜索城市…" if mode == "search" else "正在查询城市天气…"
        self.weather_worker = weather.WeatherWorker(mode, payload, self.context.parent_widget)
        self.weather_worker.result.connect(self._weather_updated, QtCore.Qt.ConnectionType.QueuedConnection)
        self.weather_worker.failed.connect(self._weather_failed, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.weather_worker, self, "weather_worker", self._weather_finished, pass_thread=True)
        self.weather_worker.start()
        self.refresh_weather_views()
        return True

    def search_weather_city(self, query):
        return self._start_weather("search", query)

    def refresh_weather(self, manual=False):
        options = self.settings["weather"]
        if is_offline():
            self.weather_status = "离线模式：不查询天气，仅查看上次缓存。"
            if manual:
                self.refresh_weather_views()
            return False
        if not options["enabled"] or not options["location"] or self.is_weather_updating():
            return False
        same = self.weather_data and weather.location_key(self.weather_data["location"]) == weather.location_key(options["location"])
        now = time.time()
        if not manual and (now < self.weather_next_attempt or (same and now - self.weather_data["checked_at"] < options["refresh_minutes"] * 60)):
            return False
        self.weather_next_attempt = now + options["refresh_minutes"] * 60
        return self._start_weather("forecast", options["location"])

    def _weather_updated(self, payload):
        try:
            if not self.settings["weather"]["enabled"] or not self.weather_worker or self.weather_worker.isInterruptionRequested():
                return
            if payload["mode"] == "search":
                self.weather_cities = payload["data"]
                self.weather_status = f"找到 {len(self.weather_cities)} 个城市，选择后点击“使用该城市”。"
                if self.dialog:
                    self.dialog.weather.show_cities(self.weather_cities)
            else:
                data = payload["data"]
                if weather.location_key(data["location"]) != weather.location_key(self.settings["weather"]["location"]):
                    return
                self.weather_data = data
                weather.save_cache(self.folder/"weather-open-meteo.json", data)
                self.weather_status = "天气已更新。预报仅供参考，不改变收运日期。"
            self.refresh_weather_views()
        except Exception as error:
            self._weather_failed("天气已获取，但保存或显示失败：" + str(error))

    def _weather_failed(self, text):
        self.weather_status = text + "（保留上次成功缓存；不会当作最新预报）"
        self.weather_next_attempt = time.time() + 15 * 60
        self.context.log("天气查询：" + text, level=logging.WARNING)
        self.refresh_weather_views()

    def _weather_finished(self, worker):
        if worker is self.weather_worker:
            self.weather_worker = None
            if worker.isInterruptionRequested():
                self.weather_status = "天气查询已取消，已有缓存保留。"
            self.refresh_weather_views()

    def weather_summary(self):
        if not self.settings["weather"]["enabled"]:
            return "天气未启用；在下方助手的“天气”分页开启，不上传素材。"
        selected, data = self.settings["weather"]["location"], self.weather_data
        if not data or not selected or weather.location_key(data["location"]) != weather.location_key(selected):
            return self.weather_status
        current = data["current"]
        age = time.time() - data["checked_at"]
        cached = "（缓存已过期）" if age >= self.settings["weather"]["refresh_minutes"] * 60 or is_offline() else ""
        return f"天气 · {selected['name']}：{weather.LABELS.get(current['weather_code'], '未知')} {weather.number(current['temperature_2m'], '℃')} {cached}\n" + weather.tonight_summary(data, self.now())

    def can_close(self):
        allowed, message = self.cooking.can_close()
        if not allowed:
            return allowed, message
        if self.is_weather_updating():
            self.weather_worker.requestInterruption()
            return False, "城市天气查询正在安全停止，稍后再退出。"
        if self.is_updating():
            self.worker.requestInterruption()
            return False, "垃圾日历后台检查正在停止，稍后再退出。"
        if self.reminder is not None and self.reminder.isVisible():
            return False, "请先在垃圾提醒中确认投放，或选择推迟。"
        return True, ""

    def stop(self):
        if self.cooking.can_close()[0] is False:
            return False
        if self.is_weather_updating():
            self.weather_worker.requestInterruption()
            return False
        if self.timer:
            self.timer.stop()
        if self.worker:
            self.worker.requestInterruption()
        if self.reminder:
            self.reminder.finish_safely()
            self.reminder = None
        if self.dialog:
            self.dialog.hide()
        self.cooking.stop()
        for dialog in list(self.details_dialogs):
            dialog.close()

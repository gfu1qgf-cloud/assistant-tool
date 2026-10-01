from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile

from qt_compat import QtCore, QtWidgets
from app_paths import APP_ROOT
from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage, PluginMainWidget
from .calendar import AnnualCalendar, CalendarError, ReminderEvent, due_event, target_day, clock
from .settings import CONFIG_KEY, WasteSettingsPage, normalize_settings
from .state import ReminderState

BUNDLED_DATA = Path(__file__).parent / "data"


class WasteReminderPlugin:
    plugin_id = "waste_reminder"
    display_name = "垃圾分类每日提醒"
    version = "1.1"
    required_api_version = 1

    def __init__(self, data_root=None):
        self.context = None
        self.root = Path(data_root) if data_root else Path(APP_ROOT)
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

    def register(self, context):
        self.context = context
        self.settings = normalize_settings(context.load_config().get(CONFIG_KEY))
        context.register_command(PluginCommand("open", "垃圾分类每日提醒…", lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), tooltip="今晚扔什么、官方日历、分类小贴士与自动更新", order=19))
        context.register_settings_page(PluginSettingsPage("settings", "垃圾分类提醒", WasteSettingsPage, order=219))
        context.register_main_widget(PluginMainWidget("tonight", self.create_main_panel, order=60, title="今晚垃圾投放"))

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
        dialog.destroyed.connect(lambda: self.details_dialogs.remove(dialog) if dialog in self.details_dialogs else None)
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
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.settings["commune"].casefold() == "valle lomellina (pv)":
            for path in BUNDLED_DATA.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(BUNDLED_DATA)
                    target = self.folder/relative
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
        self.state = ReminderState(self.folder/"state.json")
        notices_path = self.folder/"notices.json"
        if notices_path.is_file():
            self.notices = json.loads(notices_path.read_text(encoding="utf-8"))

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
        self.refresh_main_panel()
        QtCore.QTimer.singleShot(5000, self.tick)

    def tick(self):
        if self.timer is None or not self.timer.isActive():
            return
        try:
            now = self.now()
            self.refresh_main_panel()
            if self.settings.get("translate_news", True) and not self.is_updating() and self.state:
                from .translation import needs_translation
                if any(needs_translation(x) for x in self.notices.get("items", [])[:20]):
                    if self.state.notice("translate-attempt:"+now.date().isoformat()):
                        self.check_official(translation_only=True)
            if self.settings["auto_update"] and not self.is_updating() and self.state:
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
        return self.worker is not None and self.worker.isRunning()

    def check_official(self, manual=False, translation_only=False):
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
        self.worker.finished.connect(self._worker_finished)
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
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
                    changed = [day for day in calendar.dates if previous.dates.get(day) != calendar.dates[day]]
                    self.context.log(f"{calendar.year} 年官方日历变更日期：{', '.join(changed)}；旧版本已备份。")
                # Future years do not silently inherit unverified old sorting rules.
                document.setdefault("guidance", {"rules": []})
                self.folder.mkdir(parents=True, exist_ok=True)
                handle, tmp = tempfile.mkstemp(prefix="calendar-", suffix=".tmp", dir=self.folder)
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        json.dump(document, stream, ensure_ascii=False, indent=2)
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
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
        handle, tmp = tempfile.mkstemp(prefix="notices-", suffix=".tmp", dir=self.folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.notices, stream, ensure_ascii=False, indent=2)
            os.replace(tmp, self.folder/"notices.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        if self.dialog:
            self.dialog.refresh()
        self.refresh_main_panel()

    def apply_settings(self, config):
        if self.is_updating():
            self.context.log("官网检查正在进行，完成前保留当前垃圾提醒设置。")
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

    def can_close(self):
        if self.is_updating():
            self.worker.requestInterruption()
            return False, "垃圾日历后台检查正在停止，稍后再退出。"
        if self.reminder is not None and self.reminder.isVisible():
            return False, "请先在垃圾提醒中确认投放，或选择推迟。"
        return True, ""

    def stop(self):
        if self.timer:
            self.timer.stop()
        if self.worker:
            self.worker.requestInterruption()
        if self.reminder:
            self.reminder.finish_safely()
            self.reminder = None
        if self.dialog:
            self.dialog.hide()
        for dialog in list(self.details_dialogs):
            dialog.close()

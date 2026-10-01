import calendar as std_calendar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets, QtTest
from app_plugins.builtin.waste_reminder.calendar import (
    AnnualCalendar, CalendarError, ReminderEvent, due_event, message,
)
from app_plugins.builtin.waste_reminder.settings import normalize_settings, WasteSettingsPage
from app_plugins.builtin.waste_reminder.plugin import WasteReminderPlugin, BUNDLED_DATA
from app_plugins.builtin.waste_reminder.parser import ParseError, parse_pdf, read_table
from app_plugins.builtin.waste_reminder.state import ReminderState
from app_plugins.builtin.waste_reminder.ui import ReminderDialog
from app_plugins.builtin.waste_reminder.update import allowed_url, discover_calendars, parse_notices

DOCUMENT = json.loads((BUNDLED_DATA/"schedule_2026.json").read_text(encoding="utf-8"))


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = AnnualCalendar(deepcopy(DOCUMENT))
        self.options = normalize_settings({})
        self.now = datetime(2026, 10, 1, 22, 0, tzinfo=timezone(timedelta(hours=2)))
        self.state = {"events": {}}

    def test_all_days_explicit(self):
        self.assertEqual(len(self.calendar.dates), 365)
        day = date(2026, 1, 1)
        while day.year == 2026:
            self.assertIsNotNone(self.calendar.collections(day))
            day += timedelta(days=1)

    def test_holidays_and_combined_types(self):
        for day in ("2026-01-01", "2026-12-25", "2026-12-26"):
            self.assertEqual(self.calendar.collections(date.fromisoformat(day)), ())
        self.assertEqual(set(self.calendar.collections(date(2026, 12, 24))), {"umido", "plastica"})
        self.assertEqual(set(self.calendar.collections(date(2026, 1, 26))), {"umido", "verde"})

    def test_no_weekday_extrapolation(self):
        self.assertIsNone(self.calendar.collections(date(2027, 1, 1)))
        del self.calendar.dates["2026-10-02"]
        with self.assertRaises(CalendarError):
            due_event(self.now, self.options, self.calendar, self.state)

    def test_due_exactly_at_22(self):
        self.assertIsNone(due_event(self.now-timedelta(seconds=1), self.options, self.calendar, self.state))
        event = due_event(self.now, self.options, self.calendar, self.state)
        self.assertEqual(event.day, date(2026, 10, 2))
        self.assertEqual(event.types, ("plastica",))

    def test_midnight_catchup_and_cutoff(self):
        event = due_event(self.now+timedelta(hours=3), self.options, self.calendar, self.state)
        self.assertEqual(event.day, date(2026, 10, 2))
        self.assertIsNone(due_event(self.now+timedelta(hours=8), self.options, self.calendar, self.state))

    def test_confirmed_does_not_repeat(self):
        event = due_event(self.now, self.options, self.calendar, self.state)
        self.state["events"][event.key] = {"confirmed": True}
        self.assertIsNone(due_event(self.now, self.options, self.calendar, self.state))

    def test_snooze_and_expiry(self):
        event = due_event(self.now, self.options, self.calendar, self.state)
        self.state["events"][event.key] = {"snooze_until": self.now.timestamp()+15*60}
        self.assertIsNone(due_event(self.now+timedelta(minutes=14), self.options, self.calendar, self.state))
        self.assertIsNotNone(due_event(self.now+timedelta(minutes=15), self.options, self.calendar, self.state))

    def test_changed_types_re_remind(self):
        event = due_event(self.now, self.options, self.calendar, self.state)
        self.state["events"][event.key] = {"confirmed": True}
        self.calendar.dates["2026-10-02"].append("carta")
        self.assertIsNotNone(due_event(self.now, self.options, self.calendar, self.state))

    def test_morning_mode(self):
        options = normalize_settings({"mode": "morning", "reminder_time": "05:30"})
        now = self.now.replace(day=2, hour=5, minute=30)
        self.assertIsNotNone(due_event(now, options, self.calendar, self.state))
        self.assertIsNone(due_event(now-timedelta(minutes=1), options, self.calendar, self.state))
        self.assertIsNone(due_event(now.replace(hour=6), options, self.calendar, self.state))

    def test_empty_optional(self):
        now = self.now.replace(day=3)
        self.assertIsNone(due_event(now, self.options, self.calendar, self.state))
        self.options["notify_empty"] = True
        event = due_event(now, self.options, self.calendar, self.state)
        self.assertEqual(event.types, ())

    def test_disabled(self):
        self.options["enabled"] = False
        self.assertIsNone(due_event(self.now, self.options, self.calendar, self.state))

    def test_message_language_and_midnight(self):
        event = due_event(self.now, self.options, self.calendar, self.state)
        self.assertIn("明天", message(self.calendar, event, self.now))
        self.assertIn("今天", message(self.calendar, event, self.now+timedelta(hours=3)))
        self.assertIn("Domani", message(self.calendar, event, self.now, "it"))
        self.assertIn("06:00", message(self.calendar, event, self.now))
        self.assertEqual(self.calendar.label("carta", "zh"), "📦 纸和纸板")

    def test_bad_calendar_rejected(self):
        for mutation in (lambda d: d.update(year=2027),
                         lambda d: d["dates"].update({"2026-02-30": []}),
                         lambda d: d["dates"].update({"2026-10-02": ["unknown"]}),
                         lambda d: d["dates"].update({"2026-10-02": ["carta", "carta"]}),
                         lambda d: d.update(schema_version=8)):
            document = deepcopy(DOCUMENT)
            mutation(document)
            with self.assertRaises(CalendarError):
                AnnualCalendar(document)

    def test_sources_intact(self):
        for source in DOCUMENT["sources"]:
            self.assertEqual(hashlib.sha256((BUNDLED_DATA/source["file"]).read_bytes()).hexdigest(), source["sha256"])


class ParserTests(unittest.TestCase):
    def _first_half(self):
        from PIL import Image
        from pypdf import PdfReader
        return next(Image.open(io.BytesIO(x.data)).convert('RGB') for x in
                    PdfReader(BUNDLED_DATA/'sources/valle_lomellina_2026.pdf').pages[0].images
                    if x.image.width == 1234)

    def test_grid_rejects_swapped_months(self):
        with self.assertRaises(ParseError):
            read_table(self._first_half(), 2026, 7)

    def test_grid_can_parse_future_year_and_leap_day(self):
        from PIL import Image, ImageDraw
        from app_plugins.builtin.waste_reminder.parser import grid_lines, PALETTE
        original = self._first_half()
        hs, vs = grid_lines(original, 'h'), grid_lines(original, 'v')
        sprites = {}
        for day in range(1, 8):
            sprites[date(2026, 1, day).weekday()] = original.crop((vs[1]+3, hs[day]+3, vs[2]-3, hs[day+1]-2))
        for year in (2027, 2028):
            image = original.copy()
            painter = ImageDraw.Draw(image)
            for column in range(6):
                month = column+1
                for day in range(1, 32):
                    rect = (vs[column*3+1]+3, hs[day]+3, vs[column*3+2]-3, hs[day+1]-2)
                    painter.rectangle(rect, fill=(255, 255, 255))
                    if day <= std_calendar.monthrange(year, month)[1]:
                        sprite = sprites[date(year, month, day).weekday()]
                        image.paste(sprite.resize((rect[2]-rect[0], rect[3]-rect[1]), Image.Resampling.NEAREST), rect[:2])
                        if month == 2 and day == 29:
                            painter.rectangle((vs[column*3+2]+2, hs[day]+2, vs[column*3+3]-2, hs[day+1]-1), fill=PALETTE['carta'])
            parsed = read_table(image, year, 1)
            self.assertIn(f'{year}-01-01', parsed)
            self.assertEqual(f'{year}-02-29' in parsed, year == 2028)

    def test_runtime_parser_matches_all_365_crosschecked_dates(self):
        parsed = parse_pdf((BUNDLED_DATA/"sources/valle_lomellina_2026.pdf").read_bytes(), 2026)
        self.assertEqual(parsed["dates"], DOCUMENT["dates"])

    def test_wrong_year_wrong_commune_not_pdf(self):
        pdf = (BUNDLED_DATA/"sources/valle_lomellina_2026.pdf").read_bytes()
        for year, commune in ((2027, "Valle Lomellina"), (2026, "Lomello")):
            with self.assertRaises(ParseError):
                parse_pdf(pdf, year, commune)
        with self.assertRaises(ParseError):
            parse_pdf(b"<html>failure</html>", 2026)

    def test_calendar_link_discovery_not_hardcoded_year(self):
        html = '''<a href="https://teknoserviceitalia.b-cdn.net/wp-content/uploads/2026/12/Calendario-raccolta-2027-VALLE-LOMELLINA.pdf">pdf</a>
        <a href="https://bad.example/Calendario-2028-Valle-Lomellina.pdf">bad</a>
        <a href="https://www.teknoserviceitalia.com/Calendario-2027-Lomello.pdf">other</a>'''
        links = discover_calendars(html)
        self.assertEqual(list(links), [2027])

    def test_actual_official_html(self):
        html = (BUNDLED_DATA/"sources/teknoservice_local_page.html").read_text(encoding="utf-8")
        self.assertIn(2026, discover_calendars(html))

    def test_notice_filter_exact_comune(self):
        html = (BUNDLED_DATA/"sources/teknoservice_notices.html").read_text(encoding="utf-8")
        cards = parse_notices(html)
        self.assertTrue(any("sacchi neri" in x["title"].lower() for x in cards))
        self.assertTrue(all(x["local"] for x in cards))
        self.assertFalse(any("Arborea" in x["title"] for x in cards))

    def test_unknown_notice_layout_rejected(self):
        with self.assertRaises(ParseError):
            parse_notices("<p>Layout changed</p>")

    def test_only_https_official_downloads(self):
        self.assertFalse(allowed_url("http://www.teknoserviceitalia.com/a.pdf"))
        self.assertFalse(allowed_url("https://127.0.0.1/private"))
        self.assertFalse(allowed_url("https://www.teknoserviceitalia.com.evil.test/a.pdf"))
        self.assertFalse(allowed_url("https://user@www.teknoserviceitalia.com/a.pdf"))


class StateTests(unittest.TestCase):
    def test_persist_confirmation_and_snooze(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            state = ReminderState(path)
            now = datetime(2026, 10, 1, 22, tzinfo=timezone.utc)
            event = ReminderEvent(date(2026, 10, 2), ("plastica",), "test", now+timedelta(hours=8))
            state.record(event, now, False, 20)
            record = ReminderState(path).snapshot()["events"]["test"]
            self.assertFalse(record["confirmed"])
            self.assertEqual(record["snooze_until"], now.timestamp()+1200)
            state.record(event, now, True)
            self.assertTrue(ReminderState(path).snapshot()["events"]["test"]["confirmed"])
            self.assertTrue(state.notice("new"))
            self.assertFalse(ReminderState(path).notice("new"))

    def test_write_failure_preserves_memory_and_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            state = ReminderState(Path(folder)/"state.json")
            state.notice("old")
            old = state.path.read_bytes()
            with patch("app_plugins.builtin.waste_reminder.state.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    state.notice("new")
            self.assertEqual(state.path.read_bytes(), old)
            self.assertNotIn("new", state.snapshot()["notices"])

    def test_corrupt_file_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                ReminderState(path)
            self.assertEqual(path.read_text(), "broken")


class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = QtWidgets.QWidget()
        self.plugin = WasteReminderPlugin(self.temp.name)
        self.context = SimpleNamespace(parent_widget=self.parent, load_config=lambda: {},
                                       log=Mock(), notify=Mock(), save_config=Mock(return_value=True),
                                       register_command=Mock(), register_settings_page=Mock(), register_main_widget=Mock())
        self.plugin.register(self.context)
        self.plugin._prepare()

    def tearDown(self):
        self.plugin.stop()
        if self.plugin.dialog:
            self.plugin.dialog.deleteLater()
        self.parent.deleteLater()
        self.temp.cleanup()

    def test_plugin_registers_menu_and_settings(self):
        self.context.register_command.assert_called_once()
        self.context.register_settings_page.assert_called_once()
        self.context.register_main_widget.assert_called_once()

    def test_original_user_calendar_never_overwritten(self):
        path = self.plugin.calendar_path(2026)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dates"]["2026-10-02"] = ["carta"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.plugin._prepare()
        self.assertEqual(self.plugin.calendar_for(2026).dates["2026-10-02"], ["carta"])

    def test_optional_pannolini_off_but_data_retained(self):
        calendar = self.plugin.calendar_for(2026)
        self.assertNotIn("pannolini", self.plugin.active_types(calendar))
        self.assertEqual(calendar.collections(date(2026, 10, 3)), ("pannolini",))

    def test_dst_deadline_uses_morning_offset(self):
        now = datetime(2026, 10, 24, 22, tzinfo=timezone(timedelta(hours=2)))
        event = ReminderEvent(date(2026, 10, 25), (), "dst", now)
        deadline = self.plugin._deadline(event, self.plugin.calendar_for(2026)).deadline
        self.assertEqual(deadline.hour, 6)
        self.assertEqual(deadline.utcoffset(), timedelta(hours=1))

    def test_dialog_blocks_escape_and_countdown(self):
        now = datetime(2026, 10, 1, 22, tzinfo=timezone.utc)
        calendar = self.plugin.calendar_for(2026)
        event = ReminderEvent(date(2026, 10, 2), ("plastica",), "test", now)
        dialog = ReminderDialog(calendar, event, now, normalize_settings({"sound": False}), self.parent)
        dialog.show()
        self.assertEqual(dialog.windowModality(), QtCore.Qt.WindowModality.ApplicationModal)
        dialog.check.setChecked(True)
        self.assertFalse(dialog.confirm.isEnabled())
        QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)
        dialog.close()
        self.assertTrue(dialog.isVisible())
        dialog.elapsed = SimpleNamespace(elapsed=lambda: 30001)
        dialog._countdown()
        self.assertTrue(dialog.confirm.isEnabled())
        emitted = []
        dialog.actionRequested.connect(emitted.append)
        dialog.snooze.click()
        self.assertEqual(emitted, [False])
        dialog.finish_safely()
        self.assertFalse(dialog.isVisible())
        dialog.deleteLater()

    def test_dashboard_and_settings_render(self):
        with patch.object(self.plugin, "now", return_value=datetime(2026, 10, 1, 18, tzinfo=timezone.utc)):
            dialog = self.plugin.open_dialog()
            self.assertEqual(dialog.week.rowCount(), 7)
            self.assertIn("Plastica", dialog.query.text())
            self.assertEqual(dialog.sources.count(), 5)
        page = WasteSettingsPage()
        page.load_config({})
        result = page.update_config({})["waste_reminder"]
        self.assertEqual(result["reminder_time"], "22:00")
        self.assertTrue(result["auto_update"])
        page.mode.setCurrentIndex(1)
        self.assertEqual(page.at.time().toString("HH:mm"), "05:30")
        page.widget.deleteLater()

    def test_official_update_preserves_unchanged_user_guidance(self):
        before = self.plugin.calendar_path(2026).read_bytes()
        self.plugin._updated({"documents": [deepcopy(DOCUMENT)], "available_years": [2026]})
        self.assertEqual(self.plugin.calendar_path(2026).read_bytes(), before)

    def test_next_year_does_not_require_code_changes(self):
        data = deepcopy(DOCUMENT)
        data["year"] = 2027
        data["dates"] = {"2027-"+k[5:]: v for k, v in data["dates"].items()}
        data["sources"] = []
        self.plugin._updated({"documents": [data], "available_years": [2026, 2027]})
        self.assertEqual(self.plugin.calendar_for(2027).year, 2027)

    def test_calendar_changes_backed_up(self):
        data = deepcopy(DOCUMENT)
        data["dates"]["2026-10-02"] = ["carta"]
        self.plugin._updated({"documents": [data], "available_years": [2026]})
        self.assertEqual(self.plugin.calendar_for(2026).dates["2026-10-02"], ["carta"])
        self.assertEqual(len(list((self.plugin.folder/"calendar_backups").glob("*.json"))), 1)

    def test_new_notices_and_no_duplicate_notifications(self):
        card = {"id": "one", "title": "test notice", "date": "2026-10-01", "alert": True}
        payload = {"documents": [], "available_years": [2026], "notices": [card],
                   "notices_checked_at": "2026-10-01T17:00:00Z"}
        self.plugin._updated(payload)
        notifications = self.context.notify.call_count
        self.plugin._updated(payload)
        self.assertEqual(self.context.notify.call_count, notifications)
        self.plugin._prepare()
        self.assertEqual(self.plugin.notices["items"][0]["id"], "one")

    def test_setting_change_does_not_reload_ai_models(self):
        value = normalize_settings({"reminder_time": "22:30"})
        self.assertTrue(self.plugin.apply_settings({"waste_reminder": value}))
        self.assertEqual(self.plugin.settings["reminder_time"], "22:30")

    def test_main_panel_tonight_and_after_midnight(self):
        with patch.object(self.plugin, "now", return_value=datetime(2026, 10, 1, 18, tzinfo=timezone.utc)):
            panel = self.plugin.create_main_panel(self.parent)
            self.assertIn("10-02", panel.title.text())
            self.assertEqual(panel.items.count(), len(self.plugin.calendar_for(2026).collections(date(2026, 10, 2))))
            self.assertIn("还没到", panel.note.text())
            self.assertEqual(panel.items.itemAt(0).widget().contextMenuPolicy(), QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        with patch.object(self.plugin, "now", return_value=datetime(2026, 10, 2, 1, tzinfo=timezone.utc)):
            panel.refresh()
            self.assertIn("10-03", panel.title.text())
            self.assertIn("专项", panel.note.text())

    def test_main_panel_missing_data_not_no_collection(self):
        with patch.object(self.plugin, "now", return_value=datetime(2026, 12, 31, 18, tzinfo=timezone.utc)):
            panel = self.plugin.create_main_panel(self.parent)
            self.assertIn("需要检查", panel.title.text())
            self.assertNotIn("无需", panel.note.text())

    def test_details_examples_and_original_sources(self):
        from app_plugins.builtin.waste_reminder.details import category_data
        data = category_data()
        self.assertEqual(set(data["categories"]), set(DOCUMENT["types"]))
        self.assertGreater(sum(len(x["yes"])+len(x["no"]) for x in data["categories"].values()), 100)
        dialog = self.plugin.show_category_details(self.plugin.calendar_for(2026), "plastica")
        self.assertIn("塑料玩具", dialog.text.toPlainText())
        self.assertIn("半透明黄色袋", dialog.text.toPlainText())
        dialog.category.setCurrentIndex(dialog.category.findData("secco"))
        self.assertIn("黑袋", dialog.text.toPlainText())
        dialog.close()

    def test_news_chinese_before_original(self):
        self.plugin.notices["items"] = [{"id": "cn", "title": "Avviso originale", "title_zh": "中文标题",
            "date": "2026-10-01", "alert": True, "text": "Originale italiano", "text_zh": "中文译文",
            "translation_provider": "Google"}]
        with patch.object(self.plugin, "now", return_value=datetime(2026, 10, 1, 18, tzinfo=timezone.utc)):
            dialog = self.plugin.open_dialog()
        text = dialog.news_text.toPlainText()
        self.assertLess(text.index("中文译文"), text.index("Originale italiano"))
        self.assertIn("中文标题", dialog.news_list.item(0).text())

    def test_song_settings_round_trip(self):
        page = WasteSettingsPage()
        page.load_config({"waste_reminder": {"sound_file": "C:/music/song.mp3", "sound_volume": 37}})
        value = page.update_config({})["waste_reminder"]
        self.assertEqual(value["sound_file"], "C:/music/song.mp3")
        self.assertEqual(value["sound_volume"], 37)
        self.assertTrue(value["translate_news"])
        page.widget.deleteLater()

    def test_song_loop_and_stop(self):
        from app_plugins.builtin.waste_reminder.sound import ReminderAudio
        path = Path(self.temp.name)/"song.mp3"
        path.write_bytes(b"test")
        audio = ReminderAudio(self.parent)
        with patch("app_plugins.builtin.waste_reminder.sound.QtMultimedia") as multimedia:
            multimedia.QMediaPlayer.Loops.Infinite = -1
            player = multimedia.QMediaPlayer.return_value
            audio.start(normalize_settings({"sound_file": str(path), "sound_volume": 42}))
            player.setLoops.assert_called_once_with(-1)
            multimedia.QAudioOutput.return_value.setVolume.assert_called_once_with(.42)
            player.play.assert_called_once()
            audio.stop()
            self.assertFalse(audio.active)
            self.assertFalse(audio.beep_timer.isActive())
            player.setSource.assert_called_with(QtCore.QUrl())

    def test_bad_song_fallback_is_repeated_and_stops(self):
        from app_plugins.builtin.waste_reminder.sound import ReminderAudio
        audio = ReminderAudio(self.parent)
        errors = []
        audio.failed.connect(errors.append)
        with patch.object(audio, "beep"):
            audio.start(normalize_settings({"sound_file": "missing-file.mp3"}))
            self.assertTrue(audio.beep_timer.isActive())
            self.assertIn("不存在", errors[0])
            audio.stop()
            self.assertFalse(audio.beep_timer.isActive())


class TranslationTests(unittest.TestCase):
    def test_spaced_pdf_and_normal_text(self):
        from app_plugins.builtin.waste_reminder.translation import readable_text
        self.assertEqual(readable_text("R i c o r d i a m o  a i  c i t t a d i ni  c he  i l  r i f i u t o"),
                         "Ricordiamo ai cittadini che il rifiuto")
        self.assertEqual(readable_text("Usare sacchi semitrasparenti"), "Usare sacchi semitrasparenti")

    def test_utf8_chunks_preserve_text(self):
        from app_plugins.builtin.waste_reminder.translation import chunks
        text = "È già così: più qualità per la città. "*100
        parts = list(chunks(text))
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(x.encode()) <= 450 for x in parts))

    def test_cache_persists_and_body_change_retranslated(self):
        from app_plugins.builtin.waste_reminder.translation import NoticeTranslator, needs_translation
        with tempfile.TemporaryDirectory() as folder, patch(
                "app_plugins.builtin.waste_reminder.translation.translate_remote", side_effect=lambda text: ("中"+text, "fake")) as remote:
            translator = NoticeTranslator(folder)
            card = {"id": "a", "title": "Titolo", "text": "Testo"}
            result = translator.translate_card(card)
            self.assertEqual(remote.call_count, 2)
            self.assertFalse(needs_translation(result))
            NoticeTranslator(folder).translate_card(card)
            self.assertEqual(remote.call_count, 2)
            result["text"] = "Testo nuovo"
            self.assertTrue(needs_translation(result))
            result = NoticeTranslator(folder).translate_card(result)
            self.assertEqual(remote.call_count, 3)
            self.assertIn("nuovo", result["text_zh"])

    def test_local_waste_glossary_not_bathtub_and_original_preserved(self):
        from app_plugins.builtin.waste_reminder.translation import NoticeTranslator
        with tempfile.TemporaryDirectory() as folder, patch(
                "app_plugins.builtin.waste_reminder.translation.translate_remote", return_value=("译文", "fake")) as remote:
            card = {"title": "Avviso", "text": "Utilizzare il mastello\ncon sacchi neri."}
            result = NoticeTranslator(folder).translate_card(card)
            source = remote.call_args.args[0]
            self.assertIn("contenitore per i rifiuti", source)
            self.assertIn("sacchi neri per i rifiuti", source)
            self.assertNotIn("\n", source)
            self.assertEqual(result["text"], card["text"])

    def test_failure_preserves_original_and_retry(self):
        from app_plugins.builtin.waste_reminder.translation import NoticeTranslator, needs_translation
        with tempfile.TemporaryDirectory() as folder, patch(
                "app_plugins.builtin.waste_reminder.translation.translate_remote", side_effect=OSError("network")):
            result = NoticeTranslator(folder).translate_card({"title": "Titolo", "text": "Testo"})
            self.assertEqual(result["text"], "Testo")
            self.assertIn("translation_error", result)
            self.assertTrue(needs_translation(result))

    def test_translation_only_does_not_fetch_calendar(self):
        from app_plugins.builtin.waste_reminder.update import OfficialUpdateWorker
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/"notices.json").write_text(json.dumps({"checked_at": "yesterday", "items": [
                {"id": "a", "title": "Titolo", "text": "Testo"}]}), encoding="utf-8")
            worker = OfficialUpdateWorker(folder, normalize_settings(), [2026], translation_only=True)
            results = []
            worker.result.connect(results.append)
            with patch("app_plugins.builtin.waste_reminder.update.fetch") as fetch, patch(
                    "app_plugins.builtin.waste_reminder.translation.translate_remote", return_value=("中文", "fake")):
                worker.run()
                fetch.assert_not_called()
            self.assertEqual(results[0]["notices"][0]["text_zh"], "中文")
            self.assertEqual(results[0]["notices_checked_at"], "yesterday")


if __name__ == "__main__":
    unittest.main()

from datetime import datetime, timezone
import gc
import os
from pathlib import Path
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets, QtTest
from tests import test_waste_reminder as base
from tests.test_waste_weather import snapshot
from app_plugins.builtin.waste_reminder.scene import forecast_for_day, weather_scene
from app_plugins.builtin.waste_reminder.scene import WeatherSceneDialog


class LifeDesignTests(base.QtTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Offscreen Qt cannot always enumerate Windows fonts automatically.
        for name in ("msyh.ttc", "segoeui.ttf", "seguiemj.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.is_file():
                QtGui.QFontDatabase.addApplicationFont(str(path))
        cls.app.setFont(QtGui.QFont("Microsoft YaHei", 10))

    def prepare_weather(self):
        data = snapshot()
        for row, code in zip(data["daily"], (0, 3, 63, 75, 95, 45, 2)):
            row["weather_code"] = code
        self.plugin.weather_data = data
        self.plugin.settings["weather"]["enabled"] = True
        self.plugin.now = lambda: datetime(2026, 10, 9, 18, tzinfo=timezone.utc)
        return data

    def test_switching_all_pages_keeps_tabs_and_header_in_exact_same_place(self):
        self.prepare_weather()
        dialog = self.plugin.open_dialog()
        dialog.show()
        self.app.processEvents()
        before = QtCore.QRect(dialog.tabs.geometry())
        size = QtCore.QSize(dialog.size())
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            self.app.processEvents()
            self.assertEqual(dialog.tabs.geometry(), before)
            self.assertEqual(dialog.size(), size)
            self.assertFalse(dialog.garbage_actions.isHidden())
        self.assertIn(dialog.garbage_actions.parentWidget(),
                      [dialog.tabs.widget(i) for i in range(dialog.tabs.count())])

    def test_clicking_weather_day_updates_backdrop_date_and_garbage_query(self):
        self.prepare_weather()
        dialog = self.plugin.open_dialog()
        dialog.tabs.setCurrentWidget(dialog.weather)
        self.app.processEvents()
        self.assertEqual(dialog.scene, "rain")
        table = dialog.weather.table
        table.scrollToItem(table.item(3, 0))
        self.app.processEvents()
        point = table.visualItemRect(table.item(3, 0)).center()
        QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertEqual(dialog.date.date(), QtCore.QDate(2026, 10, 11))
        self.assertEqual(dialog.scene, "snow")
        self.assertIn("2026-10-11", dialog.query.text())
        dialog.tabs.setCurrentIndex(0)
        item = dialog.week.item(0, 0)
        QtTest.QTest.mouseClick(dialog.week.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               pos=dialog.week.visualItemRect(item).center())
        self.assertEqual(dialog.date.date(), QtCore.QDate(2026, 10, 9))
        self.assertEqual(dialog.scene, "cloud")

    def test_unknown_day_or_other_city_never_reuses_today_weather(self):
        data = self.prepare_weather()
        dialog = self.plugin.open_dialog()
        dialog.date.setDate(QtCore.QDate(2026, 11, 1))
        self.assertEqual(dialog.scene, "neutral")
        self.assertIn("暂无该日预报", dialog.scene_summary.text())
        self.assertIsNone(forecast_for_day(data, {**data["location"], "latitude": 42}, QtCore.QDate(2026, 10, 9)))
        # The worker currently refreshes only WeatherTab; its completion must
        # still update the dashboard background without recursively refreshing.
        dialog.date.setDate(QtCore.QDate(2026, 10, 10))
        self.plugin.weather_data["daily"][2]["weather_code"] = 0
        dialog.weather.refresh()
        self.assertEqual(dialog.scene, "sun")

    def test_all_scenes_render_in_both_themes_and_cache_is_bounded(self):
        self.prepare_weather()
        dialog = self.plugin.open_dialog()
        original = QtGui.QPalette(self.app.palette())
        self.addCleanup(self.app.setPalette, original)
        output = os.environ.get("LZX_LIFE_QA_DIR")
        directory = Path(output) if output else None
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
        for is_dark in (False, True):
            palette = QtGui.QPalette(original)
            palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#152333" if is_dark else "#f5f8fa"))
            palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#edf3f8" if is_dark else "#243d50"))
            self.app.setPalette(palette)
            self.app.processEvents()
            self.assertEqual(dialog._night, is_dark)
            for code in (0, 2, 3, 45, 63, 75, 95, None):
                dialog.set_weather_scene(code)
                image = dialog.grab()
                self.assertFalse(image.isNull())
                self.assertEqual(dialog.scene, weather_scene(code))
                self.assertEqual(len(dialog._scene_cache), 2)
            dialog.date.setDate(QtCore.QDate(2026, 10, 10))
            dialog._refresh_scene()
            if directory:
                theme = "dark" if is_dark else "light"
                for label, page in (("garbage", 0), ("weather", 1), ("cooking", 2)):
                    dialog.tabs.setCurrentIndex(page)
                    self.app.processEvents()
                    self.assertTrue(dialog.grab().save(str(directory / f"{theme}-{label}.png")))
        self.app.setPalette(original)
        dialog.apply_life_style()

    def test_theme_changes_and_queued_window_destruction_are_safe(self):
        original = QtGui.QPalette(self.app.palette())
        self.addCleanup(self.app.setPalette, original)
        for index in range(30):
            window = WeatherSceneDialog()
            palette = QtGui.QPalette(original)
            palette.setColor(QtGui.QPalette.ColorRole.Window,
                             QtGui.QColor("#172a3b" if index % 2 else "#f0f6fa"))
            self.app.setPalette(palette)
            window.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_closed_category_windows_are_released_without_destroyed_callbacks(self):
        calendar = self.plugin.calendar_for(2026)
        for _ in range(20):
            detail = self.plugin.show_category_details(calendar, "plastica")
            self.assertEqual(len(self.plugin.details_dialogs), 1)
            detail.close()
            self.assertEqual(self.plugin.details_dialogs, [])
            detail = None
            gc.collect()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.prepare_weather()
        dashboard = self.plugin.open_dialog()
        self.app.processEvents()
        self.assertTrue(dashboard.isVisible())

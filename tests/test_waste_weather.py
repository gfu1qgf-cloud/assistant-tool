from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from tests import test_waste_reminder as base
from app_plugins.builtin.waste_reminder import weather
from app_plugins.builtin.waste_reminder.settings import normalize_settings
from model.OfflineMode import OfflineModeError
from app_plugins.builtin.waste_reminder.weather_visuals import WeatherGlyph, ForecastChart
from app_plugins.builtin.waste_reminder.weather_physics import BallWorld


def raw_forecast(day="2026-10-08", days=7):
    anchor = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    result = {"current": {"time": anchor + 12 * 3600, **{key: 10 for key in weather.FIELDS["current"]}}}
    result["current"]["weather_code"] = 3
    for key, size, step in (("hourly", days * 24, 3600), ("daily", days, 86400)):
        result[key] = {"time": [anchor + index * step for index in range(size)]}
        result[key].update({field: [0 if field == "precipitation" else 10] * size for field in weather.FIELDS[key]})
        result[key]["weather_code"] = [3] * size
    return result


def snapshot(day="2026-10-08", days=7):
    with patch.object(weather, "get_json", return_value=raw_forecast(day, days)):
        return weather.forecast(weather.DEFAULT_LOCATION)


class Response:
    status_code = 200
    def __init__(self, data): self.data, self.closed = data, False
    def __enter__(self): return self
    def __exit__(self, *_args): self.closed = True
    def iter_content(self, _size): yield json.dumps(self.data).encode()


class WeatherTests(unittest.TestCase):
    def test_default_disabled_and_coarse_city_only(self):
        config = normalize_settings({})
        self.assertFalse(config["weather"]["enabled"])
        self.assertEqual(config["weather"]["location"]["latitude"], 45.15)
        value = weather.location({**weather.DEFAULT_LOCATION, "latitude": 45.150966, "longitude": 8.669570})
        self.assertEqual((value["latitude"], value["longitude"]), (45.15, 8.67))
        self.assertIsNone(weather.options({}, "Other Comune")["location"])

    def test_requests_do_not_send_files_keys_or_application_config(self):
        response = Response(raw_forecast())
        with patch.object(weather.requests, "get", return_value=response) as request:
            data = weather.forecast({**weather.DEFAULT_LOCATION, "api_key": "never-send-this", "private_path": "C:/secret"})
        args, kwargs = request.call_args
        self.assertEqual(args[0], weather.FORECAST_URL)
        self.assertEqual(kwargs["params"]["latitude"], 45.15)
        self.assertEqual(kwargs["params"]["forecast_days"], 16)
        self.assertNotIn("never-send-this", json.dumps(kwargs))
        self.assertNotIn("C:/secret", json.dumps(kwargs))
        self.assertNotIn("files", kwargs)
        self.assertNotIn("data", kwargs)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotEqual(kwargs.get("verify"), False)
        self.assertEqual(len(data["daily"]), 7)
        self.assertEqual(len(data["hourly"]), 168)
        self.assertTrue(response.closed)

    def test_offline_and_cancellation_prevent_requests(self):
        with patch.object(weather, "require_online", side_effect=OfflineModeError("offline")), patch.object(weather.requests, "get") as request:
            with self.assertRaises(OfflineModeError): weather.forecast(weather.DEFAULT_LOCATION)
            request.assert_not_called()
        with patch.object(weather.requests, "get") as request:
            with self.assertRaisesRegex(weather.WeatherError, "取消"):
                weather.forecast(weather.DEFAULT_LOCATION, lambda: True)
            request.assert_not_called()

    def test_unsupported_endpoint_http_error_and_oversized_response(self):
        with self.assertRaises(weather.WeatherError): weather.get_json("http://localhost/private", {})
        response = Response({})
        response.status_code = 429
        with patch.object(weather.requests, "get", return_value=response), self.assertRaisesRegex(weather.WeatherError, "429"):
            weather.forecast(weather.DEFAULT_LOCATION)
        response = Response({"text": "x" * 1024 * 1024})
        with patch.object(weather.requests, "get", return_value=response), self.assertRaisesRegex(weather.WeatherError, "过大"):
            weather.forecast(weather.DEFAULT_LOCATION)

    def test_malformed_data_and_null_numbers(self):
        raw = raw_forecast()
        raw["hourly"]["precipitation_probability"].pop()
        with patch.object(weather, "get_json", return_value=raw), self.assertRaises(weather.WeatherError):
            weather.forecast(weather.DEFAULT_LOCATION)
        raw = raw_forecast()
        raw["current"]["temperature_2m"] = None
        with patch.object(weather, "get_json", return_value=raw):
            self.assertIsNone(weather.forecast(weather.DEFAULT_LOCATION)["current"]["temperature_2m"])
        self.assertEqual(weather.number(None, "℃"), "—")

    def test_city_search_filters_countries_and_invalid_locations(self):
        raw = {"results": [weather.DEFAULT_LOCATION, {**weather.DEFAULT_LOCATION, "country_code": "US"},
                           {**weather.DEFAULT_LOCATION, "latitude": float("nan")} ]}
        with patch.object(weather, "get_json", return_value=raw) as requested:
            cities = weather.search_cities("Valle Lomellina")
        self.assertEqual(cities, [weather.DEFAULT_LOCATION])
        self.assertEqual(requested.call_args.args[1]["countryCode"], "IT")

    def test_cache_roundtrip_and_invalid_cache_does_not_become_forecast(self):
        data = snapshot()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "weather.json"
            weather.save_cache(path, data)
            self.assertEqual(weather.load_cache(path), data)
            path.write_text('invalid', encoding="utf-8")
            self.assertIsNone(weather.load_cache(path))
            bad = deepcopy(data)
            bad["checked_at"] = float("nan")
            with self.assertRaises(weather.WeatherError): weather.save_cache(path, bad)
            self.assertEqual(path.read_text(), "invalid")

    def test_exposure_hours_include_last_hour_and_dst_transition(self):
        data = snapshot("2026-10-24")
        end = QtCore.QDateTime(QtCore.QDate(2026, 10, 25), QtCore.QTime(6, 0), QtCore.QTimeZone(b"Europe/Rome")).toSecsSinceEpoch()
        for row in data["hourly"]:
            row["precipitation_probability"] = 80 if row["time"] == end else 0
        now = datetime(2026, 10, 24, 21, tzinfo=timezone(timedelta(hours=2)))
        self.assertIn("80%", weather.tonight_summary(data, now))
        self.assertIn("备雨具", weather.tonight_summary(data, now))
        missing = deepcopy(data)
        missing["hourly"] = []
        self.assertIn("数据不足", weather.tonight_summary(missing, now))
        for row in data["hourly"]:
            row["precipitation_probability"] = row["precipitation"] = None
        self.assertIn("不能判断", weather.tonight_summary(data, now))

    def test_extended_cache_and_old_cache_remain_compatible(self):
        for days in (7, 16):
            data = snapshot(days=days)
            self.assertEqual(len(data["hourly"]), days * 24)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "weather.json"
                weather.save_cache(path, data)
                self.assertEqual(weather.load_cache(path), data)
        with patch.object(weather, "get_json", return_value=raw_forecast(days=17)):
            with self.assertRaises(weather.WeatherError):
                weather.forecast(weather.DEFAULT_LOCATION)

    def test_span_setting_is_bounded_and_old_configs_default_to_24_hours(self):
        self.assertEqual(weather.options({})["chart_hours"], 24)
        for value in ("bad", None, 100000, -1):
            self.assertEqual(weather.options({"chart_hours": value})["chart_hours"], 24)
        for hours, _label in weather.CHART_SPANS:
            self.assertEqual(weather.options({"chart_hours": hours})["chart_hours"], hours)


class WeatherBallTests(unittest.TestCase):
    def test_ball_falls_bounces_rests_explodes_and_becomes_idle(self):
        world = BallWorld(seed=1)
        bounds = (0, 0, 300, 180)
        line = [((0, 100), (300, 100))]
        self.assertTrue(world.spawn(120, 20, bounds))
        bounced, exploded = False, False
        lowest = 0
        for _ in range(600):
            world.advance(1 / 60, line, bounds)
            if world.balls:
                ball = world.balls[0]
                lowest = max(lowest, ball.y)
                bounced |= ball.vy < -10
                self.assertLessEqual(ball.y + ball.radius, 100.1)
            exploded |= bool(world.sparks)
        self.assertGreater(lowest, 85)
        self.assertTrue(bounced)
        self.assertTrue(exploded)
        self.assertFalse(world.active())

    def test_ball_rolls_down_slope_and_limits_concurrency(self):
        world = BallWorld(seed=2)
        bounds = (0, 0, 300, 180)
        line = [((0, 70), (300, 140))]
        world.spawn(100, 20, bounds)
        for _ in range(60):
            world.advance(1 / 60, line, bounds)
        self.assertGreater(world.balls[0].x, 105)
        for _ in range(20):
            world.spawn(100, 20, bounds)
        self.assertEqual(len(world.balls), 8)
        world.clear()
        self.assertFalse(world.active())

    def test_long_stall_does_not_tunnel_through_curve_or_corrupt_coordinates(self):
        world = BallWorld()
        bounds = (0, 0, 300, 180)
        world.spawn(120, 20, bounds)
        world.balls[0].vy = 100000
        for _ in range(10):
            world.advance(100, [((0, 100), (300, 100))], bounds)
            if world.balls:
                self.assertLessEqual(world.balls[0].y + 7, 100.1)

    def test_missing_curve_uses_floor_without_inventing_weather_points(self):
        world = BallWorld(seed=1)
        bounds = (0, 0, 300, 180)
        world.spawn(120, 20, bounds)
        exploded = False
        for _ in range(600):
            world.advance(1 / 60, [], bounds)
            exploded |= bool(world.sparks)
        self.assertTrue(exploded)
        self.assertFalse(world.active())


class WeatherQtTests(base.QtTests):
    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate())

    def tearDown(self):
        if self.plugin.weather_worker:
            self.plugin.weather_worker.requestInterruption()
            self.wait(lambda: not self.plugin.is_weather_updating())
        super().tearDown()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_panel_and_dashboard_share_weather_without_extra_registration(self):
        dialog = self.plugin.open_dialog()
        self.assertIn("天气", [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())])
        panel = self.plugin.create_main_panel(self.parent)
        self.assertIn("天气未启用", panel.weather_label.text())
        self.assertFalse(dialog.weather.enabled.isChecked())
        self.context.register_main_widget.assert_called_once()

    def test_no_network_until_enabled_and_safe_worker_lifecycle(self):
        data = snapshot()
        with patch.object(weather, "forecast", return_value=data) as request:
            self.assertFalse(self.plugin.refresh_weather(manual=True))
            request.assert_not_called()
            self.plugin.change_weather_options(enabled=True)
            self.assertTrue(self.plugin.refresh_weather(manual=True))
            self.wait(lambda: not self.plugin.is_weather_updating())
        self.assertEqual(self.plugin.weather_data, data)
        self.assertTrue((self.plugin.folder / "weather-open-meteo.json").is_file())
        self.assertFalse(self.plugin.refresh_weather())
        self.assertTrue(self.plugin.can_close()[0])

    def test_results_and_gui_refresh_run_on_main_thread(self):
        data = snapshot()
        caller, workers = [], []
        original = self.plugin._weather_updated
        self.plugin._weather_updated = lambda value: (caller.append(threading.get_ident()), original(value))
        def request(*_args):
            workers.append(threading.get_ident())
            return data
        self.plugin.change_weather_options(enabled=True)
        with patch.object(weather, "forecast", side_effect=request):
            self.plugin.refresh_weather(manual=True)
            self.wait(lambda: not self.plugin.is_weather_updating())
        self.assertEqual(caller, [threading.get_ident()])
        self.assertNotEqual(workers[0], threading.get_ident())

    def test_close_waits_without_force_terminating_worker(self):
        data = snapshot()
        release, entered = threading.Event(), threading.Event()
        def request(*_args):
            entered.set()
            release.wait(3)
            return data
        try:
            self.plugin.change_weather_options(enabled=True)
            with patch.object(weather, "forecast", side_effect=request):
                self.plugin.refresh_weather(manual=True)
                self.wait(entered.is_set)
                worker = self.plugin.weather_worker
                self.assertFalse(self.plugin.can_close()[0])
                self.assertFalse(self.plugin.stop())
                self.assertIs(self.plugin.weather_worker, worker)
                release.set()
                self.wait(lambda: self.plugin.weather_worker is None)
                self.assertIsNone(self.plugin.weather_data)
        finally:
            release.set()

    def test_location_change_hides_old_cache_and_preserves_garbage_settings(self):
        self.plugin.weather_data = snapshot()
        self.plugin.change_weather_options(enabled=True, location={**weather.DEFAULT_LOCATION, "name": "Roma", "latitude": 41.9, "longitude": 12.5})
        dialog = self.plugin.open_dialog()
        self.assertEqual(dialog.weather.table.rowCount(), 0)
        self.assertIn("尚无当前城市", dialog.weather.current.text())
        self.assertEqual(self.plugin.settings["reminder_time"], "22:00")
        self.assertEqual(self.plugin.settings["commune"], "Valle Lomellina (PV)")

    def test_save_failure_rolls_back_including_ui(self):
        old = deepcopy(self.plugin.settings["weather"])
        self.context.save_config.return_value = False
        with self.assertRaises(OSError): self.plugin.change_weather_options(enabled=True)
        self.assertEqual(self.plugin.settings["weather"], old)
        dialog = self.plugin.open_dialog()
        dialog.weather.enabled.setChecked(True)
        self.assertFalse(dialog.weather.enabled.isChecked())
        self.assertIn("未保存", dialog.weather.message.text())

    def test_disabling_cancels_request_and_discards_result(self):
        data, release, entered = snapshot(), threading.Event(), threading.Event()
        def request(*_args):
            entered.set()
            release.wait(3)
            return data
        try:
            self.plugin.change_weather_options(enabled=True)
            with patch.object(weather, "forecast", side_effect=request):
                self.plugin.refresh_weather(manual=True)
                self.wait(entered.is_set)
                self.plugin.change_weather_options(enabled=False)
                self.assertTrue(self.plugin.weather_worker.isInterruptionRequested())
                release.set()
                self.wait(lambda: not self.plugin.is_weather_updating())
            self.assertIsNone(self.plugin.weather_data)
        finally:
            release.set()

    def test_config_roundtrip_preserves_unrelated_fields_and_reminder_rules(self):
        self.plugin.change_weather_options(enabled=True, refresh_minutes=90, auto_refresh=False)
        config = {"gemini_api_keys": ["synthetic-fixture"], "business": {"keep": 1}}
        self.plugin.update_config(config)
        restored = normalize_settings(config["waste_reminder"])
        self.assertEqual(restored["weather"], self.plugin.settings["weather"])
        self.assertEqual(restored["reminder_time"], "22:00")
        self.assertEqual(config["gemini_api_keys"], ["synthetic-fixture"])
        self.assertEqual(config["business"], {"keep": 1})

    def test_request_failure_keeps_old_cache_and_backoff(self):
        self.plugin.weather_data = snapshot()
        self.plugin.change_weather_options(enabled=True)
        with patch.object(weather, "forecast", side_effect=weather.WeatherError("网络错误")):
            self.plugin.refresh_weather(manual=True)
            self.wait(lambda: not self.plugin.is_weather_updating())
        self.assertIsNotNone(self.plugin.weather_data)
        self.assertIn("上次成功缓存", self.plugin.weather_status)
        self.assertGreater(self.plugin.weather_next_attempt, time.time())

    def test_other_commune_can_opt_in_before_selecting_city(self):
        self.plugin.settings["commune"] = "Other Comune"
        self.plugin.settings["weather"]["location"] = None
        self.plugin.change_weather_options(enabled=True)
        self.assertIsNone(self.plugin.settings["weather"]["location"])
        self.assertFalse(self.plugin.refresh_weather(manual=True))
        with patch.object(weather, "search_cities", return_value=[weather.DEFAULT_LOCATION]):
            self.assertTrue(self.plugin.search_weather_city("Valle Lomellina"))
            self.wait(lambda: not self.plugin.is_weather_updating())
        self.assertEqual(self.plugin.weather_cities, [weather.DEFAULT_LOCATION])

    def test_local_glyphs_render_all_weather_codes_and_unknown(self):
        glyph = WeatherGlyph(self.parent)
        for code in [None, *weather.LABELS]:
            glyph.set_code(code)
            self.assertFalse(glyph.grab().isNull())

    def test_hourly_chart_limits_range_handles_gaps_and_shows_hover(self):
        data = snapshot()
        now = datetime(2026, 10, 8, 20, tzinfo=timezone(timedelta(hours=2)))
        chart = ForecastChart(self.parent)
        chart.resize(700, 180)
        chart.set_forecast(data, now)
        self.assertEqual(len(chart.rows), 24)
        chart.rows[3]["temperature_2m"] = None
        chart.rows[4]["precipitation_probability"] = None
        self.assertFalse(chart.grab().isNull())
        event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(100, 80),
                                 QtCore.QPointF(100, 80), QtCore.Qt.MouseButton.NoButton,
                                 QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier)
        with patch.object(QtWidgets.QToolTip, "showText") as tooltip:
            chart.mouseMoveEvent(event)
        self.assertIn("降雨概率", tooltip.call_args.args[1])
        chart.set_forecast(data, now + timedelta(days=10))
        self.assertFalse(chart.rows)
        self.assertFalse(chart.grab().isNull())

    def test_chart_and_glyph_render_with_dark_palette(self):
        previous = self.app.palette()
        palette = QtGui.QPalette(previous)
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#20252b"))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#eeeeee"))
        try:
            self.app.setPalette(palette)
            dialog = self.plugin.open_dialog()
            self.plugin.weather_data = snapshot()
            dialog.weather.refresh()
            self.assertFalse(dialog.weather.chart.grab().isNull())
            self.assertFalse(dialog.weather.glyph.grab().isNull())
        finally:
            self.app.setPalette(previous)

    def test_chart_spans_use_available_data_and_hover_never_invents_monthly_forecast(self):
        data = snapshot(days=16)
        now = datetime(2026, 10, 8, 6, tzinfo=timezone.utc)
        chart = ForecastChart(self.parent)
        chart.resize(780, 215)
        chart.set_forecast(data, now)
        for hours in (24, 72, 120, 168, 336):
            chart.set_span(hours)
            self.assertEqual(len(chart.rows), hours)
            self.assertFalse(chart.grab().isNull())
        chart.set_span(720)
        self.assertEqual(len(chart.rows), 16 * 24 - 6)
        self.assertIn("灰色区域暂无预报", chart.coverage_text())
        self.assertFalse(chart.grab().isNull())
        plot = chart._plot()
        point = QtCore.QPointF(plot.right() - 10, plot.center().y())
        event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, point, point,
                                 QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
                                 QtCore.Qt.KeyboardModifier.NoModifier)
        with patch.object(QtWidgets.QToolTip, "showText") as tooltip:
            chart.mouseMoveEvent(event)
        self.assertIn("暂无预报", tooltip.call_args.args[1])

    def test_chart_selection_persists_without_network_or_refresh_backoff_reset(self):
        self.plugin.weather_data = snapshot(days=16)
        dialog = self.plugin.open_dialog()
        self.plugin.weather_next_attempt = 12345
        old_status = self.plugin.weather_status
        with patch.object(weather.requests, "get") as request:
            dialog.weather.chart_range.setCurrentIndex(dialog.weather.chart_range.findData(336))
        request.assert_not_called()
        self.assertEqual(self.plugin.weather_next_attempt, 12345)
        self.assertEqual(self.plugin.weather_status, old_status)
        self.assertEqual(dialog.weather.chart.span_hours, 336)
        config = {}
        self.plugin.update_config(config)
        self.assertEqual(normalize_settings(config["waste_reminder"])["weather"]["chart_hours"], 336)

    def test_failed_span_save_reverts_combo_and_chart(self):
        dialog = self.plugin.open_dialog()
        self.context.save_config.return_value = False
        dialog.weather.chart_range.setCurrentIndex(dialog.weather.chart_range.findData(336))
        self.assertEqual(dialog.weather.chart_range.currentData(), 24)
        self.assertEqual(dialog.weather.chart.span_hours, 24)

    def test_daily_table_has_weather_icons_and_relative_dates_in_both_themes(self):
        self.plugin.weather_data = snapshot(days=16)
        dialog = self.plugin.open_dialog()
        for shade in ("#eeeeee", "#20252b"):
            palette = QtGui.QPalette(self.app.palette())
            palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(shade))
            dialog.weather.setPalette(palette)
            dialog.weather.refresh()
            self.assertEqual(dialog.weather.table.rowCount(), 16)
            for row in range(16):
                self.assertFalse(dialog.weather.table.item(row, 1).icon().isNull())
            self.assertFalse(dialog.weather.grab().isNull())

    def test_middle_click_animates_then_range_change_and_hide_stop_timer(self):
        data = snapshot()
        chart = ForecastChart(self.parent)
        self.parent.show()
        chart.show()
        chart.resize(700, 215)
        self.app.processEvents()
        chart.set_forecast(data, datetime(2026, 10, 8, tzinfo=timezone.utc))
        point = QtCore.QPointF(130, 45)
        def click(button):
            chart.mousePressEvent(QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, point, point,
                button, button, QtCore.Qt.KeyboardModifier.NoModifier))
        click(QtCore.Qt.MouseButton.LeftButton)
        self.assertFalse(chart.world.active())
        click(QtCore.Qt.MouseButton.MiddleButton)
        self.assertEqual(len(chart.world.balls), 1)
        self.assertTrue(chart._animation.isActive())
        before = chart.world.balls[0].y
        QtWidgets.QApplication.processEvents()
        QtCore.QEventLoop().processEvents()
        from qt_compat import QtTest
        # Timer delivery depends on native event scheduling under offscreen Qt.
        # Wait for observed motion, not an exact 35 ms deadline.
        for _ in range(20):
            if chart.world.balls[0].y > before:
                break
            QtTest.QTest.qWait(20)
        self.assertGreater(chart.world.balls[0].y, before)
        self.assertFalse(chart.grab().isNull())
        chart.set_span(72)
        self.assertFalse(chart.world.active())
        self.assertFalse(chart._animation.isActive())
        click(QtCore.Qt.MouseButton.MiddleButton)
        chart.hide()
        self.assertFalse(chart._animation.isActive())
        self.assertFalse(chart.world.active())


if __name__ == "__main__":
    unittest.main()

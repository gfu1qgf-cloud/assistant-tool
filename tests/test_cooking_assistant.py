from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.cooking_assistant import CookingAssistantPlugin
from app_plugins.builtin.cooking_assistant.recipes import generate_recipes, parse_recipes, request_id, recipe_text
from app_plugins.builtin.cooking_assistant.settings import CookingSettingsPage, normalize_settings
from app_plugins.builtin.cooking_assistant.store import FoodStore, eligible_items, stock_status, today, validate_item
from app_plugins.builtin.cooking_assistant.ui import CookingDialog, FoodEditDialog, RecipeThread
from model.GeminiKeyManager import GeminiKeyManager

RECIPE = {"title": "番茄鸡蛋", "minutes": 20, "reason": "简单",
    "ingredients": ["番茄 2 个", "鸡蛋 3 个", "盐少许"], "steps": ["洗净番茄并切块。", "鸡蛋充分煮熟。"],
    "missing": [], "leftovers": "及时分装冷藏。", "warnings": []}


class FakeContext:
    parent_widget = None
    def __init__(self):
        self.gemini_keys = GeminiKeyManager({"gemini_api_keys": ["dummy-secret-one", "dummy-secret-two"]})
        self.log, self.notify, self.open_gemini_key_manager = Mock(), Mock(), Mock()
        self.commands, self.pages = [], []
    def load_config(self):
        return {}
    def register_command(self, command):
        self.commands.append(command)
    def register_settings_page(self, page):
        self.pages.append(page)


class FoodStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FoodStore(Path(self.temp.name) / "food.sqlite3")
    def tearDown(self):
        self.temp.cleanup()
    def test_batches_preserved_and_reopen(self):
        self.store.save_item({"name": "土豆", "quantity": 500, "unit": "g"})
        self.store.save_item({"name": "土豆", "quantity": 200, "unit": "g"})
        restored = FoodStore(self.store.path).items()
        self.assertEqual(len(restored), 2)
        self.assertEqual(sum(x["quantity"] for x in restored), 700)
    def test_atomic_consumption_and_no_overdraw(self):
        item = self.store.save_item({"name": "土豆", "quantity": 1, "unit": "kg"})
        workers = [threading.Thread(target=self.store.consume, args=(item["id"], 0.01)) for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(self.store.items()[0]["quantity"], 0.8)
        with self.assertRaises(ValueError):
            self.store.consume(item["id"], 2)
        self.assertEqual(self.store.items()[0]["quantity"], 0.8)
    def test_invalid_input_does_not_write(self):
        for item in ({"name": "", "quantity": 1}, {"name": "x", "quantity": float("nan")},
                     {"name": "x", "quantity": -1}, {"name": "x", "due": "not-a-date"}):
            with self.assertRaises(ValueError):
                self.store.save_item(item)
        self.assertEqual(self.store.items(), [])
    def test_labels_and_unsafe_items_never_recommended(self):
        day = date(2026, 10, 1)
        expired = validate_item({"name": "牛奶", "quantity": 1, "date_kind": "use_by", "due": "2026-09-30"})
        self.assertIn("禁止", stock_status(expired, day)[1])
        check = validate_item({"name": "白菜", "quantity": 1, "due": "2026-10-02"})
        unknown = validate_item({"name": "土豆", "quantity": 1})
        spoiled = validate_item({"name": "坏掉的白菜", "quantity": 1, "spoiled": True})
        self.assertEqual([x["name"] for x in eligible_items([expired, check, unknown, spoiled], day)], ["白菜", "土豆"])
        self.assertEqual(stock_status(check, day)[0], "soon")
        self.assertEqual(stock_status(unknown, day)[0], "unknown")
    def test_recipe_cache_is_bounded_and_last_result_survives(self):
        for i in range(40):
            self.store.save_recipes(str(i), {"title": str(i)})
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM recipes").fetchone()[0], 30)
        self.assertEqual(self.store.get("last_recipes"), {"title": "39"})
        self.store.remove(self.store.save_item({"name": "x", "quantity": 1})["id"])
        self.assertEqual(self.store.items(), [])


class CookingRecipeTests(unittest.TestCase):
    def test_parse_and_human_readable_total(self):
        result = parse_recipes('```json\n' + json.dumps({"recipes": [RECIPE]}, ensure_ascii=False) + '\n```')
        text = recipe_text(result[0], 2, 2)
        self.assertIn("共 4 份", text)
        self.assertIn("1. 洗净", text)
        with self.assertRaises(ValueError):
            parse_recipes('{"recipes":[{"title":"x","steps":[]}]}')
    def test_cache_key_respects_day_servings_model(self):
        payload = {"ingredients": [{"name": "番茄"}], "people": 1, "meals": 2, "date": "2026-10-01"}
        original = request_id(payload, "m")
        for changed in ({**payload, "meals": 1}, {**payload, "date": "2026-10-02"}):
            self.assertNotEqual(original, request_id(changed, "m"))
        self.assertNotEqual(original, request_id(payload, "other"))
    def response(self, text=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"candidates": [{"content": {"parts": [
            {"thought": True, "text": "private thought not a recipe"},
            {"text": text if text is not None else json.dumps({"recipes": [RECIPE]})}]}}]}).encode()
        return response
    def test_api_rotates_keys_and_reports_cooldown_without_logging_secret(self):
        manager = GeminiKeyManager({"gemini_api_keys": ["secret-one", "secret-two"]})
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, io.BytesIO(b'retry in 300s'))
        with patch("urllib.request.urlopen", side_effect=[error, self.response()]) as call:
            result = generate_recipes({"ingredients": ["鸡蛋"], "people": 1, "meals": 2}, "m", manager)
        self.assertEqual(result[0]["title"], "番茄鸡蛋")
        self.assertEqual(manager.request_keys("m"), ["secret-two"])
        request = call.call_args.args[0]
        self.assertNotIn("secret", request.full_url)
        payload = json.loads(request.data)
        self.assertIn("只使用用户列出的食材", payload["systemInstruction"]["parts"][0]["text"])
    def test_invalid_output_does_not_disable_working_key(self):
        manager = GeminiKeyManager({"gemini_api_keys": ["key"]})
        with patch("urllib.request.urlopen", return_value=self.response("not JSON")):
            with self.assertRaisesRegex(RuntimeError, "不完整"):
                generate_recipes({"ingredients": ["egg"]}, "m", manager)
        self.assertTrue(manager.is_available("key", "m"))
    def test_unavailable_model_stops_without_trying_every_key(self):
        manager = GeminiKeyManager({"gemini_api_keys": ["a", "b"]})
        error = urllib.error.HTTPError("https://example.test", 404, "missing", {}, io.BytesIO(b'{}'))
        with patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, "模型不可用"):
                generate_recipes({"ingredients": ["egg"]}, "m", manager)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(manager.request_keys("other"), ["a", "b"])


class CookingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = FakeContext()
        self.plugin = CookingAssistantPlugin(self.temp.name)
        self.plugin.register(self.context)
        self.dialog = self.plugin.open_dialog()
    def tearDown(self):
        self.assertFalse(self.dialog.is_busy())
        self.dialog.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()
    def test_plugin_registration_defaults_and_hidden_window_reuse(self):
        self.assertEqual(self.context.commands[0].title, "做饭小助手…")
        self.assertEqual(self.context.pages[0].title, "做饭小助手")
        self.assertEqual(self.dialog.people.value(), 1)
        self.assertEqual(self.dialog.meals.value(), 2)
        self.assertEqual([k for k, x in self.dialog.condiments.items() if x.isChecked()], ["盐"])
        self.dialog.extra.setPlainText("番茄 2 个")
        self.dialog.close()
        self.assertFalse(self.dialog.isVisible())
        self.assertIs(self.plugin.open_dialog(), self.dialog)
        self.assertEqual(self.dialog.extra.toPlainText(), "番茄 2 个")
    def test_near_date_selected_unsafe_hidden_and_no_automatic_consumption(self):
        valid = self.plugin.store.save_item({"name": "青菜", "quantity": 2, "due": today().isoformat()})
        self.plugin.store.save_item({"name": "坏菜", "quantity": 1, "spoiled": True})
        self.dialog.refresh_stock()
        payload = self.dialog.payload()
        self.assertEqual(len(payload["ingredients"]), 1)
        self.assertEqual(payload["ingredients"][0]["name"], "青菜")
        self.dialog.generated({"recipes": [RECIPE], "people": 1, "meals": 2})
        self.assertEqual(next(x for x in self.plugin.store.items() if x["id"] == valid["id"])["quantity"], 2)
    def test_cache_works_offline_and_settings_use_shared_key_entry(self):
        self.dialog.extra.setPlainText("鸡蛋 3 个、番茄 2 个")
        payload = self.dialog.payload()
        key = request_id(payload, self.plugin.settings["model"])
        self.plugin.store.save_recipes(key, {"recipes": [RECIPE], "people": 1, "meals": 2})
        with patch("urllib.request.urlopen", side_effect=AssertionError("offline cache must not call API")):
            self.dialog.generate()
        self.assertIn("复用", self.dialog.status.text())
        page = CookingSettingsPage(self.context)
        page.load_config({"cooking_assistant": {"model": "custom-model", "meals": 3}})
        config = page.update_config({})
        self.assertEqual(config["cooking_assistant"]["model"], "custom-model")
        self.assertEqual(config["cooking_assistant"]["meals"], 3)
        page.widget.deleteLater()
    def test_edit_dialog_has_optional_date_not_invented_expiry(self):
        edit = FoodEditDialog(parent=self.dialog)
        self.assertFalse(edit.has_due.isChecked())
        edit.name.setText("白菜")
        self.assertEqual(edit.value()["due"], "")
        edit.has_due.setChecked(True)
        self.assertEqual(edit.value()["date_kind"], "check")
        edit.deleteLater()
    def test_notice_daily_dedup_and_disabled_setting(self):
        self.plugin.store.save_item({"name": "白菜", "quantity": 1, "due": today().isoformat()})
        self.plugin.start()
        self.plugin.tick()
        self.plugin.tick()
        self.context.notify.assert_called_once()
        self.plugin.apply_settings({"cooking_assistant": {"notify": False}})
        self.plugin.tick()
        self.context.notify.assert_called_once()
        self.plugin.stop()
    def test_worker_result_runs_through_qt_signals_and_releases_reference(self):
        self.dialog.extra.setPlainText("鸡蛋 3 个、番茄 2 个")
        with patch("app_plugins.builtin.cooking_assistant.ui.generate_recipes", return_value=[RECIPE]):
            self.dialog.generate()
            self.assertTrue(self.dialog.is_busy())
            self.assertFalse(self.plugin.can_close()[0])
            self.dialog.worker.wait(5000)
            self.assertTrue(self.dialog.is_busy())  # finished has not been delivered yet
            for _ in range(10):
                self.app.processEvents()
            self.assertFalse(self.dialog.is_busy())
        self.assertIn("共 2 份", self.dialog.output.toPlainText())
        self.assertTrue(self.plugin.can_close()[0])


if __name__ == "__main__":
    unittest.main()

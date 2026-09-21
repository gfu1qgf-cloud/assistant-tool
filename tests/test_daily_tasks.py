"""Unit and integration tests for DailyTasksPlugin and Execution Suggestion algorithm."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from app_plugins.api import MAIN_MENU
from app_plugins.builtin.daily_tasks import (
    DIFFICULTY_EASY,
    DIFFICULTY_HARD,
    DIFFICULTY_MEDIUM,
    URGENCY_DAYS,
    URGENCY_HOURS,
    URGENCY_TODAY,
    DailyTasksDialog,
    DailyTasksPlugin,
    DailyTaskStore,
    TaskEditDialog,
    generate_execution_suggestion,
    normalize_difficulty,
    normalize_urgency,
    render_suggestion_html,
)
from app_plugins.host import PluginHost


class FakeMainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.logs = []

    def appendLog(self, message, end="", level=None):
        self.logs.append(str(message))

    def load_config(self):
        return {}

    def saveCurrentConfig(self):
        return True

    def showDesktopNotification(self, title, message, critical=False):
        pass


class TestDailyTaskSuggestionAlgorithm(unittest.TestCase):
    """Programmatic verification of the execution suggestion algorithm and its edge cases."""

    def test_normalize_urgency_and_difficulty(self):
        # Urgency normalization
        self.assertEqual(normalize_urgency("hours"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("2 hours"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("2h"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("4hrs"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("30分钟"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("15 min"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("半小时"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("数小时内"), URGENCY_HOURS)
        self.assertEqual(normalize_urgency("紧急"), URGENCY_HOURS)

        self.assertEqual(normalize_urgency("today"), URGENCY_TODAY)
        self.assertEqual(normalize_urgency("今日内"), URGENCY_TODAY)
        self.assertEqual(normalize_urgency("当天完成"), URGENCY_TODAY)
        self.assertEqual(normalize_urgency("半天"), URGENCY_TODAY)
        self.assertEqual(normalize_urgency("1 day"), URGENCY_TODAY)

        self.assertEqual(normalize_urgency("days"), URGENCY_DAYS)
        self.assertEqual(normalize_urgency("3 days"), URGENCY_DAYS)
        self.assertEqual(normalize_urgency("数日内"), URGENCY_DAYS)
        self.assertEqual(normalize_urgency("下周"), URGENCY_DAYS)

        self.assertEqual(normalize_urgency(None), URGENCY_TODAY)

        # Difficulty normalization
        self.assertEqual(normalize_difficulty("easy"), DIFFICULTY_EASY)
        self.assertEqual(normalize_difficulty("简单"), DIFFICULTY_EASY)
        self.assertEqual(normalize_difficulty("轻松"), DIFFICULTY_EASY)
        self.assertEqual(normalize_difficulty("不难"), DIFFICULTY_EASY)
        self.assertEqual(normalize_difficulty("毫无难度"), DIFFICULTY_EASY)
        self.assertEqual(normalize_difficulty("1"), DIFFICULTY_EASY)

        self.assertEqual(normalize_difficulty("medium"), DIFFICULTY_MEDIUM)
        self.assertEqual(normalize_difficulty("中等"), DIFFICULTY_MEDIUM)
        self.assertEqual(normalize_difficulty("中等（适度烧脑）"), DIFFICULTY_MEDIUM)
        self.assertEqual(normalize_difficulty("2"), DIFFICULTY_MEDIUM)

        self.assertEqual(normalize_difficulty("hard"), DIFFICULTY_HARD)
        self.assertEqual(normalize_difficulty("困难"), DIFFICULTY_HARD)
        self.assertEqual(normalize_difficulty("烧脑"), DIFFICULTY_HARD)
        self.assertEqual(normalize_difficulty("3"), DIFFICULTY_HARD)
        self.assertEqual(normalize_difficulty("10"), DIFFICULTY_HARD)
        self.assertEqual(normalize_difficulty("难度10"), DIFFICULTY_HARD)

        self.assertEqual(normalize_difficulty(None), DIFFICULTY_MEDIUM)

    def test_highly_urgent_difficult_vs_low_urgency_easy(self):
        """Verify that highly urgent + difficult tasks ALWAYS rank before low urgency + easy tasks."""
        task_urgent_hard = {
            "id": "task_1",
            "title": "生产数据库主从同步中断修复",
            "urgency": "hours",  # Highly urgent
            "difficulty": "hard",  # Difficult
            "completed": False,
        }
        task_low_easy = {
            "id": "task_2",
            "title": "整理下周五茶歇水果愿望单",
            "urgency": "days",  # Low urgency
            "difficulty": "easy",  # Easy
            "completed": False,
        }

        # Test across 100 different random runs / seeds
        for seed in range(100):
            res = generate_execution_suggestion(
                [task_low_easy, task_urgent_hard], seed=seed
            )
            order = res["recommended_order"]
            self.assertEqual(len(order), 2)
            self.assertEqual(
                order[0]["task_id"],
                "task_1",
                f"Failed at seed {seed}: urgent+hard should rank 1st!",
            )
            self.assertEqual(
                order[1]["task_id"],
                "task_2",
                f"Failed at seed {seed}: low+easy should rank 2nd!",
            )
            self.assertTrue(len(order[0]["humorous_comment"]) > 0)
            self.assertTrue(len(order[1]["humorous_comment"]) > 0)

    def test_urgency_hierarchy_strictly_preserved(self):
        """Verify that hours > today > days is strictly preserved regardless of difficulty."""
        tasks = [
            {"id": "d_hard", "title": "远期难题", "urgency": "days", "difficulty": "hard"},
            {"id": "d_easy", "title": "远期简单", "urgency": "days", "difficulty": "easy"},
            {"id": "t_hard", "title": "今日难题", "urgency": "today", "difficulty": "hard"},
            {"id": "t_easy", "title": "今日简单", "urgency": "today", "difficulty": "easy"},
            {"id": "h_hard", "title": "急迫难题", "urgency": "hours", "difficulty": "hard"},
            {"id": "h_easy", "title": "急迫简单", "urgency": "hours", "difficulty": "easy"},
        ]

        for seed in range(50):
            res = generate_execution_suggestion(tasks, seed=seed)
            order = res["recommended_order"]
            task_ids = [item["task_id"] for item in order]

            # Indices of hours tasks
            idx_h_hard = task_ids.index("h_hard")
            idx_h_easy = task_ids.index("h_easy")
            # Indices of today tasks
            idx_t_hard = task_ids.index("t_hard")
            idx_t_easy = task_ids.index("t_easy")
            # Indices of days tasks
            idx_d_hard = task_ids.index("d_hard")
            idx_d_easy = task_ids.index("d_easy")

            # All hours tasks must be before all today tasks
            self.assertTrue(idx_h_hard < idx_t_hard)
            self.assertTrue(idx_h_hard < idx_t_easy)
            self.assertTrue(idx_h_easy < idx_t_hard)
            self.assertTrue(idx_h_easy < idx_t_easy)

            # All today tasks must be before all days tasks
            self.assertTrue(idx_t_hard < idx_d_hard)
            self.assertTrue(idx_t_hard < idx_d_easy)
            self.assertTrue(idx_t_easy < idx_d_hard)
            self.assertTrue(idx_t_easy < idx_d_easy)

    def test_empty_task_list_and_all_completed(self):
        # Empty list across 50 seeds to verify all random zero-task commentary variants
        for seed in range(50):
            res = generate_execution_suggestion([], seed=seed)
            self.assertEqual(res["total_pending"], 0)
            self.assertEqual(res["recommended_order"], [])
            self.assertIn("摸鱼", res["overall_commentary"])
            self.assertIn("诊断报告", res["overall_commentary"])

        # None
        res_none = generate_execution_suggestion(None)
        self.assertEqual(res_none["total_pending"], 0)
        self.assertEqual(res_none["recommended_order"], [])
        self.assertIn("摸鱼", res_none["overall_commentary"])

        # All completed tasks
        tasks = [
            {"id": "1", "title": "任务1", "completed": True},
            {"id": "2", "title": "任务2", "completed": True},
            {"id": "3", "title": "任务3", "completed": "true"},
        ]
        res_completed = generate_execution_suggestion(tasks)
        self.assertEqual(res_completed["total_pending"], 0)
        self.assertEqual(res_completed["recommended_order"], [])
        self.assertIn("摸鱼", res_completed["overall_commentary"])

    def test_single_task(self):
        task = {
            "id": "solo_1",
            "title": "唯一剩下的任务",
            "urgency": "today",
            "difficulty": "medium",
            "completed": False,
        }
        res = generate_execution_suggestion([task])
        self.assertEqual(res["total_pending"], 1)
        self.assertEqual(len(res["recommended_order"]), 1)
        item = res["recommended_order"][0]
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["title"], "唯一剩下的任务")
        self.assertTrue(len(item["humorous_comment"]) > 0)

    def test_randomness_produces_variations(self):
        """Verify that multiple runs produce different commentaries or rankings due to randomness."""
        tasks = [
            {"id": "t1", "title": "写周报", "urgency": "today", "difficulty": "easy"},
            {"id": "t2", "title": "修重构Bug", "urgency": "today", "difficulty": "hard"},
            {"id": "t3", "title": "跟客户对齐需求", "urgency": "today", "difficulty": "medium"},
        ]

        commentary_set = set()
        order_set = set()

        for _ in range(30):
            res = generate_execution_suggestion(tasks)
            commentary_set.add(res["overall_commentary"])
            order_tuple = tuple(item["task_id"] for item in res["recommended_order"])
            order_set.add(order_tuple)

        # Must have generated more than 1 variation of commentary and ordering
        self.assertGreater(len(commentary_set), 1)
        self.assertGreater(len(order_set), 1)

    def test_deterministic_with_seed(self):
        tasks = [
            {"id": "t1", "title": "A", "urgency": "today", "difficulty": "easy"},
            {"id": "t2", "title": "B", "urgency": "today", "difficulty": "hard"},
        ]
        res1 = generate_execution_suggestion(tasks, seed=42)
        res2 = generate_execution_suggestion(tasks, seed=42)
        self.assertEqual(res1["strategy_name"], res2["strategy_name"])
        self.assertEqual(res1["overall_commentary"], res2["overall_commentary"])
        self.assertEqual(
            [i["task_id"] for i in res1["recommended_order"]],
            [i["task_id"] for i in res2["recommended_order"]],
        )

    def test_malformed_and_edge_inputs(self):
        tasks = [
            {"id": "t1", "title": "正常任务", "urgency": "unknown_value", "difficulty": "weird"},
            {"id": "t2", "title": "   ", "urgency": "hours"},  # Blank title
            "not a dict",
            None,
        ]
        res = generate_execution_suggestion(tasks)
        self.assertIsNotNone(res)
        # Should cleanly process the valid dicts without crashing
        self.assertTrue(res["total_pending"] >= 1)

    def test_large_task_count_performance_and_ordering(self):
        """Stress test with 600 tasks to verify performance and strict urgency hierarchy."""
        import time

        tasks = []
        urgencies = ["hours", "today", "days"]
        difficulties = ["easy", "medium", "hard"]
        for i in range(600):
            tasks.append({
                "id": f"task_{i}",
                "title": f"大规模测试任务 #{i}",
                "urgency": urgencies[i % 3],
                "difficulty": difficulties[i % 3],
                "completed": False,
            })

        t0 = time.perf_counter()
        res = generate_execution_suggestion(tasks, seed=99)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.2, f"Algorithm too slow for 600 tasks: {elapsed:.3f}s")
        self.assertEqual(res["total_pending"], 600)
        self.assertEqual(len(res["recommended_order"]), 600)

        # Confirm strict tier hierarchy: all hours tasks appear before any today task, which appear before any days task
        seen_today = False
        seen_days = False
        for item in res["recommended_order"]:
            u = item["urgency"]
            if u == "hours":
                self.assertFalse(seen_today, "Hours task ranked after a Today task!")
                self.assertFalse(seen_days, "Hours task ranked after a Days task!")
            elif u == "today":
                seen_today = True
                self.assertFalse(seen_days, "Today task ranked after a Days task!")
            elif u == "days":
                seen_days = True

    def test_single_task_randomness_and_title_sanitization(self):
        """Verify single task execution produces varied diagnoses and sanitizes blank titles."""
        task_blank_title = {
            "id": "single_blank",
            "title": "   ",
            "urgency": "today",
            "difficulty": "medium",
            "completed": False,
        }
        res = generate_execution_suggestion([task_blank_title], seed=12)
        self.assertEqual(res["total_pending"], 1)
        self.assertEqual(res["recommended_order"][0]["title"], "未命名任务")

        # Test randomness over multiple seeds for a single task
        diagnoses = set()
        comments = set()
        for seed in range(30):
            r = generate_execution_suggestion([task_blank_title], seed=seed)
            diagnoses.add(r["overall_commentary"])
            comments.add(r["recommended_order"][0]["humorous_comment"])
        self.assertGreater(len(diagnoses), 1, "Single task overall commentary lacks randomness!")
        self.assertGreater(len(comments), 1, "Single task humorous comment lacks randomness!")

    def test_render_suggestion_html_with_none_values(self):
        """Verify render_suggestion_html handles None and missing fields gracefully."""
        sug = {
            "title": None,
            "strategy_name": None,
            "strategy_desc": None,
            "overall_commentary": None,
            "created_at": None,
            "recommended_order": [
                {
                    "rank": None,
                    "title": None,
                    "urgency": None,
                    "urgency_label": None,
                    "difficulty": None,
                    "difficulty_label": None,
                    "humorous_comment": None,
                },
                "invalid_item",
            ],
        }
        html_out = render_suggestion_html(sug)
        self.assertIn("未命名任务", html_out)
        self.assertIn("稳定推进，按步就班！", html_out)

        # Non-dict input
        self.assertIn("无可用的建议内容", render_suggestion_html(None))


class TestDailyTaskStore(unittest.TestCase):
    """Verification of file storage, persistence, and CRUD operations."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "DailyTasks.json"
        self.store = DailyTaskStore(self.file_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_and_persist_tasks(self):
        task1 = self.store.add_task(
            title="测试任务 1",
            urgency="hours",
            difficulty="hard",
            description="很重要",
        )
        self.assertEqual(task1["title"], "测试任务 1")
        self.assertEqual(task1["urgency"], URGENCY_HOURS)
        self.assertEqual(task1["difficulty"], DIFFICULTY_HARD)
        self.assertFalse(task1["completed"])
        self.assertTrue(self.file_path.exists())

        # Verify disk persistence by creating a second store instance
        store2 = DailyTaskStore(self.file_path)
        tasks = store2.get_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "测试任务 1")
        self.assertEqual(tasks[0]["urgency"], URGENCY_HOURS)

    def test_update_and_delete_task(self):
        task = self.store.add_task("初始名称", "today", "medium")
        t_id = task["id"]

        updated = self.store.update_task(
            t_id,
            title="新名称",
            urgency="days",
            difficulty="easy",
            description="备注更新",
        )
        self.assertEqual(updated["title"], "新名称")
        self.assertEqual(updated["urgency"], URGENCY_DAYS)
        self.assertEqual(updated["difficulty"], DIFFICULTY_EASY)

        # Toggle completed
        toggled = self.store.toggle_completed(t_id)
        self.assertTrue(toggled["completed"])

        # Re-check from disk
        store2 = DailyTaskStore(self.file_path)
        reloaded = store2.get_task(t_id)
        self.assertTrue(reloaded["completed"])
        self.assertEqual(reloaded["title"], "新名称")

        # Delete
        self.assertTrue(self.store.delete_task(t_id))
        self.assertEqual(len(self.store.get_tasks()), 0)

        store3 = DailyTaskStore(self.file_path)
        self.assertEqual(len(store3.get_tasks()), 0)

    def test_invalid_task_operations(self):
        with self.assertRaises(ValueError):
            self.store.add_task("   ")

        with self.assertRaises(KeyError):
            self.store.update_task("non-existent-id", title="x")

        with self.assertRaises(KeyError):
            self.store.toggle_completed("non-existent-id")

    def test_save_and_retrieve_suggestions(self):
        suggestion = {
            "id": "sug_1",
            "title": "执行建议 - 2026-09-17",
            "strategy_name": "先吃青蛙流",
            "strategy_desc": "先干大Boss",
            "overall_commentary": "今日宜战斗",
            "recommended_order": [
                {
                    "rank": 1,
                    "task_id": "1",
                    "title": "任务A",
                    "urgency": "hours",
                    "urgency_label": "数小时内",
                    "difficulty": "hard",
                    "difficulty_label": "困难",
                    "humorous_comment": "别怕，冲！",
                }
            ],
            "total_pending": 1,
        }

        saved = self.store.save_suggestion(suggestion)
        self.assertEqual(saved["id"], "sug_1")

        # Check from disk
        store2 = DailyTaskStore(self.file_path)
        all_sug = store2.get_saved_suggestions()
        self.assertEqual(len(all_sug), 1)
        self.assertEqual(all_sug[0]["id"], "sug_1")
        self.assertEqual(all_sug[0]["strategy_name"], "先吃青蛙流")

        # Delete suggestion
        self.assertTrue(self.store.delete_suggestion("sug_1"))
        self.assertEqual(len(self.store.get_saved_suggestions()), 0)

    def test_load_with_utf8_bom(self):
        """Verify that files written with UTF-8 BOM are loaded cleanly without error."""
        bom_data = {
            "version": 1,
            "tasks": [
                {
                    "id": "bom_1",
                    "title": "BOM任务",
                    "urgency": "today",
                    "difficulty": "medium",
                    "completed": False,
                }
            ],
            "saved_suggestions": [],
        }
        self.file_path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(bom_data).encode("utf-8")
        )
        loaded_store = DailyTaskStore(self.file_path)
        tasks = loaded_store.get_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "BOM任务")

    def test_corrupted_json_auto_backup(self):
        """Verify that malformed JSON triggers an automatic backup file before state resets."""
        self.file_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        store = DailyTaskStore(self.file_path)
        self.assertEqual(len(store.get_tasks()), 0)

        # Check that a .corrupted.*.bak file was created
        bak_files = list(self.temp_dir.name.glob if hasattr(self.temp_dir.name, "glob") else Path(self.temp_dir.name).glob("*.corrupted.*.bak"))
        self.assertGreater(len(bak_files), 0, "Corrupted file backup was not created!")
        self.assertIn("NOT VALID JSON", bak_files[0].read_text(encoding="utf-8"))

    def test_saved_suggestions_invalid_total_pending_recovery(self):
        """Verify that invalid/empty total_pending in saved suggestions is recovered without losing records."""
        raw_data = {
            "version": 1,
            "tasks": [{"id": "t1", "title": "任务1", "urgency": "today", "difficulty": "medium"}],
            "saved_suggestions": [
                {"id": "s1", "title": "建议1", "total_pending": "", "recommended_order": [{"task_id": "t1"}]},
                {"id": "s2", "title": "建议2", "total_pending": "invalid", "recommended_order": []},
            ],
        }
        self.file_path.write_text(json.dumps(raw_data), encoding="utf-8")
        store = DailyTaskStore(self.file_path)
        self.assertEqual(len(store.get_tasks()), 1)
        saved = store.get_saved_suggestions()
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["total_pending"], 1)
        self.assertEqual(saved[1]["total_pending"], 0)

    def test_save_failure_rollback_and_error_propagation(self):
        """Verify that if disk saving fails, mutations raise OSError and roll back memory state."""
        self.store.add_task("现有任务", "today", "medium")
        self.assertEqual(len(self.store.get_tasks()), 1)

        # Patch save to return False simulating disk write failure
        with patch.object(self.store, "save", return_value=False):
            with self.assertRaises(OSError):
                self.store.add_task("失败的新任务")
            # In-memory tasks must still be 1 (rolled back!)
            self.assertEqual(len(self.store.get_tasks()), 1)

            with self.assertRaises(OSError):
                self.store.update_task(self.store.get_tasks()[0]["id"], title="修改的名字")
            # Title must remain old title (rolled back!)
            self.assertEqual(self.store.get_tasks()[0]["title"], "现有任务")

            with self.assertRaises(OSError):
                self.store.toggle_completed(self.store.get_tasks()[0]["id"])
            # Completed status must remain False (rolled back!)
            self.assertFalse(self.store.get_tasks()[0]["completed"])

            with self.assertRaises(OSError):
                self.store.delete_task(self.store.get_tasks()[0]["id"])
            # Task must still exist in memory (rolled back!)
            self.assertEqual(len(self.store.get_tasks()), 1)

    def test_store_load_with_null_tasks_and_null_suggestions(self):
        """Verify that JSON with null tasks or null saved_suggestions is safely recovered."""
        raw_data = {
            "version": 1,
            "tasks": None,
            "saved_suggestions": None,
        }
        self.file_path.write_text(json.dumps(raw_data), encoding="utf-8")
        store = DailyTaskStore(self.file_path)
        self.assertEqual(len(store.get_tasks()), 0)
        self.assertEqual(len(store.get_saved_suggestions()), 0)

    def test_store_load_with_null_fields_sanitization(self):
        """Verify that null titles are not converted into string 'None' and invalid tasks are skipped."""
        raw_data = {
            "version": 1,
            "tasks": [
                {"title": None, "description": None},
                {"title": "   ", "description": None},
                {"title": "有效任务", "description": None},
            ],
            "saved_suggestions": [
                {"title": None, "strategy_name": None, "strategy_desc": None, "overall_commentary": None}
            ],
        }
        self.file_path.write_text(json.dumps(raw_data), encoding="utf-8")
        store = DailyTaskStore(self.file_path)
        tasks = store.get_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "有效任务")
        self.assertEqual(tasks[0]["description"], "")

        suggestions = store.get_saved_suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["title"], "未命名建议")
        self.assertEqual(suggestions[0]["strategy_name"], "")

    def test_store_load_with_null_recommended_order(self):
        """Verify that null recommended_order does not crash load and subsequent suggestions are kept."""
        raw_data = {
            "version": 1,
            "tasks": [{"title": "任务1"}],
            "saved_suggestions": [
                {"id": "s1", "title": "建议1", "recommended_order": None},
                {"id": "s2", "title": "建议2", "recommended_order": []},
            ],
        }
        self.file_path.write_text(json.dumps(raw_data), encoding="utf-8")
        store = DailyTaskStore(self.file_path)
        self.assertEqual(len(store.get_saved_suggestions()), 2)
        self.assertEqual(store.get_saved_suggestions()[0]["recommended_order"], [])
        self.assertEqual(store.get_saved_suggestions()[1]["recommended_order"], [])

    def test_save_suggestion_repeated_unique_ids(self):
        """Verify saving the same suggestion multiple times generates distinct unique IDs."""
        suggestion = {
            "id": "fixed_id",
            "title": "测试建议",
            "recommended_order": [],
        }
        s1 = self.store.save_suggestion(suggestion)
        s2 = self.store.save_suggestion(suggestion)
        self.assertNotEqual(s1["id"], s2["id"])
        self.assertEqual(len(self.store.get_saved_suggestions()), 2)
        # Deleting s1 should not delete s2
        self.assertTrue(self.store.delete_suggestion(s1["id"]))
        self.assertEqual(len(self.store.get_saved_suggestions()), 1)
        self.assertEqual(self.store.get_saved_suggestions()[0]["id"], s2["id"])


class TestDailyTasksUIAndIntegration(unittest.TestCase):
    """Verification of plugin registration, menu integration, and UI flows."""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "DailyTasks.json"
        self.store = DailyTaskStore(self.file_path)
        self.fake_main = FakeMainWindow()
        self.host = PluginHost(self.fake_main)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_plugin_registration_and_open_manager(self):
        plugin = DailyTasksPlugin()
        plugin.store = self.store
        self.host.install(plugin)

        menu_bar = QtWidgets.QMenuBar(self.fake_main)
        plugin_menu = self.host.attach_main_menu(menu_bar)

        menu_actions = plugin_menu.actions()
        menu_titles = [act.text() for act in menu_actions]
        self.assertIn("每日任务管理", menu_titles)

        # Trigger command to open manager
        dialog = plugin.open_manager()
        try:
            self.assertIsNotNone(dialog)
            self.assertFalse(dialog.isModal())
            self.assertEqual(dialog.windowModality(), QtCore.Qt.NonModal)
            self.assertEqual(dialog.windowTitle(), "每日任务管理与智能建议")
        finally:
            dialog.close()

    def test_ui_add_task_and_persistence_across_window_reopen(self):
        # Create dialog
        dialog = DailyTasksDialog(self.store)
        try:
            self.assertEqual(dialog.task_table.rowCount(), 0)

            # Add tasks
            self.store.add_task("测试UI任务A", "hours", "hard", "描述A")
            self.store.add_task("测试UI任务B", "today", "easy", "描述B")
            dialog.refresh_tasks()

            self.assertEqual(dialog.task_table.rowCount(), 2)

            # Check table cell contents
            row0_title = dialog.task_table.item(0, 1).text()
            row1_title = dialog.task_table.item(1, 1).text()
            self.assertEqual(row0_title, "测试UI任务A")
            self.assertEqual(row1_title, "测试UI任务B")
        finally:
            dialog.close()

        # Reopen window with fresh dialog pointing to same store file
        reopened_store = DailyTaskStore(self.file_path)
        dialog2 = DailyTasksDialog(reopened_store)
        try:
            self.assertEqual(dialog2.task_table.rowCount(), 2)
            self.assertEqual(dialog2.task_table.item(0, 1).text(), "测试UI任务A")
            self.assertEqual(dialog2.task_table.item(1, 1).text(), "测试UI任务B")
        finally:
            dialog2.close()

    def test_ui_generate_and_save_suggestion(self):
        self.store.add_task("修复紧急漏洞", "hours", "hard")
        self.store.add_task("给绿植浇水", "days", "easy")

        dialog = DailyTasksDialog(self.store)
        try:
            self.assertFalse(dialog.save_btn.isEnabled())

            # Generate suggestion
            dialog.generate_suggestion_action()

            self.assertIsNotNone(dialog.current_suggestion)
            self.assertEqual(dialog.tabs.currentIndex(), 1)  # Switched to suggestion tab
            self.assertTrue(dialog.save_btn.isEnabled())
            self.assertEqual(dialog.suggestion_table.rowCount(), 2)

            # Check ranked order in suggestion table
            rank1_title = dialog.suggestion_table.item(0, 1).text()
            rank1_comment = dialog.suggestion_table.item(0, 4).text()
            self.assertEqual(rank1_title, "修复紧急漏洞")
            self.assertTrue(len(rank1_comment) > 0)

            # Save suggestion
            with patch.object(QtWidgets.QMessageBox, "information") as info_box:
                dialog.save_suggestion_action()
                info_box.assert_called_once()

            # Verify in store and Tab 3
            self.assertEqual(len(self.store.get_saved_suggestions()), 1)
            self.assertEqual(dialog.saved_list.count(), 1)

            # Test HTML rendering
            html = render_suggestion_html(dialog.current_suggestion)
            self.assertIn("修复紧急漏洞", html)
            self.assertIn("建议执行顺位表", html)
        finally:
            dialog.close()

    def test_task_edit_dialog_validation(self):
        edit_dlg = TaskEditDialog()
        try:
            with patch.object(QtWidgets.QMessageBox, "warning") as warn_box:
                edit_dlg.title_edit.setText("   ")
                edit_dlg.validate_and_accept()
                warn_box.assert_called_once()
                # Since title is empty, dialog should not accept
                self.assertNotEqual(edit_dlg.result(), QtWidgets.QDialog.Accepted)

            edit_dlg.title_edit.setText("有效标题")
            edit_dlg.validate_and_accept()
            self.assertEqual(edit_dlg.result(), QtWidgets.QDialog.Accepted)
            data = edit_dlg.get_data()
            self.assertEqual(data["title"], "有效标题")
        finally:
            edit_dlg.close()

    def test_dialog_lifecycle_and_reopen_on_destruction(self):
        """Verify plugin lifecycle handles dialog destruction safely."""
        plugin = DailyTasksPlugin()
        plugin.store = self.store
        self.host.install(plugin)

        dialog = plugin.open_manager()
        self.assertIsNotNone(dialog)
        self.assertIs(plugin.dialog, dialog)

        # Simulate widget destruction
        dialog.close()
        dialog.deleteLater()
        QtWidgets.QApplication.sendPostedEvents()

        # Re-opening manager must create a fresh, valid dialog without crashing
        dialog2 = plugin.open_manager()
        try:
            self.assertIsNotNone(dialog2)
            self.assertEqual(dialog2.windowTitle(), "每日任务管理与智能建议")
        finally:
            dialog2.close()

    def test_table_double_click_and_button_signals(self):
        """Verify that edit action correctly handles row numbers from cellDoubleClicked and QPushButton False checked."""
        self.store.add_task("首项任务", "hours", "hard")
        self.store.add_task("次项任务", "today", "easy")

        dialog = DailyTasksDialog(self.store)
        try:
            dialog.refresh_tasks()
            self.assertEqual(dialog.task_table.rowCount(), 2)

            # Test simulating double-click on row 1: passes (1, 0)
            with patch.object(TaskEditDialog, "exec_", return_value=QtWidgets.QDialog.Rejected):
                dialog.edit_task_action(1, 0)
                # Confirm row 1 is now selected
                self.assertEqual(dialog.task_table.currentRow(), 1)

            # Test button click which passes boolean False (checked)
            # Should NOT override existing selected row 1 to row 0
            with patch.object(TaskEditDialog, "exec_", return_value=QtWidgets.QDialog.Rejected):
                dialog.edit_task_action(False)
                self.assertEqual(dialog.task_table.currentRow(), 1)
        finally:
            dialog.close()

    def test_table_right_click_context_menu_selection(self):
        """Verify that right clicking on a table row selects that row and updates context menu actions."""
        self.store.add_task("任务A", "today", "medium")
        self.store.add_task("任务B", "today", "medium")

        dialog = DailyTasksDialog(self.store)
        try:
            dialog.show()
            self.assertEqual(dialog.task_table.currentRow(), 0)

            # Right click on row 1
            rect = dialog.task_table.visualRect(dialog.task_table.model().index(1, 0))
            with patch.object(QtWidgets.QMenu, "exec_"):
                dialog._show_task_context_menu(rect.center())

            self.assertEqual(dialog.task_table.currentRow(), 1)
            self.assertEqual(dialog._selected_task_id(), self.store.get_tasks()[1]["id"])

            # Right click on empty space below rows
            with patch.object(QtWidgets.QMenu, "exec_"):
                dialog._show_task_context_menu(QtCore.QPoint(50, 900))
            self.assertEqual(dialog.task_table.currentRow(), -1)
            self.assertIsNone(dialog._selected_task_id())
        finally:
            dialog.close()

    def test_refresh_tasks_preserves_selection(self):
        """Verify that refresh_tasks preserves the selected row instead of resetting it."""
        self.store.add_task("任务1", "today", "medium")
        self.store.add_task("任务2", "today", "medium")
        self.store.add_task("任务3", "today", "medium")

        dialog = DailyTasksDialog(self.store)
        try:
            dialog.task_table.setCurrentCell(1, 0)
            self.assertEqual(dialog.task_table.currentRow(), 1)

            dialog.refresh_tasks()
            self.assertEqual(dialog.task_table.currentRow(), 1)

            # Toggle task 2 completion
            dialog.toggle_completed_action()
            self.assertEqual(dialog.task_table.currentRow(), 1)
        finally:
            dialog.close()

    def test_keyboard_shortcuts_and_row_resize(self):
        """Verify delete, return, numpad enter, space, insert, and ctrl+N shortcuts are installed, and suggestion table auto-resizes rows."""
        self.store.add_task("紧急攻坚任务", "hours", "hard")
        dialog = DailyTasksDialog(self.store)
        try:
            # Check shortcuts exist and have valid key sequences
            self.assertEqual(dialog.del_task_shortcut.key().toString(), "Del")
            self.assertEqual(dialog.edit_task_shortcut.key().toString(), "Return")
            self.assertEqual(dialog.edit_task_enter_shortcut.key().toString(), "Enter")
            self.assertEqual(dialog.toggle_task_shortcut.key().toString(), "Space")
            self.assertEqual(dialog.add_task_shortcut.key().toString(), "Ctrl+N")
            self.assertEqual(dialog.add_task_ins_shortcut.key().toString(), "Ins")
            self.assertEqual(dialog.del_saved_shortcut.key().toString(), "Del")

            # Test space shortcut triggers toggle_completed_action
            self.assertFalse(self.store.get_tasks()[0]["completed"])
            dialog.toggle_task_shortcut.activated.emit()
            self.assertTrue(self.store.get_tasks()[0]["completed"])
            dialog.toggle_task_shortcut.activated.emit()
            self.assertFalse(self.store.get_tasks()[0]["completed"])

            # Generate suggestion and check row resizing
            dialog.generate_suggestion_action()
            self.assertEqual(dialog.suggestion_table.rowCount(), 1)
            # Row height should be resized to fit text
            self.assertGreaterEqual(dialog.suggestion_table.rowHeight(0), 24)
        finally:
            dialog.close()

    def test_refresh_saved_list_with_null_title_and_order(self):
        """Verify that saved suggestions with null title and recommended_order do not crash refresh_saved_list."""
        dialog = DailyTasksDialog(self.store)
        try:
            self.store._data["saved_suggestions"].append({
                "id": "s_null",
                "title": None,
                "created_at": None,
                "recommended_order": None,
                "total_pending": None,
            })
            dialog.refresh_saved_list()
            self.assertEqual(dialog.saved_list.count(), 1)
            item_text = dialog.saved_list.item(0).text()
            self.assertIn("未命名建议", item_text)
        finally:
            dialog.close()

    def test_add_task_switches_completed_filter(self):
        """Verify that adding a task while viewing completed tasks switches filter to all tasks so the new task is visible."""
        self.store.add_task("已完成旧任务", "today", "medium")
        t_id = self.store.get_tasks()[0]["id"]
        self.store.toggle_completed(t_id)

        dialog = DailyTasksDialog(self.store)
        try:
            dialog.filter_combo.setCurrentIndex(2)  # "仅看已完成"
            self.assertEqual(dialog.filter_combo.currentData(), "completed")
            self.assertEqual(dialog.task_table.rowCount(), 1)

            # Add a new task via TaskEditDialog
            with patch.object(
                TaskEditDialog,
                "exec_",
                return_value=QtWidgets.QDialog.Accepted,
            ), patch.object(
                TaskEditDialog,
                "get_data",
                return_value={
                    "title": "新待办任务",
                    "urgency": "today",
                    "difficulty": "medium",
                    "description": "",
                },
            ):
                dialog.add_task_action()

            # Filter combo should have automatically switched back to index 0 ("all")
            self.assertEqual(dialog.filter_combo.currentIndex(), 0)
            self.assertEqual(dialog.task_table.rowCount(), 2)
        finally:
            dialog.close()

    def test_ui_error_handling_when_store_save_fails(self):
        """Verify that UI actions catch save failures and display critical message boxes."""
        self.store.add_task("现有任务", "today", "medium")
        dialog = DailyTasksDialog(self.store)
        try:
            dialog.task_table.setCurrentCell(0, 0)
            with patch.object(self.store, "save", return_value=False), patch.object(
                QtWidgets.QMessageBox, "critical"
            ) as crit_box:
                dialog.toggle_completed_action()
                crit_box.assert_called_once()
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()

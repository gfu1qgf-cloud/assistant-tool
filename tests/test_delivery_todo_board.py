import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.task_delivery_board import DeliveryBoardDialog
from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
from model.DeliveryTodoStore import DeliveryTodoStore, collect_delivery_todos
from types import SimpleNamespace
from unittest import mock


class DeliveryTodoBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_sources_are_deduplicated_by_id_and_revision(self):
        upload = [{
            "event_id": "upload-1", "file_name": "A.mp4", "drive_file_id": "file-1",
            "drive_link": "https://drive.google.com/file/d/file-1/view",
            "batch_date": "2026-09-28", "batch_slot": "1",
            "task": {"admin": "张三"},
            "task_submission": {"status": "failed", "reason": "网络错误"},
        }]
        review = [{
            "key": "google:file-1", "name": "A.mp4", "admin": "张三",
            "link": "https://drive.google.com/file/d/file-1/view",
            "review_revision": "v1", "status": "needs_changes", "note": "改片尾",
        }]
        daily = {"2026-09-28": {"people": {}, "task_sheet_failures": {
            "1": {"A.mp4": {"reason": "网络错误", "saved_at": "2026-09-28"}},
        }}}
        sources = collect_delivery_todos(upload, review, daily)
        self.assertEqual(len(sources), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "todos.sqlite3"
            store = DeliveryTodoStore(path)
            self.assertEqual(store.add_sources(sources), 2)
            self.assertEqual(store.add_sources(sources), 0)
            review_id = next(source["id"] for source in sources if source["kind"] == "rework")
            store.complete(review_id)
            self.assertEqual(store.add_sources(sources), 0)
            self.assertEqual(len(store.list_tasks("completed")), 1)
            revision = dict(review[0], review_revision="v2")
            self.assertEqual(store.add_sources(collect_delivery_todos([], [revision], {})), 1)
            self.assertEqual(len(store.list_tasks()), 2)
            self.assertEqual(store.mark_old_review_versions([revision]), 0)
            self.assertEqual(len(DeliveryTodoStore(path).list_tasks()), 2)

    def test_new_review_revision_marks_unfinished_old_version(self):
        review = {
            "key": "google:file-1", "name": "A.mp4", "status": "passed",
            "link": "https://drive.google.com/file/d/file-1/view",
            "review_revision": "v1",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            store.add_sources(collect_delivery_todos([], [review], {}))
            newer = dict(review, review_revision="v2")
            store.add_sources(collect_delivery_todos([], [newer], {}))
            self.assertEqual(store.mark_old_review_versions([newer]), 1)
            by_revision = {task["review_revision"]: task for task in store.list_tasks()}
            self.assertEqual(by_revision["v1"]["stale"], 1)
            self.assertEqual(by_revision["v2"]["stale"], 0)
            self.assertEqual(store.mark_old_review_versions([newer]), 0)

    def test_older_upload_failure_is_not_imported_after_success(self):
        old = {
            "event_id": "old", "logical_key": "video-a", "file_name": "A.mp4",
            "recorded_at": "2026-09-27T10:00:00", "batch_date": "2026-09-27",
            "batch_slot": "1", "task_submission": {"status": "failed"},
        }
        new = dict(old, event_id="new", recorded_at="2026-09-27T11:00:00",
                   task_submission={"status": "confirmed"})
        daily = {"2026-09-27": {"people": {}, "task_sheet_failures": {
            "1": {"A.mp4": {"reason": "旧错误", "saved_at": "2026-09-27"}},
        }}}
        self.assertEqual(collect_delivery_todos([old, new], [], daily), [])

    def test_existing_database_schema_is_upgraded_without_losing_tasks(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "todos.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("""CREATE TABLE todos (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', admin TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '', local_file TEXT NOT NULL DEFAULT '',
                    source_time TEXT NOT NULL DEFAULT '', review_key TEXT NOT NULL DEFAULT '',
                    quadrant INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open',
                    created_at REAL NOT NULL, completed_at REAL
                )""")
                db.execute("INSERT INTO todos(id,kind,title,created_at) VALUES ('old','manual','旧待办',1)")
                db.commit()
            finally:
                db.close()
            store = DeliveryTodoStore(path)
            task = store.list_tasks()[0]
            self.assertEqual(task["title"], "旧待办")
            self.assertEqual(task["review_revision"], "")
            self.assertEqual(task["stale"], 0)

    def test_only_user_completion_moves_task_and_archives_later(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            task_id = store.add_manual("发送链接", "给管理员", quadrant=0)
            store.add_sources([])
            self.assertEqual(len(store.list_tasks("open")), 1)
            store.set_quadrant(task_id, 2)
            self.assertEqual(store.list_tasks("open")[0]["quadrant"], 2)
            self.assertEqual(store.complete(task_id, now=1_000_000), 1)
            self.assertEqual(len(store.list_tasks("open")), 0)
            self.assertEqual(store.archive_completed(now=1_000_000 + 2 * 86400), 0)
            self.assertEqual(store.archive_completed(now=1_000_000 + 3 * 86400), 1)
            self.assertEqual(len(store.list_tasks("archived")), 1)
            store.restore(task_id)
            self.assertEqual(len(store.list_tasks("open")), 1)

    def test_quadrant_board_completion_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            task_id = store.add_manual("修改片尾", "客户要求更短", quadrant=0)
            dialog = DeliveryBoardDialog(store)
            self.assertEqual(dialog.lists[0].count(), 1)
            dialog.move_task(task_id, 1)
            self.assertEqual(dialog.lists[0].count(), 0)
            self.assertEqual(dialog.lists[1].count(), 1)
            dialog.lists[1].item(0).setCheckState(QtCore.Qt.CheckState.Checked)
            self.app.processEvents()
            self.assertEqual(dialog.lists[1].count(), 0)
            self.assertEqual(dialog.completed_tree.topLevelItemCount(), 1)
            dialog.tabs.setCurrentWidget(dialog.completed_tree)
            dialog.completed_tree.setCurrentItem(dialog.completed_tree.topLevelItem(0))
            dialog.restore_selected()
            self.assertEqual(dialog.lists[1].count(), 1)
            dialog.close()

    def test_plugin_opens_the_existing_delivery_todo_entry_as_board(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = TaskDeliveryPlugin()
            plugin.todo_store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            plugin.context = SimpleNamespace(
                load_config=lambda: {}, parent_widget=None,
                log=lambda _message: None,
            )
            plugin.controller = SimpleNamespace(daily_link_history={})
            with mock.patch("app_plugins.builtin.task_delivery.load_video_upload_history",
                            return_value={"records": []}), mock.patch(
                "app_plugins.builtin.task_delivery.review_history_snapshot",
                return_value={"all": []},
            ):
                dialog = plugin.open_delivery_inbox()
            self.assertIsInstance(dialog, DeliveryBoardDialog)
            self.assertTrue(dialog.isVisible())
            dialog.close()


if __name__ == "__main__":
    unittest.main()

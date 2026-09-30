import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.task_delivery_board import DeliveryBoardDialog
from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
from model.DeliveryTodoStore import DeliveryTodoStore, collect_delivery_todos
from model.DeliveryTodoMedia import verify_sendable_folders
from model.ClipboardHelper import INTERNAL_CLIPBOARD_MIME, set_internal_clipboard_links
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
            self.assertEqual(store.add_sources(collect_delivery_todos([], [newer], {})), 0)
            self.assertEqual(store.mark_old_review_versions([newer]), 0)
            self.assertEqual(len(store.list_tasks()), 1)
            import json
            item = json.loads(store.list_tasks()[0]["items_json"])[0]
            self.assertEqual(item["review_revision"], "v2")
            self.assertEqual(store.mark_old_review_versions([newer]), 0)

    def test_send_reminders_group_no_review_and_approved_by_day_and_admin(self):
        import json
        uploads = [
            {"event_id": f"e{n}", "logical_key": f"video-{n}",
             "file_name": f"{n}.mp4", "drive_file_id": f"file-{n}",
             "drive_link": f"https://drive.google.com/file/d/file-{n}/view",
             "batch_date": day, "review_routed": False,
             "task": {"admin": admin}, "task_submission": {"status": "confirmed"}}
            for n, day, admin in (
                (1, "2026-09-28", "张三"), (2, "2026-09-28", "张三"),
                (3, "2026-09-29", "张三"), (4, "2026-09-28", "李四"),
            )
        ]
        approved = {
            "key": "google:file-5", "name": "5.mp4", "admin": "张三",
            "link": "https://drive.google.com/file/d/file-5/view",
            "review_revision": "v1", "status": "passed",
            "status_updated_at": "2026-09-28T15:00:00",
        }
        groups = [source for source in collect_delivery_todos(uploads, [approved], {})
                  if source["kind"] == "send"]
        self.assertEqual(len(groups), 3)
        together = next(source for source in groups if "2026-09-28" in source["title"]
                        and "张三" in source["title"])
        self.assertEqual({item["name"] for item in together["items"]},
                         {"1.mp4", "2.mp4", "5.mp4"})
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            self.assertEqual(store.add_sources(groups), 3)
            row = next(row for row in store.list_tasks() if row["group_key"] == together["id"])
            self.assertEqual(len(json.loads(row["items_json"])), 3)
            store.complete(row["id"])
            self.assertEqual(store.add_sources(groups), 0)
            updated = dict(together, items=together["items"] + [{
                "id": "upload:e6", "name": "6.mp4", "link": "https://drive.google.com/file/d/file-6/view",
                "note": "", "source_time": "2026-09-28T17:00:00",
            }])
            self.assertEqual(store.add_sources([updated]), 1)
            open_group = next(row for row in store.list_tasks() if row["group_key"] == together["id"])
            self.assertEqual([item["name"] for item in json.loads(open_group["items_json"])], ["6.mp4"])

    def test_pending_review_is_not_mistaken_for_no_review(self):
        upload = [{"event_id": "e1", "file_name": "A.mp4", "drive_file_id": "file-1",
                   "drive_link": "https://drive.google.com/file/d/file-1/view",
                   "batch_date": "2026-09-28", "review_routed": True,
                   "task": {"admin": "张三"}}]
        review = [{"key": "google:file-1", "status": "pending"}]
        self.assertEqual(collect_delivery_todos(upload, review, {}), [])

    def test_old_individual_card_is_hidden_but_preserved_after_grouping(self):
        import sqlite3
        review = {"key": "google:file-1", "name": "A.mp4", "status": "passed",
                  "link": "https://drive.google.com/file/d/file-1/view",
                  "review_revision": "v1", "admin": "张三"}
        source = collect_delivery_todos([], [review], {})[0]
        child_id = source["items"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            store.add_sources([{"id": child_id, "kind": "send", "title": "旧视频卡"}])
            self.assertEqual(store.add_sources([source]), 1)
            self.assertEqual(len(store.list_tasks()), 1)
            with store._connect() as db:
                self.assertEqual(db.execute("SELECT status FROM todos WHERE id=?", (child_id,)).fetchone()[0], "merged")

    def test_group_card_exposes_each_video_link_for_copying(self):
        review = [
            {"key": f"google:file-{n}", "name": f"{n}.mp4", "admin": "张三",
             "link": f"https://drive.google.com/file/d/file-{n}/view",
             "review_revision": "v1", "status": "passed",
             "status_updated_at": "2026-09-28T15:00:00"}
            for n in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            store.add_sources(collect_delivery_todos([], review, {}))
            dialog = DeliveryBoardDialog(store)
            row = store.list_tasks()[0]
            with mock.patch("app_plugins.builtin.task_delivery_board.set_internal_clipboard_links") as copy:
                dialog._copy_id(row["id"])
                self.assertEqual([link for _name, link in copy.call_args.args[0]],
                                 [review[0]["link"], review[1]["link"]])
            seen = []
            def inspect_details(modal):
                table = modal.findChild(QtWidgets.QTableWidget)
                seen.extend(table.item(index, 1).text() for index in range(table.rowCount()))
                return QtWidgets.QDialog.DialogCode.Accepted
            with mock.patch.object(QtWidgets.QDialog, "exec", inspect_details):
                dialog._show_details(row["id"])
            self.assertEqual(seen, ["1.mp4", "2.mp4"])
            dialog.close()

    def test_clipboard_links_include_rich_hyperlinks_and_plain_urls(self):
        set_internal_clipboard_links([("视频 A", "https://drive.google.com/file/d/a/view")])
        mime = self.app.clipboard().mimeData()
        self.assertEqual(mime.text(), "https://drive.google.com/file/d/a/view")
        self.assertIn('href="https://drive.google.com/file/d/a/view"', mime.html())
        self.assertIn("视频 A", mime.html())
        self.assertTrue(mime.hasFormat(INTERNAL_CLIPBOARD_MIME))

    def test_folder_is_offered_only_when_every_remote_child_is_in_send_batch(self):
        items = [{"folder_id": "folder-1", "drive_file_id": "a"},
                 {"folder_id": "folder-1", "drive_file_id": "b"}]
        files = SimpleNamespace()
        service = SimpleNamespace(files=lambda: files)
        def list_files(**_kwargs):
            return SimpleNamespace(execute=lambda: {"files": [
                {"id": "a", "mimeType": "video/mp4"},
                {"id": "b", "mimeType": "video/mp4"},
            ]})
        files.list = list_files
        self.assertEqual(verify_sendable_folders(items, service), {
            "folder-1": "https://drive.google.com/drive/folders/folder-1",
        })
        items.pop()
        self.assertEqual(verify_sendable_folders(items, service), {})
        items.append({"folder_id": "folder-1", "drive_file_id": "b"})
        files.list = lambda **_kwargs: SimpleNamespace(execute=lambda: {"files": [
            {"id": "a", "mimeType": "video/mp4"},
            {"id": "b", "mimeType": "video/mp4"},
            {"id": "extra", "mimeType": "application/vnd.google-apps.folder"},
        ]})
        self.assertEqual(verify_sendable_folders(items, service), {})

    def test_review_item_uses_matching_upload_for_preview_and_folder(self):
        upload = [{"event_id": "e1", "file_name": "A.mp4", "drive_file_id": "file-1",
                   "drive_link": "https://drive.google.com/file/d/file-1/view",
                   "batch_date": "2026-09-28", "review_routed": True,
                   "local_file": "C:/videos/A.mp4", "remote_parent_id": "folder-1"}]
        review = [{"key": "google:file-1", "name": "A.mp4", "status": "passed",
                   "link": upload[0]["drive_link"], "review_revision": "v1"}]
        item = collect_delivery_todos(upload, review, {})[0]["items"][0]
        self.assertEqual(item["local_file"], "C:/videos/A.mp4")
        self.assertEqual(item["folder_id"], "folder-1")
        self.assertEqual(item["drive_file_id"], "file-1")

    def test_detail_ctrl_c_copies_link_not_filename(self):
        review = [{"key": "google:file-1", "name": "A.mp4", "status": "passed",
                   "link": "https://drive.google.com/file/d/file-1/view",
                   "review_revision": "v1"}]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            store.add_sources(collect_delivery_todos([], review, {}))
            dialog = DeliveryBoardDialog(store)
            row = store.list_tasks()[0]
            def inspect_details(modal):
                table = modal.findChild(QtWidgets.QTableWidget)
                table.selectRow(0)
                event = QtGui.QKeyEvent(
                    QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C,
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                )
                table.keyPressEvent(event)
                self.assertEqual(self.app.clipboard().text(), review[0]["link"])
                return QtWidgets.QDialog.DialogCode.Accepted
            with mock.patch.object(QtWidgets.QDialog, "exec", inspect_details):
                dialog._show_details(row["id"])
            dialog.close()

    def test_board_ctrl_c_copies_drive_link(self):
        review = [{"key": "google:file-1", "name": "A.mp4", "status": "passed",
                   "link": "https://drive.google.com/file/d/file-1/view",
                   "review_revision": "v1"}]
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTodoStore(Path(directory) / "todos.sqlite3")
            store.add_sources(collect_delivery_todos([], review, {}))
            dialog = DeliveryBoardDialog(store)
            card_list = dialog.lists[0]
            card_list.setCurrentItem(card_list.item(0))
            event = QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C,
                QtCore.Qt.KeyboardModifier.ControlModifier,
            )
            card_list.keyPressEvent(event)
            self.assertEqual(self.app.clipboard().text(), review[0]["link"])
            dialog.close()

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

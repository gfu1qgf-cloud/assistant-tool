import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets
from model.DailyLinkArchive import (
    load_daily_link_archive,
    recover_daily_link_archive,
)
from model.DailyLinkHistory import (
    DAILY_LINK_HISTORY_CONFIG_KEY,
    daily_link_counts,
    format_daily_links,
    normalize_daily_link_history,
)
from PYUI.daily_links_pyui import DailyLinksDialog
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController


def _day_entry(link, person="Alice", slot="03"):
    return {"people": {person: {slot: {"link": link}}}}


class DailyLinkArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_recovers_backups_and_delivery_log_without_exposing_links(self):
        today = date.today()
        older = (today - timedelta(days=10)).isoformat()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_key = today.isoformat()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json.bak-settings-old").write_text(json.dumps({
                DAILY_LINK_HISTORY_CONFIG_KEY: {
                    older: _day_entry("https://example.test/old"),
                }
            }), encoding="utf-8")
            (root / "config.json").write_text(json.dumps({
                DAILY_LINK_HISTORY_CONFIG_KEY: {
                    today_key: _day_entry("https://example.test/today"),
                }
            }), encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            (logs / "assistant-tool.log.1").write_text(
                f"{yesterday} 21:43:10,100 | INFO | [插件/task_delivery] "
                "各人员 Google Drive 文件夹链接（可直接分发）：\n"
                f"{yesterday} 21:43:10,120 | INFO | [插件/task_delivery] "
                "Bob：https://drive.google.com/drive/folders/abc_123\n"
                f"{yesterday} 21:43:10,140 | INFO | [插件/task_delivery] "
                f"每日链接已保存 {yesterday} / 批次 03：1 人\n",
                encoding="utf-8",
            )
            recovered = recover_daily_link_archive(root)
            self.assertEqual(set(recovered), {older, yesterday, today_key})
            self.assertEqual(daily_link_counts(recovered, older, retain_all=True), (1, 1))
            self.assertIn("example.test/old", format_daily_links(
                recovered, older, retain_all=True
            ))
            archive = load_daily_link_archive(root=root)
            self.assertEqual(archive, recovered)
            self.assertTrue((root / "DailyLinkArchive.json").is_file())

            dialog = DailyLinksDialog(archive)
            try:
                self.assertEqual(dialog.date_combo.count(), 3)
            finally:
                dialog.close()

    def test_live_config_save_does_not_drop_disk_links_or_failure_only_day(self):
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_key = today.isoformat()
        existing = {
            yesterday: {
                **_day_entry("https://example.test/yesterday"),
                "task_sheet_failures": {"03": {
                "video.mp4": {"reason": "need check"},
                }},
            },
        }
        controller = SimpleNamespace(
            gemini_api_keys=[], global_hotkey="Ctrl+Alt+F9",
            daily_link_history={today_key: _day_entry("https://example.test/today")},
        )
        config = {DAILY_LINK_HISTORY_CONFIG_KEY: existing}
        TaskDeliveryController.update_config(controller, config)
        normalized = normalize_daily_link_history(config[DAILY_LINK_HISTORY_CONFIG_KEY])
        self.assertIn(yesterday, normalized)
        self.assertEqual(normalized[today_key]["people"]["Alice"]["03"]["link"],
                         "https://example.test/today")
        self.assertIn("video.mp4", normalized[yesterday]["task_sheet_failures"]["03"])


if __name__ == "__main__":
    unittest.main()

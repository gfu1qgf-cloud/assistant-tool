"""Export blockers must be directly visible and actionable, not just counted."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_video_editor.engine import (
    build_script_word_records,
    smart_video_export_blockers,
)
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog


class ExportBlockersUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dialogs = []
        for target in (
            "app_plugins.builtin.smart_video_editor.timeline_review.FfplayReviewSurface.prepare",
            "app_plugins.builtin.smart_video_editor.timeline_review.WaveformLoader.load_task",
        ):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "app_plugins.builtin.smart_video_editor.timeline_review._media_shape",
            return_value=(1080, 1920, 30),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def clip(self, name, start, end, text, similarity=1.0):
        source = self.root / name
        source.write_bytes(name.encode("utf-8"))
        return {
            "file_name": name, "source": str(source), "included": True,
            "status": "green", "original_duration": 4.0,
            "trim_start": 0.0, "trim_end": 4.0, "kept_ranges": [[0.0, 4.0]],
            "script_word_start": start, "script_word_end": end,
            "content_order_start": start, "content_order_end": end,
            "content_order_reliable": True, "recognized_text": text,
            "expected_text": text, "similarity": similarity, "issues": [],
        }

    def bundle(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel"
        tasks = [{"task_id": "clean", "label": "正常任务", "clips": [
            self.clip("clean.mp4", 0, 7, text),
        ]}]
        clips = [
            self.clip(f"normal-{index}.mp4", index * 8, index * 8 + 7,
                      text if index == 5 else f"speech number {index}" )
            for index in range(12)
        ]
        clips.append(self.clip("extra-hidden-at-end.mp4", 40, 47, text, 0.8))
        for index, clip in enumerate(clips):
            clip["source_index"] = index
            clip["export_order"] = index + 1
        tasks.append({"task_id": "many", "label": "很多片段的任务", "clips": clips})
        return {"tasks": tasks, "settings": {}, "summary": {}}

    def dialog(self, bundle=None):
        dialog = SmartVideoReviewDialog(bundle or self.bundle())
        self.dialogs.append(dialog)
        return dialog

    def test_green_extra_is_visible_and_automatically_located_among_many_clips(self):
        dialog = self.dialog()
        self.assertEqual(len(smart_video_export_blockers(dialog.bundle)), 1)
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.blocker_panel.isHidden())
        self.assertEqual(dialog.blocker_tree.topLevelItemCount(), 1)
        item = dialog.blocker_tree.topLevelItem(0)
        self.assertEqual(item.text(2), "extra-hidden-at-end.mp4")
        self.assertIn("很多片段的任务", item.toolTip(2))
        self.assertEqual(dialog.timeline_review.task_index, 1)
        self.assertEqual(dialog.timeline_review.current_segment["clip_index"], 12)
        marker = dialog.timeline_review.issue_tree.currentItem().data(0, QtCore.Qt.UserRole)
        self.assertEqual(marker["kind"], "extra_clip")
        self.assertEqual(marker["clip_index"], 12)
        self.assertTrue(marker["export_blocker"])
        self.assertTrue(dialog.exclude_blocker_button.isEnabled())

    def test_exclude_directly_from_blocker_list_unlocks_without_deleting_source(self):
        dialog = self.dialog()
        source = Path(dialog.bundle["tasks"][1]["clips"][12]["source"])
        dialog.exclude_blocker_button.click()
        self.assertFalse(dialog.bundle["tasks"][1]["clips"][12]["included"])
        self.assertTrue(source.is_file())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertTrue(dialog.blocker_panel.isHidden())
        self.assertFalse(smart_video_export_blockers(dialog.bundle))

    def test_confirm_retaining_selected_extra_unlocks_and_keeps_it_included(self):
        dialog = self.dialog()
        dialog.approve_blocker_button.click()
        clip = dialog.bundle["tasks"][1]["clips"][12]
        self.assertTrue(clip["included"])
        self.assertTrue(clip["extra_clip_approved"])
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertFalse(any(marker.get("kind") == "extra_clip" for marker in dialog.timeline_review.markers))

    def test_empty_filter_does_not_unlock_export_or_leave_destructive_actions_enabled(self):
        dialog = self.dialog()
        dialog.blocker_filter_combo.setCurrentIndex(dialog.blocker_filter_combo.findData("missing"))
        self.assertIsNone(dialog._selected_export_blocker())
        self.assertFalse(dialog.exclude_blocker_button.isEnabled())
        self.assertFalse(dialog.approve_blocker_button.isEnabled())
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.blocker_panel.isHidden())
        self.assertIn("显示 0 / 共 1", dialog.blocker_count_label.text())
        dialog.blocker_filter_combo.setCurrentIndex(dialog.blocker_filter_combo.findData("extra_clip"))
        self.assertEqual(dialog._selected_export_blocker()["clip_index"], 12)

    def test_task_and_issue_filters_use_exact_export_gate_not_generic_severity(self):
        bundle = self.bundle()
        bundle["tasks"][0]["clips"][0]["issues"] = [{
            "severity": "pink", "title": "普通差异", "start": 1.0, "end": 2.0,
        }]
        dialog = self.dialog(bundle)
        timeline = dialog.timeline_review
        for mode in ("blocked", "extra_clip"):
            timeline.task_filter_combo.setCurrentIndex(timeline.task_filter_combo.findData(mode))
            self.assertEqual(timeline.task_combo.count(), 1)
            self.assertEqual(timeline.task_combo.itemData(0), 1)
            timeline.issue_filter_combo.setCurrentIndex(timeline.issue_filter_combo.findData(mode))
            self.assertEqual(timeline.issue_tree.topLevelItemCount(), 1)
            self.assertEqual(timeline.issue_tree.topLevelItem(0).data(0, QtCore.Qt.UserRole)["kind"], "extra_clip")
        dialog.approve_blocker_button.click()
        self.assertEqual(timeline.task_combo.count(), 0)
        self.assertTrue(dialog.export_button.isEnabled())

    def test_locator_recovers_task_hidden_by_missing_only_filter(self):
        dialog = self.dialog()
        timeline = dialog.timeline_review
        timeline.task_filter_combo.setCurrentIndex(timeline.task_filter_combo.findData("missing"))
        self.assertEqual(timeline.task_combo.count(), 0)
        dialog.locate_blocker_button.click()
        self.assertEqual(timeline.task_combo.currentData(), 1)
        self.assertEqual(timeline.current_segment["clip_index"], 12)
        self.assertEqual(timeline.issue_filter_combo.currentData(), "blocked")

    def test_every_live_missing_blocker_has_marker_even_when_cached_list_is_stale(self):
        bundle = self.bundle()
        script = "alpha bravo charlie delta echo foxtrot golf hotel"
        lines, words = build_script_word_records(script)
        task = {"task_id": "gap", "label": "缺段任务", "script": script,
                "script_lines": lines, "script_words": words, "missing_blocks": [],
                "clips": [self.clip("missing.mp4", 0, 3, "alpha bravo charlie delta")]}
        bundle["tasks"].append(task)
        dialog = self.dialog(bundle)
        blockers = smart_video_export_blockers(dialog.bundle)
        missing = next(block for block in blockers if block.get("kind") != "extra_clip")
        dialog.blocker_filter_combo.setCurrentIndex(dialog.blocker_filter_combo.findData("missing"))
        dialog.locate_blocker_button.click()
        timeline = dialog.timeline_review
        self.assertEqual(timeline.task_index, 2)
        marker = timeline.issue_tree.currentItem().data(0, QtCore.Qt.UserRole)
        self.assertEqual(marker["kind"], "missing")
        self.assertEqual(marker["expected"], missing["text"])
        self.assertTrue(marker["export_blocker"])
        self.assertFalse(dialog.exclude_blocker_button.isEnabled())
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertTrue(timeline.approve_task_button.isEnabled())
        self.assertTrue(timeline.skip_task_button.isEnabled())
        timeline.skip_task_button.click()
        self.assertFalse(any(block.get("kind") != "extra_clip" for block in dialog._export_blockers))
        self.assertFalse(dialog.export_button.isEnabled())  # The extra clip still needs handling.

    def test_repeated_refresh_does_not_duplicate_blockers_or_markers(self):
        dialog = self.dialog()
        original = copy.deepcopy(dialog.bundle)
        for _ in range(3):
            dialog._refresh_export_blockers()
            dialog.timeline_review.refresh(dialog.bundle, preserve_time=True)
            dialog._locate_selected_blocker()
            self.assertEqual(dialog.blocker_tree.topLevelItemCount(), 1)
            self.assertEqual(sum(marker.get("kind") == "extra_clip" for marker in dialog.timeline_review.markers), 1)
        self.assertEqual(dialog.bundle, original)

    def test_handling_one_extra_does_not_unlock_while_another_is_unresolved(self):
        bundle = self.bundle()
        clips = bundle["tasks"][1]["clips"]
        text = "india juliet kilo lima mike november oscar papa"
        clips[11]["recognized_text"] = text
        clips[11]["expected_text"] = text
        clips.append(self.clip("another-extra.mp4", 88, 95, text, 0.8))
        clips[-1]["export_order"] = 14
        dialog = self.dialog(bundle)
        self.assertEqual(len(dialog._export_blockers), 2)
        dialog.approve_blocker_button.click()
        self.assertEqual(len(dialog._export_blockers), 1)
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertEqual(dialog._selected_export_blocker()["file_name"], "another-extra.mp4")
        dialog.exclude_blocker_button.click()
        self.assertTrue(dialog.export_button.isEnabled())

    def test_closing_invalidates_pending_decoder_and_waveform_callbacks(self):
        dialog = self.dialog()
        timeline = dialog.timeline_review
        media_generation = timeline._media_generation
        waveform_generation = timeline._waveform_generation
        dialog.close()
        self.assertGreater(timeline._media_generation, media_generation)
        self.assertGreater(timeline._waveform_generation, waveform_generation)


if __name__ == "__main__":
    unittest.main()

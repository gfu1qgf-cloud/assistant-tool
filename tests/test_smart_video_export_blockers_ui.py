"""Export blockers must be directly visible and actionable, not just counted."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.smart_video_editor.engine import (
    build_script_word_records,
    export_smart_video_bundle,
    save_analysis_reports,
    set_smart_video_missing_review,
    smart_video_export_blockers,
    smart_video_task_review_decision,
    update_smart_video_pending_reviews,
)
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin


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

    def wrong_speech_bundle(self):
        bundle = self.bundle()
        script = "alpha bravo charlie delta echo foxtrot golf hotel"
        lines, words = build_script_word_records(script)
        bundle["tasks"][1] = {
            "task_id": "wrong", "label": "整项口播错误", "task_dir": str(self.root),
            "script": script, "script_lines": lines, "script_words": words,
            "clips": [self.clip("all-wrong.mp4", -1, -1, "unrelated wrong spoken content")],
        }
        return bundle

    def test_skip_extra_only_task_without_deleting_or_approving_clips(self):
        dialog = self.dialog()
        dialog.collect()  # Normalize valid table values before comparing the plan.
        task = dialog.bundle["tasks"][1]
        before = copy.deepcopy(task["clips"])
        self.assertTrue(dialog.timeline_review.skip_task_button.isEnabled())
        dialog.timeline_review.skip_task_button.click()
        self.assertEqual(smart_video_task_review_decision(task), "skipped")
        self.assertEqual(dialog._export_blockers, [])
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertEqual(task["clips"], before)
        self.assertTrue(all(Path(clip["source"]).is_file() for clip in task["clips"]))
        self.assertIn("暂缓 1", dialog.blocker_banner.text())
        dialog.timeline_review.clear_task_decision_button.click()
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertEqual(len(dialog._export_blockers), 1)

    def test_skip_wrong_speech_bypasses_missing_and_extra_for_same_task(self):
        dialog = self.dialog(self.wrong_speech_bundle())
        self.assertTrue(any(b.get("kind") == "extra_clip" for b in dialog._export_blockers))
        self.assertTrue(any(b.get("kind") != "extra_clip" for b in dialog._export_blockers))
        dialog.timeline_review.skip_task_button.click()
        self.assertEqual(dialog._export_blockers, [])
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertEqual(smart_video_task_review_decision(dialog.bundle["tasks"][1]), "skipped")
        self.assertTrue(dialog.bundle["tasks"][1]["clips"][0]["included"])

    def test_advanced_review_can_skip_task_from_extra_clip_record(self):
        dialog = self.dialog()
        self.assertTrue(dialog.skip_missing_button.isEnabled())
        dialog.skip_missing_button.click()
        self.assertEqual(smart_video_task_review_decision(dialog.bundle["tasks"][1]), "skipped")
        self.assertTrue(dialog.export_button.isEnabled())

    def test_skipping_one_task_keeps_other_task_blockers(self):
        bundle = self.wrong_speech_bundle()
        other = copy.deepcopy(bundle["tasks"][1])
        other["task_id"], other["label"] = "other", "另一个错误任务"
        bundle["tasks"].append(other)
        dialog = self.dialog(bundle)
        dialog.timeline_review.skip_task_button.click()
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertTrue(dialog._export_blockers)
        self.assertTrue(all(b["task_index"] == 2 for b in dialog._export_blockers))

    def test_export_only_processes_remaining_tasks_in_both_modes(self):
        for mode in ("joined", "segments"):
            with self.subTest(mode=mode):
                bundle = self.wrong_speech_bundle()
                set_smart_video_missing_review(bundle["tasks"][1], "skipped")
                with mock.patch("app_plugins.builtin.smart_video_editor.engine._resolve_ffmpeg", return_value="ffmpeg"), mock.patch(
                    "app_plugins.builtin.smart_video_editor.engine._export_task", return_value={"task_id": "clean"}
                ) as joined, mock.patch(
                    "app_plugins.builtin.smart_video_editor.engine._export_segmented_task", return_value={"task_id": "clean"}
                ) as segmented:
                    result = export_smart_video_bundle(bundle, {"export_mode": mode})
                exporter = segmented if mode == "segments" else joined
                self.assertEqual(exporter.call_count, 1)
                self.assertIs(exporter.call_args.args[0], bundle["tasks"][0])
                self.assertEqual([item["task_id"] for item in result["skipped"]], ["wrong"])
                self.assertFalse(result["failed"])

    def test_all_skipped_can_finish_with_no_included_clips_or_ffmpeg(self):
        bundle = self.wrong_speech_bundle()
        bundle["tasks"] = bundle["tasks"][1:]
        bundle["tasks"][0]["clips"][0]["included"] = False
        dialog = self.dialog(bundle)
        dialog.timeline_review.skip_task_button.click()
        with mock.patch("app_plugins.builtin.smart_video_editor.ui.QtWidgets.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        with mock.patch("app_plugins.builtin.smart_video_editor.engine._resolve_ffmpeg") as resolve:
            result = export_smart_video_bundle(dialog.reviewed_bundle)
        resolve.assert_not_called()
        self.assertEqual(len(result["skipped"]), 1)

    def test_skipped_extra_only_persists_in_report_and_pending_list(self):
        bundle = self.bundle()
        task = bundle["tasks"][1]
        task["task_dir"] = str(self.root)
        task["report_path"] = str(self.root / "review.json")
        set_smart_video_missing_review(task, "skipped")
        save_analysis_reports(bundle)
        restored = json.loads(Path(task["report_path"]).read_text(encoding="utf-8-sig"))["task"]
        self.assertEqual(smart_video_task_review_decision(restored), "skipped")
        records = update_smart_video_pending_reviews([], {"tasks": [restored]})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertEqual(len(records[0]["sources"]), len(task["clips"]))
        set_smart_video_missing_review(restored, "")
        self.assertTrue(smart_video_export_blockers({"tasks": [restored]}))

    def test_invalid_trim_draft_does_not_prevent_skipping_wrong_task(self):
        dialog = self.dialog(self.wrong_speech_bundle())
        row = dialog._row_for_clip(1, 0)
        dialog.table.item(row, dialog.COL_END).setText("invalid draft")
        with mock.patch("app_plugins.builtin.smart_video_editor.ui.QtWidgets.QMessageBox.warning") as warning:
            dialog.timeline_review.skip_task_button.click()
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        self.assertEqual(smart_video_task_review_decision(dialog.reviewed_bundle["tasks"][1]), "skipped")

    def test_skip_without_missing_does_not_allow_false_completeness_approval(self):
        task = self.bundle()["tasks"][1]
        with self.assertRaisesRegex(ValueError, "没有.*缺段"):
            set_smart_video_missing_review(task, "approved")
        set_smart_video_missing_review(task, "skipped")
        self.assertEqual(smart_video_task_review_decision(task), "skipped")
        task["clips"][0]["trim_start"] = 0.5
        self.assertEqual(smart_video_task_review_decision(task), "")

    def test_skipping_clean_task_is_visible_and_all_skipped_avoids_model_check(self):
        bundle = self.bundle()
        bundle["tasks"] = bundle["tasks"][:1]
        dialog = self.dialog(bundle)
        self.assertTrue(dialog.timeline_review.skip_task_button.isEnabled())
        dialog.timeline_review.skip_task_button.click()
        self.assertFalse(dialog.blocker_banner.isHidden())
        self.assertIn("暂缓 1", dialog.blocker_banner.text())
        self.assertEqual(dialog.export_button.text(), "保存暂缓并结束")
        plugin = SmartVideoEditorPlugin()
        plugin.context = mock.Mock()
        plugin._remember_pending_reviews = mock.Mock()
        plugin._model_ready = mock.Mock(return_value=False)
        plugin._start_worker = mock.Mock()
        with mock.patch.object(SmartVideoReviewDialog, "get_reviewed_bundle", return_value=dialog.bundle):
            plugin._handle_analysis(dialog.bundle, force_review=True)
        plugin._model_ready.assert_not_called()
        plugin._remember_pending_reviews.assert_called_once_with(dialog.bundle)
        self.assertEqual(plugin._start_worker.call_args.args[0], "export")

    def test_invalid_drafts_in_multiple_tasks_do_not_prevent_deferring_each(self):
        dialog = self.dialog(self.wrong_speech_bundle())
        for row in range(dialog.table.rowCount()):
            dialog.table.item(row, dialog.COL_END).setText("invalid draft")
        with mock.patch("app_plugins.builtin.smart_video_editor.ui.QtWidgets.QMessageBox.warning") as warning:
            dialog.timeline_review.skip_task_button.click()
            dialog.timeline_review.select_clip(0, 0)
            dialog.timeline_review.skip_task_button.click()
            dialog.accept()
        warning.assert_not_called()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        self.assertTrue(all(smart_video_task_review_decision(task) == "skipped"
                            for task in dialog.reviewed_bundle["tasks"]))

    def test_shared_breath_editor_does_not_offer_unsupported_task_skip(self):
        bundle = self.bundle()
        bundle["workflow"] = "breath_cut"
        dialog = self.dialog(bundle)
        self.assertFalse(dialog.timeline_review.skip_task_button.isEnabled())

    def test_delete_unmatched_clip_then_approve_gaps_uses_final_script_allocation(self):
        script = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima missing final sentence"
        lines, words = build_script_word_records(script)
        common = "echo foxtrot golf hotel india juliet kilo lima"
        clips = [self.clip("intro-and-quote.mp4", 0, 3, "alpha bravo charlie delta " + common, 0.8),
                 self.clip("quote.mp4", 4, 11, common),
                 self.clip("unrelated.mp4", -1, -1, "unrelated incorrect speech")]
        clips[0]["content_order_end"] = 11  # Original recognition overlaps; final allocation does not.
        bundle = {"tasks": [{"task_id": "reviewed", "label": "人工核对任务",
                              "script": script, "script_lines": lines, "script_words": words,
                              "clips": clips}], "settings": {}, "summary": {}}
        dialog = self.dialog(bundle)
        dialog.timeline_review._set_clip_included(2, False)
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes):
            dialog.timeline_review.approve_task_button.click()
        self.assertEqual(smart_video_task_review_decision(dialog.bundle["tasks"][0]), "approved")
        self.assertFalse(dialog.bundle["tasks"][0]["clips"][2]["included"])
        self.assertEqual(dialog._export_blockers, [])
        self.assertTrue(dialog.export_button.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.Accepted)

    def test_trimming_duplicate_content_checks_retained_word_timeline(self):
        bundle = self.bundle()
        bundle["tasks"] = bundle["tasks"][1:]
        clips = bundle["tasks"][0]["clips"]
        bundle["tasks"][0]["clips"] = [clips[5], clips[12]]
        for clip in bundle["tasks"][0]["clips"]:
            clip["word_timeline"] = [{"start": i * 0.5, "end": (i + 1) * 0.5,
                                       "script_word_index": 40 + i, "anchor": True}
                                      for i in range(8)]
        dialog = self.dialog(bundle)
        first, second = dialog.bundle["tasks"][0]["clips"]
        first.update(trim_end=2.0, kept_ranges=[[0.0, 2.0]])
        second.update(trim_start=2.0, kept_ranges=[[2.0, 4.0]])
        dialog._timeline_task_changed(0)
        dialog.timeline_review.refresh(dialog.bundle)
        self.assertFalse(dialog._export_blockers)
        self.assertTrue(dialog.export_button.isEnabled())

    def test_explicit_seek_to_grey_range_plays_clicked_position_and_repeats_after_pause(self):
        bundle = self.bundle()
        bundle["tasks"] = bundle["tasks"][:1]
        clip = bundle["tasks"][0]["clips"][0]
        clip.update(pause_removals=[[1.0, 2.0]], kept_ranges=[[0.0, 1.0], [2.0, 4.0]])
        dialog = self.dialog(bundle)
        timeline = dialog.timeline_review
        timeline.player.is_playing = mock.Mock(return_value=False)
        timeline.player.play = mock.Mock(return_value=True)
        timeline._slider_moved(1500)
        self.assertTrue(timeline.current_segment["is_removed"])
        timeline.toggle_playback()
        timeline.player.prepare.assert_called_with(clip["source"], 1.0, 2.0, 0.5)
        timeline.player.pause()
        timeline.toggle_playback()
        timeline.player.prepare.assert_called_with(clip["source"], 1.0, 2.0, 0.5)

    def test_deleting_visible_small_segment_keeps_other_parts_of_same_source(self):
        bundle = self.bundle()
        bundle["tasks"] = bundle["tasks"][:1]
        bundle["tasks"][0]["clips"][0].update(pause_removals=[[1.0, 2.0]], kept_ranges=[[0.0, 1.0], [2.0, 4.0]])
        dialog = self.dialog(bundle)
        timeline = dialog.timeline_review
        segment = next(part for part in timeline.segments if part["source_start"] == 2.0)
        timeline._delete_visible_segment(segment)
        clip = dialog.bundle["tasks"][0]["clips"][0]
        self.assertTrue(clip["included"])
        self.assertEqual(clip["kept_ranges"], [[0.0, 1.0]])
        self.assertTrue(Path(clip["source"]).is_file())

    def test_split_delete_and_restore_preserves_independent_piece_boundaries(self):
        bundle = self.bundle()
        bundle["tasks"] = bundle["tasks"][:1]
        dialog = self.dialog(bundle)
        timeline = dialog.timeline_review
        timeline._split_visible_segment(timeline.segments[0], 1.5)
        before = [(part["source_start"], part["source_end"]) for part in timeline.segments]
        self.assertEqual(before, [(0.0, 1.5), (1.5, 4.0)])
        timeline._delete_visible_segment(timeline.segments[0])
        removed = next(part for part in timeline.segments if part["is_removed"])
        timeline._restore_removed_segment(removed)
        self.assertEqual([(part["source_start"], part["source_end"]) for part in timeline.segments], before)
        self.assertEqual(dialog.bundle["tasks"][0]["clips"][0]["kept_ranges"], [[0.0, 4.0]])

    def test_context_menu_has_distinct_local_and_whole_source_delete_actions(self):
        dialog = self.dialog()
        widget = dialog.timeline_review.timeline
        segment = dict(dialog.timeline_review.segments[0], source_start=1.0, source_end=2.0)
        widget._segment_rects = [(QtCore.QRectF(200, 36, 100, 35), segment)]
        deleted, whole = [], []
        widget.segmentDeletionRequested.connect(deleted.append)
        widget.clipInclusionRequested.connect(lambda index, included: whole.append((index, included)))
        for desired in ("删除当前小段", "删除整个原始片段"):
            menu = mock.Mock()
            actions = []
            def add_action(text):
                action = mock.Mock()
                action.label = text
                actions.append(action)
                return action
            menu.addAction.side_effect = add_action
            menu.exec.side_effect = lambda _point: next(action for action in actions if action.label.startswith(desired))
            event = mock.Mock()
            event.pos.return_value = QtCore.QPoint(250, 50)
            with mock.patch("app_plugins.builtin.smart_video_editor.timeline_widget.QtWidgets.QMenu", return_value=menu):
                widget.contextMenuEvent(event)
            self.assertTrue(any(action.label.startswith("删除当前小段") for action in actions))
            self.assertTrue(any(action.label.startswith("删除整个原始片段") for action in actions))
        self.assertEqual(deleted, [segment])
        self.assertEqual(whole, [(segment["clip_index"], False)])

    def test_restoring_one_piece_of_excluded_source_does_not_restore_other_pieces(self):
        dialog = self.dialog()
        timeline = dialog.timeline_review
        timeline.select_clip(1, 0)
        segment = next(part for part in timeline.segments if part["clip_index"] == 0)
        timeline._split_visible_segment(segment, 1.5)
        timeline._set_clip_included(0, False)
        removed = next(part for part in timeline.segments if part["clip_index"] == 0 and part["source_start"] == 0.0)
        timeline._restore_removed_segment(removed)
        clip = dialog.bundle["tasks"][1]["clips"][0]
        self.assertTrue(clip["included"])
        self.assertEqual(clip["kept_ranges"], [[0.0, 1.5]])
        from app_plugins.builtin.smart_video_editor.timeline_model import build_task_review_timeline
        restored = json.loads(json.dumps(dialog.bundle["tasks"][1]))
        parts, _duration = build_task_review_timeline(restored)
        selected = [part for part in parts if part["clip_index"] == 0]
        self.assertEqual([(part["source_start"], part["source_end"], part["is_removed"]) for part in selected],
                         [(0.0, 1.5, False), (1.5, 4.0, True)])

    def test_right_click_confirm_retention_unlocks_and_persists_without_restoring_deleted_parts(self):
        bundle = self.bundle()
        task = bundle["tasks"][1]
        task["report_path"] = str(self.root / "retained-review.json")
        task["clips"][12].update(pause_removals=[[1.0, 1.5]], manual_delete_ranges=[[1.0, 1.5]],
                                 kept_ranges=[[0.0, 1.0], [1.5, 4.0]])
        dialog = self.dialog(bundle)
        dialog.collect()
        clip = dialog.bundle["tasks"][1]["clips"][12]
        before_ranges = copy.deepcopy(clip["kept_ranges"])
        source_bytes = Path(clip["source"]).read_bytes()
        widget = dialog.timeline_review.timeline
        segment = next(part for part in dialog.timeline_review.segments
                       if part["clip_index"] == 12 and not part["is_removed"])
        widget._segment_rects = [(QtCore.QRectF(200, 36, 100, 35), segment)]
        event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse,
                                       QtCore.QPoint(250, 50), QtCore.QPoint(250, 50))
        with mock.patch.object(QtWidgets.QMenu, "exec", lambda menu, *_args: next(
            action for action in menu.actions() if action.text().startswith("确认保留"))):
            widget.contextMenuEvent(event)
        self.assertTrue(clip["included"])
        self.assertTrue(clip["extra_clip_approved"])
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertEqual(clip["kept_ranges"], before_ranges)
        self.assertEqual(clip["manual_delete_ranges"], [[1.0, 1.5]])
        self.assertEqual(Path(clip["source"]).read_bytes(), source_bytes)
        saved = json.loads(Path(task["report_path"]).read_text(encoding="utf-8-sig"))
        self.assertTrue(saved["task"]["clips"][12]["extra_clip_approved"])

    def test_previously_reviewed_clip_can_still_resolve_a_new_extra_blocker(self):
        bundle = self.bundle()
        bundle["tasks"][1]["clips"][12]["review_acknowledged"] = True
        dialog = self.dialog(bundle)
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertTrue(dialog.timeline_review.acknowledge_button.isEnabled())
        self.assertIn("确认保留", dialog.timeline_review.acknowledge_button.text())
        self.assertTrue(dialog.acknowledge_clip_button.isEnabled())
        dialog.timeline_review.acknowledge_button.click()
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertTrue(dialog.bundle["tasks"][1]["clips"][12]["extra_clip_approved"])


if __name__ == "__main__":
    unittest.main()

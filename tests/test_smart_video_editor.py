import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from globalValue import GlobalValue, WhisperModelRestartRequired
from qt_compat import QtCore, QtGui, QtWidgets

from app_plugins.builtin.smart_video_editor.settings import (
    SmartVideoEditorSettingsPage,
)
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.breath_editor import (
    BreathCutReviewDialog,
    BreathCutSourceDialog,
)
from app_plugins.builtin.smart_video_editor.engine import (
    _compact_record_similarity,
    _mark_duplicate_clips,
    _protect_edges_adjacent_to_missing_script,
    smart_video_extra_clip_findings,
    _token_equivalent,
)
from app_plugins.builtin.smart_video_editor.timeline_review import (
    SmartVideoTimelineReview,
)
from app_plugins.builtin.smart_video_editor.timeline_widget import (
    SmartTimelineWidget,
)
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.smart_video_editor_pyui import (
    SmartVideoExportResultDialog,
    SmartVideoPendingDialog,
    SmartVideoReviewDialog,
    SmartVideoSourceDialog,
    _comparison_html,
)
from model.SmartVideoEditor import (
    _build_clip_plan,
    analyze_breath_cut_files,
    analyze_smart_video_jobs,
    apply_manual_breath_overrides,
    apply_clip_review,
    best_unit_window,
    breath_cut_plan,
    build_script_word_records,
    build_srt_cues,
    combine_breath_gap_ranges,
    detect_silence_ranges,
    detect_voice_absence_ranges,
    discover_task_videos,
    export_smart_video_bundle,
    export_breath_cut_bundle,
    find_pause_removals,
    kept_ranges,
    normalize_smart_video_editor_settings,
    normalize_smart_video_pending_reviews,
    refine_trim_boundaries,
    render_task_problem_report,
    select_duplicate_group_clip,
    smart_video_jobs_require_model,
    smart_video_export_blockers,
    set_smart_video_missing_review,
    source_time_in_kept_ranges,
    split_script_blocks,
    subtitle_text_for_export,
    text_units,
    validate_smart_video_bundle_for_export,
    update_smart_video_pending_reviews,
)
from model.SubtitleHelper import generate_srt_whisper_only
from model.TextHelper import smart_split_sentences


class _Word:
    def __init__(self, word, start, end, probability=0.95):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class _Segment:
    def __init__(self, text, words):
        self.text = text
        self.words = words


class _Info:
    language = "sk"
    duration = 3.0


class _MouseMoveEvent:
    def __init__(self, x, y):
        self._point = QtCore.QPointF(x, y)

    def position(self):
        return QtCore.QPointF(self._point)


class SmartTimelineQt6EventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_mouse_move_uses_qpointf_for_qrectf_hit_testing(self):
        widget = SmartTimelineWidget()
        block = {
            "timeline_start": 0.0,
            "text": "subtitle",
            "suggestion": "",
            "kind": "recognized",
        }
        widget._subtitle_rects = [(QtCore.QRectF(0, 0, 80, 30), block)]

        widget.mouseMoveEvent(_MouseMoveEvent(10, 10))

        self.assertIn("subtitle", widget.toolTip())
        widget.deleteLater()

    def test_missing_subtitle_is_a_compact_badge_with_full_tooltip(self):
        widget = SmartTimelineWidget()
        widget.resize(720, 260)
        missing_text = "this full missing sentence must only appear in the tooltip"
        block = {
            "timeline_start": 4.0,
            "timeline_end": 4.04,
            "anchor_time": 4.0,
            "text": missing_text,
            "missing_text": missing_text,
            "display_text": "⛔ 缺段",
            "suggestion": "please review",
            "kind": "aligned",
            "severity": "pink",
            "clip_index": 0,
            "is_missing": True,
        }
        widget.set_data([], [], 10.0, [], [block])
        widget.show()
        self.app.processEvents()
        widget.grab()

        rect, _record = widget._subtitle_rects[0]
        self.assertLessEqual(rect.width(), 64.0)
        self.assertLess(rect.height(), widget.SUBTITLE_HEIGHT)
        widget.mouseMoveEvent(_MouseMoveEvent(rect.center().x(), rect.center().y()))
        self.assertIn(missing_text, widget.toolTip())
        widget.close()

    def test_context_menu_excludes_clip_without_deleting_source(self):
        widget = SmartTimelineWidget()
        widget.resize(720, 260)
        segment = {
            "clip_index": 2, "timeline_start": 0.0, "timeline_end": 5.0,
            "source_start": 0.0, "source_end": 5.0,
            "file_name": "take.mp4", "included": True,
        }
        widget.set_data([segment], [], 5.0)
        widget.show()
        self.app.processEvents()
        widget.grab()
        rect, _ = widget._segment_rects[0]
        point = rect.center().toPoint()
        event = QtGui.QContextMenuEvent(
            QtGui.QContextMenuEvent.Reason.Mouse, point,
            widget.mapToGlobal(point),
        )
        requested = []
        widget.clipInclusionRequested.connect(
            lambda index, included: requested.append((index, included))
        )
        with mock.patch.object(
            QtWidgets.QMenu, "exec", lambda menu, *_args: menu.actions()[0]
        ):
            widget.contextMenuEvent(event)
        self.assertEqual(requested, [(2, False)])
        widget.close()


class _FakeWhisperModel:
    def __init__(self):
        self.calls = 0

    def transcribe(self, _path, **_kwargs):
        self.calls += 1
        segment = _Segment(
            "Moj bože prosím zdravie",
            [
                _Word("Moj", 0.4, 0.7),
                _Word("bože", 0.8, 1.1),
                _Word("prosím", 1.2, 1.6),
                _Word("zdravie", 2.1, 2.5),
            ],
        )
        return iter([segment]), _Info()


class _MappingWhisperModel:
    def transcribe(self, path, **_kwargs):
        text = Path(path).stem.replace("_", " ")
        words = []
        cursor = 0.25
        for value in text.split():
            words.append(_Word(value, cursor, cursor + 0.3))
            cursor += 0.4
        info = _Info()
        info.duration = cursor + 0.2
        return iter([_Segment(text, words)]), info


class _DictionaryWhisperModel:
    def __init__(self, values):
        self.values = values

    def transcribe(self, path, **_kwargs):
        text = self.values[Path(path).name]
        words = []
        cursor = 0.25
        for value in text.split():
            words.append(_Word(value, cursor, cursor + 0.3))
            cursor += 0.4
        info = _Info()
        info.duration = cursor + 0.2
        return iter([_Segment(text, words)]), info


class SmartVideoEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_text_comparison_ignores_case_and_punctuation(self):
        self.assertEqual(
            text_units("MÔJ Bože, prosím!"),
            text_units("môj bože prosím"),
        )
        self.assertEqual(text_units("你好，世界！"), ["你", "好", "世", "界"])

    def test_whisper_model_switch_requires_restart_without_double_loading(self):
        created = []

        def fake_model(source, **options):
            value = {"source": source, "options": options}
            created.append(value)
            return value

        values = GlobalValue()
        with mock.patch("globalValue.WhisperModel", side_effect=fake_model):
            base = values.get_whisper_model("base")
            with self.assertRaisesRegex(WhisperModelRestartRequired, "重启"):
                values.get_whisper_model("medium")
            with mock.patch.object(values, "_configured_whisper_model_name", return_value="medium"):
                repeated = values.get_whisper_model()

        self.assertIs(base, repeated)
        self.assertEqual([item["source"] for item in created], ["base"])
        self.assertEqual(values.loaded_whisper_model_name(), "base")

    def test_runtime_autosave_does_not_revert_new_model_choice(self):
        plugin = SmartVideoEditorPlugin()
        plugin.settings = normalize_smart_video_editor_settings({
            "whisper_model_size": "base"
        })
        config = {"smart_video_editor": {"whisper_model_size": "large-v3"}}
        plugin.update_config(config)
        self.assertEqual(config["smart_video_editor"]["whisper_model_size"], "large-v3")
        plugin.settings["whisper_model_size"] = "medium"
        plugin._settings_dirty = True
        plugin.update_config(config)
        self.assertEqual(config["smart_video_editor"]["whisper_model_size"], "medium")

    def test_smart_editor_prompts_for_restart_before_new_model_work(self):
        plugin = SmartVideoEditorPlugin()
        context = mock.Mock()
        context.loaded_whisper_model_name.return_value = "base"
        plugin.context = context
        with mock.patch(
            "app_plugins.builtin.smart_video_editor.plugin.QMessageBox.information"
        ) as notice:
            self.assertFalse(plugin._model_ready({"whisper_model_size": "large-v3"}))
        self.assertIn("重启", notice.call_args.args[2])
        self.assertTrue(plugin._model_ready({"whisper_model_size": "base"}))

    def test_standalone_breath_ui_reuses_timeline_and_keeps_full_frame_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "preview.mp4"
            source.write_bytes(b"not-a-real-video-but-present")
            source_dialog = BreathCutSourceDialog([source])
            try:
                self.assertEqual(source_dialog.list_widget.count(), 1)
            finally:
                source_dialog.close()
            bundle = {
                "workflow": "breath_cut",
                "settings": normalize_smart_video_editor_settings({}),
                "tasks": [{
                    "task_id": "preview",
                    "label": "preview.mp4",
                    "output_dir": str(Path(temporary) / "气口剪辑结果"),
                    "missing_blocks": [],
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "included": True,
                        "source_index": 0,
                        "export_order": 1,
                        "original_duration": 4.0,
                        "trim_start": 0.5,
                        "trim_end": 3.5,
                        "pause_removals": [[1.5, 2.0]],
                        "kept_ranges": [[0.5, 1.5], [2.0, 3.5]],
                        "issues": [],
                        "words": [],
                    }],
                }],
            }
            dialog = BreathCutReviewDialog(bundle)
            try:
                review = dialog.timeline_review
                dialog.resize(1500, 900)
                dialog.ensurePolished()
                dialog.layout().activate()
                review.layout().activate()
                review.breath_panel.layout().activate()
                normal_parameter_width = max(
                    review.pause_keep_after_spinbox.sizeHint().width(),
                    review.min_silence_spinbox.sizeHint().width(),
                )
                self.assertLessEqual(
                    review.pause_keep_after_spinbox.width(),
                    normal_parameter_width + 12,
                )
                self.assertLessEqual(review.apply_breath_button.width(), 110)
                self.assertTrue(review.breath_only)
                self.assertTrue(review.task_filter_widget.isHidden())
                self.assertTrue(review.issue_panel.isHidden())
                self.assertEqual(
                    review.breath_detection_mode_combo.currentData(),
                    "voice_refined",
                )
                self.assertEqual(
                    review.mark_delete_start_button.text(), "记删除起点"
                )
                self.assertEqual(
                    review.restore_removed_button.text(), "恢复当前灰色区"
                )
                self.assertIn(
                    "裁剪后：00:02.50",
                    review.remaining_duration_label.text(),
                )
                with mock.patch(
                    "app_plugins.builtin.smart_video_editor.timeline_review._media_shape",
                    return_value=(1920, 1080, 30.0),
                ):
                    review._media_aspect_cache.clear()
                    review._detected_preview_aspect = (
                        review._detect_task_preview_aspect(bundle["tasks"][0])
                    )
                    review._apply_preview_aspect()
                self.assertEqual(review._detected_preview_aspect, "landscape")
                self.assertGreaterEqual(review.player.minimumWidth(), 360)
                internal_removed = next(
                    segment for segment in review.segments
                    if segment.get("is_removed")
                    and segment.get("remove_reason") == "句内气口"
                )
                review._restore_removed_segment(internal_removed)
                self.assertEqual(
                    review.bundle["tasks"][0]["clips"][0]["pause_removals"],
                    [],
                )
                backend = review.player.backend
                if hasattr(backend, "video_widgets"):
                    self.assertTrue(all(
                        video.aspectRatioMode() == QtCore.Qt.KeepAspectRatio
                        for video in backend.video_widgets
                    ))
            finally:
                dialog.close()

    def test_timeline_subtitle_controls_preview_and_save_global_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "speech.mp4"
            source.write_bytes(b"fake")
            words = [
                {"text": text, "start": index * 0.4, "end": index * 0.4 + 0.3}
                for index, text in enumerate(("alpha", "beta", "gamma", "delta"))
            ]
            aligned = [
                {
                    "raw": value["text"],
                    "normalized": value["text"],
                    "start": value["start"],
                    "end": value["end"],
                    "line_index": 0,
                }
                for value in words
            ]
            bundle = {
                "settings": normalize_smart_video_editor_settings({
                    "whisper_model_size": "base",
                    "srt_max_words_per_block": 4,
                    "srt_max_chars_per_block": 50,
                }),
                "summary": {"clip_count": 1, "green_count": 1},
                "tasks": [{
                    "label": "subtitle-preview",
                    "missing_blocks": [],
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "source_index": 0,
                        "export_order": 1,
                        "status": "green",
                        "trim_start": 0.0,
                        "trim_end": 2.0,
                        "original_duration": 2.0,
                        "pause_removals": [],
                        "kept_ranges": [[0.0, 2.0]],
                        "removed_seconds": 0.0,
                        "expected_text": "alpha beta gamma delta",
                        "recognized_text": "alpha beta gamma delta",
                        "words": words,
                        "word_timeline": aligned,
                        "issues": [],
                        "included": True,
                    }],
                }],
            }
            saved = []

            def save_defaults(values):
                saved.append(dict(values))
                return True, "已保存"

            dialog = SmartVideoReviewDialog(
                bundle,
                save_subtitle_defaults=save_defaults,
            )
            try:
                timeline = dialog.timeline_review
                dialog.resize(1480, 860)
                dialog.ensurePolished()
                dialog.layout().activate()
                timeline.layout().activate()
                timeline.subtitle_settings_panel.layout().activate()
                self.assertLessEqual(
                    timeline.subtitle_gap_spinbox.width(),
                    timeline.subtitle_gap_spinbox.sizeHint().width() + 12,
                )
                self.assertEqual(len(timeline.aligned_subtitles), 1)
                timeline.subtitle_model_combo.setCurrentIndex(
                    timeline.subtitle_model_combo.findData("large-v3")
                )
                timeline.subtitle_max_words_spinbox.setValue(2)
                timeline.subtitle_max_chars_spinbox.setValue(0)
                timeline.subtitle_gap_spinbox.setValue(0)
                timeline.subtitle_line_break_checkbox.setChecked(True)
                reviewed = dialog.collect()

                self.assertEqual(
                    reviewed["settings"]["whisper_model_size"],
                    "large-v3",
                )
                self.assertEqual(len(timeline.aligned_subtitles), 2)
                self.assertAlmostEqual(
                    timeline.aligned_subtitles[0]["timeline_end"],
                    timeline.aligned_subtitles[1]["timeline_start"],
                )
                timeline._save_subtitle_defaults()
                self.assertEqual(saved[0]["srt_max_words_per_block"], 2)
                self.assertEqual(saved[0]["srt_block_gap_ms"], 0)
                self.assertTrue(saved[0]["srt_include_line_breaks"])
                self.assertEqual(saved[0]["whisper_model_size"], "large-v3")
            finally:
                dialog.close()

    def test_timeline_recalculates_voice_detection_in_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "voice.mp4"
            source.write_bytes(b"fake")
            settings = normalize_smart_video_editor_settings({
                "breath_detection_mode": "voice_only",
            })
            clip = {
                "source": str(source),
                "file_name": source.name,
                "included": True,
                "source_index": 0,
                "export_order": 1,
                "original_duration": 4.0,
                "trim_start": 0.0,
                "trim_end": 4.0,
                "pause_removals": [],
                "kept_ranges": [[0.0, 4.0]],
                "issues": [],
                "words": [],
            }
            bundle = {
                "workflow": "breath_cut",
                "settings": settings,
                "tasks": [{
                    "task_id": "voice",
                    "label": "voice.mp4",
                    "clips": [clip],
                    "missing_blocks": [],
                }],
            }
            dialog = BreathCutReviewDialog(bundle)
            try:
                review = dialog.timeline_review
                with mock.patch.object(
                    review.voice_loader, "start"
                ) as voice_start, mock.patch.object(
                    review.silence_loader, "start"
                ) as db_start:
                    review._apply_breath_settings()
                voice_start.assert_called_once()
                db_start.assert_not_called()
                review._breath_voice_complete([{
                    "clip_index": 0,
                    "ranges": [[0.0, 0.7], [3.1, 4.0]],
                    "error": "",
                }])
                reviewed_clip = review.bundle["tasks"][0]["clips"][0]
                self.assertEqual(
                    reviewed_clip["voice_absence_ranges"],
                    [[0.0, 0.7], [3.1, 4.0]],
                )
                self.assertEqual(reviewed_clip["trim_start"], 0.58)
                self.assertEqual(reviewed_clip["trim_end"], 3.32)
            finally:
                dialog.close()

    def test_smart_timeline_recalculates_tail_after_detection_mode_changes(self):
        settings = normalize_smart_video_editor_settings({
            "breath_detection_mode": "voice_only",
        })
        clip = {
            "file_name": "11.mp4",
            "included": True,
            "original_duration": 10.005,
            "trim_start": 0.0,
            "trim_end": 10.005,
            "automatic_trim_start": 0.0,
            "automatic_trim_end": 10.005,
            "pause_removals": [],
            "automatic_pause_removals": [],
            "word_timeline": [
                {"start": 0.0, "end": 0.46, "anchor": True},
                {"start": 7.7, "end": 8.2, "anchor": True},
            ],
            "words": [],
            "issues": [],
            "boundary_warnings": [],
            "boundary_decisions": [],
        }
        review = mock.Mock()
        review._breath_reanalysis_running = True
        review.breath_only = False
        review.task_index = 0
        review.bundle = {
            "settings": settings,
            "tasks": [{
                "clips": [clip],
                "missing_blocks": [],
                "unverified_blocks": [],
            }],
        }
        SmartVideoTimelineReview._breath_reanalysis_complete(review, [{
            "clip_index": 0,
            "ranges": [],
            "error": "",
            "voice_ranges": [[8.44, 10.005]],
            "voice_error": "",
        }])
        self.assertAlmostEqual(clip["automatic_trim_end"], 8.66, places=3)
        self.assertAlmostEqual(clip["trim_end"], 8.66, places=3)
        self.assertIn(
            "tail_cut",
            [item.get("kind") for item in clip["boundary_decisions"]],
        )

    def test_review_diff_highlights_only_real_word_changes(self):
        self.assertIn("文案与识别内容一致", _comparison_html("Alpha", "alpha"))
        difference = _comparison_html("alpha beta", "alpha gamma")
        self.assertIn("#F4CCCC", difference)
        self.assertIn("#FCE5CD", difference)

    def test_best_window_ignores_false_start_and_tail(self):
        expected = text_units("prosím zdravie a silu")
        observed = text_units("ehm znovu prosím zdravie a silu ďakujem")
        match = best_unit_window(expected, observed)
        self.assertGreater(match["score"], 0.95)
        self.assertEqual(observed[match["start"]:match["end"]], expected)

    def test_script_is_balanced_when_one_paragraph_has_many_clips(self):
        blocks = split_script_blocks(
            "one two three four five six seven eight", target_count=2
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual(" ".join(blocks), "one two three four five six seven eight")
        weighted = split_script_blocks(
            "one two three four five six seven eight",
            target_count=2,
            weights=[1, 3],
        )
        self.assertEqual(weighted[0], "one two")
        self.assertEqual(weighted[1], "three four five six seven eight")

    def test_pause_compression_keeps_configured_breath(self):
        settings = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
            "internal_pause_mode": "experimental",
            "pause_threshold_ms": 1000,
            "retained_pause_ms": 300,
        })
        words = [
            {"text": "a", "start": 0.2, "end": 0.5},
            {"text": "b", "start": 2.0, "end": 2.3},
        ]
        removals = find_pause_removals(
            words, 0.0, 2.5, settings, [[0.5, 2.0]]
        )
        self.assertEqual(removals, [[0.65, 1.85]])
        self.assertEqual(kept_ranges(0.0, 2.5, removals), [[0.0, 0.65], [1.85, 2.5]])
        self.assertAlmostEqual(
            source_time_in_kept_ranges(2.3, [[0.0, 0.65], [1.85, 2.5]]),
            1.1,
        )
        legacy = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
        })
        self.assertFalse(legacy["compress_internal_pauses"])
        self.assertEqual(
            find_pause_removals(words, 0.0, 2.5, settings), []
        )

    def test_standalone_breath_plan_handles_edges_and_internal_silence(self):
        settings = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
            "internal_pause_mode": "experimental",
            "lead_padding_ms": 100,
            "tail_padding_ms": 200,
            "pause_threshold_ms": 800,
            "retained_pause_ms": 300,
        })
        start, end, removals, ranges = breath_cut_plan(
            6.0,
            [[0.0, 0.7], [2.0, 3.5], [5.2, 6.0]],
            settings,
        )
        self.assertEqual(start, 0.6)
        self.assertEqual(end, 5.4)
        self.assertEqual(removals, [[2.15, 3.35]])
        self.assertEqual(ranges, [[0.6, 2.15], [3.35, 5.4]])

    def test_voice_absence_and_db_evidence_are_merged(self):
        ranges = combine_breath_gap_ranges(
            6.016,
            [[3.574, 5.032], [5.290, 6.016]],
            [[0.0, 0.304], [4.016, 6.016]],
        )
        self.assertEqual(ranges, [[0.0, 0.304], [3.574, 6.016]])

    def test_breath_detection_modes_use_clear_set_operations(self):
        arguments = (6.0, [[2.0, 4.0]], [[3.0, 5.0]])
        self.assertEqual(
            combine_breath_gap_ranges(*arguments, mode="db_only"),
            [[2.0, 4.0]],
        )
        self.assertEqual(
            combine_breath_gap_ranges(*arguments, mode="voice_only"),
            [[3.0, 5.0]],
        )
        self.assertEqual(
            combine_breath_gap_ranges(*arguments, mode="union"),
            [[2.0, 5.0]],
        )
        self.assertEqual(
            combine_breath_gap_ranges(*arguments, mode="intersection"),
            [[3.0, 4.0]],
        )

    def test_pause_compression_supports_asymmetric_keep_amounts(self):
        settings = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
            "internal_pause_mode": "experimental",
            "pause_threshold_ms": 800,
            "pause_keep_before_ms": 100,
            "pause_keep_after_ms": 250,
        })
        _start, _end, removals, _ranges = breath_cut_plan(
            5.0, [[2.0, 3.5]], settings
        )
        self.assertEqual(removals, [[2.1, 3.25]])

    def test_manual_breath_edits_survive_automatic_plan(self):
        clip = {
            "original_duration": 10.0,
            "manual_delete_ranges": [[6.0, 7.0]],
            "manual_keep_ranges": [[3.4, 3.6]],
            "manual_keep_head": True,
        }
        apply_manual_breath_overrides(
            clip, 1.0, 9.0, [[3.0, 4.0]]
        )
        self.assertEqual(clip["trim_start"], 0.0)
        self.assertEqual(clip["trim_end"], 9.0)
        self.assertEqual(
            clip["pause_removals"],
            [[3.0, 3.4], [3.6, 4.0], [6.0, 7.0]],
        )
        self.assertEqual(clip["automatic_trim_start"], 1.0)

    def test_db_silence_cannot_override_detected_quiet_speech(self):
        ranges = combine_breath_gap_ranges(
            6.0,
            [[2.0, 3.0]],
            [[0.0, 0.4], [5.5, 6.0]],
        )
        self.assertEqual(ranges, [[0.0, 0.4], [5.5, 6.0]])

    def test_db_silence_is_used_when_voice_detection_failed(self):
        ranges = combine_breath_gap_ranges(
            6.0,
            [[0.0, 0.7], [5.2, 6.0]],
            [],
            voice_detection_available=False,
        )
        self.assertEqual(ranges, [[0.0, 0.7], [5.2, 6.0]])

    def test_voice_detection_uses_bundled_faster_whisper_vad(self):
        settings = normalize_smart_video_editor_settings({
            "vad_threshold": 0.55,
            "vad_speech_pad_ms": 300,
        })
        fake_audio = mock.MagicMock()
        fake_audio.size = 16000 * 4
        fake_audio.__len__.return_value = 16000 * 4
        with mock.patch(
            "faster_whisper.audio.decode_audio",
            return_value=fake_audio,
        ), mock.patch(
            "faster_whisper.vad.get_speech_timestamps",
            return_value=[{"start": 8000, "end": 40000}],
        ) as vad:
            ranges, error = detect_voice_absence_ranges(
                "clip.mp4", 4.0, settings
            )
        self.assertFalse(error)
        self.assertEqual(ranges, [[0.0, 0.5], [2.5, 4.0]])
        options = vad.call_args.args[1]
        self.assertAlmostEqual(options.threshold, 0.55)
        self.assertAlmostEqual(options.neg_threshold, 0.5)
        self.assertEqual(options.min_speech_duration_ms, 120)
        self.assertEqual(options.speech_pad_ms, 300)

    def test_standalone_breath_analysis_and_export_without_script(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "standalone.mp4"
            subprocess.run([
                ffmpeg, "-y", "-f", "lavfi", "-i",
                "color=c=blue:s=320x240:r=25:d=4",
                "-f", "lavfi", "-i",
                "aevalsrc=if(between(t\\,0.7\\,1.5)+between(t\\,2.7\\,3.4)\\,0.45*sin(2*PI*440*t)\\,0):s=48000:d=4",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "ffmpeg_path": ffmpeg,
                "voice_detection_enabled": False,
                "compress_internal_pauses": True,
                "internal_pause_mode": "experimental",
                "silence_threshold_db": -35,
                "min_silence_ms": 300,
                "pause_threshold_ms": 700,
                "retained_pause_ms": 250,
                "existing_output": "overwrite",
            })
            bundle = analyze_breath_cut_files([source], settings)
            self.assertEqual(bundle["workflow"], "breath_cut")
            clip = bundle["tasks"][0]["clips"][0]
            self.assertTrue(clip["pause_removals"])
            self.assertLess(clip["trim_start"], clip["trim_end"])
            result = export_breath_cut_bundle(bundle, settings)
            self.assertFalse(result["failed"])
            self.assertTrue(Path(result["completed"][0]["video"]).is_file())

    def test_standalone_breath_export_preserves_video_when_audio_is_missing(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "no_audio.mp4"
            subprocess.run([
                ffmpeg, "-y", "-f", "lavfi", "-i",
                "color=c=green:s=160x120:r=20:d=1", "-c:v", "libx264",
                str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "ffmpeg_path": ffmpeg,
                "compress_internal_pauses": True,
                "internal_pause_mode": "experimental",
                "existing_output": "overwrite",
            })
            bundle = analyze_breath_cut_files([source], settings)
            clip = bundle["tasks"][0]["clips"][0]
            self.assertTrue(clip["silence_detection_error"])
            self.assertEqual(clip["kept_ranges"], [[0.0, 1.0]])
            result = export_breath_cut_bundle(bundle, settings)
            self.assertFalse(result["failed"])
            self.assertTrue(Path(result["completed"][0]["video"]).is_file())

    def test_db_silence_detection_places_cuts_inside_measured_quiet_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "silence-tone.wav"
            subprocess.run([
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=if(between(t\,1\,2.5)\,0.5*sin(2*PI*440*t)\,0):s=48000:d=4",
                str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "silence_threshold_db": -35,
                "min_silence_ms": 350,
            })
            ranges, error = detect_silence_ranges(
                source, ffmpeg, 4.0, settings
            )
            self.assertFalse(error)
            self.assertGreaterEqual(len(ranges), 2)
            start, end, decisions, warnings = refine_trim_boundaries(
                1.05, 2.45, 4.0, ranges, settings
            )
            self.assertFalse(warnings)
            self.assertAlmostEqual(start, 0.88, delta=0.08)
            self.assertAlmostEqual(end, 2.72, delta=0.08)
            self.assertEqual(
                [item["kind"] for item in decisions],
                ["head_cut", "tail_cut"],
            )

    def test_missing_db_boundary_preserves_audio_and_reports_exact_side(self):
        settings = normalize_smart_video_editor_settings({})
        start, end, decisions, warnings = refine_trim_boundaries(
            1.0, 2.0, 4.0, [], settings
        )
        self.assertEqual((start, end), (0.0, 4.0))
        self.assertFalse(decisions)
        self.assertEqual(
            [item["kind"] for item in warnings],
            ["head_boundary_unconfirmed", "tail_boundary_unconfirmed"],
        )

    def test_unmatched_script_still_trims_voice_proven_tail_gap(self):
        """A script mismatch must not retain a proven multi-second tail gap."""
        settings = normalize_smart_video_editor_settings({})
        script_lines, script_words = build_script_word_records(
            "Nepredstieraj, že si túto správu nevidela."
        )
        transcription = {
            "text": "Nie predstíraj, že si tu to správňe videla.",
            "duration": 6.016,
            "words": [
                {"text": "Nie", "start": 0.30, "end": 0.84},
                {"text": "predstíraj", "start": 0.84, "end": 1.56},
                {"text": "že", "start": 1.88, "end": 2.06},
                {"text": "si", "start": 2.06, "end": 2.22},
                {"text": "tu", "start": 2.22, "end": 2.36},
                {"text": "to", "start": 2.36, "end": 2.50},
                {"text": "správňe", "start": 2.50, "end": 3.12},
                {"text": "videla", "start": 3.12, "end": 3.44},
            ],
        }
        clip = _build_clip_plan(
            Path("1_20260918144407.mp4"),
            transcription,
            None,
            script_words,
            script_lines,
            settings,
            0,
            [[3.574, 5.032], [5.290, 6.016]],
            "",
            [[0.0, 0.304], [4.016, 6.016]],
            "",
        )
        self.assertEqual(clip["status"], "pink")
        self.assertAlmostEqual(clip["trim_end"], 4.016, places=3)
        self.assertIn(
            "script_position_unmatched",
            [item["kind"] for item in clip["issues"]],
        )
        self.assertIn(
            "tail_cut",
            [item["kind"] for item in clip["boundary_decisions"]],
        )

    def test_short_boundary_word_typo_counts_as_audio_evidence(self):
        expected = [{"norm": "ak"}]
        observed = [{"norm": "a"}]
        self.assertGreaterEqual(
            _compact_record_similarity(expected, observed), 0.72
        )

    def test_accent_only_word_variant_keeps_tail_anchor_and_trims_dead_air(self):
        self.assertTrue(_token_equivalent("amen", "ámen"))
        settings = normalize_smart_video_editor_settings({})
        script_lines, script_words = build_script_word_records(
            "V mene Pána Ježiša Krista. Amen."
        )
        transcription = {
            "text": "V mene Pána Ježiša Krista. Ámen.",
            "duration": 8.0,
            "words": [
                {"text": "V", "start": 0.0, "end": 0.12},
                {"text": "mene", "start": 0.12, "end": 0.42},
                {"text": "Pána", "start": 0.42, "end": 0.94},
                {"text": "Ježiša", "start": 0.94, "end": 1.52},
                {"text": "Krista.", "start": 1.52, "end": 2.02},
                {"text": "Ámen.", "start": 2.32, "end": 3.12},
            ],
        }
        clip = _build_clip_plan(
            Path("Maria_speaking_Slovak_Amen.mp4"),
            transcription,
            {"start": 0, "end": 5, "similarity": 1.0, "overlap_ratio": 0.0},
            script_words,
            script_lines,
            settings,
            0,
            [],
            "",
            [[2.328, 2.696], [4.344, 8.0]],
            "",
        )
        self.assertTrue(clip["word_timeline"][-1]["anchor"])
        self.assertAlmostEqual(clip["trim_end"], 4.564, places=3)
        self.assertIn(
            "tail_cut",
            [item["kind"] for item in clip["boundary_decisions"]],
        )

    def test_unanchored_text_does_not_restore_vad_proven_tail_gap(self):
        settings = normalize_smart_video_editor_settings({})
        script_lines, script_words = build_script_word_records("alpha omega")
        transcription = {
            "text": "alpha ending",
            "duration": 6.0,
            "words": [
                {"text": "alpha", "start": 0.2, "end": 0.8},
                {"text": "ending", "start": 1.0, "end": 2.0},
            ],
        }
        clip = _build_clip_plan(
            Path("unanchored-tail.mp4"),
            transcription,
            {"start": 0, "end": 1, "similarity": 0.7, "overlap_ratio": 0.0},
            script_words,
            script_lines,
            settings,
            0,
            [],
            "",
            [[3.0, 6.0]],
            "",
        )
        self.assertFalse(clip["word_timeline"][-1]["anchor"])
        self.assertAlmostEqual(clip["trim_end"], 3.22, places=3)
        self.assertNotIn(
            "unconfirmed_last_word",
            [item["kind"] for item in clip["boundary_warnings"]],
        )

    def test_missing_word_guard_does_not_restore_proven_no_voice_tail(self):
        clip = {
            "script_word_start": 164,
            "script_word_end": 179,
            "original_duration": 10.005,
            "trim_start": 0.0,
            "trim_end": 8.66,
            "word_timeline": [{
                "script_word_index": 179,
                "start": 7.7,
                "end": 8.2,
                "anchor": True,
            }],
            "voice_absence_ranges": [[8.44, 10.005]],
            "voice_detection_error": "",
            "boundary_decisions": [{"kind": "tail_cut"}],
            "boundary_warnings": [],
            "alignment_issues": [],
            "issues": [],
            "pause_removals": [],
        }
        _protect_edges_adjacent_to_missing_script([clip], [{
            "script_word_start": 180,
            "script_word_end": 180,
            "text": "Ak",
            "edge_audio_supported": False,
        }])
        self.assertAlmostEqual(clip["trim_end"], 8.66, places=3)
        self.assertNotIn(
            "missing_script_boundary_guard",
            [item.get("kind") for item in clip.get("issues", [])],
        )

    def test_source_discovery_excludes_generated_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "001.mp4").write_bytes(b"")
            (root / "nested").mkdir()
            (root / "nested" / "002.mov").write_bytes(b"")
            output = root / "智能剪辑结果"
            output.mkdir()
            (output / "001_智能剪辑.mp4").write_bytes(b"")
            files = discover_task_videos(root)
            self.assertEqual([path.name for path in files], ["001.mp4", "002.mov"])

    def test_analysis_uses_task_script_and_reuses_integer_fingerprint_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            source = task_dir / "clip.mp4"
            source.write_bytes(b"fake")
            jobs = [{
                "task_id": "0801",
                "task_name": "test",
                "label": "0801 | test",
                "task_dir": str(task_dir),
                "script": "Moj bože prosím zdravie",
                "language": "sk",
                "sources": [str(source)],
            }]
            model = _FakeWhisperModel()
            safe_settings = {"silence_detection_enabled": False}
            first = analyze_smart_video_jobs(jobs, model, safe_settings)
            stat = source.stat()
            os.utime(
                source,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
            )
            progress = []
            self.assertFalse(smart_video_jobs_require_model(jobs, safe_settings))
            self.assertTrue(smart_video_jobs_require_model(
                jobs,
                {
                    **safe_settings,
                    "whisper_model_size": "medium",
                },
            ))
            second = analyze_smart_video_jobs(
                jobs, None, safe_settings, progress=progress.append
            )
            self.assertEqual(model.calls, 1)
            self.assertEqual(first["summary"]["green_count"], 1)
            self.assertTrue(second["tasks"][0]["clips"][0]["from_cache"])
            self.assertEqual(second["summary"]["task_cache_count"], 1)
            self.assertTrue(any("完整分析缓存命中" in item for item in progress))
            report = task_dir / "智能剪辑结果" / "智能剪辑审核.json"
            self.assertTrue(report.is_file())
            text_report = task_dir / "智能剪辑结果" / "智能剪辑问题报告.txt"
            self.assertTrue(text_report.is_file())

    def test_unordered_files_are_sorted_by_spoken_script_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                root / "third_words.mp4",
                root / "first_words.mp4",
                root / "second_words.mp4",
            ]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-order",
                "task_name": "unordered",
                "label": "T-order | unordered",
                "task_dir": str(root),
                "script": "first words\nsecond words\nthird words",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            self.assertEqual(
                [clip["expected_text"] for clip in clips],
                ["first words", "second words", "third words"],
            )
            self.assertEqual(
                [clip["file_name"] for clip in clips],
                ["first_words.mp4", "second_words.mp4", "third_words.mp4"],
            )

    def test_numbered_order_survives_a_weak_but_unique_content_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1_20260913.mp4", root / "2.mp4", root / "3.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            script = "alpha bravo charlie\ndelta echo foxtrot\ngolf hotel india"
            model = _DictionaryWhisperModel({
                "1_20260913.mp4": "alpha bravcharlie",
                "2.mp4": "delta echo foxtrot",
                "3.mp4": "golf hotel india",
            })
            result = analyze_smart_video_jobs([{
                "task_id": "T-numbered",
                "task_name": "numbered",
                "label": "T-numbered | numbered",
                "task_dir": str(root),
                "script": script,
                "language": "en",
                "sources": [str(path) for path in sources],
            }], model, {"silence_detection_enabled": False})
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "content_verified_filename")
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["1_20260913.mp4", "2.mp4", "3.mp4"],
            )
            combined = " ".join(
                clip["expected_text"] for clip in task["clips"]
            )
            self.assertEqual(text_units(combined), text_units(script))

    def test_confident_content_order_overrides_incorrect_filename_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1.mp4", root / "2.mp4", root / "3.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-wrong-numbers",
                "task_name": "wrong-numbers",
                "label": "T-wrong-numbers | wrong-numbers",
                "task_dir": str(root),
                "script": "first spoken section\nsecond spoken section\nthird spoken section",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                "1.mp4": "third spoken section",
                "2.mp4": "first spoken section",
                "3.mp4": "second spoken section",
            }), {"silence_detection_enabled": False})
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "content_override_filename")
            self.assertTrue(task["ordering_conflict"])
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["2.mp4", "3.mp4", "1.mp4"],
            )
            self.assertTrue(all(
                any(
                    issue["kind"] == "filename_content_order_conflict"
                    for issue in clip["issues"]
                )
                for clip in task["clips"]
            ))

    def test_filename_order_is_only_a_visible_fallback_when_audio_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "2.mp4", root / "1.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-fallback",
                "task_name": "fallback",
                "label": "T-fallback | fallback",
                "task_dir": str(root),
                "script": "first section second section",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({"1.mp4": "", "2.mp4": ""}), {
                "silence_detection_enabled": False,
            })
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "filename_fallback")
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["1.mp4", "2.mp4"],
            )
            self.assertTrue(all(clip["status"] == "pink" for clip in task["clips"]))

    def test_srt_keeps_authoritative_first_and_last_words_missed_by_asr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"clip")
            script = "First, middle words, last!"
            result = analyze_smart_video_jobs([{
                "task_id": "T-edges",
                "task_name": "edges",
                "label": "T-edges | edges",
                "task_dir": str(root),
                "script": script,
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({"clip.mp4": "middle words"}), {
                "silence_detection_enabled": False,
            })
            task = result["tasks"][0]
            cues = build_srt_cues(task, result["settings"])
            self.assertEqual(
                text_units(" ".join(cue["text"] for cue in cues)),
                text_units(script),
            )
            self.assertEqual(
                " ".join(cue["text"] for cue in cues),
                script,
            )
            self.assertEqual(task["clips"][0]["script_word_start"], 0)
            self.assertEqual(task["clips"][0]["script_word_end"], 3)
            self.assertEqual(result["summary"]["missing_count"], 0)
            self.assertEqual(result["summary"]["unverified_count"], 2)
            self.assertFalse(result["summary"]["export_blocked"])

    def test_unanchored_leading_word_stays_with_following_numbered_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1.mp4", root / "2.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-numbered-boundary",
                "task_name": "numbered-boundary",
                "label": "T-numbered-boundary | numbered-boundary",
                "task_dir": str(root),
                "script": "alpha beta gamma delta epsilon vaša prosba bude hotová",
                "language": "sk",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                "1.mp4": "alpha beta gamma delta epsilon",
                "2.mp4": "váša prosba bude hotová",
            }), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            self.assertEqual(
                clips[0]["expected_text"], "alpha beta gamma delta epsilon"
            )
            self.assertEqual(clips[1]["expected_text"], "vaša prosba bude hotová")

    def test_srt_fills_missing_timing_inside_an_authoritative_range(self):
        lines, words = build_script_word_records("first middle last")
        task = {
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source_index": 0,
                "export_order": 1,
                "included": True,
                "script_word_start": 0,
                "script_word_end": 2,
                "trim_start": 0.0,
                "trim_end": 2.0,
                "kept_ranges": [[0.0, 2.0]],
                "word_timeline": [{
                    "script_word_index": 1,
                    "line_index": 0,
                    "start": 0.7,
                    "end": 1.2,
                }],
                "words": [],
            }],
        }
        cues = build_srt_cues(
            task,
            normalize_smart_video_editor_settings({"srt_max_words_per_block": 1}),
        )
        self.assertEqual([cue["text"] for cue in cues], ["first", "middle", "last"])

    def test_duplicate_take_is_auto_excluded_instead_of_exported_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "alpha_beta.mp4"
            second = root / "gamma_delta.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            result = analyze_smart_video_jobs([{
                "task_id": "T-duplicate",
                "task_name": "duplicate",
                "label": "T-duplicate | duplicate",
                "task_dir": str(root),
                "script": "alpha beta\ngamma delta",
                "language": "en",
                "sources": [str(first), str(first), str(second)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            duplicates = [
                clip for clip in clips if clip.get("auto_excluded_duplicate")
            ]
            self.assertEqual(len(duplicates), 1)
            self.assertFalse(duplicates[0]["included"])
            self.assertEqual(duplicates[0]["status"], "orange")
            self.assertEqual(duplicates[0]["duplicate_of"], "alpha_beta.mp4")
            self.assertEqual(result["summary"]["duplicate_count"], 1)
            self.assertEqual(
                [clip["file_name"] for clip in clips if clip["included"]],
                ["alpha_beta.mp4", "gamma_delta.mp4"],
            )
            self.assertEqual(
                subtitle_text_for_export(result["tasks"][0]),
                "alpha beta\ngamma delta",
            )
            self.assertTrue(result["summary"]["needs_review"])

    def test_different_files_with_same_take_and_script_position_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                root / "take_a.mp4",
                root / "take_b.mp4",
                root / "ending.mp4",
            ]
            for index, source in enumerate(sources):
                source.write_bytes(f"video-{index}".encode("utf-8"))
            model = _DictionaryWhisperModel({
                "take_a.mp4": "alpha beta gamma",
                "take_b.mp4": "alpha beta gamma",
                "ending.mp4": "delta epsilon zeta",
            })
            result = analyze_smart_video_jobs([{
                "task_id": "T-repeat-take",
                "label": "T-repeat-take",
                "task_dir": str(root),
                "script": "alpha beta gamma\ndelta epsilon zeta",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], model, {"silence_detection_enabled": False})
            duplicates = [
                clip for clip in result["tasks"][0]["clips"]
                if clip.get("auto_excluded_duplicate")
            ]
            self.assertEqual(len(duplicates), 1)
            self.assertFalse(duplicates[0]["included"])
            self.assertIn("重复", duplicates[0]["issue_reason"])

    def test_near_duplicate_with_short_omission_and_same_script_slot(self):
        complete = (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
            "kilo lima mike november oscar papa quebec romeo sierra tango "
            "uniform victor whiskey"
        )
        omitted = complete.replace("uniform victor whiskey", "")
        clips = [
            {"source": "take_complete.mp4", "file_name": "take_complete.mp4",
             "script_word_start": 0, "similarity": 1.0, "included": True},
            {"source": "take_omitted.mp4", "file_name": "take_omitted.mp4",
             "script_word_start": -1, "similarity": 0.0, "included": True},
        ]
        transcriptions = [
            {"text": complete, "duration": 10.0},
            {"text": omitted, "duration": 10.0},
        ]
        same_slot = [
            {"reliable": True, "start": 30, "end": 52},
            {"reliable": True, "start": 30, "end": 52},
        ]
        with mock.patch(
            "app_plugins.builtin.smart_video_editor.engine._stable_fingerprint",
            side_effect=[{"id": 1}, {"id": 2}],
        ):
            self.assertEqual(
                _mark_duplicate_clips(clips, transcriptions, same_slot), 1
            )
        self.assertEqual(sum(bool(clip["included"]) for clip in clips), 1)
        self.assertEqual(clips[0]["duplicate_group_id"], clips[1]["duplicate_group_id"])
        self.assertTrue(clips[1]["auto_excluded_duplicate"])

        # Similar speech at separate script positions must remain two clips.
        separate = [same_slot[0], {"reliable": True, "start": 60, "end": 82}]
        fresh = [dict(clip, included=True) for clip in clips]
        for clip in fresh:
            clip.pop("duplicate_group_id", None)
        with mock.patch(
            "app_plugins.builtin.smart_video_editor.engine._stable_fingerprint",
            side_effect=[{"id": 1}, {"id": 2}],
        ):
            self.assertEqual(
                _mark_duplicate_clips(fresh, transcriptions, separate), 0
            )

    def test_unresolved_extra_take_blocks_export_until_manually_handled(self):
        transcript = "alpha bravo charlie delta echo foxtrot golf hotel"
        clips = [
            {
                "file_name": name, "source": name, "included": True,
                "script_word_start": 0, "script_word_end": 7,
                "recognized_text": transcript, "content_order_start": 0,
                "content_order_end": 7, "content_order_reliable": True,
                "similarity": similarity, "source_index": index,
            }
            for index, (name, similarity) in enumerate((
                ("good.mp4", 1.0), ("extra.mp4", 0.8)
            ))
        ]
        bundle = {"tasks": [{"task_id": "extra", "label": "extra", "clips": clips}]}
        findings = smart_video_extra_clip_findings(bundle)
        self.assertEqual([item["file_name"] for item in findings], ["extra.mp4"])
        self.assertEqual(len(smart_video_export_blockers(bundle)), 1)
        clips[1]["extra_clip_approved"] = True
        self.assertFalse(smart_video_export_blockers(bundle))
        clips[1]["extra_clip_approved"] = False
        clips[1]["included"] = False
        self.assertFalse(smart_video_export_blockers(bundle))

    def test_review_dialog_requires_decision_for_suspected_extra_take(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "first.mp4", root / "second.mp4"]
            for source in sources:
                source.write_bytes(b"video")
            first_text = "alpha bravo charlie delta echo foxtrot golf hotel"
            second_text = "india juliet kilo lima mike november oscar papa"
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-extra-review", "label": "T-extra-review",
                "task_dir": str(root), "script": first_text + "\n" + second_text,
                "language": "en", "sources": [str(source) for source in sources],
            }], _DictionaryWhisperModel({
                sources[0].name: first_text,
                sources[1].name: second_text,
            }), {"silence_detection_enabled": False})
            clips = bundle["tasks"][0]["clips"]
            clips[1]["recognized_text"] = clips[0]["recognized_text"]
            clips[1]["content_order_start"] = clips[0]["content_order_start"]
            clips[1]["content_order_end"] = clips[0]["content_order_end"]
            clips[1]["content_order_reliable"] = True
            clips[1]["similarity"] = 0.8
            dialog = SmartVideoReviewDialog(bundle)
            try:
                self.assertFalse(dialog.export_button.isEnabled())
                self.assertIn("多余 1", dialog.detail_tabs.tabText(dialog.coverage_tab_index))
                dialog.table.setCurrentCell(1, dialog.COL_FILE)
                self.assertTrue(dialog.acknowledge_clip_button.isEnabled())
                dialog._set_clip_acknowledged(True)
                self.assertTrue(dialog.export_button.isEnabled())
            finally:
                dialog.close()

    def test_reordered_title_take_is_grouped_and_can_replace_kept_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                root / "take_title_first.mp4",
                root / "take_title_last.mp4",
                root / "ending.mp4",
            ]
            for index, source in enumerate(sources):
                source.write_bytes(f"video-{index}".encode("utf-8"))
            title_first = (
                "prayer for strength lord I come to you when I am weak"
            )
            title_last = (
                "lord I come to you when I am weak prayer for strength"
            )
            result = analyze_smart_video_jobs([{
                "task_id": "T-reordered-duplicate",
                "label": "T-reordered-duplicate",
                "task_dir": str(root),
                "script": title_first + "\nthank you amen",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                sources[0].name: title_first,
                sources[1].name: title_last,
                sources[2].name: "thank you amen",
            }), {"silence_detection_enabled": False})

            task = result["tasks"][0]
            grouped = [
                (index, clip)
                for index, clip in enumerate(task["clips"])
                if clip.get("duplicate_group_id")
            ]
            self.assertEqual(len(grouped), 2)
            self.assertEqual(
                len({clip["duplicate_group_id"] for _index, clip in grouped}),
                1,
            )
            self.assertEqual(
                sum(bool(clip.get("duplicate_selected")) for _index, clip in grouped),
                1,
            )
            previous_index = next(
                index for index, clip in grouped
                if clip.get("duplicate_selected")
            )
            replacement_index = next(
                index for index, clip in grouped
                if not clip.get("duplicate_selected")
            )
            previous_order = task["clips"][previous_index]["export_order"]
            replacement_old_order = task["clips"][replacement_index]["export_order"]

            select_duplicate_group_clip(
                task, replacement_index, result["settings"]
            )

            self.assertFalse(task["clips"][previous_index]["included"])
            self.assertTrue(task["clips"][replacement_index]["included"])
            self.assertEqual(
                task["clips"][replacement_index]["export_order"],
                previous_order,
            )
            self.assertEqual(
                task["clips"][previous_index]["export_order"],
                replacement_old_order,
            )
            self.assertEqual(
                sum(
                    bool(clip.get("included"))
                    for _index, clip in grouped
                ),
                1,
            )
            self.assertEqual(
                task["clips"][replacement_index]["script_word_start"], 0
            )

            review = SmartVideoTimelineReview(result)
            try:
                review._select_clip(replacement_index)
                self.assertFalse(review.duplicate_group_panel.isHidden())
                self.assertEqual(
                    len(review.duplicate_button_group.buttons()), 2
                )
                segment = next(
                    item for item in review.segments
                    if item["clip_index"] == replacement_index
                )
                self.assertTrue(segment["duplicate_selected"])
                review.duplicate_button_group.button(previous_index).click()
                self.assertTrue(
                    task["clips"][previous_index]["duplicate_selected"]
                )
                self.assertFalse(
                    task["clips"][replacement_index]["included"]
                )
            finally:
                review.close_player()
                review.close()

    def test_same_words_at_two_real_script_positions_are_not_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first-video")
            second.write_bytes(b"second-video")
            model = _DictionaryWhisperModel({
                first.name: "thank you all",
                second.name: "thank you all",
            })
            result = analyze_smart_video_jobs([{
                "task_id": "T-real-repeat",
                "label": "T-real-repeat",
                "task_dir": str(root),
                "script": "thank you all\nthank you all",
                "language": "en",
                "sources": [str(first), str(second)],
            }], model, {"silence_detection_enabled": False})
            self.assertEqual(result["summary"]["duplicate_count"], 0)
            self.assertTrue(all(
                clip.get("included", True)
                for clip in result["tasks"][0]["clips"]
            ))

    def test_timeline_can_exclude_and_restore_whole_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "first.mp4", root / "second.mp4"]
            for source in sources:
                source.write_bytes(b"video")
            result = analyze_smart_video_jobs([{
                "task_id": "T-manual-exclude",
                "label": "T-manual-exclude",
                "task_dir": str(root),
                "script": "alpha beta gamma\ndelta epsilon zeta",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                "first.mp4": "alpha beta gamma",
                "second.mp4": "delta epsilon zeta",
            }), {"silence_detection_enabled": False})
            review = SmartVideoTimelineReview(result)
            try:
                review._set_clip_included(1, False)
                self.assertFalse(result["tasks"][0]["clips"][1]["included"])
                self.assertTrue(sources[1].is_file())
                self.assertTrue(result["tasks"][0]["missing_blocks"])
                review._set_clip_included(1, True)
                self.assertTrue(result["tasks"][0]["clips"][1]["included"])
                self.assertFalse(result["tasks"][0]["missing_blocks"])
            finally:
                review.close_player()
                review.close()

    def test_cjk_phrase_token_maps_to_individual_script_character_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "你好世界.mp4"
            source.write_bytes(b"cjk")
            result = analyze_smart_video_jobs([{
                "task_id": "T-cjk",
                "task_name": "cjk",
                "label": "T-cjk | cjk",
                "task_dir": str(root),
                "script": "你好，世界！",
                "language": "zh",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clip = result["tasks"][0]["clips"][0]
            self.assertEqual(clip["expected_text"], "你好，世界！")
            self.assertEqual(len(clip["word_timeline"]), 4)
            self.assertTrue(all(item["anchor"] for item in clip["word_timeline"]))
            self.assertEqual(clip["status"], "green")

    def test_word_error_reports_exact_time_expected_and_recognized_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alpha_delta_gamma.mp4"
            source.write_bytes(b"mismatch")
            result = analyze_smart_video_jobs([{
                "task_id": "T-word-error",
                "task_name": "word-error",
                "label": "T-word-error | word-error",
                "task_dir": str(root),
                "script": "alpha beta gamma",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clip = result["tasks"][0]["clips"][0]
            word_issues = [
                issue for issue in clip["issues"]
                if issue["kind"] == "word_difference"
            ]
            self.assertEqual(len(word_issues), 1)
            issue = word_issues[0]
            self.assertEqual(issue["expected"], "beta")
            self.assertEqual(issue["recognized"], "delta")
            self.assertAlmostEqual(issue["start"], 0.65, places=2)
            self.assertAlmostEqual(issue["end"], 0.95, places=2)
            self.assertEqual(clip["status"], "orange")

    def test_repeated_reading_is_cut_without_silence_and_can_be_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"repeated speech")
            bundle = analyze_smart_video_jobs([{
                "task_id": "repeat", "task_dir": str(root),
                "script": "alpha beta gamma delta", "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({"clip.mp4": "alpha beta beta gamma delta"}), {
                "silence_detection_enabled": False, "voice_detection_enabled": False,
            })
            clip = bundle["tasks"][0]["clips"][0]
            self.assertEqual(clip["repeated_speech_removals"], [[0.65, 0.95]])
            self.assertTrue(any(issue["kind"] == "repeated_speech_cut" for issue in clip["issues"]))
            self.assertFalse(any(issue["kind"] == "extra_words" for issue in clip["alignment_issues"]))
            self.assertEqual(len(clip["word_timeline"]), 4)
            self.assertAlmostEqual(clip["word_timeline"][1]["start"], 1.05)
            self.assertTrue(all(not (left <= 0.8 < right) for left, right in clip["kept_ranges"]))
            clip["manual_keep_ranges"] = [[0.65, 0.95]]
            apply_manual_breath_overrides(clip, 0, clip["original_duration"], [])
            self.assertTrue(any(left <= 0.8 < right for left, right in clip["kept_ranges"]))

    def test_restart_with_fused_word_is_cut_but_unrelated_extra_words_are_not(self):
        for spoken, should_cut in (
            ("hovoril proti duchovoril proti duchu", True),
            ("hovoril extra words proti duchu", False),
            ("hovoril proti duchu", False),
        ):
            with self.subTest(spoken=spoken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "clip.mp4"
                source.write_bytes(spoken.encode("utf-8"))
                bundle = analyze_smart_video_jobs([{
                    "task_id": "restart", "task_dir": str(root),
                    "script": "hovoril proti duchu", "language": "sk",
                    "sources": [str(source)],
                }], _DictionaryWhisperModel({"clip.mp4": spoken}), {
                    "silence_detection_enabled": False, "voice_detection_enabled": False,
                })
                clip = bundle["tasks"][0]["clips"][0]
                self.assertEqual(bool(clip.get("repeated_speech_removals")), should_cut)

    def test_repetition_written_in_task_script_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"intended repetition")
            script = "alpha beta alpha beta gamma"
            bundle = analyze_smart_video_jobs([{
                "task_id": "intended", "task_dir": str(root),
                "script": script, "language": "en", "sources": [str(source)],
            }], _DictionaryWhisperModel({"clip.mp4": script}), {
                "silence_detection_enabled": False, "voice_detection_enabled": False,
            })
            self.assertFalse(bundle["tasks"][0]["clips"][0].get("repeated_speech_removals"))

    def test_boundary_repetition_is_removed_from_previous_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1.mp4", root / "2.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode())
            script = "alpha beta gamma delta epsilon zeta"
            bundle = analyze_smart_video_jobs([{
                "task_id": "boundary", "task_dir": str(root), "script": script,
                "language": "en", "sources": [str(source) for source in sources],
            }], _DictionaryWhisperModel({
                "1.mp4": "alpha beta gamma delta", "2.mp4": "gamma delta epsilon zeta",
            }), {"silence_detection_enabled": False, "voice_detection_enabled": False})
            task = bundle["tasks"][0]
            clip = task["clips"][0]
            self.assertEqual(clip["repeated_speech_removals"], [[1.05, 1.75]])
            self.assertTrue(all(not (left <= 1.3 < right) for left, right in clip["kept_ranges"]))
            self.assertEqual(text_units(" ".join(cue["text"] for cue in build_srt_cues(task, bundle["settings"]))), text_units(script))

    def test_srt_chunks_use_real_word_anchors_and_keep_punctuation(self):
        lines, words = build_script_word_records("Hello, world!")
        task = {
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source_index": 0,
                "export_order": 1,
                "included": True,
                "script_word_start": 0,
                "script_word_end": 1,
                "kept_ranges": [[0.0, 2.0]],
                "word_timeline": [
                    {"script_word_index": 0, "line_index": 0, "start": 0.2, "end": 0.55},
                    {"script_word_index": 1, "line_index": 0, "start": 1.15, "end": 1.60},
                ],
            }],
        }
        settings = normalize_smart_video_editor_settings({
            "srt_max_words_per_block": 1,
        })
        cues = build_srt_cues(task, settings)
        self.assertEqual([item["text"] for item in cues], ["Hello,", "world!"])
        self.assertEqual(
            [(round(item["start"], 2), round(item["end"], 2)) for item in cues],
            [(0.2, 0.55), (1.15, 1.6)],
        )

    def test_uncovered_script_is_reported_instead_of_forced_into_a_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            result = analyze_smart_video_jobs([{
                "task_id": "T-missing",
                "task_name": "missing",
                "label": "T-missing | missing",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            self.assertTrue(result["summary"]["needs_review"])
            self.assertEqual(result["summary"]["missing_count"], 1)
            self.assertTrue(result["summary"]["export_blocked"])
            self.assertIn(
                "second sentence is absent",
                result["tasks"][0]["missing_blocks"][0]["text"],
            )
            block = result["tasks"][0]["missing_blocks"][0]
            self.assertTrue(block["blocks_export"])
            self.assertEqual(block["previous_clip"], source.name)
            self.assertEqual(
                result["tasks"][0]["clips"][0]["expected_text"],
                "first words",
            )
            blockers = smart_video_export_blockers(result)
            self.assertEqual(len(blockers), 1)
            self.assertIn("second sentence is absent", blockers[0]["text"])
            with self.assertRaisesRegex(ValueError, "禁止生成"):
                validate_smart_video_bundle_for_export(result)

    def test_export_core_refuses_missing_segment_before_starting_ffmpeg(self):
        lines, words = build_script_word_records("first line\nmissing full line")
        bundle = {
            "settings": {},
            "tasks": [{
                "task_id": "T-blocked",
                "label": "T-blocked | missing",
                "script_lines": lines,
                "script_words": words,
                "clips": [{
                    "file_name": "first.mp4",
                    "included": True,
                    "script_word_start": 0,
                    "script_word_end": 1,
                }],
            }],
        }
        with mock.patch("model.SmartVideoEditor._resolve_ffmpeg") as resolve:
            with self.assertRaisesRegex(ValueError, "missing full line"):
                export_smart_video_bundle(bundle)
        resolve.assert_not_called()

    def test_short_complete_sentence_is_a_blocker_but_edge_word_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_sentence.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-short-sentence",
                "label": "T-short-sentence",
                "task_dir": str(root),
                "script": "First sentence. Go now! Final sentence.",
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({
                "first_sentence.mp4": "First sentence Final sentence",
            }), {"silence_detection_enabled": False})
            blockers = smart_video_export_blockers(bundle)
            self.assertTrue(any("Go now" in block["text"] for block in blockers))

    def test_mistokenized_opening_sentence_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "1_20260914113802.mp4"
            source.write_bytes(b"first")
            script = (
                "Nevynechaj to! Táto modlitba už zmenila život mnohých ľudí. "
                "Mnohí povedali, že keď ju prečítali a modlili sa, "
                "skutočne pocítili zmenu."
            )
            recognized = (
                "Neví nechajto, táto modlitba už zmennila život mnohých ľudí. "
                "Mnohý povedali, že keď ju prečítali a modlili sa "
                "skutočne pocítili zmenu."
            )
            bundle = analyze_smart_video_jobs([{
                "task_id": "212",
                "label": "212 | regression",
                "task_dir": str(root),
                "script": script,
                "language": "sk",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({source.name: recognized}), {
                "silence_detection_enabled": False,
            })

            task = bundle["tasks"][0]
            clip = task["clips"][0]
            self.assertEqual(task["missing_blocks"], [])
            self.assertFalse(bundle["summary"]["export_blocked"])
            self.assertEqual(clip["script_word_start"], 0)
            self.assertEqual(text_units(clip["expected_text"]), text_units(script))
            self.assertTrue(any(
                issue.get("kind") == "word_difference"
                and "Nevynechaj to" in issue.get("expected", "")
                and "Neví nechajto" in issue.get("recognized", "")
                for issue in clip["issues"]
            ), clip["issues"])
            self.assertEqual(clip["status"], "pink")

    def test_wrong_words_stay_severe_review_without_becoming_a_missing_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "wrong_words.mp4"
            source.write_bytes(b"wrong")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-wrong-words",
                "label": "T-wrong-words",
                "task_dir": str(root),
                "script": "alpha beta gamma delta epsilon zeta eta theta",
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({
                "wrong_words.mp4": "alpha one two three epsilon zeta eta theta",
            }), {"silence_detection_enabled": False})
            clip = bundle["tasks"][0]["clips"][0]
            self.assertEqual(clip["status"], "pink")
            self.assertFalse(smart_video_export_blockers(bundle))

    def test_review_dialog_disables_export_and_lists_exact_missing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-dialog-missing",
                "task_name": "dialog-missing",
                "label": "T-dialog-missing | dialog-missing",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                self.assertFalse(dialog.export_button.isEnabled())
                self.assertFalse(dialog.blocker_banner.isHidden())
                self.assertEqual(dialog.missing_tree.topLevelItemCount(), 1)
                row = dialog.missing_tree.topLevelItem(0)
                self.assertEqual(row.text(0), "未处理")
                self.assertIn("second sentence is absent", row.text(3))
                self.assertIn("first_words.mp4", row.text(4))
                block = dialog._selected_missing_block()
                neighbor = dialog._missing_neighbor_clip(block, "previous")
                self.assertIsNotNone(neighbor)
                self.assertEqual(neighbor["file_name"], "first_words.mp4")
                self.assertTrue(dialog.preview_missing_before_button.isEnabled())
            finally:
                dialog.close()

    def test_review_dialog_names_source_video_from_reviewed_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-rename",
                "label": "T-rename",
                "task_dir": str(root),
                "script": "first words",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                with mock.patch.object(
                    QtWidgets.QMessageBox,
                    "question",
                    return_value=QtWidgets.QMessageBox.Yes,
                ), mock.patch.object(QtWidgets.QMessageBox, "information"):
                    dialog.rename_videos_button.click()
                renamed = root / "[01] first_words.mp4"
                self.assertTrue(renamed.is_file())
                self.assertFalse(source.exists())
                self.assertEqual(dialog.bundle["tasks"][0]["clips"][0]["source"], str(renamed))
                self.assertEqual(dialog.table.item(0, dialog.COL_FILE).text(), renamed.name)
            finally:
                dialog.close()

    def test_human_approval_allows_export_and_expires_after_edit_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-approved",
                "task_name": "approved",
                "label": "T-approved | approved",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            task = bundle["tasks"][0]
            set_smart_video_missing_review(task, "approved")
            self.assertFalse(smart_video_export_blockers(bundle))
            self.assertEqual(subtitle_text_for_export(task), task["script"])

            task["clips"][0]["trim_end"] -= 0.1
            self.assertTrue(smart_video_export_blockers(bundle))

    def test_review_dialog_can_approve_or_skip_a_missing_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-review-action",
                "label": "T-review-action",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                with mock.patch.object(
                    QtWidgets.QMessageBox,
                    "question",
                    return_value=QtWidgets.QMessageBox.Yes,
                ):
                    dialog.timeline_review.approve_task_button.click()
                self.assertTrue(dialog.export_button.isEnabled())
                self.assertEqual(
                    dialog.missing_tree.topLevelItem(0).text(0), "人工通过"
                )
                self.assertIn(
                    "继续生成",
                    dialog.timeline_review.task_decision_status.text(),
                )

                dialog.timeline_review.skip_task_button.click()
                self.assertTrue(dialog.export_button.isEnabled())
                self.assertEqual(dialog.export_button.text(), "导出其余任务")
                self.assertIn(
                    "本次将跳过",
                    dialog.timeline_review.task_decision_status.text(),
                )
            finally:
                dialog.close()

    def test_timeline_filters_and_locates_missing_segment_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clean_source = root / "gamma_delta.mp4"
            missing_source = root / "first_words.mp4"
            clean_source.write_bytes(b"clean")
            missing_source.write_bytes(b"missing")
            bundle = analyze_smart_video_jobs([
                {
                    "task_id": "T-clean",
                    "label": "T-clean",
                    "task_dir": str(root / "clean"),
                    "script": "gamma delta",
                    "language": "en",
                    "sources": [str(clean_source)],
                },
                {
                    "task_id": "T-missing-filter",
                    "label": "T-missing-filter",
                    "task_dir": str(root / "missing"),
                    "script": "first words\nsecond sentence is absent",
                    "language": "en",
                    "sources": [str(missing_source)],
                },
            ], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                timeline = dialog.timeline_review
                self.assertEqual(
                    timeline.missing_task_count_label.text(),
                    "缺段任务 1（未处理 1）",
                )
                timeline.task_filter_combo.setCurrentIndex(
                    timeline.task_filter_combo.findData("unresolved")
                )
                self.assertEqual(timeline.task_combo.count(), 1)
                self.assertEqual(timeline.task_combo.itemData(0), 1)
                self.assertEqual(timeline.task_index, 1)
                self.assertIn("⛔ 未处理 · 缺段 1", timeline.task_combo.itemText(0))

                timeline.issue_filter_combo.setCurrentIndex(
                    timeline.issue_filter_combo.findData("missing")
                )
                self.assertEqual(timeline.issue_tree.topLevelItemCount(), 1)
                marker = timeline.issue_tree.topLevelItem(0).data(
                    0, QtCore.Qt.UserRole
                )
                self.assertEqual(marker["kind"], "missing")

                set_smart_video_missing_review(bundle["tasks"][1], "approved")
                timeline.refresh(bundle)
                self.assertEqual(timeline.task_combo.count(), 0)
                self.assertEqual(
                    timeline.missing_task_count_label.text(),
                    "缺段任务 1（未处理 0）",
                )
                timeline.task_filter_combo.setCurrentIndex(
                    timeline.task_filter_combo.findData("missing")
                )
                self.assertEqual(timeline.task_combo.count(), 1)
                self.assertIn("✓ 已确认完整", timeline.task_combo.itemText(0))

                timeline.select_clip(0, 0)
                self.assertEqual(timeline.task_filter_combo.currentData(), "all")
                self.assertEqual(timeline.task_index, 0)
            finally:
                dialog.close()

    def test_export_result_dialog_keeps_long_paths_inside_visible_columns(self):
        long_name = "very-long-video-name-" + ("x" * 180) + ".mp4"
        video_path = str(Path("C:/exports") / long_name)
        srt_path = str(Path("C:/exports") / long_name.replace(".mp4", ".srt"))
        dialog = SmartVideoExportResultDialog({
            "completed": [{
                "task_id": "T-very-long-name",
                "video": video_path,
                "srt": srt_path,
            }],
            "failed": [],
            "skipped": [],
        })
        try:
            row = dialog.tree.topLevelItem(0)
            self.assertEqual(row.text(2), long_name)
            self.assertEqual(row.toolTip(2), video_path)
            self.assertEqual(row.toolTip(3), srt_path)
            self.assertEqual(
                dialog.tree.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarAlwaysOff,
            )
            header = dialog.tree.header()
            self.assertEqual(
                header.sectionResizeMode(2), QtWidgets.QHeaderView.Stretch
            )
            self.assertEqual(
                header.sectionResizeMode(3), QtWidgets.QHeaderView.Stretch
            )
        finally:
            dialog.close()

    def test_skipped_missing_task_does_not_block_other_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first_words.mp4"
            second = root / "gamma_delta.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            bundle = analyze_smart_video_jobs([
                {
                    "task_id": "T-skip",
                    "label": "T-skip",
                    "task_dir": str(root / "skip"),
                    "script": "first words\nsecond sentence is absent",
                    "language": "en",
                    "sources": [str(first)],
                },
                {
                    "task_id": "T-normal",
                    "label": "T-normal",
                    "task_dir": str(root / "normal"),
                    "script": "gamma delta",
                    "language": "en",
                    "sources": [str(second)],
                },
            ], _MappingWhisperModel(), {"silence_detection_enabled": False})
            set_smart_video_missing_review(bundle["tasks"][0], "skipped")
            with mock.patch(
                "model.SmartVideoEditor._resolve_ffmpeg", return_value="ffmpeg"
            ), mock.patch(
                "model.SmartVideoEditor._export_task",
                return_value={"task_id": "T-normal", "status": "completed"},
            ) as export_task:
                result = export_smart_video_bundle(bundle)
            self.assertEqual(len(result["skipped"]), 1)
            self.assertEqual(result["skipped"][0]["task_id"], "T-skip")
            self.assertEqual([item["task_id"] for item in result["completed"]], ["T-normal"])
            self.assertEqual(export_task.call_count, 1)

    def test_all_skipped_tasks_do_not_require_ffmpeg(self):
        lines, words = build_script_word_records("first words\nmissing full line")
        task = {
            "task_id": "T-all-skipped",
            "label": "T-all-skipped",
            "task_dir": "C:/missing-task",
            "script": "first words\nmissing full line",
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source": "C:/first.mp4",
                "file_name": "first.mp4",
                "included": True,
                "export_order": 1,
                "trim_start": 0.0,
                "trim_end": 1.0,
                "script_word_start": 0,
                "script_word_end": 1,
                "word_timeline": [
                    {"script_word_index": 0, "anchor": True},
                    {"script_word_index": 1, "anchor": True},
                ],
            }],
        }
        set_smart_video_missing_review(task, "skipped")
        bundle = {"settings": {}, "tasks": [task]}
        with mock.patch("model.SmartVideoEditor._resolve_ffmpeg") as resolve:
            result = export_smart_video_bundle(bundle)
        resolve.assert_not_called()
        self.assertFalse(result["completed"])
        self.assertEqual(len(result["skipped"]), 1)

    def test_export_failure_keeps_traceback_and_logs_it(self):
        bundle = {"tasks": [{
            "task_id": "145",
            "label": "145",
            "task_dir": "C:/task/145",
        }]}
        with mock.patch(
            "model.SmartVideoEditor.validate_smart_video_bundle_for_export"
        ), mock.patch(
            "model.SmartVideoEditor._resolve_ffmpeg", return_value="ffmpeg"
        ), mock.patch(
            "model.SmartVideoEditor._export_task",
            side_effect=AttributeError("missing_attribute"),
        ):
            result = export_smart_video_bundle(bundle)

        failure = result["failed"][0]
        self.assertEqual(failure["task_id"], "145")
        self.assertIn("AttributeError: missing_attribute", failure["traceback"])
        context = mock.Mock()
        plugin = SmartVideoEditorPlugin()
        plugin.context = context
        plugin._log_export_failures("智能剪辑", result["failed"])
        message = context.log.call_args.args[0]
        self.assertIn("任务 145", message)
        self.assertIn("C:/task/145", message)
        self.assertIn("AttributeError: missing_attribute", message)
        self.assertEqual(context.log.call_args.kwargs["level"], 40)

    def test_pending_review_records_are_persistent_and_reopenable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-pending",
                "label": "T-pending",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            records = update_smart_video_pending_reviews([], bundle)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending")
            self.assertEqual(
                normalize_smart_video_pending_reviews(records), records
            )
            dialog = SmartVideoPendingDialog(records)
            try:
                self.assertEqual(dialog.tree.topLevelItemCount(), 1)
                self.assertIn("second sentence is absent", dialog.tree.topLevelItem(0).text(3))
            finally:
                dialog.close()

            set_smart_video_missing_review(bundle["tasks"][0], "approved")
            self.assertEqual(update_smart_video_pending_reviews(records, bundle), [])

    def test_excluding_unique_clip_immediately_blocks_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "first_words.mp4", root / "gamma_delta.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-exclude",
                "task_name": "exclude",
                "label": "T-exclude | exclude",
                "task_dir": str(root),
                "script": "first words\ngamma delta",
                "language": "en",
                "sources": [str(source) for source in sources],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                self.assertTrue(dialog.export_button.isEnabled())
                dialog.table.item(1, dialog.COL_INCLUDE).setCheckState(
                    QtCore.Qt.Unchecked
                )
                self.app.processEvents()
                self.assertFalse(dialog.export_button.isEnabled())
                self.assertIn("gamma delta", dialog.missing_tree.topLevelItem(0).text(3))
            finally:
                dialog.close()

    def test_reviewed_trim_rebuilds_ranges_and_srt_uses_authoritative_text(self):
        clip = {
            "file_name": "clip.mp4",
            "included": True,
            "original_duration": 5.0,
            "trim_start": 0.0,
            "trim_end": 5.0,
            "pause_removals": [[1.0, 2.0]],
            "cues": [{"text": "wrong", "start": 0.5, "end": 4.5}],
        }
        apply_clip_review(clip, True, 0.2, 4.8, "correct task words")
        self.assertEqual(clip["kept_ranges"], [[0.2, 1.0], [2.0, 4.8]])
        task = {"clips": [clip]}
        cues = build_srt_cues(
            task,
            normalize_smart_video_editor_settings({"srt_max_words_per_block": 2}),
        )
        self.assertEqual([cue["text"] for cue in cues], ["correct task words"])

    def test_final_alignment_text_uses_included_reviewed_clips_in_export_order(self):
        task = {
            "script": "stale complete task text",
            "clips": [
                {
                    "source_index": 0,
                    "export_order": 2,
                    "included": True,
                    "expected_text": "second corrected part",
                },
                {
                    "source_index": 1,
                    "export_order": 1,
                    "included": True,
                    "expected_text": "first part",
                },
                {
                    "source_index": 2,
                    "export_order": 3,
                    "included": False,
                    "expected_text": "excluded bad take",
                },
            ],
        }
        self.assertEqual(
            subtitle_text_for_export(task),
            "first part\nsecond corrected part",
        )

    def test_final_alignment_preserves_full_script_when_old_clip_ranges_lost_edges(self):
        task = {
            "script": "Tí z vás full middle Vaša final words.",
            "clips": [
                {
                    "source_index": 0,
                    "export_order": 1,
                    "included": True,
                    "expected_text": "full middle",
                },
                {
                    "source_index": 1,
                    "export_order": 2,
                    "included": True,
                    "expected_text": "final words.",
                },
            ],
        }
        self.assertEqual(
            subtitle_text_for_export(task),
            "Tí z vás full middle Vaša final words.",
        )

    def test_proven_subtitle_path_reuses_loaded_model_for_forced_alignment(self):
        class FakeAlignmentResult:
            def merge_all_segments(self):
                return self

            def split_by_length(self, **_kwargs):
                return self

            def to_srt_vtt(self, output_path, **_kwargs):
                Path(output_path).write_text(
                    "1\n00:00:00,100 --> 00:00:01,200\ncorrect words\n",
                    encoding="utf-8",
                )

        loaded_model = object()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "aligned.srt"
            with mock.patch(
                "stable_whisper.alignment.align",
                return_value=FakeAlignmentResult(),
            ) as align_mock, mock.patch(
                "stable_whisper.load_faster_whisper"
            ) as load_mock:
                generate_srt_whisper_only(
                    "final.mp4",
                    "correct words",
                    str(output),
                    language="en",
                    max_words_per_block=3,
                    model=loaded_model,
                )
            load_mock.assert_not_called()
            self.assertIs(align_mock.call_args.args[0], loaded_model)
            self.assertEqual(align_mock.call_args.args[1], "final.mp4")
            self.assertEqual(align_mock.call_args.kwargs["text"], "correct words")
            self.assertTrue(output.is_file())

    def test_subtitle_sentence_split_preserves_original_terminal_period(self):
        self.assertEqual(
            smart_split_sentences("First sentence. Final sentence."),
            "First sentence.\nFinal sentence.",
        )
        self.assertEqual(
            smart_split_sentences("First sentence. Final sentence"),
            "First sentence.\nFinal sentence",
        )

    def test_program_settings_exposes_smart_editor_without_touching_subtitle_values(self):
        page = SmartVideoEditorSettingsPage()
        try:
            page.lead_padding_spinbox.setValue(180)
            page.silence_db_spinbox.setValue(-42)
            page.vad_threshold_spinbox.setValue(0.6)
            page.vad_neg_threshold_spinbox.setValue(0.4)
            page.vad_min_speech_spinbox.setValue(180)
            page.vad_speech_pad_spinbox.setValue(275)
            page.breath_detection_mode_combo.setCurrentIndex(
                page.breath_detection_mode_combo.findData("intersection")
            )
            page.pause_keep_before_spinbox.setValue(110)
            page.pause_keep_after_spinbox.setValue(240)
            page.whisper_model_combo.setCurrentIndex(
                page.whisper_model_combo.findData("medium")
            )
            page.use_main_subtitle_settings_checkbox.setChecked(False)
            page.subtitle_max_words_spinbox.setValue(4)
            page.subtitle_max_chars_spinbox.setValue(32)
            page.subtitle_gap_spinbox.setValue(0)
            page.subtitle_line_break_checkbox.setChecked(True)
            settings = page.settings()
            self.assertEqual(settings["lead_padding_ms"], 180)
            self.assertEqual(settings["silence_threshold_db"], -42)
            self.assertTrue(settings["voice_detection_enabled"])
            self.assertAlmostEqual(settings["vad_threshold"], 0.6)
            self.assertAlmostEqual(settings["vad_neg_threshold"], 0.4)
            self.assertEqual(settings["vad_min_speech_ms"], 180)
            self.assertEqual(settings["vad_speech_pad_ms"], 275)
            self.assertEqual(settings["breath_detection_mode"], "intersection")
            self.assertEqual(settings["pause_keep_before_ms"], 110)
            self.assertEqual(settings["pause_keep_after_ms"], 240)
            self.assertEqual(settings["whisper_model_size"], "medium")
            self.assertFalse(settings["use_main_subtitle_settings"])
            self.assertEqual(settings["srt_max_words_per_block"], 4)
            self.assertEqual(settings["srt_max_chars_per_block"], 32)
            self.assertEqual(settings["srt_block_gap_ms"], 0)
            self.assertTrue(settings["srt_include_line_breaks"])
        finally:
            page.widget.close()

    def test_program_settings_exposes_output_filename_length(self):
        dialog = MainSettingDialog()
        try:
            dialog._load_task_result_config({})
            self.assertEqual(
                dialog.task_result_filename_max_length_spinbox.value(),
                50,
            )
            dialog.task_result_filename_max_length_spinbox.setValue(72)
            self.assertEqual(
                dialog._get_task_result_config()[
                    "task_output_filename_max_length"
                ],
                72,
            )
        finally:
            dialog.close()

    def test_source_and_review_dialogs_keep_user_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            source.write_bytes(b"fake")
            jobs = [{
                "task_id": "1",
                "label": "1 | demo",
                "task_dir": temporary,
                "script": "correct words",
                "sources": [str(source)],
            }]
            source_dialog = SmartVideoSourceDialog(jobs)
            try:
                source_dialog.accept()
                self.assertEqual(source_dialog.selected_jobs[0]["sources"], [str(source)])
            finally:
                source_dialog.close()

            bundle = {
                "settings": normalize_smart_video_editor_settings({}),
                "summary": {
                    "clip_count": 1,
                    "green_count": 0,
                    "orange_count": 1,
                    "pink_count": 0,
                },
                "tasks": [{
                    "label": "1 | demo",
                    "report_path": str(Path(temporary) / "report.json"),
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "status": "orange",
                        "issue_reason": "check",
                        "similarity": 0.65,
                        "trim_start": 0.1,
                        "trim_end": 2.0,
                        "original_duration": 2.2,
                        "pause_removals": [],
                        "removed_seconds": 0.0,
                        "kept_ranges": [[0.1, 2.0]],
                        "expected_text": "correct words",
                        "recognized_text": "correct word",
                        "issues": [{
                            "severity": "orange",
                            "kind": "word_difference",
                            "start": 0.8,
                            "end": 1.1,
                            "line_start": 1,
                            "line_end": 1,
                            "title": "单词不一致",
                            "expected": "words",
                            "recognized": "word",
                            "detail": "正确文案与视频在此处出现替换或错读。",
                        }],
                        "cues": [{
                            "text": "correct words",
                            "start": 0.2,
                            "end": 1.8,
                        }],
                        "included": True,
                    }],
                }],
            }
            review_dialog = SmartVideoReviewDialog(bundle)
            try:
                reviewed = review_dialog.collect()
                self.assertTrue(reviewed["tasks"][0]["clips"][0]["included"])
                self.assertEqual(
                    review_dialog.review_mode_tabs.currentIndex(),
                    review_dialog.timeline_tab_index,
                )
                self.assertGreaterEqual(len(review_dialog.timeline_review.segments), 1)
                self.assertTrue(any(
                    segment.get("is_removed")
                    for segment in review_dialog.timeline_review.segments
                ))
                self.assertEqual(len(review_dialog.timeline_review.markers), 1)
                self.assertFalse(review_dialog.table.isColumnHidden(
                    review_dialog.COL_ISSUES
                ))
                self.assertTrue(review_dialog.table.isColumnHidden(
                    review_dialog.COL_EXPECTED
                ))
                self.assertEqual(review_dialog.issue_tree.topLevelItemCount(), 1)
                self.assertIn(
                    "0.800-1.100s",
                    review_dialog.issue_tree.topLevelItem(0).text(1),
                )
                self.assertTrue(review_dialog.problem_only_checkbox.isChecked())
                self.assertEqual(review_dialog.problem_position_label.text(), "问题 1 / 1")
                self.assertIn("正确", review_dialog.detail_diff.toPlainText())
                self.assertIn("识别", review_dialog.detail_diff.toPlainText())
                with mock.patch(
                    "PYUI.smart_video_editor_pyui.save_analysis_reports"
                ):
                    review_dialog._set_clip_acknowledged(True)
                reviewed_clip = review_dialog.bundle["tasks"][0]["clips"][0]
                self.assertTrue(reviewed_clip["review_acknowledged"])
                self.assertEqual(
                    review_dialog.table.item(0, review_dialog.COL_STATUS).text(),
                    "人工已核对",
                )
            finally:
                review_dialog.close()

    def test_timeline_reorders_clips_and_can_clear_internal_breath_cuts(self):
        with tempfile.TemporaryDirectory() as temporary:
            sources = []
            for name in ("one.mp4", "two.mp4"):
                source = Path(temporary) / name
                source.write_bytes(b"fake")
                sources.append(source)
            script_lines, script_words = build_script_word_records(
                "alpha beta gamma delta"
            )
            clips = []
            for index, source in enumerate(sources):
                clips.append({
                    "source": str(source),
                    "file_name": source.name,
                    "source_index": index,
                    "export_order": index + 1,
                    "status": "green",
                    "trim_start": 0.0,
                    "trim_end": 2.0,
                    "original_duration": 2.0,
                    "pause_removals": [[0.7, 1.2]] if index == 0 else [],
                    "silence_ranges": [],
                    "voice_absence_ranges": [],
                    "kept_ranges": (
                        [[0.0, 0.7], [1.2, 2.0]]
                        if index == 0 else [[0.0, 2.0]]
                    ),
                    "removed_seconds": 0.5 if index == 0 else 0.0,
                    "expected_text": (
                        "alpha beta" if index == 0 else "gamma delta"
                    ),
                    "recognized_text": (
                        "alpha beta" if index == 0 else "gamma delta"
                    ),
                    "script_word_start": index * 2,
                    "script_word_end": index * 2 + 1,
                    "issues": [],
                    "words": [],
                    "included": True,
                })
            bundle = {
                "settings": normalize_smart_video_editor_settings({
                    "compress_internal_pauses": True,
                    "internal_pause_mode": "experimental",
                }),
                "summary": {"clip_count": 2, "green_count": 2},
                "tasks": [{
                    "label": "demo",
                    "script": "alpha beta gamma delta",
                    "script_lines": script_lines,
                    "script_words": script_words,
                    "clips": clips,
                }],
            }
            dialog = SmartVideoReviewDialog(bundle)
            try:
                timeline = dialog.timeline_review
                timeline.select_clip(0, 0)
                self.assertEqual(
                    timeline.task_script_text.toPlainText(),
                    "alpha beta gamma delta",
                )
                self.assertEqual(
                    timeline.task_script_text.textCursor().selectedText(),
                    "alpha beta",
                )
                timeline.select_clip(0, 1)
                self.assertEqual(
                    timeline.task_script_text.textCursor().selectedText(),
                    "gamma delta",
                )
                timeline._move_clip_to(0, 1)
                self.assertEqual(
                    [clip["export_order"] for clip in dialog.bundle["tasks"][0]["clips"]],
                    [2, 1],
                )
                self.assertEqual(
                    dialog.table.item(0, dialog.COL_ORDER).text(), "2"
                )

                timeline.compress_pauses_checkbox.setChecked(False)
                timeline._apply_breath_settings()
                self.assertEqual(
                    dialog.bundle["tasks"][0]["clips"][0]["pause_removals"], []
                )
                self.assertEqual(
                    dialog.table.item(0, dialog.COL_REMOVED).text(), "0.00"
                )
            finally:
                dialog.close()

    def test_readable_problem_report_contains_time_and_word_difference(self):
        task = {
            "label": "T | demo",
            "clips": [{
                "export_order": 1,
                "source_index": 0,
                "file_name": "clip.mp4",
                "status": "orange",
                "trim_start": 0.0,
                "trim_end": 2.0,
                "original_duration": 2.2,
                "expected_text": "correct words",
                "recognized_text": "correct word",
                "issues": [{
                    "severity": "orange",
                    "title": "单词不一致",
                    "start": 0.8,
                    "end": 1.1,
                    "expected": "words",
                    "recognized": "word",
                    "detail": "此处错读。",
                }],
            }],
            "missing_blocks": [],
        }
        report = render_task_problem_report(task, {})
        self.assertIn("0.800-1.100 秒", report)
        self.assertIn("应为「words」", report)
        self.assertIn("识别为「word」", report)

    def test_ffmpeg_export_creates_trimmed_video_and_matching_srt(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run([
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=25:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "ffmpeg_path": ffmpeg,
                "output_folder_name": "out",
                "existing_output": "overwrite",
                "srt_max_words_per_block": 3,
                "srt_include_line_breaks": True,
                "srt_block_gap_ms": 0,
            })
            bundle = {
                "settings": settings,
                "tasks": [{
                    "task_id": "T1",
                    "task_name": "demo",
                    "label": "T1 | demo",
                    "output_dir": str(root / "out"),
                    "script_lines": ["authoritative task text"],
                    "script_words": build_script_word_records(
                        "authoritative task text"
                    )[1],
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "included": True,
                        "kept_ranges": [[0.0, 0.5], [1.5, 2.0]],
                        "removed_seconds": 1.0,
                        "source_index": 0,
                        "export_order": 1,
                        "script_word_start": 0,
                        "script_word_end": 2,
                        "word_timeline": [
                            {"script_word_index": 0, "line_index": 0, "start": 0.1, "end": 0.3},
                            {"script_word_index": 1, "line_index": 0, "start": 0.3, "end": 1.7},
                            {"script_word_index": 2, "line_index": 0, "start": 1.7, "end": 1.9},
                        ],
                    }],
                }],
            }
            alignment_calls = []

            def fake_generate_srt(audio_path, text, output_path, **kwargs):
                alignment_calls.append((audio_path, text, output_path, kwargs))
                Path(output_path).write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n"
                    f"{text}\n",
                    encoding="utf-8",
                )

            with mock.patch(
                "model.SubtitleHelper.generate_srt_whisper_only",
                side_effect=fake_generate_srt,
            ):
                result = export_smart_video_bundle(bundle, settings)
            self.assertFalse(result["failed"])
            item = result["completed"][0]
            self.assertTrue(Path(item["video"]).is_file())
            self.assertEqual(item["subtitle_alignment"], "final_media_stable_whisper")
            srt_text = Path(item["srt"]).read_text(encoding="utf-8-sig")
            self.assertIn("authoritative task text", srt_text)
            self.assertEqual(len(alignment_calls), 1)
            aligned_media, aligned_text, _output_path, aligned_options = alignment_calls[0]
            self.assertEqual(Path(aligned_media), Path(item["video"]))
            self.assertEqual(aligned_text, "authoritative task text")
            self.assertTrue(aligned_options["include_line_breaks"])
            self.assertEqual(aligned_options["max_words_per_block"], 3)
            self.assertEqual(aligned_options["max_chars_per_block"], 50)
            self.assertEqual(aligned_options["block_gap_ms"], 0)
            ffprobe = shutil.which("ffprobe")
            if ffprobe:
                probe = subprocess.run([
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=start_time,has_b_frames,duration,nb_frames,r_frame_rate",
                    "-of", "json",
                    item["video"],
                ], check=True, capture_output=True, text=True)
                timing = json.loads(probe.stdout)["streams"][0]
                self.assertAlmostEqual(float(timing["start_time"]), 0.0, places=3)
                self.assertEqual(int(timing["has_b_frames"]), 0)
                rate_num, rate_den = map(
                    int, timing["r_frame_rate"].split("/", 1)
                )
                frame_duration = (
                    int(timing["nb_frames"]) / (rate_num / rate_den)
                )
                # Joining independently AAC-encoded MP4 segments used to add
                # encoder padding before every following segment.  Their video
                # timestamps then contained gaps even though the frame count
                # was correct, so Resolve displayed a longer clip.
                self.assertAlmostEqual(
                    float(timing["duration"]), frame_duration, delta=0.002
                )


if __name__ == "__main__":
    unittest.main()

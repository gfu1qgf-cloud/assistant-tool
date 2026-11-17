"""Preset values must reach detection, cuts, both UIs and per-run submission."""
import copy
import os
import math
from pathlib import Path
import shutil
import sys
import tempfile
import wave
from array import array
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtWidgets
from app_plugins.builtin.smart_video_editor import engine
from app_plugins.builtin.smart_video_editor.breath_presets import BREATH_PRESETS, get_preset, apply_preset
from app_plugins.builtin.smart_video_editor.breath_editor import BreathCutSourceDialog
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage
from app_plugins.builtin.smart_video_editor.timeline_review import SmartVideoTimelineReview


class BreathPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def choose(self, picker, key):
        picker.choice.setCurrentIndex(picker.choice.findData(key))
        picker.apply_button.click()

    def panel(self, callback=None, workflow="breath_cut"):
        settings = engine.normalize_smart_video_editor_settings({"export_mode": "video_only"})
        panel = SmartVideoTimelineReview({"workflow": workflow, "settings": settings, "tasks": [], "summary": {}},
                                        save_breath_defaults=callback)
        self.addCleanup(panel.close_player)
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.close)
        return panel

    def page(self):
        page = SmartVideoEditorSettingsPage()
        self.addCleanup(page.widget.deleteLater)
        self.addCleanup(page.widget.close)
        return page

    def test_three_profiles_match_requested_values_and_valid_internal_padding(self):
        expected = (("gentle", 850, 220, .60), ("clear", 900, 250, .60), ("brisk", 700, 170, .68))
        for key, pause, padding, sensitivity in expected:
            values = engine.normalize_smart_video_editor_settings(get_preset(key).values())
            self.assertEqual(values["min_silence_ms"], pause)
            self.assertEqual(values["pause_threshold_ms"], pause)
            self.assertEqual(values["lead_padding_ms"], padding)
            self.assertEqual(values["tail_padding_ms"], padding)
            self.assertEqual(values["pause_keep_before_ms"], padding)
            self.assertEqual(values["pause_keep_after_ms"], padding)
            self.assertEqual(values["retained_pause_ms"], 2*padding)
            self.assertLess(2*padding, pause)
            self.assertEqual(values["vad_threshold"], sensitivity)
            self.assertLess(values["vad_neg_threshold"], sensitivity)
            self.assertTrue(values["compress_internal_pauses"])

    def test_apply_only_changes_breath_parameters_and_does_not_mutate_caller(self):
        source = engine.normalize_smart_video_editor_settings({"export_mode": "video_only", "whisper_model_size": "large-v3",
                 "seam_cover_enabled": True, "seam_cover_source": "cover.mp4", "postprocess_enabled": True,
                 "postprocess_workflow": "custom", "srt_max_chars_per_block": 77})
        original = copy.deepcopy(source)
        result = apply_preset(source, "gentle")
        self.assertEqual(source, original)
        changed = {key for key in source if source[key] != result[key]}
        self.assertLessEqual(changed, set(engine.BREATH_SETTINGS_KEYS))
        for key in ("export_mode", "whisper_model_size", "seam_cover_source", "postprocess_workflow", "srt_max_chars_per_block"):
            self.assertEqual(result[key], original[key])
        result["pause_threshold_ms"] = 9999
        self.assertEqual(get_preset("gentle").values()["pause_threshold_ms"], 850)
        with self.assertRaises(ValueError):
            get_preset("unknown")

    def test_actual_cut_plan_uses_threshold_and_preserves_each_side_of_the_gap(self):
        for preset in BREATH_PRESETS:
            options = engine.normalize_smart_video_editor_settings(preset.values())
            quiet = [[2.0, 2.8], [4.0, 5.2]]
            _start, _end, removals, _kept = engine.breath_cut_plan(8, quiet, options)
            padding = preset.padding_ms/1000
            self.assertEqual(removals[-1], [round(4+padding, 3), round(5.2-padding, 3)])
            self.assertEqual(len(removals), 2 if preset.id == "brisk" else 1)
            start, end, _removals, _kept = engine.breath_cut_plan(5, [[0, 1], [4, 5]], options)
            self.assertAlmostEqual(start, 1-padding)
            self.assertAlmostEqual(end, 4+padding)

    def test_detector_receives_preset_sensitivity_and_minimum_silence(self):
        import numpy as np
        audio, vad = ModuleType("faster_whisper.audio"), ModuleType("faster_whisper.vad")
        audio.decode_audio = Mock(return_value=np.zeros(48000, dtype=np.float32))
        vad.VadOptions = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        vad.get_speech_timestamps = Mock(return_value=[{"start": 16000, "end": 32000}])
        with patch.dict(sys.modules, {"faster_whisper.audio": audio, "faster_whisper.vad": vad}):
            for preset in BREATH_PRESETS:
                gaps, error = engine.detect_voice_absence_ranges("synthetic.mp4", 3, preset.values())
                self.assertFalse(error)
                self.assertEqual(gaps, [[0.0, 1.0], [2.0, 3.0]])
                options = vad.VadOptions.call_args.kwargs
                self.assertEqual(options["threshold"], preset.sensitivity)
                self.assertEqual(options["min_silence_duration_ms"], preset.pause_ms)

    def test_settings_are_unchanged_until_apply_and_roundtrip_all_profiles(self):
        page = self.page()
        original = page.settings()
        for preset in BREATH_PRESETS:
            page.breath_preset_picker.choice.setCurrentIndex(page.breath_preset_picker.choice.findData(preset.id))
            if preset.id == "gentle":
                self.assertEqual(page.settings(), original, "choosing a name alone must not overwrite custom controls")
            page.breath_preset_picker.apply_button.click()
            values = page.settings()
            for key, expected in preset.values().items():
                self.assertEqual(values[key], expected, key)
            page.validate()
            config = page.update_config({"other": "preserve"})
            restored = self.page()
            restored.load_config(config)
            self.assertEqual(restored.settings(), values)
            self.assertEqual(config["other"], "preserve")

    def test_timeline_presets_are_drafts_until_detection_or_default_save(self):
        for workflow in ("breath_cut", ""):
            panel = self.panel(workflow=workflow)
            original = copy.deepcopy(panel.bundle)
            for preset in BREATH_PRESETS:
                self.choose(panel.breath_preset_picker, preset.id)
                self.assertEqual(panel._breath_settings_from_controls(),
                    {key: value for key, value in dict(engine.normalize_smart_video_editor_settings(), **preset.values()).items()
                     if key in engine.BREATH_SETTINGS_KEYS})
                self.assertEqual(panel.bundle, original)
                self.assertIn("检测并应用", panel.breath_status_label.text())

    def test_timeline_can_fine_tune_and_save_edge_padding_with_the_preset(self):
        callback = Mock(return_value=(True, "saved"))
        panel = self.panel(callback)
        self.choose(panel.breath_preset_picker, "brisk")
        panel.lead_padding_spinbox.setValue(180)
        panel.pause_keep_after_spinbox.setValue(150)
        panel.save_breath_defaults_button.click()
        values = callback.call_args.args[0]
        self.assertEqual(values["lead_padding_ms"], 180)
        self.assertEqual(values["tail_padding_ms"], 170)
        self.assertEqual(values["retained_pause_ms"], 320)

    def test_presets_cannot_change_controls_while_detection_or_refresh_is_running(self):
        panel = self.panel()
        before = panel._breath_settings_from_controls()
        for attribute in ("_breath_reanalysis_running", "_task_refresh_running"):
            setattr(panel, attribute, True)
            panel._update_breath_control_state()
            self.assertFalse(panel.breath_preset_picker.isEnabled())
            panel._apply_breath_preset("brisk")
            self.assertEqual(panel._breath_settings_from_controls(), before)
            setattr(panel, attribute, False)
        panel._update_breath_control_state()
        self.assertTrue(panel.breath_preset_picker.isEnabled())

    def test_source_selection_factory_passes_per_run_preset_only_when_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/"clip.mp4"
            source.write_bytes(b"selection fixture")
            def choose(dialog):
                self.choose(dialog.breath_preset_picker, "gentle")
                picker = dialog.breath_preset_picker
                picker.choice.setCurrentIndex(picker.choice.findData("brisk"))
                self.assertIn("待应用", picker.description.text())
                dialog.accept()
                return dialog.result()
            output = {}
            with patch.object(BreathCutSourceDialog, "exec", choose):
                selected = BreathCutSourceDialog.get_sources([source], settings_out=output)
            self.assertEqual(selected, [str(source)])
            self.assertEqual(output, get_preset("gentle").values())
            output = {"unchanged": True}
            def cancel(dialog):
                self.choose(dialog.breath_preset_picker, "brisk")
                return QtWidgets.QDialog.DialogCode.Rejected
            with patch.object(BreathCutSourceDialog, "exec", cancel):
                self.assertIsNone(BreathCutSourceDialog.get_sources([source], settings_out=output))
            self.assertEqual(output, {"unchanged": True})

    def test_plugin_submits_explicit_preset_over_saved_internal_pause_off_without_saving_config(self):
        plugin = SmartVideoEditorPlugin()
        saved = {engine.SMART_VIDEO_EDITOR_CONFIG_KEY: engine.normalize_smart_video_editor_settings({
            "compress_internal_pauses": False, "export_mode": "video_only"})}
        plugin.context = SimpleNamespace(parent_widget=None, load_config=Mock(return_value=saved), log=Mock(), save_config=Mock())
        baseline = copy.deepcopy(saved)
        def choose(*args, settings_out, **kwargs):
            settings_out.update(get_preset("clear").values())
            return ["example.mp4"]
        with patch.object(plugin, "_effective_settings", return_value=saved[engine.SMART_VIDEO_EDITOR_CONFIG_KEY]), \
             patch.object(BreathCutSourceDialog, "get_sources", side_effect=choose), \
             patch.object(plugin, "_start_worker") as start:
            plugin.start_breath_editor()
        submitted = start.call_args.args[1]
        self.assertEqual(start.call_args.args[0], "breath_analyze")
        for key, value in get_preset("clear").values().items():
            self.assertEqual(submitted[key], value)
        self.assertEqual(saved, baseline)
        plugin.context.save_config.assert_not_called()

    def test_manual_settings_do_not_require_using_a_preset(self):
        panel = self.panel()
        self.assertFalse(panel.breath_preset_picker.apply_button.isEnabled())
        panel.pause_threshold_spinbox.setValue(1350)
        panel.lead_padding_spinbox.setValue(140)
        self.assertEqual(panel._breath_settings_from_controls()["pause_threshold_ms"], 1350)
        self.assertEqual(panel._breath_settings_from_controls()["lead_padding_ms"], 140)

    def test_mode_hint_explains_when_db_cannot_independently_add_a_gap(self):
        panel = self.panel(workflow="")
        self.assertIn("分贝只修边", panel.breath_mode_hint.text())
        self.assertIn("保护已识别单词", panel.breath_mode_hint.text())
        mode = panel.breath_detection_mode_combo
        mode.setCurrentIndex(mode.findData("voice_only"))
        panel._update_breath_control_state()
        self.assertIn("分贝阈值不参与", panel.breath_mode_hint.text())
        self.assertFalse(panel.silence_db_spinbox.isEnabled())


class BreathBoundaryRegressionTests(unittest.TestCase):
    def settings(self):
        return engine.normalize_smart_video_editor_settings()

    def test_quiet_interval_crossing_first_word_still_allows_safe_head_cut(self):
        start, end, decisions, warnings = engine.refine_trim_boundaries(
            .75, 9.19, 10.027, [[0, 1.126]], self.settings())
        self.assertAlmostEqual(start, .63)
        self.assertLess(start, .75)
        self.assertTrue(any(item["kind"] == "head_cut" for item in decisions))

    def test_wider_quiet_tail_does_not_make_the_previous_tail_cut_disappear(self):
        lower = engine.refine_trim_boundaries(.75, 9.19, 10.027, [[9.239, 10.027]], self.settings())
        higher = engine.refine_trim_boundaries(.75, 9.19, 10.027, [[9.18, 10.027]], self.settings())
        self.assertAlmostEqual(lower[1], 9.459)
        self.assertAlmostEqual(higher[1], 9.41)
        self.assertLessEqual(higher[1], lower[1])
        self.assertGreater(higher[1], 9.19, "the final matched word must be retained")

    def test_full_quiet_interval_clips_only_outside_the_matched_speech(self):
        start, end, _decisions, _warnings = engine.refine_trim_boundaries(
            1.5, 8., 10., [[0., 10.]], self.settings())
        self.assertAlmostEqual(start, 1.38)
        self.assertAlmostEqual(end, 8.22)
        self.assertLessEqual(start, 1.5)
        self.assertGreaterEqual(end, 8.)

    def test_remote_quiet_or_failed_detection_does_not_override_boundary_protection(self):
        for ranges, error in (([[0., .5], [11., 12.]], ""), ([[0., 12.]], "detector failed")):
            result = engine.refine_trim_boundaries(5., 8., 12., ranges, self.settings(), error)
            self.assertEqual(result[:2], (0., 12.))

    def test_real_ffmpeg_threshold_increase_can_cross_word_without_losing_tail_cut(self):
        ffmpeg = os.environ.get("NODE_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/"quiet-tail.wav"
            rate = 16000
            samples = array("h")
            for index in range(rate*10):
                t = index/rate
                amplitude = .001 if t < 1 or t >= 9.2 else (.05 if t >= 8.9 else .6)
                samples.append(round(32767*amplitude*math.sin(2*math.pi*440*t)))
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(samples.tobytes())
            tails = []
            for threshold in (-35, -10):
                settings = dict(self.settings(), silence_threshold_db=threshold)
                ranges, error = engine.detect_silence_ranges(source, ffmpeg, 10., settings)
                self.assertFalse(error)
                tails.append(engine.refine_trim_boundaries(1.1, 9., 10., ranges, settings)[1])
            self.assertAlmostEqual(tails[0], 9.42, delta=.01)
            self.assertAlmostEqual(tails[1], 9.22, delta=.01)
            self.assertLess(tails[1], tails[0])

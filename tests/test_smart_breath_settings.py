import copy
import json
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtWidgets
from app_plugins.builtin.smart_video_editor.engine import (
    SMART_VIDEO_EDITOR_CONFIG_KEY as KEY,
    normalize_smart_video_editor_settings as normalize,
)
from app_plugins.builtin.smart_video_editor.settings import SmartVideoEditorSettingsPage
from app_plugins.builtin.smart_video_editor.timeline_review import SmartVideoTimelineReview
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog
from app_plugins.builtin.smart_video_editor.breath_editor import BreathCutReviewDialog


class BreathDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_switch_roundtrip_and_legacy_mode(self):
        self.assertFalse(normalize()["compress_internal_pauses"])
        for mode in (None, "off", "experimental", "enabled"):
            for enabled in (False, True):
                with self.subTest(mode=mode, enabled=enabled):
                    settings = normalize({"compress_internal_pauses": enabled,
                                          "internal_pause_mode": mode})
                    self.assertEqual(settings["compress_internal_pauses"], enabled)
                    self.assertEqual(settings["internal_pause_mode"], "enabled" if enabled else "off")
                    self.assertEqual(normalize(json.loads(json.dumps(settings))), settings)

    def test_global_page_saves_and_restores(self):
        page = SmartVideoEditorSettingsPage()
        self.addCleanup(page.widget.close)
        page.load_config({})
        page.compress_pauses_checkbox.setChecked(True)
        page.pause_threshold_spinbox.setValue(1500)
        page.pause_keep_before_spinbox.setValue(100)
        page.pause_keep_after_spinbox.setValue(250)
        config = page.update_config({"unrelated": "kept"})
        self.assertEqual(config["unrelated"], "kept")
        restored = SmartVideoEditorSettingsPage()
        self.addCleanup(restored.widget.close)
        restored.load_config(json.loads(json.dumps(config)))
        self.assertTrue(restored.compress_pauses_checkbox.isChecked())
        self.assertEqual(restored.pause_threshold_spinbox.value(), 1500)
        self.assertEqual(restored.pause_keep_before_spinbox.value(), 100)
        self.assertEqual(restored.pause_keep_after_spinbox.value(), 250)
        self.assertNotIn("实验", restored.compress_pauses_checkbox.text())

    def make_panel(self, callback=None):
        bundle = {"settings": normalize(), "tasks": [], "summary": {}}
        panel = SmartVideoTimelineReview(bundle, save_breath_defaults=callback)
        self.addCleanup(panel.close_player)
        self.addCleanup(panel.close)
        return panel

    def test_timeline_saves_without_reanalysis_or_bundle_mutation(self):
        callback = Mock(return_value=(True, "已保存"))
        panel = self.make_panel(callback)
        original = copy.deepcopy(panel.bundle)
        panel.compress_pauses_checkbox.setChecked(True)
        panel.pause_threshold_spinbox.setValue(1400)
        panel.pause_keep_before_spinbox.setValue(100)
        panel.pause_keep_after_spinbox.setValue(200)
        with patch.object(panel, "_apply_breath_settings") as reanalysis:
            panel.save_breath_defaults_button.click()
            reanalysis.assert_not_called()
        values = callback.call_args.args[0]
        self.assertEqual(values["retained_pause_ms"], 300)
        self.assertEqual(values["internal_pause_mode"], "enabled")
        self.assertEqual(panel.bundle, original)
        self.assertEqual(panel.breath_status_label.text(), "已保存")

    def test_invalid_pause_padding_cannot_save(self):
        callback = Mock(return_value=(True, "saved"))
        panel = self.make_panel(callback)
        panel.compress_pauses_checkbox.setChecked(True)
        panel.pause_threshold_spinbox.setValue(300)
        panel.pause_keep_before_spinbox.setValue(200)
        panel.pause_keep_after_spinbox.setValue(200)
        panel.save_breath_defaults_button.click()
        callback.assert_not_called()
        self.assertIn("保留量", panel.breath_status_label.text())

    def test_invalid_voice_threshold_cannot_save(self):
        callback = Mock(return_value=(True, "saved"))
        panel = self.make_panel(callback)
        panel.vad_neg_threshold_spinbox.setValue(0.90)
        panel.save_breath_defaults_button.click()
        callback.assert_not_called()
        self.assertIn("结束阈值", panel.breath_status_label.text())

    def test_missing_callback_and_running_detection_disable_save(self):
        panel = self.make_panel()
        self.assertFalse(panel.save_breath_defaults_button.isEnabled())
        panel._save_breath_defaults_callback = Mock()
        panel._breath_reanalysis_running = True
        panel._update_breath_control_state()
        self.assertFalse(panel.save_breath_defaults_button.isEnabled())

    def test_callback_exception_is_visible(self):
        panel = self.make_panel(Mock(side_effect=RuntimeError("test save failure")))
        panel.save_breath_defaults_button.click()
        self.assertIn("保存失败", panel.breath_status_label.text())

    def test_both_review_dialogs_forward_callback(self):
        callback = Mock(return_value=(True, "saved"))
        for dialog_type, workflow in ((SmartVideoReviewDialog, ""),
                                      (BreathCutReviewDialog, "breath_cut")):
            dialog = dialog_type({"tasks": [], "settings": normalize(),
                                  "summary": {}, "workflow": workflow},
                                 save_breath_defaults=callback)
            try:
                self.assertIs(dialog.timeline_review._save_breath_defaults_callback, callback)
                self.assertTrue(dialog.timeline_review.save_breath_defaults_button.isEnabled())
            finally:
                dialog.timeline_review.close_player()
                dialog.close()

    def make_plugin(self, save_result=True):
        plugin = SmartVideoEditorPlugin()
        plugin.settings = normalize({"whisper_model_size": "base"})
        plugin._settings_dirty = False
        plugin.pending_reviews = []
        disk = {KEY: normalize({"whisper_model_size": "large-v3", "export_mode": "segments",
                                "srt_max_chars_per_block": 77}),
                "private_other_config": {"untouched": True}}
        context = Mock()
        context.load_config.side_effect = lambda: copy.deepcopy(disk)
        def save():
            if isinstance(save_result, Exception):
                raise save_result
            if save_result:
                disk.update(plugin.update_config(copy.deepcopy(disk)))
            return save_result
        context.save_config.side_effect = save
        plugin.context = context
        return plugin, context, disk

    def test_plugin_merges_latest_settings_and_only_breath_keys(self):
        plugin, context, disk = self.make_plugin()
        saved, _ = plugin._save_timeline_breath_defaults({
            "compress_internal_pauses": True, "pause_keep_before_ms": 100,
            "pause_keep_after_ms": 250, "whisper_model_size": "tiny",
            "export_mode": "joined", "private_other_config": "do not save"})
        self.assertTrue(saved)
        self.assertEqual(disk[KEY]["whisper_model_size"], "large-v3")
        self.assertEqual(disk[KEY]["export_mode"], "segments")
        self.assertEqual(disk[KEY]["srt_max_chars_per_block"], 77)
        self.assertEqual(disk[KEY]["retained_pause_ms"], 350)
        self.assertTrue(disk[KEY]["compress_internal_pauses"])
        self.assertEqual(disk["private_other_config"], {"untouched": True})
        context.set_subtitle_generation_settings.assert_not_called()
        context.whisper_model.assert_not_called()
        self.assertFalse(plugin._settings_dirty)

    def test_failed_save_restores_runtime_and_does_not_autosave(self):
        for result in (False, RuntimeError("disk error")):
            plugin, context, disk = self.make_plugin(result)
            original = copy.deepcopy(plugin.settings)
            if isinstance(result, Exception):
                with self.assertRaises(RuntimeError):
                    plugin._save_timeline_breath_defaults({"compress_internal_pauses": True})
            else:
                self.assertFalse(plugin._save_timeline_breath_defaults({"compress_internal_pauses": True})[0])
            self.assertEqual(plugin.settings, original)
            self.assertFalse(plugin._settings_dirty)
            self.assertFalse(plugin.update_config(copy.deepcopy(disk))[KEY]["compress_internal_pauses"])

    def test_standalone_breath_analysis_honors_saved_switch(self):
        for enabled in (False, True):
            plugin, context, disk = self.make_plugin()
            context.parent_widget = None
            disk[KEY] = normalize({"compress_internal_pauses": enabled,
                                   "pause_keep_before_ms": 100,
                                   "pause_keep_after_ms": 250})
            with patch.object(plugin, "_effective_settings", return_value=disk[KEY]), \
                    patch("app_plugins.builtin.smart_video_editor.plugin.BreathCutSourceDialog.get_sources",
                          return_value=["test.mp4"]), \
                    patch.object(plugin, "_start_worker") as start:
                plugin.start_breath_editor()
            settings = start.call_args.args[1]
            self.assertEqual(settings["compress_internal_pauses"], enabled)
            self.assertEqual(settings["retained_pause_ms"], 350)


if __name__ == "__main__":
    unittest.main()

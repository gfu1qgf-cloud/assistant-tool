import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app_plugins.builtin.video_prompt_assistant.cases import PromptCaseStore
from app_plugins.builtin.video_prompt_assistant.templates import build_suggestions
from app_plugins.builtin.video_prompt_assistant.vision import parse_scene
from app_plugins.builtin.smart_video_editor.engine import summarize_smart_video_export_blockers
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.video_prompt_assistant.ui import VideoPromptDialog


class _FakeContext:
    def load_config(self):
        return {}

    def log(self, _message):
        pass


class PromptAssistantCoreTests(unittest.TestCase):
    def test_poster_suggestions_keep_text_and_no_camera_orbit(self):
        proposals = build_suggestions("poster", "画面中的祈祷手", True)
        self.assertEqual(len(proposals), 3)
        for _, prompt in proposals:
            self.assertIn("文字部分不要变", prompt)
            self.assertNotIn("环绕", prompt)

    def test_person_suggestions_keep_plain_spoken_silent_rule(self):
        proposals = build_suggestions("person", "玛丽亚", expression="tears")
        self.assertEqual(len(proposals), 3)
        self.assertTrue(all("全程不说话" in prompt for _, prompt in proposals))
        self.assertTrue(all("默默流泪" in prompt for _, prompt in proposals))
        self.assertNotIn("向前行走", proposals[1][1])
        self.assertIn("向前行走", build_suggestions("person", "玛丽亚", full_body=True)[1][1])

    def test_untrusted_vision_scene_falls_back(self):
        result = parse_scene('{"scene":"alien","subject":"玛丽亚","has_text":true}')
        self.assertEqual(result["scene"], "unknown")
        self.assertTrue(result["has_text"])

    def test_case_crud_and_future_tags_survive_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            store = PromptCaseStore(path)
            original = store.upsert("慢慢前行", "镜头始终跟随。", "镜头剧烈晃动。", "成功")
            self.assertEqual(len(store.load()), 1)
            self.assertEqual(original["tags"], [])
            updated = store.upsert("慢慢前行", "镜头保持固定距离跟随。", case_id=original["id"])
            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["created_at"], original["created_at"])
            self.assertEqual(len(store.load()), 1)
            self.assertTrue(store.delete(original["id"]))
            self.assertEqual(store.load(), [])

    def test_bad_case_file_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                PromptCaseStore(path).upsert("标题", "成功文案")
            self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_export_warning_names_actual_issue_types(self):
        self.assertEqual(
            summarize_smart_video_export_blockers([{"kind": "extra_clip"}]),
            "疑似多余片段 1 个",
        )
        self.assertEqual(
            summarize_smart_video_export_blockers([{}, {"kind": "extra_clip"}]),
            "缺段 1 个、疑似多余片段 1 个",
        )

    def test_dialog_load_image_and_edit_case_without_cloud(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            image_path = str(Path(folder) / "sample.png")
            self.assertTrue(QtGui.QImage(60, 90, QtGui.QImage.Format.Format_RGB32).save(image_path))
            dialog = VideoPromptDialog(_FakeContext(), store=PromptCaseStore(Path(folder) / "cases.json"))
            dialog.load_image(image_path)
            self.assertEqual(dialog.suggestion_list.count(), 3)
            dialog.save_from_suggestion()
            dialog.case_title.setText("我改好的一版")
            dialog.case_prompt.setPlainText("镜头始终跟随人物。")
            dialog.case_failed.setPlainText("镜头乱动。")
            dialog.save_case()
            self.assertEqual(dialog.store.load()[0]["title"], "我改好的一版")
            dialog.close()


if __name__ == "__main__":
    unittest.main()

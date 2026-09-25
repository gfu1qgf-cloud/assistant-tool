import tempfile
import unittest
from pathlib import Path

from app_plugins.builtin.smart_video_editor.renaming import (
    apply_video_rename_plan,
    build_video_rename_plan,
    update_bundle_video_paths,
)
from app_plugins.builtin.smart_video_editor.engine import _explicit_sequence_number


class SmartVideoRenamingTests(unittest.TestCase):
    def test_rename_uses_reviewed_order_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.mp4"
            second = root / "b.mp4"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            bundle = {"tasks": [{"clips": [
                {"source": str(first), "file_name": first.name, "export_order": 2},
                {"source": str(second), "file_name": second.name, "export_order": 1},
            ], "filename_order": [first.name, second.name]}]}
            plan = build_video_rename_plan(bundle)
            apply_video_rename_plan(plan)
            update_bundle_video_paths(bundle, plan)
            self.assertEqual(bundle["tasks"][0]["clips"][0]["file_name"], "[02] a.mp4")
            self.assertEqual(bundle["tasks"][0]["clips"][1]["file_name"], "[01] b.mp4")
            self.assertEqual(bundle["tasks"][0]["filename_order"], ["[02] a.mp4", "[01] b.mp4"])
            self.assertEqual(_explicit_sequence_number(root / "[02] a.mp4"), 2)
            self.assertEqual(build_video_rename_plan(bundle), [])

            bundle["tasks"][0]["clips"][0]["export_order"] = 1
            bundle["tasks"][0]["clips"][1]["export_order"] = 2
            swapped = build_video_rename_plan(bundle)
            apply_video_rename_plan(swapped)
            update_bundle_video_paths(bundle, swapped)
            self.assertEqual((root / "[01] a.mp4").read_bytes(), b"a")
            self.assertEqual((root / "[02] b.mp4").read_bytes(), b"b")

    def test_existing_target_prevents_any_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.mp4"
            source.write_bytes(b"a")
            (root / "[01] a.mp4").write_bytes(b"other")
            bundle = {"tasks": [{"clips": [{
                "source": str(source), "export_order": 1,
            }]}]}
            with self.assertRaisesRegex(ValueError, "目标文件已存在"):
                build_video_rename_plan(bundle)
            self.assertEqual(source.read_bytes(), b"a")


if __name__ == "__main__":
    unittest.main()

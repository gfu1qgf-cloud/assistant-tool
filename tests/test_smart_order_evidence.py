"""Order uncertainty stays local; filename numbers cannot invent script matches."""
from pathlib import Path
import tempfile
import unittest

from app_plugins.builtin.smart_video_editor import engine
from test_smart_video_editor import _DictionaryWhisperModel


class LocalOrderEvidenceTests(unittest.TestCase):
    def analyze(self, texts, script):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sources = []
        mapping = {}
        for index, text in enumerate(texts, 1):
            source = root / f"[{index:02}] speech.mp4"
            source.write_bytes(f"synthetic-{index}".encode())
            sources.append(str(source))
            mapping[source.name] = text
        return engine.analyze_smart_video_jobs([{
            "task_id": "order-test", "task_dir": str(root), "script": script,
            "language": "en", "sources": sources,
        }], _DictionaryWhisperModel(mapping), {
            "silence_detection_enabled": False, "voice_detection_enabled": False,
        })

    def test_unmatched_numbered_tail_does_not_turn_verified_clips_red(self):
        spoken = ["alpha bravo charlie delta", "echo foxtrot golf hotel", "india juliet kilo lima"]
        unrelated = ["cats sleep under moonlight", "rivers flow towards oceans"]
        result = self.analyze(spoken + unrelated, "\n".join(spoken))
        task = result["tasks"][0]
        self.assertEqual(task["ordering_mode"], "filename_fallback")
        for clip in task["clips"][:3]:
            self.assertTrue(clip["content_order_reliable"])
            self.assertNotEqual(clip["status"], "pink")
            self.assertFalse(any(issue["kind"] == "filename_order_fallback" for issue in clip["issues"]))
        for clip in task["clips"][3:]:
            self.assertEqual(clip["script_word_start"], -1)
            self.assertEqual(clip["expected_text"], "")
            self.assertEqual(clip["status"], "pink")
            self.assertTrue(any(issue["kind"] == "script_position_unmatched" for issue in clip["issues"]))
            self.assertFalse(any(issue["kind"] == "authoritative_script_range_inferred" for issue in clip["issues"]))
        blockers = engine.smart_video_export_blockers(result)
        self.assertEqual([item["clip_index"] for item in blockers], [3, 4])
        for clip in task["clips"][3:]:
            clip["included"] = False
        self.assertEqual(engine.smart_video_export_blockers(result), [])
        self.assertEqual(" ".join(clip["expected_text"] for clip in task["clips"][:3]), " ".join(spoken))

    def test_unmatched_middle_is_not_assigned_a_word_between_good_neighbors(self):
        script = "alpha bravo charlie delta\necho foxtrot golf hotel"
        result = self.analyze(["alpha bravo charlie delta", "cats sleep under moonlight", "echo foxtrot golf hotel"], script)
        clips = result["tasks"][0]["clips"]
        self.assertEqual(clips[1]["script_word_start"], -1)
        self.assertEqual(clips[1]["expected_text"], "")
        self.assertEqual((clips[0]["script_word_start"], clips[0]["script_word_end"]), (0, 3))
        self.assertEqual((clips[2]["script_word_start"], clips[2]["script_word_end"]), (4, 7))

    def test_overlap_warning_is_limited_to_involved_clips(self):
        evidence = [{"reliable": True, "start": 0, "end": 9},
                    {"reliable": True, "start": 1, "end": 10},
                    {"reliable": True, "start": 20, "end": 30},
                    {"reliable": False, "start": -1, "end": -1}]
        self.assertEqual(engine._content_order_review_indexes(evidence), {0, 1, 3})
        self.assertFalse(engine._content_order_is_complete(evidence))
        self.assertTrue(engine._content_order_is_complete([evidence[0], evidence[2]]))

    def test_warning_does_not_promote_uncertainty_to_severe_or_hide_real_error(self):
        clips = [{"status": "green"}, {"status": "pink", "issue_reason": "speech mismatch"}]
        engine._add_order_warning(clips, "语音顺序证据不足", "review this position", "filename_order_fallback")
        self.assertEqual(clips[0]["status"], "orange")
        self.assertEqual(clips[1]["status"], "pink")
        self.assertEqual(clips[1]["issue_reason"], "speech mismatch")
        self.assertTrue(all(clip["issues"][0]["severity"] == "orange" for clip in clips))

    def test_nested_overlaps_mark_all_involved_clips_without_marking_clear_tail(self):
        evidence = [{"reliable": True, "start": 0, "end": 100},
                    {"reliable": True, "start": 5, "end": 10},
                    {"reliable": True, "start": 50, "end": 60},
                    {"reliable": True, "start": 110, "end": 120}]
        self.assertEqual(engine._content_order_review_indexes(evidence), {0, 1, 2})

    def test_confident_filename_content_conflict_still_requires_review(self):
        clips = [{"status": "green"}, {"status": "orange"}]
        engine._add_order_warning(clips, "编号与语音顺序冲突", "conflicting order", "filename_content_order_conflict")
        self.assertTrue(all(clip["status"] == "pink" for clip in clips))

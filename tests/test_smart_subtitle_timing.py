"""Display recognition evidence separately from intentional caption hold time."""
import copy
import unittest
from app_plugins.builtin.smart_video_editor.timeline_model import build_task_review_timeline, build_subtitle_tracks


class SubtitleEvidenceTimingTests(unittest.TestCase):
    def setUp(self):
        self.task = {"clips": [self.clip("one.mp4", 8.53, 9.19, 0, "end."),
                               self.clip("two.mp4", 2.86, 3.68, 1, "next.")]}
        self.segments, _ = build_task_review_timeline(self.task)

    def clip(self, source, start, end, index, text):
        return {"source": source, "file_name": source, "included": True, "export_order": index+1,
                "original_duration": 10.027, "trim_start": 0., "trim_end": 10.027,
                "kept_ranges": [[0., 10.027]], "words": [{"text": text, "start": start, "end": end}],
                "word_timeline": [{"raw": text, "start": start, "end": end, "script_word_index": index, "anchor": True}]}

    def tracks(self, gap):
        return build_subtitle_tracks(self.task, self.segments, settings={"srt_block_gap_ms": gap})

    def test_zero_caption_gap_does_not_paint_model_evidence_across_silence(self):
        original = copy.deepcopy(self.task)
        model, preview = self.tracks(0)
        self.assertAlmostEqual(model[0]["timeline_end"], 9.19)
        self.assertAlmostEqual(preview[0]["timeline_end"], 12.887)
        self.assertLess(model[0]["timeline_end"], model[1]["timeline_start"])
        self.assertEqual(self.task, original)

    def test_model_times_are_identical_for_original_zero_and_positive_gap(self):
        timing = []
        for gap in (-1, 0, 200, 500):
            model, _ = self.tracks(gap)
            timing.append([(block["timeline_start"], block["timeline_end"]) for block in model])
        self.assertTrue(all(value == timing[0] for value in timing))

    def test_keep_original_gap_preserves_silence_on_both_tracks(self):
        model, preview = self.tracks(-1)
        self.assertEqual(model[0]["timeline_end"], preview[0]["timeline_end"])
        self.assertLess(preview[0]["timeline_end"], preview[1]["timeline_start"])

    def test_explicit_positive_gap_remains_available_on_caption_preview(self):
        model, preview = self.tracks(200)
        self.assertAlmostEqual(preview[1]["timeline_start"]-preview[0]["timeline_end"], .2)
        self.assertAlmostEqual(model[0]["timeline_end"], 9.19)

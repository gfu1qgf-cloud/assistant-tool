import unittest
from array import array

from app_plugins.builtin.smart_video_editor.timeline_model import (
    build_issue_markers,
    build_subtitle_tracks,
    build_task_review_timeline,
    build_task_timeline,
    output_time_for_source,
    source_location_for_output,
)
from app_plugins.builtin.smart_video_editor.waveform import (
    parse_silence_output,
    pcm_waveform,
)


class SmartVideoTimelineModelTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            "clips": [
                {
                    "source": "later.mp4",
                    "file_name": "later.mp4",
                    "export_order": 2,
                    "source_index": 0,
                    "trim_start": 1.0,
                    "trim_end": 4.0,
                    "original_duration": 5.0,
                    "kept_ranges": [[1.0, 2.0], [3.0, 4.0]],
                    "included": True,
                    "issues": [{
                        "severity": "orange",
                        "start": 3.2,
                        "end": 3.5,
                        "title": "单词不一致",
                    }],
                },
                {
                    "source": "first.mp4",
                    "file_name": "first.mp4",
                    "export_order": 1,
                    "source_index": 1,
                    "trim_start": 0.0,
                    "trim_end": 2.0,
                    "original_duration": 2.0,
                    "kept_ranges": [[0.0, 2.0]],
                    "included": True,
                },
                {
                    "source": "excluded.mp4",
                    "file_name": "excluded.mp4",
                    "export_order": 0,
                    "trim_start": 0.0,
                    "trim_end": 9.0,
                    "included": False,
                },
            ],
            "missing_blocks": [{
                "previous_clip": "first.mp4",
                "following_clip": "later.mp4",
                "text": "缺少的一段",
                "issue_reason": "没有视频覆盖",
            }],
        }

    def test_timeline_uses_export_order_and_kept_ranges(self):
        segments, duration = build_task_timeline(self.task)

        self.assertEqual([value["clip_index"] for value in segments], [1, 0, 0])
        self.assertEqual(
            [(value["source_start"], value["source_end"]) for value in segments],
            [(0.0, 2.0), (1.0, 2.0), (3.0, 4.0)],
        )
        self.assertAlmostEqual(duration, 4.0)

    def test_output_and_source_time_map_across_removed_pause(self):
        segments, _duration = build_task_timeline(self.task)

        segment, source_time = source_location_for_output(segments, 3.25)
        self.assertEqual(segment["file_name"], "later.mp4")
        self.assertAlmostEqual(source_time, 3.25)
        self.assertAlmostEqual(output_time_for_source(segments, 0, 3.2), 3.2)
        self.assertAlmostEqual(output_time_for_source(segments, 0, 2.5), 3.0)

    def test_issue_and_missing_markers_use_final_timeline_positions(self):
        segments, _duration = build_task_timeline(self.task)
        markers = build_issue_markers(self.task, segments)

        self.assertEqual([value["kind"] for value in markers], ["missing", "issue"])
        self.assertAlmostEqual(markers[0]["time"], 2.0)
        self.assertAlmostEqual(markers[1]["time"], 3.2)
        self.assertEqual(markers[0]["expected"], "缺少的一段")

    def test_review_timeline_keeps_deleted_breaths_as_gray_spans(self):
        task = {
            "clips": [{
                "source": "clip.mp4",
                "file_name": "clip.mp4",
                "original_duration": 5.0,
                "trim_start": 1.0,
                "trim_end": 4.0,
                "pause_removals": [[2.0, 3.0]],
                "included": True,
            }],
        }
        segments, duration = build_task_review_timeline(task)

        self.assertAlmostEqual(duration, 5.0)
        self.assertEqual(
            [value["is_removed"] for value in segments],
            [True, False, True, False, True],
        )
        self.assertEqual(
            [value["remove_reason"] for value in segments if value["is_removed"]],
            ["片头气口", "句内气口", "片尾气口"],
        )

    def test_dual_subtitle_tracks_mark_word_difference_and_offer_advice(self):
        task = {
            "clips": [{
                "source": "clip.mp4",
                "file_name": "clip.mp4",
                "original_duration": 2.0,
                "trim_start": 0.0,
                "trim_end": 2.0,
                "included": True,
                "words": [
                    {"text": "alpha", "start": 0.1, "end": 0.5},
                    {"text": "wrong", "start": 0.6, "end": 1.0},
                ],
                "word_timeline": [
                    {"raw": "alpha", "start": 0.1, "end": 0.5},
                    {"raw": "beta", "start": 0.6, "end": 1.0},
                ],
                "cues": [{"text": "alpha beta", "start": 0.1, "end": 1.0}],
                "issues": [{
                    "severity": "orange",
                    "kind": "word_difference",
                    "start": 0.6,
                    "end": 1.0,
                }],
            }],
        }
        segments, _duration = build_task_review_timeline(task)
        recognized, aligned = build_subtitle_tracks(task, segments)

        self.assertEqual(recognized[0]["severity"], "orange")
        self.assertEqual(aligned[0]["severity"], "orange")
        self.assertIn("任务文本", recognized[0]["suggestion"])
        self.assertIn("试听", aligned[0]["suggestion"])

    def test_subtitle_tracks_honor_word_char_and_zero_gap_settings(self):
        words = [
            {"text": "alpha", "raw": "alpha", "start": 0.1, "end": 0.4},
            {"text": "beta", "raw": "beta", "start": 0.6, "end": 0.9},
            {"text": "gamma", "raw": "gamma", "start": 1.2, "end": 1.5},
            {"text": "delta", "raw": "delta", "start": 1.7, "end": 2.0},
        ]
        task = {
            "clips": [{
                "source": "clip.mp4",
                "file_name": "clip.mp4",
                "original_duration": 2.2,
                "trim_start": 0.0,
                "trim_end": 2.2,
                "included": True,
                "words": [
                    {key: value for key, value in word.items() if key != "raw"}
                    for word in words
                ],
                "word_timeline": [
                    {key: value for key, value in word.items() if key != "text"}
                    for word in words
                ],
                "issues": [],
            }],
        }
        segments, _duration = build_task_review_timeline(task)
        settings = {
            "srt_max_words_per_block": 2,
            "srt_max_chars_per_block": 11,
            "srt_block_gap_ms": 0,
        }
        recognized, aligned = build_subtitle_tracks(
            task, segments, settings=settings
        )
        self.assertEqual(
            [block["text"] for block in recognized],
            ["alpha beta", "gamma delta"],
        )
        self.assertEqual(
            [block["text"] for block in aligned],
            ["alpha beta", "gamma delta"],
        )
        self.assertAlmostEqual(
            aligned[0]["timeline_end"], aligned[1]["timeline_start"]
        )

    def test_confirmed_missing_text_is_visible_on_aligned_subtitle_track(self):
        segments, _duration = build_task_timeline(self.task)
        _recognized, aligned = build_subtitle_tracks(self.task, segments)

        warnings = [block for block in aligned if block.get("is_missing")]
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["severity"], "pink")
        self.assertIn("缺少的一段", warning["text"])
        self.assertLessEqual(warning["timeline_start"], 2.0)
        self.assertGreaterEqual(warning["timeline_end"], 2.0)
        self.assertEqual(warning["clip_index"], 1)
        self.assertIn("补拍", warning["suggestion"])

    def test_waveform_and_silence_parser_are_bounded(self):
        raw = array("h", [0, 1000, -2000, 4000, -8000, 16000]).tobytes()
        points = pcm_waveform(raw, sample_rate=2, maximum_points=3)
        self.assertLessEqual(len(points), 3)
        self.assertTrue(all(0.0 <= amplitude <= 1.0 for _time, amplitude in points))
        ranges = parse_silence_output(
            "silence_start: 0.4\nsilence_end: 1.2\nsilence_start: 2.5", 3.0
        )
        self.assertEqual(ranges, [[0.4, 1.2], [2.5, 3.0]])


if __name__ == "__main__":
    unittest.main()

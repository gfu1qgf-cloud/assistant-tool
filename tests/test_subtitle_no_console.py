"""Exercise real tqdm with absent GUI-process streams, without ASR inference."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tqdm import tqdm
from model.SubtitleHelper import generate_srt_whisper_only
from app_plugins.builtin.smart_video_editor.engine import (
    _generate_final_aligned_srt, normalize_smart_video_editor_settings,
)


class AlignmentResult:
    def merge_all_segments(self): return self
    def split_by_length(self, **kwargs): return self
    def to_srt_vtt(self, path, **kwargs):
        Path(path).write_text("1\n00:00:00,100 --> 00:00:01,200\nAmen.\n", encoding="utf-8")


class SubtitleNoConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.options = []

    def tearDown(self):
        self.temporary.cleanup()

    def align(self, *args, **kwargs):
        self.options.append(kwargs)
        # Same progress construction as the installed stable-whisper aligner.
        with tqdm(total=1, unit="sec", desc="Align",
                  disable=kwargs.get("verbose", False) is not False) as bar:
            bar.update(1)
        return AlignmentResult()

    def generate_without_console(self, name, **kwargs):
        output = self.root / name
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            generate_srt_whisper_only("final.mp4", "Amen.", str(output),
                                      language="sk", block_gap_ms=0, **kwargs)
        self.assertIn("Amen.", output.read_text(encoding="utf-8"))
        self.assertIn("verbose", self.options[-1])
        self.assertIsNone(self.options[-1]["verbose"])
        self.assertEqual(self.options[-1]["language"], "sk")

    def test_shared_loaded_model_aligns_without_stdout_or_stderr(self):
        with patch("stable_whisper.alignment.align", side_effect=self.align) as align:
            model = object()
            self.generate_without_console("loaded.srt", model=model)
            self.assertIs(align.call_args.args[0], model)

    def test_lazy_model_loader_aligns_without_stdout_or_stderr(self):
        with patch("stable_whisper.alignment.align", side_effect=self.align), \
             patch("stable_whisper.load_faster_whisper") as fallback:
            self.generate_without_console("lazy.srt", model_loader=lambda: object())
            fallback.assert_not_called()

    def test_fallback_model_aligns_without_stdout_or_stderr(self):
        with patch("stable_whisper.load_faster_whisper",
                   return_value=SimpleNamespace(align=self.align)) as load:
            self.generate_without_console("fallback.srt")
            load.assert_called_once_with("base", device="cpu", compute_type="int8")

    def test_final_smart_export_publishes_srt_without_console(self):
        output = self.root / "160_智能剪辑.srt"
        messages = []
        task = {"script": "Amen.", "language": "sk", "clips": []}
        with patch("stable_whisper.alignment.align", side_effect=self.align), \
             patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            _generate_final_aligned_srt(self.root / "160_智能剪辑.mp4", output, task,
                normalize_smart_video_editor_settings({"srt_block_gap_ms": 0}),
                messages.append, alignment_model=object())
        self.assertIn("Amen.", output.read_text(encoding="utf-8"))
        self.assertTrue(any("完成" in message for message in messages))
        self.assertFalse(list(self.root.glob("*.aligning.srt")))

    def test_actual_alignment_errors_are_not_hidden_and_previous_srt_survives(self):
        output = self.root / "160_智能剪辑.srt"
        output.write_text("previous subtitles", encoding="utf-8")
        with patch("stable_whisper.alignment.align", side_effect=ValueError("alignment failure")), \
             patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            with self.assertRaisesRegex(ValueError, "alignment failure"):
                _generate_final_aligned_srt(self.root / "160_智能剪辑.mp4", output,
                    {"script": "Amen.", "language": "sk", "clips": []},
                    normalize_smart_video_editor_settings({}), lambda _: None,
                    alignment_model=object())
        self.assertEqual(output.read_text(encoding="utf-8"), "previous subtitles")
        self.assertFalse(list(self.root.glob("*.aligning.srt")))

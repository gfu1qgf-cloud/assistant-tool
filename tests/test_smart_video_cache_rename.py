import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app_plugins.builtin.smart_video_editor.engine import (
    _cache_path,
    analyze_smart_video_jobs,
    analyze_source_silence,
    analyze_source_voice_absence,
    normalize_smart_video_editor_settings,
    refresh_renamed_task_signatures,
    save_analysis_reports,
    smart_video_jobs_require_model,
    transcribe_source,
)
from app_plugins.builtin.smart_video_editor.renaming import update_bundle_video_paths
from app_plugins.builtin.smart_video_editor.waveform import _cache_key


class _Word:
    def __init__(self, text, start, end):
        self.word = text
        self.start = start
        self.end = end
        self.probability = 0.95


class _Segment:
    text = "Moj bože prosím zdravie"
    words = [
        _Word("Moj", 0.4, 0.7),
        _Word("bože", 0.8, 1.1),
        _Word("prosím", 1.2, 1.6),
        _Word("zdravie", 2.1, 2.5),
    ]


class _Info:
    language = "sk"
    duration = 3.0


class _Model:
    def __init__(self):
        self.calls = 0

    def transcribe(self, _path, **_kwargs):
        self.calls += 1
        return iter([_Segment()]), _Info()


def _job(root, source):
    return {
        "task_id": "0801",
        "task_name": "test",
        "label": "0801 | test",
        "task_dir": str(root),
        "script": _Segment.text,
        "language": "sk",
        "sources": [str(source)],
    }


class SmartVideoRenameCacheTests(unittest.TestCase):
    def test_external_rename_reuses_transcription_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"video bytes")
            settings = {"silence_detection_enabled": False,
                        "voice_detection_enabled": False}
            model = _Model()
            analyze_smart_video_jobs([_job(root, source)], model, settings)
            renamed = root / "[01] clip.mp4"
            source.rename(renamed)
            self.assertFalse(smart_video_jobs_require_model(
                [_job(root, renamed)], settings
            ))
            result = analyze_smart_video_jobs([_job(root, renamed)], None, settings)
            self.assertEqual(model.calls, 1)
            self.assertEqual(result["summary"]["transcription_cache_count"], 1)
            self.assertEqual(result["tasks"][0]["clips"][0]["source"], str(renamed))

    def test_internal_rename_preserves_full_analysis_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"video bytes")
            settings = {"silence_detection_enabled": False,
                        "voice_detection_enabled": False}
            model = _Model()
            bundle = analyze_smart_video_jobs([_job(root, source)], model, settings)
            renamed = root / "[01] clip.mp4"
            source.rename(renamed)
            update_bundle_video_paths(bundle, [(source, renamed)])
            refresh_renamed_task_signatures(bundle)
            save_analysis_reports(bundle)
            progress = []
            result = analyze_smart_video_jobs(
                [_job(root, renamed)], None, settings, progress=progress.append
            )
            self.assertEqual(result["summary"]["task_cache_count"], 1)
            self.assertEqual(result["tasks"][0]["clips"][0]["source"], str(renamed))
            self.assertTrue(any("完整分析缓存命中" in line for line in progress))

    def test_old_path_named_cache_is_found_and_migrated_after_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"video bytes")
            settings = normalize_smart_video_editor_settings({})
            model = _Model()
            transcribe_source(model, source, "sk", root, settings)
            current_cache = _cache_path(root, source, settings)
            old_digest = hashlib.sha256(
                os.path.normcase(os.path.abspath(str(source))).encode(
                    "utf-8", "replace"
                )
            ).hexdigest()[:20]
            old_cache = current_cache.with_name(f"{old_digest}.json")
            current_cache.rename(old_cache)
            renamed = root / "[01] clip.mp4"
            source.rename(renamed)
            result = transcribe_source(None, renamed, "sk", root, settings)
            self.assertTrue(result["from_cache"])
            self.assertEqual(result["source"], str(renamed))
            self.assertTrue(_cache_path(root, renamed, settings).is_file())
            self.assertEqual(model.calls, 1)

    def test_content_change_still_invalidates_transcription_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"one")
            settings = normalize_smart_video_editor_settings({})
            model = _Model()
            transcribe_source(model, source, "sk", root, settings)
            source.write_bytes(b"two")
            transcribe_source(model, source, "sk", root, settings)
            self.assertEqual(model.calls, 2)

    def test_separate_identical_files_keep_separate_cache_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"same bytes")
            second.write_bytes(b"same bytes")
            settings = normalize_smart_video_editor_settings({})
            model = _Model()
            transcribe_source(model, first, "sk", root, settings)
            transcribe_source(model, second, "sk", root, settings)
            self.assertEqual(model.calls, 2)
            self.assertNotEqual(
                _cache_path(root, first, settings),
                _cache_path(root, second, settings),
            )

    def test_silence_voice_and_waveform_cache_survive_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"video bytes")
            settings = normalize_smart_video_editor_settings({})
            with mock.patch(
                "app_plugins.builtin.smart_video_editor.engine.detect_silence_ranges",
                return_value=([[0.0, 1.0]], ""),
            ) as silence, mock.patch(
                "app_plugins.builtin.smart_video_editor.engine.detect_voice_absence_ranges",
                return_value=([[1.0, 2.0]], ""),
            ) as voice:
                self.assertEqual(
                    analyze_source_silence(source, root, 3.0, settings, "ffmpeg")[0],
                    [[0.0, 1.0]],
                )
                self.assertEqual(
                    analyze_source_voice_absence(source, root, 3.0, settings)[0],
                    [[1.0, 2.0]],
                )
                waveform_key = _cache_key(source)
                renamed = root / "[01] clip.mp4"
                source.rename(renamed)
                self.assertEqual(_cache_key(renamed), waveform_key)
                self.assertEqual(
                    analyze_source_silence(renamed, root, 3.0, settings, "ffmpeg")[0],
                    [[0.0, 1.0]],
                )
                self.assertEqual(
                    analyze_source_voice_absence(renamed, root, 3.0, settings)[0],
                    [[1.0, 2.0]],
                )
                self.assertEqual(silence.call_count, 1)
                self.assertEqual(voice.call_count, 1)


if __name__ == "__main__":
    unittest.main()

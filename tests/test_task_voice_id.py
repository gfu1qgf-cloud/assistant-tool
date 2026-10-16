import copy
import base64
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtWidgets
from model.AudioHelper import (CreateAudioLatest, LATEST_ELEVENLABS_MODEL_ID,
                              _split_latest_audio_text, _speech_ffmpeg)
from model.OdsHelper import ReadTaskOds2, TaskData
from model.TaskTableSchema import normalize_task_table_schema
from app_plugins.builtin.task_audio_subtitle import TaskAudioSubtitlePlugin
from PYUI.main_pyui import MainDialog
from PYUI.task_schema_pyui import TaskTableSchemaDialog
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableRow, TableCell
from odf.text import P


def _timed(audio):
    return dict(audio_base64=base64.b64encode(audio).decode(), alignment=None)


class TaskVoiceIdSchemaTests(unittest.TestCase):
    def test_optional_voice_id_taskdata_empty_or_stripped(self):
        self.assertEqual(TaskData({}).task_audio_voice_id, "")
        self.assertEqual(TaskData({"task_audio_voice_id": " voice-123 "}).task_audio_voice_id, "voice-123")

    def test_old_schema_gains_defaults_without_changing_existing_aliases(self):
        raw = {"fields": {"task_audio_type": {"aliases": ["custom type"]}}}
        baseline = copy.deepcopy(raw)
        schema = normalize_task_table_schema(raw)
        self.assertEqual(schema["fields"]["task_audio_type"]["aliases"], ["custom type"])
        self.assertIn("语音ID", schema["fields"]["task_audio_voice_id"]["aliases"])
        self.assertEqual(raw, baseline)

    def read_sheet(self, headers, values, extra=None):
        schema = {"fields": {"task_id": {"aliases": ["编号"]},
                             "task_type": {"default": "oral"},
                             "task_audio_text": {"aliases": ["任务语音"]},
                             "task_audio_type": {"aliases": ["语音类型"]}}}
        if extra:
            schema["fields"].update(extra)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ods"
            document = OpenDocumentSpreadsheet()
            sheet = Table(name="tasks")
            document.spreadsheet.addElement(sheet)
            for data in (headers, values):
                row = TableRow()
                for value in data:
                    cell = TableCell()
                    cell.addElement(P(text=value))
                    row.addElement(cell)
                sheet.addElement(row)
            document.save(str(path))
            return ReadTaskOds2(path, schema=schema, return_report=True)

    def test_existing_chinese_voice_id_column_readable_after_column_reorder(self):
        for headers, values in ((["语音ID", "编号", "任务语音", "语音类型"], ["my-voice", "27", "Amen.", ""]),
                                (["任务语音", "语音类型", "编号", "语音ID"], ["Amen.", "", "27", "my-voice"])):
            tasks, report = self.read_sheet(headers, values)
            self.assertEqual(tasks[0].task_audio_voice_id, "my-voice")
            self.assertEqual(tasks[0].task_audio_text, "Amen.")
            self.assertEqual(tasks[0].task_audio_type, "")
            self.assertEqual(report["matched_headers"]["task_audio_voice_id"], ["语音ID"])

    def test_old_table_without_voice_id_keeps_type_and_text(self):
        tasks, _report = self.read_sheet(["编号", "任务语音", "语音类型"], ["28", "Amen.", "old profile"])
        self.assertEqual(tasks[0].task_audio_voice_id, "")
        self.assertEqual(tasks[0].task_audio_type, "old profile")

    def test_custom_alias_still_supported(self):
        tasks, _report = self.read_sheet(["编号", "任务语音", "custom voice"], ["29", "Amen.", "voice-id"],
                                         {"task_audio_voice_id": {"aliases": ["custom voice"]}})
        self.assertEqual(tasks[0].task_audio_voice_id, "voice-id")


class LatestAudioApiTests(unittest.TestCase):
    def test_single_request_uses_latest_model_exact_voice_and_original_text(self):
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.return_value = _timed(b"audio-data")
        progress = []
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client):
            target = Path(directory) / "task_audio.mp3"
            self.assertTrue(CreateAudioLatest("Nevynechaj to!", str(target), voice_id="my-voice",
                                             api_keys=["dummy-test-key"], api_key_status_config=None,
                                             progress_callback=progress.append))
            self.assertEqual(target.read_bytes(), b"audio-data")
            self.assertFalse(Path(str(target) + ".part").exists())
        arguments = client.text_to_dialogue.convert_with_timestamps.call_args.kwargs
        self.assertEqual(arguments["model_id"], "eleven_v4")
        self.assertEqual(arguments["inputs"][0].voice_id, "my-voice")
        self.assertEqual(arguments["inputs"][0].text, "Nevynechaj to!")
        self.assertEqual(arguments["output_format"], "mp3_44100_128")
        self.assertNotIn("voice_settings", arguments)
        client.text_to_speech.convert.assert_not_called()
        self.assertIn("eleven_v4", "\n".join(progress))
        self.assertNotIn("dummy-test-key", "\n".join(progress))

    def test_empty_key_does_not_send_requests(self):
        with patch("model.AudioHelper.ElevenLabs") as client:
            self.assertFalse(CreateAudioLatest("Amen.", "unused.mp3", "voice", api_keys=[],
                                               progress_callback=lambda *_args: None))
        client.assert_not_called()

    def test_real_installed_sdk_serializes_latest_dialogue_request(self):
        import httpx
        from elevenlabs.client import ElevenLabs
        received = []
        def transport(request):
            received.append(request)
            return httpx.Response(200, json=_timed(b"sdk-audio"))
        with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
            client = ElevenLabs(api_key="dummy", httpx_client=http_client)
            with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client):
                self.assertTrue(CreateAudioLatest("Nevynechaj to!", str(Path(directory) / "out.mp3"), "my-voice",
                    api_keys=["dummy"], api_key_status_config=None, progress_callback=lambda *_args: None))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].url.path, "/v1/text-to-dialogue/with-timestamps")
        body = json.loads(received[0].content)
        self.assertEqual(body["model_id"], "eleven_v4")
        self.assertEqual(body["inputs"], [{"text": "Nevynechaj to!", "voice_id": "my-voice"}])

    def test_offline_policy_blocks_latest_api_before_client_creation(self):
        from model.OfflineMode import OfflineModeError
        with patch("model.OfflineMode.require_online", side_effect=OfflineModeError("offline")), \
                patch("model.AudioHelper.ElevenLabs") as client:
            with self.assertRaises(OfflineModeError):
                CreateAudioLatest("Amen.", "out.mp3", "voice", api_keys=["dummy"], progress_callback=lambda *_args: None)
        client.assert_not_called()

    def test_voice_and_text_validation_before_network(self):
        with patch("model.AudioHelper.ElevenLabs") as client:
            for text, voice in (("", "voice"), ("Amen.", ""), ("Amen.", "voice extra label")):
                with self.assertRaises(ValueError):
                    CreateAudioLatest(text, "unused.mp3", voice, api_keys=["dummy"], progress_callback=lambda *_args: None)
            with self.assertRaisesRegex(ValueError, "2000"):
                CreateAudioLatest("x" * 2001, "unused.mp3", "voice", api_keys=["dummy"], progress_callback=lambda *_args: None)
        client.assert_not_called()

    def test_long_text_chunks_preserve_complete_words_and_all_original_characters(self):
        source = ('Mária plače, prosím POŠLI JEJ RUŽU. Začínam ja. Nevynechaj to! ' * 100).strip()
        chunks = _split_latest_audio_text(source)
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(len(chunk) <= 2000 for chunk in chunks))
        cursor = 0
        boundaries = set()
        for chunk in chunks[:-1]:
            cursor += len(chunk)
            boundaries.add(cursor)
        for word in re.finditer(r"\S+", source):
            self.assertFalse(any(word.start() < cut < word.end() for cut in boundaries))

    def test_short_text_does_not_require_ffmpeg(self):
        with patch("model.AudioHelper._speech_ffmpeg", side_effect=AssertionError("unneeded")), \
                patch("model.AudioHelper._save_audio_with_api_keys", return_value=True) as save:
            self.assertTrue(CreateAudioLatest("Amen.", "output.mp3", "voice", api_keys=["dummy"]))
        save.assert_called_once()

    def test_missing_long_text_tool_fails_before_spending_credits(self):
        with patch("model.AudioHelper._speech_ffmpeg", side_effect=FileNotFoundError("missing ffmpeg")), \
                patch("model.AudioHelper.ElevenLabs") as client:
            with self.assertRaises(FileNotFoundError):
                CreateAudioLatest("word " * 450, "unused.mp3", "voice", api_keys=["dummy"])
        client.assert_not_called()

    def test_empty_response_preserves_old_output_and_removes_partial(self):
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.return_value = _timed(b"")
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client):
            target = Path(directory) / "task_audio.mp3"
            target.write_bytes(b"old-audio")
            self.assertFalse(CreateAudioLatest("Amen.", str(target), "voice", api_keys=["dummy"],
                                               api_key_status_config=None, progress_callback=lambda *_args: None))
            self.assertEqual(target.read_bytes(), b"old-audio")
            self.assertFalse(Path(str(target) + ".part").exists())

    def test_permission_error_does_not_change_voice_model_or_try_other_keys(self):
        class PermissionErrorFromApi(Exception):
            status_code = 403
            body = {"detail": {"status": "voice_access_denied"}}
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.side_effect = PermissionErrorFromApi()
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client) as factory:
            self.assertFalse(CreateAudioLatest("Amen.", str(Path(directory) / "out.mp3"), "voice",
                api_keys=["dummy1", "dummy2"], api_key_status_config=None, progress_callback=lambda *_args: None))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(client.text_to_dialogue.convert_with_timestamps.call_args.kwargs["model_id"], LATEST_ELEVENLABS_MODEL_ID)

    def test_invalid_key_rotation_preserved(self):
        class InvalidKey(Exception):
            status_code = 401
            body = {"detail": {"status": "invalid_api_key"}}
        first, second = MagicMock(), MagicMock()
        first.text_to_dialogue.convert_with_timestamps.side_effect = InvalidKey()
        second.text_to_dialogue.convert_with_timestamps.return_value = _timed(b"complete")
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", side_effect=[first, second]) as factory:
            self.assertTrue(CreateAudioLatest("Amen.", str(Path(directory) / "out.mp3"), "voice",
                api_keys=["dummy1", "dummy2"], api_key_status_config=None, progress_callback=lambda *_args: None))
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(second.text_to_dialogue.convert_with_timestamps.call_args.kwargs["inputs"][0].voice_id, "voice")

    def test_long_text_native_ffmpeg_merge_and_decode_has_full_duration(self):
        try:
            ffmpeg = _speech_ffmpeg()
        except FileNotFoundError:
            self.skipTest("native FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mp3"
            generated = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", str(fixture)], capture_output=True)
            self.assertEqual(generated.returncode, 0, generated.stderr[-500:])
            audio = fixture.read_bytes()
            client = MagicMock()
            client.text_to_dialogue.convert_with_timestamps.side_effect = lambda **_kwargs: _timed(audio)
            target = Path(directory) / "out.mp3"
            with patch("model.AudioHelper.ElevenLabs", return_value=client):
                self.assertTrue(CreateAudioLatest("word " * 500, str(target), "voice", api_keys=["dummy"],
                                                  api_key_status_config=None, progress_callback=lambda *_args: None))
            calls = client.text_to_dialogue.convert_with_timestamps.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual("".join(call.kwargs["inputs"][0].text for call in calls), ("word " * 500).strip())
            decoded = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(target),
                                      "-f", "s16le", "-ac", "1", "-ar", "44100", "pipe:1"], capture_output=True)
            self.assertEqual(decoded.returncode, 0, decoded.stderr[-500:])
            self.assertAlmostEqual(len(decoded.stdout) / 2 / 44100, 2.0, delta=.08)
            self.assertFalse(list(Path(directory).glob(".ele-audio-*")))

    def test_failure_midway_in_long_text_never_leaves_partial_output(self):
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.side_effect = [_timed(b"first-chunk"), RuntimeError("api failure")]
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client), \
                patch("model.AudioHelper._speech_ffmpeg", return_value="not-used"):
            target = Path(directory) / "out.mp3"
            target.write_bytes(b"old-output")
            self.assertFalse(CreateAudioLatest("word " * 500, str(target), "voice", api_keys=["dummy"],
                                               api_key_status_config=None, progress_callback=lambda *_args: None))
            self.assertEqual(target.read_bytes(), b"old-output")
            self.assertFalse(list(Path(directory).glob(".ele-audio-*")))
            self.assertFalse(Path(str(target) + ".part").exists())

    def test_ffmpeg_failure_preserves_existing_output_and_removes_temporary_chunks(self):
        client = MagicMock()
        client.text_to_dialogue.convert_with_timestamps.side_effect = lambda **_kwargs: _timed(b"chunk-audio")
        with tempfile.TemporaryDirectory() as directory, patch("model.AudioHelper.ElevenLabs", return_value=client), \
                patch("model.AudioHelper._speech_ffmpeg", return_value="mock-ffmpeg"), \
                patch("model.AudioHelper.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            target = Path(directory) / "out.mp3"
            target.write_bytes(b"old-output")
            self.assertFalse(CreateAudioLatest("word " * 500, str(target), "voice", api_keys=["dummy"],
                                               api_key_status_config=None, progress_callback=lambda *_args: None))
            self.assertEqual(target.read_bytes(), b"old-output")
            self.assertFalse(list(Path(directory).glob(".ele-audio-*")))
            self.assertFalse(Path(str(target) + ".part").exists())


class VoiceIdAudioIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.plugin = TaskAudioSubtitlePlugin()
        self.plugin.context = SimpleNamespace(parent_widget=SimpleNamespace(config_name="not-written.json"))
        self.plugin._select_api_keys = lambda _progress: ["dummy-key"]
        self.task = SimpleNamespace(task_audio_voice_id="my-voice", task_audio_type="", task_audio_text="Nevynechaj to!",
                                    task_name="Friendly task", task_id="27")

    def test_voice_id_works_without_profile_and_takes_priority_over_edge(self):
        self.plugin.audio_settings = {"edge-profile": {"model": "edge", "sex": "女声"}}
        for profile in ("", "unknown", "edge-profile"):
            self.task.task_audio_type = profile
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "task_audio.mp3"
                def create(_text, filename, **_kwargs):
                    Path(filename).write_bytes(b"complete")
                    return True
                with patch("app_plugins.builtin.task_audio_subtitle.CreateAudioLatest", side_effect=create) as latest, \
                        patch("app_plugins.builtin.task_audio_subtitle.CreateTTSAudio") as edge:
                    self.assertTrue(self.plugin._create_audio(self.task, target, lambda *_args: None))
                self.assertEqual(latest.call_args.kwargs["voice_id"], "my-voice")
                self.assertEqual(latest.call_args.kwargs["api_keys"], ["dummy-key"])
                edge.assert_not_called()

    def test_id_only_task_not_skipped_and_subtitles_keep_same_options(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {"task": self.task, "target_dir": directory, "label": "task 27", "language": "sk"}
            def create(_task, path, _progress):
                path.write_bytes(b"audio")
                return True
            with patch.object(self.plugin, "_create_audio", side_effect=create) as audio, \
                    patch.object(self.plugin, "_create_subtitle", return_value=("generated", Path(directory) / "task_audio.srt")) as subtitle:
                result = self.plugin._execute_jobs("audio", [job], {"with_subtitles": True}, lambda *_args: None)
            audio.assert_called_once()
            subtitle.assert_called_once()
            self.assertEqual(result["audio_generated"], 1)
            self.assertEqual(result["subtitle_generated"], 1)
            self.assertEqual(result["skipped_no_voice"], 0)
            self.assertEqual(result["failed"], 0)

    def test_existing_audio_is_not_regenerated_even_with_new_voice_id(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "task_audio.mp3"
            target.write_bytes(b"old")
            job = {"task": self.task, "target_dir": directory}
            with patch.object(self.plugin, "_create_audio") as create:
                result = self.plugin._execute_jobs("audio", [job], {}, lambda *_args: None)
            create.assert_not_called()
            self.assertEqual(result["audio_existing"], 1)
            self.assertEqual(target.read_bytes(), b"old")

    def test_empty_text_still_normal_skip_with_voice_id(self):
        self.task.task_audio_text = ""
        with tempfile.TemporaryDirectory() as directory, patch.object(self.plugin, "_create_audio") as create:
            result = self.plugin._execute_jobs("audio", [{"task": self.task, "target_dir": directory}], {}, lambda *_args: None)
        create.assert_not_called()
        self.assertEqual(result["skipped_no_text"], 1)
        self.assertEqual(result["failed"], 0)

    def test_no_id_keeps_legacy_model_and_random_profile_voice(self):
        self.task.task_audio_voice_id = ""
        self.task.task_audio_type = "old"
        for model, method in (("edge", "CreateTTSAudio"), ("elevenlabs", "CreateAudio"), ("elevenlabs3", "CreateAudio3")):
            self.plugin.audio_settings = {"old": {"model": model, "sex": "女声", "voice_ids": ["old-voice"]}}
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "out.mp3"
                target.write_bytes(b"existing")
                with patch("app_plugins.builtin.task_audio_subtitle." + method, return_value=True) as legacy, \
                        patch("app_plugins.builtin.task_audio_subtitle.CreateAudioLatest") as latest:
                    self.assertTrue(self.plugin._create_audio(self.task, target, lambda *_args: None))
                legacy.assert_called_once()
                latest.assert_not_called()

    def test_main_legacy_helper_also_honors_explicit_voice_id(self):
        window = SimpleNamespace(audio_settings={}, config_name="not-written.json",
                                 get_elevenlabs_api_keys=lambda _progress: ["dummy"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.mp3"
            def create(_text, filename, **_kwargs):
                Path(filename).write_bytes(b"audio")
                return True
            with patch("PYUI.main_pyui.CreateAudioLatest", side_effect=create) as latest:
                self.assertTrue(MainDialog._createTaskAudioFile(window, self.task, target))
            self.assertEqual(latest.call_args.kwargs["voice_id"], "my-voice")

    def test_schema_dialog_exposes_optional_voice_id_aliases(self):
        dialog = TaskTableSchemaDialog({})
        try:
            self.assertIn("语音ID", dialog.alias_edits["task_audio_voice_id"].text())
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

"""Typed registration, opt-in UI, frozen jobs and real post-export captions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import TaskRunContext, BackgroundTaskResultError
from model.node_workflow.contracts import ResourceRef, SubtitleTrack
from model.node_workflow.graph import NodeRegistry, register_core, new_node, validate, evaluate_graph
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.tasks import prepare_job
from model.node_workflow.preview import run_preview
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
from app_plugins.builtin.smart_video_editor import postprocess_workflow as workflow, background, engine
from app_plugins.builtin.smart_video_editor.postprocess_ui import PostprocessControls, exec_review
from app_plugins.builtin.smart_video_editor.plugin import SmartVideoEditorPlugin

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def registry():
    value = NodeRegistry()
    register_core(value)
    value.register("batch_text_video", caption_node())
    workflow.register(SimpleNamespace(register_node=lambda node: value.register("smart_video_editor", node),
        register_workflow_type=lambda item: value.register_workflow_type("smart_video_editor", item)))
    return value


class SmartPostprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = registry()
        self.library = WorkflowLibrary(self.root / "workflows")
        self.services = SimpleNamespace(available_nodes=lambda: self.registry, workflow_library=self.library,
            open_workflow_editor=Mock(), workflow_runtime_context=lambda _name: {})
        self.graph = workflow.example(self.registry)
        self.saved = self.library.save(self.graph, "智能剪辑自动字幕", self.registry)
        self.settings = engine.normalize_smart_video_editor_settings({"postprocess_enabled": True,
            "postprocess_workflow": self.saved["workflow_id"], "ffmpeg_path": FFMPEG})
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.output = self.root / "普通剪辑结果"
        self.output.mkdir()
        self.source = self.output / "1_智能剪辑.mp4"
        self.source.write_bytes(b"unit-test-media-only")
        self.srt = self.source.with_suffix(".srt")
        self.srt.write_text("1\n00:00:00,100 --> 00:00:01,800\nHello smart editor\n", encoding="utf-8")
        self.task = {"task_id": "1", "task_name": "一号任务", "task_dir": str(self.root),
            "output_dir": str(self.output), "script": "Hello smart editor", "language": "en", "clips": []}
        self.item = {"task_id": "1", "status": "completed", "video": str(self.source), "srt": str(self.srt)}

    def passthrough(self):
        graph = workflow.example(self.registry)
        graph["nodes"] = [node for node in graph["nodes"] if node["kind"] in {workflow.ENTRY, workflow.OUTPUT}]
        first, last = graph["nodes"]
        graph["connections"] = [[first["id"], "video", last["id"], "video"]]
        graph["output_node"] = last["id"]
        return graph

    def payload(self, graph=None):
        return prepare_job(graph or self.graph, self.registry, None, self.services)[0]

    def test_type_owns_unique_entry_output_and_default_template_validates(self):
        self.assertIn(workflow.TYPE_ID, self.registry.workflow_types)
        self.assertEqual(validate(self.graph, self.registry)[0], [])
        entry = self.registry.definitions[workflow.ENTRY]
        ports = {port.key: port.data_type for port in entry.outputs}
        self.assertEqual(ports["video"], "video")
        self.assertEqual(ports["subtitles"], "subtitles")
        self.assertIn("task_id", ports)
        self.assertIn("segment_index", ports)
        disabled = self.registry.subset(lambda owner: owner != "smart_video_editor")
        self.assertNotIn(workflow.TYPE_ID, disabled.workflow_types)
        self.assertTrue(validate(self.graph, disabled)[0])

    def test_disabled_defaults_never_load_workflow_or_change_existing_outputs(self):
        settings = engine.normalize_smart_video_editor_settings({})
        self.assertFalse(settings[workflow.ENABLED])
        self.assertEqual(workflow.prepare(settings, Mock()), (None, None, ()))
        result = {"completed": [self.item], "failed": [], "skipped": []}
        payload = {"phase": "export", "settings": settings, "bundle": {"tasks": [self.task]}, "source_versions": {}}
        with patch.object(background, "export_smart_video_bundle", return_value=result), patch.object(workflow, "process_results") as post:
            actual = background.run_smart_video_job(payload, self.context)
        post.assert_not_called()
        self.assertEqual(actual["completed"][0]["video"], str(self.source))

    def test_missing_selection_wrong_type_and_invalid_graph_are_preflight_errors(self):
        with self.assertRaisesRegex(ValueError, "先选择"):
            workflow.prepare(dict(self.settings, postprocess_workflow=""), self.services)
        wrong = copy.deepcopy(self.saved)
        wrong["workflow_type"] = "task_delivery.video_postprocess"
        wrong = self.library.save(wrong, "错误类型", self.registry)
        with self.assertRaisesRegex(ValueError, "类型"):
            workflow.prepare(self.settings, self.services)
        wrong["workflow_type"] = workflow.TYPE_ID
        wrong["connections"] = []
        self.library.save(wrong, "不完整图", self.registry)
        with self.assertRaises(ValueError):
            workflow.prepare(self.settings, self.services)

    def test_submission_freezes_graph_and_node_parameters(self):
        frozen, selected_registry, _ = workflow.prepare(self.settings, self.services)
        self.assertIs(selected_registry, self.registry)
        changed = self.library.load(self.saved["workflow_id"], self.registry)
        caption = next(node for node in changed["nodes"] if node["kind"] == "batch_text_video.caption")
        caption["params"]["size"] = 73
        self.library.save(changed, "已改字幕样式", self.registry)
        original_caption = next(node for node in frozen["graph"]["nodes"] if node["kind"] == "batch_text_video.caption")
        self.assertEqual(original_caption["params"]["size"], 24)

    def test_input_requires_scoped_injection_and_provides_verified_subtitles(self):
        with self.assertRaisesRegex(ValueError, "注入"):
            workflow.build_input({}, {}, self.context)
        track, ref = workflow.track_from_srt(self.srt, 2.0, "task-key")
        self.context.workflow_input = {"provider": workflow.TYPE_ID, "video": ResourceRef.capture(self.source, "video"),
            "subtitles": track, "duration": 2.0, "task_id": "1"}
        outputs = workflow.build_input({}, {}, self.context)
        self.assertIsInstance(outputs["subtitles"], SubtitleTrack)
        self.assertEqual(outputs["subtitles"].cues[0].text, "Hello smart editor")
        self.assertTrue(outputs["has_subtitles"])
        self.assertEqual(outputs["segment_index"], 1)
        self.assertEqual(ref.path, str(self.srt))

    def test_passthrough_avoids_reencoding_and_restores_context(self):
        self.context.workflow_input = {"old": "preserve"}
        before = self.source.read_bytes(), self.srt.read_bytes()
        with patch("model.node_workflow.render.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
             patch("model.node_workflow.render.probe", return_value={"duration": 2.0}), \
             patch("model.node_workflow.render.render_plan") as render:
            result = workflow.process_file(self.item, self.task, self.payload(self.passthrough()), self.registry,
                                           self.context, self.settings)
        render.assert_not_called()
        self.assertEqual(result["video"], str(self.source))
        self.assertEqual(result["postprocess"], "unchanged")
        self.assertEqual((self.source.read_bytes(), self.srt.read_bytes()), before)
        self.assertEqual(self.context.workflow_input, {"old": "preserve"})

    def test_missing_prepared_workflow_is_rejected_before_ordinary_export(self):
        payload = {"phase": "export", "settings": self.settings, "bundle": {"tasks": [self.task]}}
        with patch.object(background, "export_smart_video_bundle") as export, self.assertRaisesRegex(ValueError, "快照"):
            background.run_smart_video_job(payload, self.context)
        export.assert_not_called()

    def test_postprocess_failure_retains_original_and_continues_other_tasks(self):
        other_dir = self.root / "other"
        other_dir.mkdir()
        other_video = other_dir / "2.mp4"
        other_video.write_bytes(b"other")
        second = dict(self.item, task_id="2", video=str(other_video))
        other_task = dict(self.task, task_id="2", output_dir=str(other_dir))
        result = {"completed": [self.item, second], "failed": [], "skipped": []}
        with patch.object(workflow, "process_file", side_effect=[ValueError("synthetic subtitle error"), second]):
            actual = workflow.process_results(result, {"tasks": [self.task, other_task]}, self.payload(), self.registry,
                                               self.context, self.settings)
        self.assertEqual(actual["completed"], [second])
        self.assertEqual(actual["failed"][0]["original_video"], str(self.source))
        self.assertIn("原成片已保留", actual["failed"][0]["error"])
        self.assertTrue(self.source.is_file())

    def test_segments_get_their_own_subtitle_clock_and_metadata(self):
        lines, words = engine.build_script_word_records("alpha bravo")
        clip = {"source": str(self.root / "raw.mp4"), "included": True, "export_order": 1,
                "kept_ranges": [[1.0, 3.0]], "script_word_start": 0, "script_word_end": 1,
                "word_timeline": [{"script_word_index": 0, "start": 1.2, "end": 1.5},
                                  {"script_word_index": 1, "start": 1.5, "end": 2.5}]}
        task = dict(self.task, script_lines=lines, script_words=words, clips=[clip])
        seen = []
        def evaluate(_graph, _registry, context):
            seen.append(workflow.build_input({}, {}, context))
            return {"video": seen[-1]["video"]}
        with patch("model.node_workflow.render.resolve_tools", return_value=("ffmpeg", "ffprobe")), \
             patch("model.node_workflow.render.probe", return_value={"duration": 2.0}), \
             patch("model.node_workflow.graph.evaluate_graph", side_effect=evaluate):
            workflow.process_file(dict(self.item, srt=""), task, self.payload(self.passthrough()), self.registry,
                                  self.context, self.settings, clip=clip, index=2, count=3)
        self.assertEqual(seen[0]["segment_index"], 2)
        self.assertEqual(seen[0]["segment_count"], 3)
        self.assertAlmostEqual(seen[0]["subtitles"].cues[0].start, .2)
        self.assertAlmostEqual(seen[0]["subtitles"].cues[0].end, 1.5)

    def test_controls_default_off_filter_type_and_open_existing_panel(self):
        controls = PostprocessControls({}, self.services)
        self.addCleanup(controls.deleteLater)
        self.assertFalse(controls.enabled_box.isChecked())
        self.assertFalse(controls.choice.isEnabled())
        self.assertEqual(controls.choice.count(), 2)
        controls.choice.setCurrentIndex(1)
        controls.enabled_box.setChecked(True)
        self.assertEqual(controls.selection(validate=True)[workflow.WORKFLOW], self.saved["workflow_id"])
        controls.editor_button.click()
        self.services.open_workflow_editor.assert_called_once_with(workflow_id=self.saved["workflow_id"], workflow_type=workflow.TYPE_ID)

    def test_controls_allow_review_edits_but_require_selection_before_export(self):
        controls = PostprocessControls({}, self.services)
        self.addCleanup(controls.deleteLater)
        controls.enabled_box.setChecked(True)
        self.assertEqual(controls.selection(), {workflow.ENABLED: True, workflow.WORKFLOW: ""})
        with self.assertRaisesRegex(ValueError, "选择或创建"):
            controls.selection(validate=True)

    def test_open_panel_releases_review_modality_without_losing_review_window(self):
        owner = QtWidgets.QDialog()
        self.addCleanup(owner.deleteLater)
        owner.setModal(True)
        controls = PostprocessControls({}, self.services, owner)
        owner.show()
        self.app.processEvents()
        controls.editor_button.click()
        self.app.processEvents()
        self.assertFalse(owner.isModal())
        self.assertIsNot(self.app.activeModalWidget(), owner)
        self.assertTrue(owner.isVisible())
        owner.close()

    def test_review_waits_for_user_after_editor_opens_and_keeps_unsaved_edits(self):
        owner = QtWidgets.QDialog()
        self.addCleanup(owner.deleteLater)
        controls = PostprocessControls({}, self.services, owner)
        note = QtWidgets.QLineEdit("unsaved review", owner)
        events = []
        def open_editor():
            controls.editor_button.click()
            events.append("editor")
            QtCore.QTimer.singleShot(10, confirm)
        def confirm():
            events.append("confirm")
            self.assertEqual(note.text(), "unsaved review")
            self.assertIsNot(self.app.activeModalWidget(), owner)
            owner.accept()
        QtCore.QTimer.singleShot(0, open_editor)
        self.assertEqual(exec_review(owner), QtWidgets.QDialog.Accepted)
        self.assertEqual(events, ["editor", "confirm"])

    def test_editor_discard_cancel_restores_review_modality(self):
        owner = QtWidgets.QDialog()
        self.addCleanup(owner.deleteLater)
        self.services.open_workflow_editor.return_value = False
        controls = PostprocessControls({}, self.services, owner)
        owner.setModal(True)
        owner.show()
        controls.editor_button.click()
        self.app.processEvents()
        self.assertIs(self.app.activeModalWidget(), owner)
        owner.close()

    def test_actual_plugin_registration_publishes_type_to_enabled_host_registry(self):
        from app_plugins.host import PluginHost
        host = PluginHost(SimpleNamespace(load_config=lambda: {}))
        host.install(SmartVideoEditorPlugin())
        available = host.available_nodes()
        self.assertIn(workflow.TYPE_ID, available.workflow_types)
        self.assertIn("smart_video_editor.edit", available.definitions)
        self.assertEqual(validate(workflow.example(available), available)[0], [])

    def test_submit_prepares_before_enqueue_and_passes_frozen_worker_payload(self):
        plugin = SmartVideoEditorPlugin()
        handle = SimpleNamespace(task_id="test", completed=Mock(), failed=Mock(), finished=Mock())
        self.services.background_tasks = SimpleNamespace(is_accepting=lambda: True)
        self.services.submit_background_task = Mock(return_value=handle)
        self.services.log = Mock()
        self.services.notify = Mock()
        plugin.context = self.services
        bundle = {"tasks": [self.task]}
        plugin._submit_worker("export", self.settings, bundle=bundle)
        submitted = self.services.submit_background_task.call_args.kwargs
        self.assertEqual(submitted["payload"]["postprocess"]["graph"]["workflow_type"], workflow.TYPE_ID)
        self.assertIs(submitted["runner"].keywords["registry"], self.registry)
        self.services.submit_background_task.reset_mock()
        with self.assertRaisesRegex(ValueError, "先选择"):
            plugin._submit_worker("export", dict(self.settings, postprocess_workflow=""), bundle=bundle)
        self.services.submit_background_task.assert_not_called()
        skipped = copy.deepcopy(self.task)
        engine.set_smart_video_missing_review(skipped, "skipped")
        plugin._submit_worker("export", dict(self.settings, postprocess_workflow=""), bundle={"tasks": [skipped]})
        self.assertIsNone(self.services.submit_background_task.call_args.kwargs["payload"]["postprocess"])

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "Real FFmpeg not configured")
    def test_registered_preview_generates_simulation_with_caption_and_restores_input(self):
        graph = self.graph
        graph["simulation"] = {"width": 240, "height": 160, "duration": 2, "color": "#101010"}
        output = graph["output_node"]
        payload, _ = prepare_job(graph, self.registry, None, self.services, root_id=output)
        payload.update(preview_node=output, ffmpeg=FFMPEG)
        self.context.workflow_input = {"old": "preserve"}
        result = run_preview(payload, self.context, registry=self.registry)
        self.assertTrue(result["simulation"])
        self.assertTrue(result["png"])
        self.assertEqual(result["source_size"], [240, 160])
        self.assertIn("模拟", result["title"])
        self.assertEqual(self.context.workflow_input, {"old": "preserve"})

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "Real FFmpeg not configured")
    def test_real_background_pipeline_handles_joined_and_segment_exports(self):
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=240x160:r=20:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(self.source)], check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines, words = engine.build_script_word_records("Hello smart editor")
        clip = {"source": str(self.source), "file_name": self.source.name, "included": True,
                "status": "green", "original_duration": 2.0, "trim_start": 0., "trim_end": 2.,
                "export_order": 1, "kept_ranges": [[0., 2.]], "script_word_start": 0, "script_word_end": 2,
                "word_timeline": [{"script_word_index": i, "start": .1 + i * .5, "end": .6 + i * .5}
                                  for i in range(3)]}
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for mode in ("joined", "segments"):
            with self.subTest(mode=mode):
                output = self.root / mode
                task = dict(self.task, output_dir=str(output), clips=[clip], script_lines=lines, script_words=words)
                settings = dict(self.settings, export_mode=mode)
                snapshot, selected, _ = workflow.prepare(settings, self.services)
                payload = {"phase": "export", "settings": settings, "bundle": {"tasks": [task]}, "postprocess": snapshot}
                def align(_video, srt, *_args, **_kwargs):
                    Path(srt).write_bytes(self.srt.read_bytes())
                with patch.object(engine, "_generate_final_aligned_srt", side_effect=align):
                    result = background.run_smart_video_job(payload, self.context, registry=selected)
                self.assertFalse(result["failed"])
                entry = result["completed"][0]
                self.assertEqual(entry["postprocess"], "completed")
                self.assertTrue(Path(entry["original_video"]).exists())
                if mode == "segments":
                    entry = entry["segments"][0]
                self.assertTrue(Path(entry["srt"]).is_file())
                self.assertTrue(Path(entry["video"]).is_file())
                self.assertEqual(Path(entry["video"]).parent, output / "节点后处理结果")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "Real FFmpeg not configured")
    def test_real_export_burns_existing_subtitles_and_preserves_original_and_versions(self):
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=240x160:r=20:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(self.source)], check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        before = hashlib.sha256(self.source.read_bytes()).hexdigest(), self.srt.read_bytes()
        payload, selected, _ = workflow.prepare(self.settings, self.services)
        first = workflow.process_file(self.item, self.task, payload, selected, self.context, self.settings)
        second = workflow.process_file(self.item, self.task, payload, selected, self.context, self.settings)
        self.assertNotEqual(first["video"], str(self.source))
        self.assertNotEqual(first["video"], second["video"])
        self.assertTrue(Path(first["video"]).is_file())
        self.assertTrue(Path(first["srt"]).is_file())
        frame = subprocess.run([FFMPEG, "-v", "error", "-ss", "1", "-i", first["video"], "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=15)
        self.assertEqual(frame.returncode, 0)
        self.assertGreater(sum(pixel > 180 for pixel in frame.stdout), 20)
        self.assertEqual((hashlib.sha256(self.source.read_bytes()).hexdigest(), self.srt.read_bytes()), before)
        report = json.loads(Path(first["postprocess_report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["original_video"], str(self.source))

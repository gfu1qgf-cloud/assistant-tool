"""Task identity, unattended policies, queue snapshots and subtitle mapping."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from model.BackgroundTasks import TaskRunContext, BackgroundTaskResultError, BackgroundTaskCanceled
from model.node_workflow.contracts import (ResourceRef, VideoTimeline, VideoSection, SubtitleTrack,
                                           SubtitleCue, TaskSnapshot, RenderPlan)
from model.node_workflow.graph import (NodeRegistry, register_core, task_example_graph, validate,
                                       snapshot, build_plan, save_graph, load_graph, GraphError)
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.render import run_task_workflow, _publish
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node, build_caption
from app_plugins.builtin.smart_video_editor.workflow_nodes import edit_node, apply_policy, WorkflowTaskSkipped
from model.node_workflow.filesystem import build_listdir
from model.node_workflow.graph import _task
from app_plugins.builtin.smart_video_editor import engine


class TaskWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "01.mp4"
        self.source.write_bytes(b"synthetic")
        self.fields = {"task_id": "239", "task_type": "口播", "task_audio_text": "Ahoj svet.",
                       "task_audio_voice_id": "synthetic-voice-id", "source_row": 12,
                       "raw": {"未来列": "保留我", "文案": "标题\n正文"}}
        self.original = SimpleNamespace(**copy.deepcopy(self.fields))
        self.task = capture_task({"task": self.original, "target_dir": self.root, "label": "239 | 测试"},
                                 self.root / "tasks.ods", "sk")
        self.settings = engine.normalize_smart_video_editor_settings({"whisper_model_size": "large-v3"})
        self.services = SimpleNamespace(_effective_settings=lambda: copy.deepcopy(self.settings))
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.registry.register("smart_video_editor", edit_node(self.services))
        self.registry.register("batch_text_video", caption_node())
        self.graph = task_example_graph(self.registry)
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel, lambda *_: None, lambda *_: None)
        self.context.task = self.task
        self.context.notes = []
        self.timeline = VideoTimeline((VideoSection(ResourceRef.capture(self.source, "video"), 0, 2),),
                                      320, 240, "25", self.task.key)

    def tearDown(self):
        self.temp.cleanup()

    def plan_without_models(self):
        payload, _ = prepare_job(self.graph, self.registry, self.task, None)
        self.context.prepared = payload["prepared"]
        track = SubtitleTrack((SubtitleCue(.1, 1.5, "Ahoj svet."),), self.task.key, 2)
        with mock.patch.object(engine, "smart_video_jobs_require_model", return_value=False), \
             mock.patch("app_plugins.builtin.smart_video_editor.workflow_nodes.build_edit", return_value={"video": self.timeline, "subtitles": track}):
            # Definitions capture the callable; substitute only this one test's builder.
            from dataclasses import replace
            definition = self.registry.definitions["smart_video_editor.edit"]
            self.registry.definitions[definition.kind] = replace(definition, build=lambda *_: {"video": self.timeline, "subtitles": track})
            return build_plan(payload["graph"], self.registry, self.context)

    def test_complete_row_is_frozen_and_extensions_survive(self):
        self.original.raw["未来列"] = "changed"
        self.original.task_audio_text = "changed"
        self.assertEqual(self.task.fields["raw"]["未来列"], "保留我")
        self.assertEqual(self.task.fields["task_audio_voice_id"], "synthetic-voice-id")
        copied = self.task.fields
        copied["raw"].clear()
        self.assertTrue(self.task.fields["raw"])

    def test_task_graph_has_required_video_and_subtitle_and_no_file_picker_params(self):
        self.assertFalse(validate(self.graph, self.registry)[0])
        self.assertEqual(self.graph["nodes"][0]["kind"], "core.task")
        self.assertEqual([port.key for port in caption_node().inputs if port.required], ['subtitles'])
        self.assertFalse(any(param.kind.endswith("_file") for node in self.graph["nodes"]
                             for param in self.registry.definitions[node["kind"]].parameters))

    def test_preparation_snapshots_settings_but_does_not_search_files(self):
        payload, resources = prepare_job(self.graph, self.registry, self.task, None)
        prepared = next(iter(payload["prepared"].values()))
        self.assertEqual(prepared["settings"]["whisper_model_size"], "large-v3")
        self.assertNotIn("sources", prepared)
        self.assertTrue(resources)
        self.original.task_audio_text = "changed"
        self.assertEqual(json.loads(payload["task"]["fields_json"])["task_audio_text"], "Ahoj svet.")

    def test_task_outputs_are_individual_values_not_task_packet(self):
        outputs = _task({}, {}, self.context)
        self.assertNotIn("task", outputs)
        self.assertEqual(outputs["task_path"].path, str(self.root))
        self.assertEqual(outputs["task_audio_text"], "Ahoj svet.")
        self.assertEqual(outputs["task_id"], "239")
        self.assertTrue(all(not isinstance(value, TaskSnapshot) for value in outputs.values()))
        definition = self.registry.definitions["smart_video_editor.edit"]
        self.assertEqual([port.key for port in definition.inputs if port.required], ["files", "script"])
        self.assertFalse(any(port.data_type == "task" for port in definition.inputs))

    def test_missing_list_or_text_wire_blocks_execution(self):
        editor = next(node for node in self.graph["nodes"] if node["kind"] == "smart_video_editor.edit")
        for key in ("files", "script"):
            graph = copy.deepcopy(self.graph)
            graph["connections"] = [edge for edge in graph["connections"] if edge[2:] != [editor["id"], key]]
            self.assertTrue(any("未连接" in message for _, message in validate(graph, self.registry)[0]))

    def test_generated_results_are_not_reimported_as_sources(self):
        result = self.root / "节点工作流结果"
        result.mkdir()
        (result / "anything.mp4").write_bytes(b"result")
        files = build_listdir({"directory": _task({}, {}, self.context)["task_path"]}, {}, self.context)["files"]
        self.assertEqual([Path(item.path) for item in files.files], [self.source])

    def test_conservative_preserves_uncertain_clip_without_mutating_analysis(self):
        task = {"clips": [{"included": True, "status": "pink", "original_duration": 3,
                           "kept_ranges": [[.5, 2]], "source": str(self.source), "file_name": "01.mp4",
                           "issues": [{"severity": "pink", "detail": "必须人工核对"}]}]}
        notes = []
        result = apply_policy(task, "conservative", notes)
        self.assertEqual(result["clips"][0]["kept_ranges"], [[0, 3]])
        self.assertEqual(task["clips"][0]["kept_ranges"], [[.5, 2]])
        self.assertTrue(notes)
        self.assertNotIn("必须人工核对", notes[0]["detail"])

    def test_automatic_discards_detected_extra_without_manual_approval(self):
        task = {"clips": [{"included": True, "source": str(self.source), "file_name": "01.mp4", "original_duration": 4,
                           "script_word_start": -1, "script_word_end": -1, "recognized_text": "unrelated words go here"}]}
        result = apply_policy(task, "automatic", [])
        self.assertFalse(result["clips"][0]["included"])

    def test_conservative_keeps_proven_repeat_cut_in_uncertain_clip(self):
        task = {"clips": [{"included": True, "status": "pink", "original_duration": 3,
                           "kept_ranges": [[.5, 2]], "repeated_speech_removals": [[1, 1.5]]}]}
        result = apply_policy(task, "conservative", [])
        self.assertEqual(result["clips"][0]["kept_ranges"], [[0, 1], [1.5, 3]])

    def test_preparation_refuses_external_cache_directory(self):
        self.settings["output_folder_name"] = "../external"
        with self.assertRaisesRegex(ValueError, "输入目录"):
            prepare_job(self.graph, self.registry, self.task, None)

    def test_file_solver_cannot_export_task_outside_its_directory(self):
        from model.node_workflow.graph import _render
        from model.node_workflow.contracts import FilterOperation
        operation = FilterOperation("test", self.timeline, "null")
        with self.assertRaisesRegex(GraphError, "该任务目录"):
            _render({"video": self.timeline, "operations": (operation,)},
                    {"output": str(self.root.parent / "outside.mp4")}, self.context)

    def test_probe_retains_encoded_size_but_accounts_for_rotation(self):
        from model.node_workflow.render import probe
        data = {"streams": [{"codec_type": "video", "width": 640, "height": 360,
                             "duration": "2", "side_data_list": [{"rotation": -90}]}]}
        with mock.patch("model.node_workflow.render.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(data).encode())):
            video = probe(self.source, "ffprobe")["video"]
        self.assertEqual((video["width"], video["height"]), (640, 360))
        self.assertEqual((video["display_width"], video["display_height"]), (360, 640))

    def test_cleanup_warning_does_not_relabel_published_result_as_failed(self):
        from model.node_workflow.render import render_directory
        with mock.patch("model.node_workflow.render.shutil.rmtree", side_effect=PermissionError("locked")), \
             mock.patch("model.node_workflow.render.time.sleep"):
            with render_directory(self.root, self.context) as directory:
                self.assertTrue(Path(directory).exists())
        self.assertEqual(self.context.notes[-1]["kind"], "temporary_cleanup")

    def test_skip_is_terminal_outcome_not_wait_for_review(self):
        with self.assertRaises(WorkflowTaskSkipped):
            apply_policy({"missing_blocks": [{"text": "missing"}], "clips": []}, "skip", [])

    def test_skip_result_keeps_notes_and_node_identity(self):
        payload, _ = prepare_job(self.graph, self.registry, self.task, None)
        def stop(_payload, context, **_kwargs):
            context.notes = [{"kind": "missing"}]
            context.failed_node = {"id": "n", "title": "智能剪辑"}
            context.skipped_reason = "按策略跳过"
            raise GraphError("missing")
        with mock.patch("model.node_workflow.render.run_workflow", side_effect=stop):
            result = run_task_workflow(payload, self.context, registry=self.registry)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["failed_node"]["id"], "n")

    def test_actual_failure_returns_reviewable_result_not_fake_success(self):
        payload, _ = prepare_job(self.graph, self.registry, self.task, None)
        with mock.patch("model.node_workflow.render.run_workflow", side_effect=OSError("disk full")):
            with self.assertRaises(BackgroundTaskResultError) as caught:
                run_task_workflow(payload, self.context, registry=self.registry)
        self.assertEqual(caught.exception.result["status"], "failed")
        self.assertEqual(caught.exception.result["paths"], [])

    def test_cancellation_is_not_converted_to_failed_result(self):
        self.cancel.set()
        payload = {"task": asdict(self.task)}
        with mock.patch("model.node_workflow.render.run_workflow", side_effect=BackgroundTaskCanceled("stop")):
            with self.assertRaises(BackgroundTaskCanceled):
                run_task_workflow(payload, self.context, registry=self.registry)

    def test_cross_task_subtitles_are_rejected(self):
        track = SubtitleTrack((SubtitleCue(0, 1, "test"),), "another-task", 2)
        operations = build_caption({"subtitles": track}, {}, self.context)["operations"]
        from model.node_workflow.basic import settle
        with self.assertRaisesRegex(ValueError, "不同任务"):
            settle({"video": self.timeline, "operations": (operations,)}, {}, self.context)

    def test_timing_mismatch_is_rejected(self):
        track = SubtitleTrack((SubtitleCue(0, 1, "test"),), self.task.key, 3)
        operations = build_caption({"subtitles": track}, {}, self.context)["operations"]
        from model.node_workflow.basic import settle
        with self.assertRaisesRegex(ValueError, "时间线"):
            settle({"video": self.timeline, "operations": (operations,)}, {}, self.context)

    def test_task_output_is_automatic_versioned_and_deferred(self):
        plan = self.plan_without_models()
        self.assertIsInstance(plan, RenderPlan)
        self.assertEqual(Path(plan.output).parent, self.root / "节点工作流结果")
        self.assertFalse(Path(plan.output).exists())
        Path(plan.output).parent.mkdir()
        Path(plan.output).write_bytes(b"existing")
        second = self.plan_without_models()
        self.assertNotEqual(second.output, plan.output)
        self.assertEqual(Path(plan.output).read_bytes(), b"existing")

    def test_graph_save_does_not_persist_runtime_task_or_live_services(self):
        path = self.root / "flow.json"
        save_graph(path, self.graph)
        saved = path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-voice-id", saved)
        self.assertNotIn("fields_json", saved)
        self.assertEqual(load_graph(path), self.graph)

    def test_multi_file_publication_failure_rolls_back_only_own_outputs(self):
        temp_video, temp_srt = self.root / "tmp.mp4", self.root / "tmp.srt"
        temp_video.write_bytes(b"new")
        temp_srt.write_bytes(b"new")
        old_srt = self.root / "final.srt"
        old_srt.write_bytes(b"old")
        with self.assertRaises(OSError):
            _publish([(temp_video, self.root / "final.mp4"), (temp_srt, old_srt)], self.context)
        self.assertFalse((self.root / "final.mp4").exists())
        self.assertEqual(old_srt.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()

"""Workflow scope is persisted, explicit, and independent of node guessing."""
import copy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
from app_plugins.builtin.smart_video_editor.workflow_nodes import edit_node, build_edit
from app_plugins.builtin.smart_video_editor import engine
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import NodeDefinition, NodePort, TaskSnapshot, DirectoryRef, FileList, ResourceRef
from model.node_workflow.graph import (NodeRegistry, register_core, new_node, empty_graph, check_document,
    validate, evaluate_graph, save_graph, load_graph, task_example_graph, example_graph, export_example_graph)
from model.node_workflow.scope import execution_mode, check_execution_task, initialize_execution
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.preview import run_preview


class ExecutionModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        service = SimpleNamespace(_effective_settings=lambda: engine.normalize_smart_video_editor_settings({}))
        self.registry.register("smart_video_editor", edit_node(service))
        self.registry.register("batch_text_video", caption_node())
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.tasks = [capture_task({"task": {"task_id": str(index), "task_audio_text": f"文案 {index}"},
            "target_dir": str(self.root / str(index)), "label": str(index)}, language="sk") for index in (1, 2)]

    def preview_graph(self, mode="detail"):
        source = new_node(self.registry.definitions["core.number"])
        preview = new_node(self.registry.definitions["core.preview"])
        graph = empty_graph()
        graph.update(execution_mode=mode, nodes=[source, preview],
                     connections=[[source["id"], "number", preview["id"], "value"]])
        return graph, preview["id"]

    def test_examples_have_explicit_scope(self):
        self.assertEqual(execution_mode(empty_graph()), "detail")
        for graph in (example_graph(self.registry), export_example_graph(self.registry)):
            self.assertEqual(graph["execution_mode"], "detail")
        for graph in (task_example_graph(self.registry), export_example_graph(self.registry, True)):
            self.assertEqual(graph["execution_mode"], "line")

    def test_export_example_does_not_modify_reused_source_graph(self):
        for task_mode, factory in ((False, example_graph), (True, task_example_graph)):
            with self.subTest(task_mode=task_mode):
                original = factory(self.registry)
                snapshot = copy.deepcopy(original)
                name = 'task_example_graph' if task_mode else 'example_graph'
                with mock.patch('model.node_workflow.graph.' + name, return_value=original):
                    exported = export_example_graph(self.registry, task_mode)
                self.assertEqual(original, snapshot)
                self.assertIsNot(exported['connections'], original['connections'])
                self.assertFalse(any(edge[2] == snapshot['output_node'] for edge in exported['connections']))

    def test_invalid_mode_is_rejected_not_guessed(self):
        for value in (None, [], 1, "batch", "Detail", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_document({**empty_graph(), "execution_mode": value})

    def test_modes_roundtrip_without_saving_runtime_rows(self):
        for mode in ("detail", "line"):
            graph, _root = self.preview_graph(mode)
            path = self.root / f"{mode}.json"
            save_graph(path, graph)
            self.assertEqual(load_graph(path), graph)
            self.assertNotIn("fields_json", path.read_text(encoding="utf-8"))

    def test_detail_rejects_active_row_entry_but_not_unused_nodes(self):
        task = new_node(self.registry.definitions["core.task"])
        graph, root = self.preview_graph()
        graph["nodes"].append(task)
        self.assertFalse(validate(graph, self.registry, root_id=root)[0])
        errors = validate(graph, self.registry, root_id=task["id"])[0]
        self.assertTrue(any("仅适用于 Line" in message for _, message in errors))
        graph["execution_mode"] = "line"
        self.assertFalse(validate(graph, self.registry, root_id=task["id"])[0])

    def test_plugin_nodes_can_declare_detail_or_line_scope(self):
        for mode in ("detail", "line"):
            definition = NodeDefinition(f"test.{mode}", mode, outputs=(NodePort("number", "数值", "number"),),
                execution_scope=mode, build=lambda *_: {"number": 1})
            self.registry.register("test", definition)
            graph = empty_graph()
            node = new_node(definition)
            graph["nodes"] = [node]
            graph["execution_mode"] = mode
            self.assertFalse(validate(graph, self.registry, root_id=node["id"])[0])
            graph["execution_mode"] = "line" if mode == "detail" else "detail"
            self.assertTrue(validate(graph, self.registry, root_id=node["id"])[0])

    def test_invalid_plugin_scope_is_rejected(self):
        base = NodeDefinition("test.value", "test", build=lambda *_: {})
        for scope in (None, [], "all", 1):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.registry.register("test", replace(base, execution_scope=scope))

    def test_detail_preparation_cannot_silently_consume_a_bound_row(self):
        graph, root = self.preview_graph()
        with self.assertRaisesRegex(ValueError, "不接受当前任务行"):
            prepare_job(graph, self.registry, self.tasks[0], None, root_id=root)
        payload, _resources = prepare_job(graph, self.registry, None, None, root_id=root)
        self.assertIsNone(payload["task"])

    def test_line_requires_row_even_when_graph_has_no_task_entry(self):
        graph, root = self.preview_graph("line")
        with self.assertRaisesRegex(ValueError, "一行任务数据"):
            prepare_job(graph, self.registry, None, None, root_id=root)
        for task in self.tasks:
            payload, _resources = prepare_job(graph, self.registry, task, None, root_id=root)
            self.assertEqual(payload["task"]["key"], task.key)

    def test_mode_and_row_are_frozen_at_submission(self):
        graph, root = self.preview_graph("line")
        payload, _resources = prepare_job(graph, self.registry, self.tasks[0], None, root_id=root)
        graph["execution_mode"] = "detail"
        initialize_execution(payload, self.context)
        self.assertEqual(self.context.workflow_mode, "line")
        self.assertEqual(self.context.task.fields["task_id"], "1")

    def test_detail_runtime_does_not_reuse_previous_line_context(self):
        graph, root = self.preview_graph()
        self.context.task = self.tasks[0]
        self.context.workflow_mode = "line"
        payload, _resources = prepare_job(graph, self.registry, None, None, root_id=root)
        initialize_execution(payload, self.context)
        self.assertEqual(self.context.workflow_mode, "detail")
        self.assertIsNone(self.context.task)

    def test_runtime_checks_missing_line_row_and_wrong_detail_row(self):
        with self.assertRaisesRegex(ValueError, "一行任务数据"):
            initialize_execution({"graph": {**empty_graph(), "execution_mode": "line"}}, self.context)
        with self.assertRaisesRegex(ValueError, "不接受当前任务行"):
            initialize_execution({"graph": empty_graph(), "task": asdict(self.tasks[0])}, self.context)

    def test_two_line_jobs_do_not_share_current_row(self):
        graph, root = self.preview_graph("line")
        contexts = [SimpleNamespace(), SimpleNamespace()]
        for task, context in zip(self.tasks, contexts):
            payload, _resources = prepare_job(graph, self.registry, task, None, root_id=root)
            initialize_execution(payload, context)
        self.assertEqual([context.task.fields["task_id"] for context in contexts], ["1", "2"])
        changed = contexts[0].task.fields
        changed["task_id"] = "edited"
        self.assertEqual(contexts[1].task.fields["task_id"], "2")

    def test_detail_preview_does_not_require_ffmpeg_for_plain_values(self):
        graph, root = self.preview_graph()
        graph["nodes"][0]["params"]["value"] = 239
        payload, _resources = prepare_job(graph, self.registry, None, None, root_id=root)
        payload["preview_node"] = root
        result = run_preview(payload, self.context, registry=self.registry)
        self.assertIn("239", result["text"])
        self.assertEqual(result["task_label"], "")
        self.assertEqual(self.context.workflow_mode, "detail")

    def test_line_preview_has_only_its_current_task(self):
        graph, root = self.preview_graph("line")
        for task in self.tasks:
            payload, _resources = prepare_job(graph, self.registry, task, None, root_id=root)
            payload["preview_node"] = root
            result = run_preview(payload, self.context, registry=self.registry)
            self.assertEqual(result["task_label"], task.label)

    def test_evaluator_exposes_explicit_mode_to_plugins(self):
        definition = NodeDefinition("test.mode", "mode", outputs=(NodePort("text", "模式", "text"),),
            build=lambda _i, _p, context: {"text": context.workflow_mode})
        self.registry.register("test", definition)
        node = new_node(definition)
        for mode in ("detail", "line"):
            graph = {**empty_graph(), "execution_mode": mode, "nodes": [node]}
            self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=node["id"])["text"], mode)

    def test_smart_edit_optional_inputs_reach_job_and_have_distinct_roles(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"synthetic")
        files = FileList(DirectoryRef(str(self.root)), (ResourceRef.capture(source, "video"),))
        self.context.node_prepared = {"settings": engine.normalize_smart_video_editor_settings({})}
        self.context.ffmpeg = "ffmpeg"
        self.context.notes = []
        def inspect_job(jobs, *_args):
            self.assertEqual(jobs[0]["language"], "sk")
            self.assertEqual(jobs[0]["task_id"], "104")
            self.assertEqual(jobs[0]["task_name"], "测试名称")
            self.assertEqual(jobs[0]["script"], "Ahoj svet.")
            raise RuntimeError("verified job inputs")
        with mock.patch.object(engine, "smart_video_jobs_require_model", return_value=False), \
             mock.patch.object(engine, "analyze_smart_video_jobs", side_effect=inspect_job):
            with self.assertRaisesRegex(RuntimeError, "verified job inputs"):
                build_edit({"files": files, "script": "Ahoj svet.", "language": "sk", "task_id": "104", "task_name": "测试名称"},
                           {"language": "en"}, self.context)


if __name__ == "__main__":
    unittest.main()

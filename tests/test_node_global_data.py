"""Loop position is batch-local, not source row or completion order."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from model.node_workflow.contracts import NodeDefinition, NodePort, DirectoryRef, FileList, ResourceRef
from model.node_workflow.global_data import WorkflowGlobals, GLOBAL_OUTPUTS, restore_globals
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, evaluate_graph, save_graph, load_graph
from model.node_workflow.preview import run_preview
from model.node_workflow.scope import initialize_execution
from model.node_workflow.tasks import capture_task, prepare_job


class GlobalDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.tasks = [capture_task({"task": {"task_id": str(row), "source_row": row},
            "target_dir": str(self.root / str(row)), "label": str(row)}) for row in (20, 35, 80)]

    def graph(self, mode="line"):
        node = new_node(self.registry.definitions["core.global_data"])
        return {**empty_graph(), "execution_mode": mode, "nodes": [node]}, node["id"]

    def evaluate(self, graph, root, task, value):
        payload, _ = prepare_job(graph, self.registry, task, None, root_id=root, global_data=value)
        context = SimpleNamespace(check_canceled=lambda: None, report=lambda *_: None)
        initialize_execution(payload, context)
        return evaluate_graph(payload["graph"], self.registry, context, root_id=root)

    def test_node_has_separate_typed_outputs_and_no_row_number_port(self):
        definition = self.registry.definitions["core.global_data"]
        self.assertEqual(definition.title, "全局数据")
        self.assertEqual(definition.inputs, ())
        self.assertEqual([(p.key, p.title, p.data_type) for p in definition.outputs], list(GLOBAL_OUTPUTS))
        self.assertNotIn("source_row", [p.key for p in definition.outputs])

    def test_loop_is_1_2_3_even_when_source_rows_are_20_35_80(self):
        graph, root = self.graph()
        batch = WorkflowGlobals.create("line", 3)
        values = [self.evaluate(graph, root, task, batch.at_iteration(index))
                  for index, task in enumerate(self.tasks, 1)]
        self.assertEqual([value["iteration"] for value in values], [1, 2, 3])
        self.assertEqual([value["iteration_index"] for value in values], [0, 1, 2])
        self.assertEqual([value["iteration_count"] for value in values], [3, 3, 3])
        self.assertEqual([value["is_first"] for value in values], [True, False, False])
        self.assertEqual([value["is_last"] for value in values], [False, False, True])
        self.assertEqual(len({value["batch_id"] for value in values}), 1)
        self.assertEqual(len({value["started_at"] for value in values}), 1)

    def test_reversed_concurrent_execution_does_not_renumber(self):
        graph, root = self.graph()
        batch = WorkflowGlobals.create("line", 3)
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(self.evaluate, graph, root, self.tasks[index - 1], batch.at_iteration(index))
                       for index in (3, 1, 2)]
            self.assertEqual([future.result()["iteration"] for future in futures], [3, 1, 2])

    def test_skipped_iteration_does_not_shift_later_values(self):
        graph, root = self.graph()
        batch = WorkflowGlobals.create("line", 3)
        self.assertEqual(self.evaluate(graph, root, self.tasks[2], batch.at_iteration(3))["iteration"], 3)

    def test_new_batch_resets_iteration_and_has_new_identity(self):
        a, b = WorkflowGlobals.create("line", 3), WorkflowGlobals.create("line", 3)
        self.assertEqual((a.iteration, b.iteration), (1, 1))
        self.assertNotEqual(a.batch_id, b.batch_id)

    def test_detail_is_one_iteration_without_task(self):
        graph, root = self.graph("detail")
        values = self.evaluate(graph, root, None, WorkflowGlobals.create("detail"))
        self.assertEqual(values["iteration"], 1)
        self.assertEqual(values["iteration_count"], 1)
        self.assertTrue(values["is_first"] and values["is_last"])
        self.assertEqual(values["mode"], "detail")

    def test_snapshots_and_returned_values_cannot_mutate_other_iterations(self):
        batch = WorkflowGlobals.create("line", 3)
        first, last = batch.at_iteration(1), batch.at_iteration(3)
        with self.assertRaises(FrozenInstanceError):
            first.iteration = 9
        altered = first.values()
        altered["iteration"] = 99
        self.assertEqual(first.iteration, 1)
        self.assertEqual(last.iteration, 3)

    def test_invalid_counter_mode_time_and_flags_are_rejected(self):
        batch = WorkflowGlobals.create("line", 3)
        invalid = ({"iteration": 0}, {"iteration": 4}, {"iteration": True}, {"iteration": 1.0},
                   {"iteration_count": 0}, {"iteration_count": False}, {"mode": []},
                   {"mode": "detail"}, {"started_at": "2026-10-10T22:00:00"},
                   {"started_at": "invalid"}, {"batch_id": "invalid"}, {"is_preview": 1})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(batch, **changes)

    def test_payload_is_plain_frozen_and_mode_checked_before_execution(self):
        graph, root = self.graph()
        batch = WorkflowGlobals.create("line", 3).at_iteration(2)
        payload, _ = prepare_job(graph, self.registry, self.tasks[1], None, root_id=root, global_data=batch)
        self.assertEqual(json.loads(json.dumps(payload))["global_data"], asdict(batch))
        with self.assertRaisesRegex(ValueError, "执行模式不一致"):
            prepare_job(graph, self.registry, self.tasks[1], None, root_id=root, global_data=WorkflowGlobals.create("detail"))
        payload["global_data"]["iteration"] = 3
        self.assertEqual(batch.iteration, 2)
        with self.assertRaisesRegex(ValueError, "执行模式不一致"):
            restore_globals(asdict(batch), "detail")

    def test_runtime_context_does_not_reuse_previous_batch(self):
        graph, _ = self.graph("detail")
        context = SimpleNamespace(workflow_globals=WorkflowGlobals.create("line", 3).at_iteration(3))
        initialize_execution({"graph": graph}, context)
        self.assertEqual(context.workflow_globals.mode, "detail")
        self.assertEqual(context.workflow_globals.iteration, 1)

    def test_preview_reads_iteration_without_consuming_it(self):
        graph, root = self.graph()
        batch = WorkflowGlobals.create("line", 3).at_iteration(2)
        payload, _ = prepare_job(graph, self.registry, self.tasks[1], None, root_id=root, global_data=batch)
        payload["preview_node"] = root
        context = SimpleNamespace(check_canceled=lambda: None, report=lambda *_: None)
        result = run_preview(payload, context, registry=self.registry)
        self.assertIn("当前循环次数 · 从 1 开始\n2", result["text"])
        self.assertTrue(context.workflow_globals.is_preview)
        self.assertFalse(batch.is_preview)
        initialize_execution({key: value for key, value in payload.items() if key != "preview_node"}, context)
        self.assertFalse(context.workflow_globals.is_preview)
        self.assertEqual(context.workflow_globals.iteration, 2)

    def test_repeated_global_nodes_do_not_advance_the_loop(self):
        graph, first = self.graph()
        second = new_node(self.registry.definitions["core.global_data"])
        addition = new_node(self.registry.definitions["core.arithmetic"])
        graph["nodes"] += [second, addition]
        graph["connections"] = [[first, "iteration", addition["id"], "a"],
                                [second["id"], "iteration", addition["id"], "b"]]
        result = self.evaluate(graph, addition["id"], self.tasks[1], WorkflowGlobals.create("line", 3).at_iteration(2))
        self.assertEqual(result["number"], 4)

    def test_loop_index_can_drive_resource_rotation_with_existing_math_nodes(self):
        paths = [self.root / f"{index}.mp4" for index in (1, 2)]
        for path in paths:
            path.write_bytes(b"synthetic")
        files = FileList(DirectoryRef(str(self.root)), tuple(ResourceRef.capture(path, "video") for path in paths))
        source = NodeDefinition("test.files", "files", outputs=(NodePort("files", "files", "files"),),
                                build=lambda *_: {"files": files})
        self.registry.register("test", source)
        graph, loop = self.graph()
        pool, size, one, modulo, addition, pick = [new_node(self.registry.definitions[kind]) for kind in
            ("test.files", "core.number", "core.number", "core.arithmetic", "core.arithmetic", "core.list_video")]
        size["params"]["value"], one["params"]["value"] = 2, 1
        modulo["params"]["operation"] = "modulo"
        graph["nodes"] += [pool, size, one, modulo, addition, pick]
        graph["connections"] = [[loop, "iteration_index", modulo["id"], "a"], [size["id"], "number", modulo["id"], "b"],
            [modulo["id"], "number", addition["id"], "a"], [one["id"], "number", addition["id"], "b"],
            [addition["id"], "number", pick["id"], "index"], [pool["id"], "files", pick["id"], "files"]]
        batch = WorkflowGlobals.create("line", 3)
        outputs = [self.evaluate(graph, pick["id"], task, batch.at_iteration(index))["video"].path
                   for index, task in enumerate(self.tasks, 1)]
        self.assertEqual(outputs, [str(paths[0]), str(paths[1]), str(paths[0])])

    def test_workflow_saves_node_not_runtime_batch_data(self):
        graph, _ = self.graph()
        path = self.root / "flow.json"
        save_graph(path, graph)
        self.assertEqual(load_graph(path), graph)
        self.assertNotIn("started_at", path.read_text(encoding="utf-8"))
        self.assertNotIn("global_data\":", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

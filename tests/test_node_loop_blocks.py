"""Structured loops really re-evaluate their bodies, without feedback wires."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import NodeDefinition, NodePort, FilterOperation, ResourceRef
from model.node_workflow.graph import (NodeRegistry, register_core, empty_graph, new_node,
    evaluate_graph, validate, save_graph, load_graph, GraphError)
from model.node_workflow.loops import (BEGIN, END, LoopResults, add_loop_pair, loop_example_graph)
from model.node_workflow.preview import describe, run_data_workflow
from model.node_workflow.typing import resolve_port_types
from model.node_workflow.python_values import PythonValue


class LoopBlockTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel, lambda *_: None, lambda *_: None)
        self.context.prepared = {}
        self.graph = empty_graph()

    def node(self, kind, **params):
        node = new_node(self.registry.definitions[kind])
        node["params"].update(params)
        self.graph["nodes"].append(node)
        return node

    def wire(self, source, output, target, input_):
        self.graph["connections"].append([source["id"], output, target["id"], input_])

    def source(self, dtype, value, counter=None):
        kind = "test.source_" + str(len(self.registry.definitions))
        def build(*_args):
            if counter is not None:
                counter.append(1)
            return {"value": copy.deepcopy(value)}
        self.registry.register("test", NodeDefinition(kind, kind,
            outputs=(NodePort("value", "值", dtype),), build=build))
        return self.node(kind)

    def pair(self, mode="count", count=3):
        begin, end = add_loop_pair(self.graph, self.registry, mode=mode)
        end["params"]["count"] = count
        return begin, end

    def result(self, node, normal=False):
        if normal:
            self.graph["output_node"] = node["id"]
        return evaluate_graph(self.graph, self.registry, self.context,
                              root_id=None if normal else node["id"])

    def finish_data(self, end):
        listing = self.node("core.loop_list")
        box = self.node("core.py_data")
        output = self.node("core.py_output")
        self.wire(end, "results", listing, "results")
        self.wire(listing, "items", box, "value")
        self.wire(box, "value", output, "value")
        self.graph["output_node"] = output["id"]
        return output

    def test_three_runnable_examples(self):
        expected = {"count": [1, 4, 9, 16, 25], "foreach": ["图片一", "图片二", "图片三"], "while": [1, 2, 3, 4, 5]}
        for mode, values in expected.items():
            with self.subTest(mode=mode):
                self.graph = loop_example_graph(self.registry, mode)
                self.assertEqual(validate(self.graph, self.registry)[0], [])
                outputs = evaluate_graph(self.graph, self.registry, self.context)
                self.assertEqual(outputs["value"].value, values)

    def test_start_step_and_both_indices(self):
        begin, end = self.pair(count=3)
        begin["params"].update(start=5, step=-2)
        self.wire(begin, "number", end, "result")
        self.assertEqual(self.result(end)["results"].to_list(), [5, 3, 1])
        self.graph["connections"][-1][1] = "iteration"
        self.assertEqual(self.result(end)["results"].to_list(), [0, 1, 2])
        self.graph["connections"][-1][1] = "index"
        self.assertEqual(self.result(end)["results"].to_list(), [1, 2, 3])

    def test_zero_iterations(self):
        begin, end = self.pair(count=0)
        self.wire(begin, "number", end, "result")
        result = self.result(end)
        self.assertEqual(result["iterations"], 0)
        self.assertIsNone(result["last"])
        self.assertEqual(result["results"].to_list(), [])

    def test_zero_feedback_preserves_initial(self):
        begin, end = self.pair(count=0)
        begin["params"]["feedback"] = "yes"
        initial = self.source("text", "原文")
        self.wire(initial, "value", begin, "initial")
        self.wire(begin, "value", end, "result")
        self.assertEqual(self.result(end)["last"], "原文")

    def test_foreach_empty_nested_null(self):
        for items in ([], [None, {"a": [1, 2]}, "中文"]):
            with self.subTest(items=items):
                self.graph = empty_graph()
                begin, end = self.pair("foreach")
                initial = self.source("list", items)
                self.wire(initial, "value", begin, "items")
                self.wire(begin, "item", end, "result")
                self.assertEqual(self.result(end)["results"].to_list(), items)

    def test_foreach_missing_input(self):
        begin, end = self.pair("foreach")
        self.wire(begin, "item", end, "result")
        with self.assertRaisesRegex(GraphError, "列表"):
            self.result(end)

    def test_while_requires_stop(self):
        begin, end = self.pair("while")
        self.wire(begin, "number", end, "result")
        with self.assertRaisesRegex(GraphError, "停止条件"):
            self.result(end)

    def test_while_postcondition_and_limit(self):
        begin, end = self.pair("while")
        stop = self.node("core.boolean", value="true")
        self.wire(begin, "index", end, "result")
        self.wire(stop, "boolean", end, "stop")
        self.assertEqual(self.result(end)["results"].to_list(), [1])
        stop["params"]["value"] = "false"
        end["params"]["limit"] = 3
        with self.assertRaisesRegex(GraphError, "停止条件始终为假.*3"):
            self.result(end)

    def test_count_cap_does_not_silently_truncate(self):
        begin, end = self.pair(count=4)
        end["params"]["limit"] = 3
        self.wire(begin, "number", end, "result")
        with self.assertRaisesRegex(GraphError, "超过安全上限"):
            self.result(end)

    def test_fractional_count_rejected(self):
        begin, end = self.pair()
        count = self.source("number", 2.5)
        self.wire(count, "value", end, "count")
        self.wire(begin, "number", end, "result")
        with self.assertRaisesRegex(GraphError, "整数"):
            self.result(end)

    def test_count_input_is_outside_block(self):
        begin, end = self.pair()
        self.wire(begin, "index", end, "count")
        self.wire(begin, "number", end, "result")
        with self.assertRaisesRegex(GraphError, "次数输入必须来自循环块外"):
            self.result(end)

    def test_seed_required_only_when_value_used(self):
        begin, end = self.pair()
        self.wire(begin, "value", end, "result")
        with self.assertRaisesRegex(GraphError, "初始数据"):
            self.result(end)

    def test_implicit_feedback_type_is_checked(self):
        begin, end = self.pair()
        begin["params"]["feedback"] = "yes"
        initial = self.source("text", "a")
        self.wire(initial, "value", begin, "initial")
        self.wire(begin, "index", end, "result")
        errors = validate(self.graph, self.registry, root_id=end["id"])[0]
        self.assertTrue(any("同类型组" in text for _node, text in errors))

    def test_independent_types_without_feedback(self):
        begin, end = self.pair()
        initial = self.source("text", "a")
        self.wire(initial, "value", begin, "initial")
        self.wire(begin, "index", end, "result")
        self.assertEqual(self.result(end)["last"], 3)

    def test_shared_static_node_builds_once(self):
        begin, end = self.pair(count=5)
        calls = []
        initial = self.source("number", 10, calls)
        addition = self.node("core.arithmetic")
        self.wire(begin, "index", addition, "a")
        self.wire(initial, "value", addition, "b")
        self.wire(addition, "number", end, "result")
        self.assertEqual(self.result(end)["results"].to_list(), [11, 12, 13, 14, 15])
        self.assertEqual(len(calls), 1)
        self.result(end)
        self.assertEqual(len(calls), 2)  # New job, new cache.

    def test_nested_loops_and_correct_cache_scope(self):
        outer, outer_end = self.pair(count=2)
        inner, inner_end = self.pair(count=3)
        add = self.node("core.arithmetic")
        self.wire(outer, "number", add, "a")
        self.wire(inner, "number", add, "b")
        self.wire(add, "number", inner_end, "result")
        listing = self.node("core.loop_list")
        self.wire(inner_end, "results", listing, "results")
        self.wire(listing, "items", outer_end, "result")
        self.assertEqual(self.result(outer_end)["results"].to_list(), [[0, 1, 2], [1, 2, 3]])

    def test_crossing_loops_are_rejected(self):
        a, a_end = self.pair()
        b, b_end = self.pair()
        add = self.node("core.arithmetic")
        self.wire(a, "number", add, "a")
        self.wire(b, "number", add, "b")
        self.wire(add, "number", a_end, "result")
        self.wire(add, "number", b_end, "result")
        with self.assertRaisesRegex(GraphError, "交叉引用"):
            self.result(a_end)

    def test_end_pair_and_duplicate_ends(self):
        begin, end = self.pair()
        self.wire(begin, "number", end, "result")
        other = self.node(END)
        self.wire(begin, "block", other, "block")
        self.wire(begin, "number", other, "result")
        with self.assertRaisesRegex(GraphError, "多个 End|一个 End"):
            self.result(end)

    def test_unpaired_begin_reports_helpful_error(self):
        begin = self.node(BEGIN)
        preview = self.node("core.preview")
        self.wire(begin, "number", preview, "value")
        with self.assertRaisesRegex(GraphError, "没有配对"):
            self.result(preview)

    def test_internal_data_cannot_escape_end(self):
        begin, end = self.pair()
        self.wire(begin, "number", end, "result")
        box = self.node("core.py_data")
        output = self.node("core.py_output")
        self.wire(begin, "index", box, "value")
        self.wire(box, "value", output, "value")
        with self.assertRaisesRegex(GraphError, "越过了 End"):
            self.result(output, normal=True)

    def test_internal_preview_uses_last_real_pass(self):
        begin, end = self.pair(count=4)
        self.wire(begin, "index", end, "result")
        preview = self.node("core.preview")
        self.wire(begin, "index", preview, "value")
        self.assertEqual(self.result(preview)["value"], 4)
        end["params"]["count"] = 0
        with self.assertRaisesRegex(ValueError, "0 次"):
            self.result(preview)

    def test_rerouted_block_link(self):
        begin, end = self.pair()
        self.graph["connections"].clear()
        route = self.node("core.reroute")
        self.wire(begin, "block", route, "value")
        self.wire(route, "value", end, "block")
        self.wire(begin, "index", end, "result")
        self.assertEqual(self.result(end)["iterations"], 3)

    def test_cancellation_propagates_not_failure(self):
        begin, end = self.pair(count=20)
        calls = []
        def body(inputs, _params, _context):
            calls.append(inputs["index"])
            if len(calls) == 2:
                self.cancel.set()
            return {"value": inputs["index"]}
        self.registry.register("test", NodeDefinition("test.cancel", "取消测试",
            inputs=(NodePort("index", "次数", "number"),), outputs=(NodePort("value", "值", "number"),), build=body))
        node = self.node("test.cancel")
        self.wire(begin, "index", node, "index")
        self.wire(node, "value", end, "result")
        with self.assertRaises(BackgroundTaskCanceled):
            self.result(end)
        self.assertEqual(calls, [1, 2])

    def test_global_node_execution_budget(self):
        begin, end = self.pair(count=3)
        self.wire(begin, "index", end, "result")
        with patch("model.node_workflow.evaluation.MAX_NODE_BUILDS", 2):
            with self.assertRaisesRegex(GraphError, "节点执行"):
                self.result(end)

    def test_collect_budget_and_last_mode(self):
        begin, end = self.pair(count=4)
        text = self.source("text", "abc")
        self.wire(text, "value", end, "result")
        with patch("model.node_workflow.evaluation.MAX_COLLECT_BYTES", 5):
            with self.assertRaisesRegex(GraphError, "收集结果超过"):
                self.result(end)
            end["params"]["gather"] = "last"
            result = self.result(end)
            self.assertEqual(result["iterations"], 4)
            self.assertEqual(result["results"].to_list(), ["abc"])

    def test_media_collection_no_encoding_or_buffer_copy(self):
        begin, end = self.pair(count=3)
        original = ResourceRef("C:/unchanged/source.mp4", "video", 123, 456)
        source = self.source("video", original)
        self.wire(source, "value", begin, "initial")
        self.wire(begin, "value", end, "result")
        result = self.result(end)
        self.assertEqual(result["last"], original)
        self.assertIs(result["results"].items[0], result["results"].items[1])
        with self.assertRaises(TypeError):
            result["results"].to_list()

    def test_operations_reuse_and_merge_order(self):
        begin, end = self.pair(count=3)
        def operations(inputs, _params, _context):
            return {"operations": (FilterOperation("第" + str(inputs["index"]), None, "test.operation"),)}
        self.registry.register("test", NodeDefinition("test.ops", "操作",
            inputs=(NodePort("index", "次数", "number"),), outputs=(NodePort("operations", "操作", "operations"),), build=operations))
        op = self.node("test.ops")
        merge = self.node("core.loop_operations")
        self.wire(begin, "index", op, "index")
        self.wire(op, "operations", end, "result")
        self.wire(end, "results", merge, "results")
        self.assertEqual([item.title for item in self.result(merge)["operations"]], ["第1", "第2", "第3"])

    def test_roundtrip_and_data_runner(self):
        self.graph = loop_example_graph(self.registry)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.json"
            save_graph(path, self.graph)
            self.graph = load_graph(path)
        result = run_data_workflow({"graph": self.graph, "prepared": {}}, self.context, registry=self.registry)
        self.assertEqual(result["data_json"], "[1,4,9,16,25]")

    def test_collection_preview_is_readable(self):
        text = describe(LoopResults("number", (1, 2, 3)))
        self.assertIn("3 项", text)
        self.assertIn("2. 2", text)

    def test_nested_collections_convert_to_python_data(self):
        values = LoopResults("loop_results", (LoopResults("number", (1, 2)), LoopResults("text", ("a",))))
        self.assertEqual(values.to_list(), [[1, 2], ["a"]])

    def test_connected_missing_control_inputs_do_not_fall_back(self):
        begin, end = self.pair()
        self.wire(begin, "index", end, "result")
        count = self.source("number", None)
        self.wire(count, "value", end, "count")
        with self.assertRaisesRegex(GraphError, "未改用默认次数"):
            self.result(end)
        self.graph["connections"].remove([count["id"], "value", end["id"], "count"])
        stop = self.source("boolean", None)
        self.wire(stop, "value", end, "stop")
        with self.assertRaisesRegex(GraphError, "停止条件已连接"):
            self.result(end)

    def test_line_scope_keeps_global_task_counter_and_own_iteration(self):
        from model.node_workflow.global_data import WorkflowGlobals
        self.graph["execution_mode"] = "line"
        self.context.workflow_globals = WorkflowGlobals.create("line", 10).at_iteration(7)
        begin, end = self.pair(count=2)
        global_node = self.node("core.global_data")
        add = self.node("core.arithmetic")
        self.wire(global_node, "iteration", add, "a")
        self.wire(begin, "index", add, "b")
        self.wire(add, "number", end, "result")
        self.assertEqual(self.result(end)["results"].to_list(), [8, 9])
        self.assertEqual(self.context.workflow_globals.iteration, 7)

    def test_last_mode_keeps_exact_final_mutable_value_without_aliasing(self):
        begin, end = self.pair(count=3)
        end["params"]["gather"] = "last"
        lists = self.node("core.py_list_create")
        self.wire(begin, "index", lists, "values")
        self.wire(lists, "items", end, "result")
        result = self.result(end)
        self.assertEqual(result["last"], [3])
        result["last"].append(99)
        self.assertEqual(result["results"].to_list(), [[3]])

    def test_nested_internal_preview_uses_matching_final_scopes(self):
        outer, outer_end = self.pair(count=2)
        inner, inner_end = self.pair(count=3)
        add = self.node("core.arithmetic")
        self.wire(outer, "number", add, "a")
        self.wire(inner, "number", add, "b")
        self.wire(add, "number", inner_end, "result")
        self.wire(inner_end, "last", outer_end, "result")
        preview = self.node("core.preview")
        self.wire(add, "number", preview, "value")
        self.assertEqual(self.result(preview)["value"], 3)

    def test_preparation_is_once_per_job_and_reused_by_each_iteration(self):
        from types import SimpleNamespace
        from model.node_workflow.tasks import prepare_job
        calls, builds = [], []
        def prepare(_task, _params, _services):
            calls.append(1)
            return {"offset": 10, "resources": ["synthetic-loop-lock"]}
        def build(inputs, _params, context):
            builds.append(inputs["index"])
            return {"number": inputs["index"] + context.node_prepared["offset"]}
        self.registry.register("test", NodeDefinition("test.prepared", "准备结果测试",
            inputs=(NodePort("index", "序号", "number"),), outputs=(NodePort("number", "值", "number"),),
            build=build, prepare=prepare))
        begin, end = self.pair()
        body = self.node("test.prepared")
        self.wire(begin, "index", body, "index")
        self.wire(body, "number", end, "result")
        self.finish_data(end)
        payload, resources = prepare_job(self.graph, self.registry, None, SimpleNamespace())
        output = run_data_workflow(payload, self.context, registry=self.registry)
        self.assertEqual(output["data_json"], "[11,12,13]")
        self.assertEqual(calls, [1])
        self.assertEqual(builds, [1, 2, 3])
        self.assertIn("synthetic-loop-lock", resources)

    def test_failure_preserves_exact_body_node_location(self):
        begin, end = self.pair()
        zero = self.node("core.number", value=0)
        division = self.node("core.arithmetic", operation="divide")
        self.wire(begin, "index", division, "a")
        self.wire(zero, "number", division, "b")
        self.wire(division, "number", end, "result")
        with self.assertRaises(GraphError):
            self.result(end)
        self.assertEqual(self.context.failed_node["id"], division["id"])

    def test_fast_loops_do_not_emit_progress_for_every_node_iteration(self):
        messages = []
        self.context = TaskRunContext(self.cancel, lambda message, _percent: messages.append(message), lambda *_: None)
        begin, end = self.pair(count=500)
        self.wire(begin, "index", end, "result")
        self.assertEqual(self.result(end)["iterations"], 500)
        self.assertLess(len(messages), 50)

    def test_only_four_loop_helpers_no_break_continue(self):
        nodes = [kind for kind in self.registry.definitions if kind.startswith("core.loop_")]
        self.assertEqual(set(nodes), {BEGIN, END, "core.loop_list", "core.loop_operations"})
        self.assertFalse(any("break" in kind or "continue" in kind for kind in self.registry.definitions))


if __name__ == "__main__":
    unittest.main()

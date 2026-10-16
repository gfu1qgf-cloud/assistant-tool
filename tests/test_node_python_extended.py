"""Data tools compose through real DAGs and Houdini-style loop blocks."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import NodeDefinition, NodePort
from model.node_workflow.graph import (NodeRegistry, register_core, new_node, empty_graph,
    evaluate_graph, validate, GraphError, save_graph, load_graph)
from model.node_workflow.loops import add_loop_pair
from model.node_workflow.python_values import PythonValue
from model.node_workflow.python_extended import batch_example_graph
from model.node_workflow.tasks import prepare_job
from model.node_workflow.preview import run_data_workflow


class ExtendedPythonTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel, lambda *_: None, lambda *_: None)
        self.graph = empty_graph()

    def node(self, kind, **params):
        node = new_node(self.registry.definitions[kind])
        node["params"].update(params)
        self.graph["nodes"].append(node)
        return node

    def source(self, dtype, value, calls=None, fail=False):
        kind = "test.source_" + str(len(self.registry.definitions))
        def build(*_):
            if calls is not None:
                calls.append(1)
            if fail:
                raise AssertionError("未选中的分支不应执行")
            return {"value": copy.deepcopy(value)}
        self.registry.register("test", NodeDefinition(kind, kind, outputs=(NodePort("value", "值", dtype),), build=build))
        return self.node(kind)

    def wire(self, a, output, b, input_):
        self.graph["connections"].append([a["id"], output, b["id"], input_])

    def result(self, node):
        return evaluate_graph(self.graph, self.registry, self.context, root_id=node["id"])

    def call(self, name, values, **params):
        definition = self.registry.definitions["core.py_" + name]
        settings = {p.key: copy.deepcopy(p.default) for p in definition.parameters}
        settings.update(params)
        return definition.build({p.key: values.get(p.key) for p in definition.inputs}, settings, self.context)

    def test_categories_on_registered_nodes_not_only_gui(self):
        for kind in ("core.loop_begin", "core.loop_end", "core.choose", "core.switch", "core.py_coalesce"):
            self.assertTrue(self.registry.definitions[kind].category.startswith("Python 原生/流程控制/"))
        for kind in ("core.text", "core.number", "core.boolean", "core.arithmetic", "core.text_transform", "core.text_test"):
            self.assertTrue(self.registry.definitions[kind].category.startswith("Python 原生/"))

    def test_polymorphic_comparison_validates_graph_types(self):
        node = self.node("core.py_compare", operation="gt")
        a, b = self.source("number", 2), self.source("number", 1.5)
        self.wire(a, "value", node, "a"); self.wire(b, "value", node, "b")
        self.assertTrue(self.result(node)["boolean"])
        wrong = self.source("text", "2")
        self.graph["connections"][1] = [wrong["id"], "value", node["id"], "b"]
        self.assertTrue(validate(self.graph, self.registry, root_id=node["id"])[0])
        with self.assertRaises(GraphError): self.result(node)

    def test_compare_containers_and_bool_do_not_alias_numbers(self):
        self.assertFalse(self.call("compare", {"a": [True], "b": [1]})["boolean"])
        self.assertTrue(self.call("compare", {"a": {"x": [1]}, "b": {"x": [1.0]}})["boolean"])
        with self.assertRaises(TypeError): self.call("compare", {"a": True, "b": 1})
        for value in ([], {}, False, PythonValue.capture(None)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.call("compare", {"a": value, "b": value}, operation="gt")
        self.assertTrue(self.call("compare", {"a": "a", "b": "b"}, operation="lt")["boolean"])

    def test_empty_not_truthiness(self):
        for value in ("", [], {}, PythonValue.capture(None)):
            self.assertTrue(self.call("is_empty", {"value": value})["boolean"])
        for value in (0, False, " ", [None], {"x": None}):
            self.assertFalse(self.call("is_empty", {"value": value})["boolean"])
            self.assertFalse(self.call("is_null", {"value": value})["boolean"])

    def test_all_any_rules_and_empty_lists(self):
        self.assertTrue(self.call("truth_reduce", {"items": []})["boolean"])
        self.assertFalse(self.call("truth_reduce", {"items": []}, operation="any")["boolean"])
        with self.assertRaises(TypeError): self.call("truth_reduce", {"items": [True, 1]})
        self.assertTrue(self.call("truth_reduce", {"items": ["false"]}, truth="python")["boolean"])
        self.assertFalse(self.call("truth_reduce", {"items": [True, False]})["boolean"])

    def test_coalesce_only_computes_fallback_for_real_null(self):
        for value in (0, False, "", [], {}, "normal", None):
            with self.subTest(value=value):
                self.setUp()
                node = self.node("core.py_coalesce")
                first = self.source("data", PythonValue.capture(value))
                calls = []
                second = self.source("data", PythonValue.capture("备用"), calls=calls, fail=value is not None)
                self.wire(first, "value", node, "value"); self.wire(second, "value", node, "fallback")
                self.assertEqual(self.result(node)["value"].value, "备用" if value is None else value)
                self.assertEqual(calls, [1] if value is None else [])

    def test_ordinary_switch_is_lazy_but_missing_port_still_errors(self):
        for condition in (True, False):
            self.setUp()
            node = self.node("core.switch")
            decision = self.source("boolean", condition)
            yes = self.source("text", "yes", fail=not condition)
            no = self.source("text", "no", fail=condition)
            for source, key in ((decision, "condition"), (yes, "yes"), (no, "no")):
                self.wire(source, "value", node, key)
            self.assertEqual(self.result(node)["value"], "yes" if condition else "no")
            self.graph["connections"].pop()
            self.assertTrue(validate(self.graph, self.registry, root_id=node["id"])[0])

    def test_list_edit_pop_copy_negative_index_and_invalid_index(self):
        original = [{"id": 1}, {"id": 2}]
        result = self.call("list_edit", {"items": original, "value": PythonValue.capture({"id": 3})}, operation="set", position=-1)["items"]
        result[0]["id"] = 99
        self.assertEqual(original, [{"id": 1}, {"id": 2}])
        self.assertEqual(self.call("list_edit", {"items": [], "value": PythonValue.capture(1)}, operation="insert")["items"], [1])
        removed = self.call("list_pop", {"items": [1, None], "index": -1})
        self.assertEqual(removed["items"], [1]); self.assertIsNone(removed["removed"].value)
        for index in (3, -4, .5, True):
            with self.subTest(index=index), self.assertRaises((ValueError, IndexError)):
                self.call("list_edit", {"items": [1, 2], "index": index, "value": PythonValue.capture(3)}, operation="insert")
        with self.assertRaises(IndexError): self.call("list_pop", {"items": []})

    def test_chunks_preserve_tail_and_flatten_does_not_split_strings(self):
        self.assertEqual(self.call("chunks", {"items": list(range(23))}, count=10)["items"], [list(range(10)), list(range(10, 20)), [20, 21, 22]])
        for size in (0, -1, True, 2.5, 100001):
            with self.subTest(size=size), self.assertRaises(ValueError): self.call("chunks", {"items": [1], "size": size})
        value = [1, [2, [3]], "abc", {"x": [4]}]
        self.assertEqual(self.call("flatten", {"items": value})["items"], [1, 2, [3], "abc", {"x": [4]}])
        self.assertEqual(self.call("flatten", {"items": value}, depth=16)["items"], [1, 2, 3, "abc", {"x": [4]}])
        with self.assertRaises(ValueError): self.call("flatten", {"items": value}, depth=0)

    def test_chunks_feed_paired_foreach_loop(self):
        source = self.source("list", list(range(23)))
        chunks = self.node("core.py_chunks", count=10)
        begin, end = add_loop_pair(self.graph, self.registry, mode="foreach")
        length = self.node("core.py_len")
        self.wire(source, "value", chunks, "items"); self.wire(chunks, "items", begin, "items")
        self.wire(begin, "item", length, "value"); self.wire(length, "number", end, "result")
        self.assertEqual(self.result(end)["results"].to_list(), [10, 10, 3])

    def test_field_tools_strict_missing_and_stable_order(self):
        rows = [{"admin": "Maria", "id": 2}, {"admin": "John", "id": 1}, {"admin": "Maria", "id": 1}]
        self.assertEqual(self.call("sort_by", {"items": rows}, field="id")["items"], [rows[1], rows[2], rows[0]])
        self.assertEqual(self.call("group_by", {"items": rows}, field="admin")["items"], {"Maria": [rows[0], rows[2]], "John": [rows[1]]})
        self.assertEqual(self.call("pluck", {"items": rows}, field="id")["items"], [2, 1, 1])
        for name in ("pluck", "group_by", "sort_by"):
            with self.subTest(name=name), self.assertRaises(KeyError): self.call(name, {"items": [{}]}, field="missing")
        self.assertEqual(self.call("pluck", {"items": rows + [{}]}, field="id", missing="default", fallback="0")["items"], [2, 1, 1, 0])
        self.assertEqual(self.call("group_by", {"items": rows + [{}]}, field="admin", missing="skip")["items"]["Maria"], [rows[0], rows[2]])
        with self.assertRaises(TypeError): self.call("group_by", {"items": rows}, field="id")
        with self.assertRaises(TypeError): self.call("sort_by", {"items": [{"id": 1}, {"id": "1"}]}, field="id")

    def test_pairs_conflicts_and_bad_pairs(self):
        values = {"items": [["a", 1], ["a", 2]]}
        with self.assertRaises(ValueError): self.call("dict_from_pairs", values)
        self.assertEqual(self.call("dict_from_pairs", values, conflicts="first")["items"], {"a": 1})
        self.assertEqual(self.call("dict_from_pairs", values, conflicts="last")["items"], {"a": 2})
        for pair in ([1, "x"], ["a"], ["a", 1, 2], "ab"):
            with self.subTest(pair=pair), self.assertRaises(TypeError): self.call("dict_from_pairs", {"items": [pair]})

    def test_nested_get_null_missing_negative_index_and_dynamic_path(self):
        value = PythonValue.capture({"items": [{"name": None}, {"name": "last"}]})
        actual = self.call("nested_get", {"value": value}, route='["items",0,"name"]')
        self.assertTrue(actual["found"]); self.assertIsNone(actual["value"].value)
        missing = self.call("nested_get", {"value": value}, route='["items",8,"name"]', fallback='"备用"')
        self.assertFalse(missing["found"]); self.assertEqual(missing["value"].value, "备用")
        self.assertEqual(self.call("nested_get", {"value": value, "path": ["items", -1, "name"]})["value"].value, "last")
        for route in ('[true]', '[0.5]', '"items"', '[' + ','.join('"x"' for _ in range(33)) + ']'):
            with self.subTest(route=route), self.assertRaises((TypeError, ValueError)):
                self.call("nested_get", {"value": value}, route=route)

    def test_safe_template_and_preallocation_bound(self):
        result = self.call("template", {"text": "${date}-$name-$$.mp4", "items": {"date": "1002", "name": "reels"}})
        self.assertEqual(result["text"], "1002-reels-$.mp4")
        for template, mapping in (("${x.y}", {"x": 1}), ("$x", {}), ("$x", {"x": [1]}), ("$", {})):
            with self.subTest(template=template), self.assertRaises((ValueError, TypeError, KeyError)):
                self.call("template", {"text": template, "items": mapping})
        with self.assertRaisesRegex(ValueError, "1 MB"):
            self.call("template", {"text": "$x" * 20, "items": {"x": "a" * 100000}})

    def test_sample_save_load_and_run_through_background_data_path(self):
        graph = batch_example_graph(self.registry)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "workflow.json"
            save_graph(path, graph); restored = load_graph(path)
        payload, resources = prepare_job(restored, self.registry, None, None)
        self.assertEqual(resources, ())
        result = run_data_workflow(payload, self.context, registry=self.registry)
        self.assertEqual(result["result_kind"], "data")
        self.assertIn('"Maria"', result["data_json"])
        self.assertEqual(evaluate_graph(graph, self.registry, self.context)["value"].value["Maria"][0]["id"], 1)

    def test_new_function_nodes_still_check_cancellation(self):
        self.cancel.set()
        with self.assertRaises(BackgroundTaskCanceled): self.call("chunks", {"items": [1, 2]})


if __name__ == "__main__":
    unittest.main()

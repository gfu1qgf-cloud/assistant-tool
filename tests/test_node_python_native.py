"""Native Python functions remain typed, bounded, headless and non-executable."""
import copy
import json
import threading
import unittest

from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import NodeDefinition, NodeParameter, NodePort, DirectoryRef, FileList, ResourceRef
from model.node_workflow.function_library import node_function
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, evaluate_graph, validate, GraphError
from model.node_workflow.python_values import PythonValue, validate_data, parse_json
from model.node_workflow.preview import describe


class PythonNodesTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def call(self, node_name, inputs=None, **params):
        definition = self.registry.definitions["core.py_" + node_name]
        settings = {p.key: copy.deepcopy(p.default) for p in definition.parameters}
        settings.update(params)
        return definition.build({p.key: (inputs or {}).get(p.key) for p in definition.inputs}, settings, self.context)

    def source(self, dtype, value):
        key = "test.source_" + str(len(self.registry.definitions))
        definition = NodeDefinition(key, key, outputs=(NodePort("value", "值", dtype),),
            build=lambda *_: {"value": copy.deepcopy(value)})
        self.registry.register("test", definition)
        return new_node(definition)

    def graph_call(self, node_name, supplied, **params):
        target = new_node(self.registry.definitions["core.py_" + node_name])
        target["params"].update(params)
        graph = {**empty_graph(), "nodes": [target]}
        for key, dtype, value in supplied:
            source = self.source(dtype, value)
            graph["nodes"].append(source)
            graph["connections"].append([source["id"], "value", target["id"], key])
        return evaluate_graph(graph, self.registry, self.context, root_id=target["id"])

    def test_native_nodes_and_registry_metadata(self):
        nodes = [d for k, d in self.registry.definitions.items() if k.startswith("core.py_")]
        self.assertEqual(len(nodes), 64)
        self.assertEqual(len({d.category for d in nodes}), 8)
        self.assertTrue(all(d.execution == "compute" and d.category.startswith("Python 原生/") for d in nodes))
        self.assertTrue(all(self.registry.owners[d.kind] == "core" for d in nodes))

    def test_all_native_nodes_execute_with_representative_inputs(self):
        data = PythonValue.capture("alpha")
        files = FileList(DirectoryRef("C:/source", "task-a"), (ResourceRef("C:/source/a.mp4", "video", 1, 1),))
        cases = {
            "json": ({}, {"content": '[1,"a",null]'}), "json_parse": ({"text": '{"a":1}'}, {}),
            "json_dump": ({"value": [1, 2]}, {}), "data": ({"value": "alpha"}, {}), "output": ({"value": data}, {}),
            "str": ({"value": data}, {}), "int": ({"value": "239"}, {}), "float": ({"value": "1.25"}, {}),
            "bool": ({"value": []}, {}), "list": ({"value": PythonValue.capture([1, 2])}, {}),
            "dict": ({"value": PythonValue.capture({"a": 1})}, {}), "type": ({"value": data}, {}), "len": ({"value": [1, 2]}, {}),
            "list_create": ({"values": (1, 2)}, {}), "list_get": ({"items": [1, 2], "index": 1}, {}),
            "list_slice": ({"items": [1, 2, 3]}, {"start": 0, "stop": "2"}),
            "list_concat": ({"groups": ([1], [2])}, {}), "list_sort": ({"items": [2, 1]}, {}),
            "list_unique": ({"items": [1, 1, "1", True]}, {}), "list_contains": ({"items": ["alpha"], "value": data}, {}),
            "list_count": ({"items": ["alpha", "alpha"], "value": data}, {}),
            "list_filter": ({"items": ["alpha", "beta"], "target": data}, {}),
            "enumerate": ({"items": ["a", "b"]}, {}), "zip": ({"groups": ([1, 2], [3])}, {}), "range": ({}, {}),
            "files_paths": ({"files": files}, {}), "paths_files": ({"files": files, "items": ["C:/source/a.mp4"]}, {}),
            "dict_input": ({}, {"content": '{"a":1}'}), "dict_get": ({"items": {"a": 1}, "key": "a"}, {}),
            "dict_set": ({"items": {}, "value": data, "key": "x"}, {}), "dict_remove": ({"items": {"x": 1}}, {"key": "x"}),
            "dict_merge": ({"dictionaries": ({"x": 1}, {"y": 2})}, {}), "dict_items": ({"items": {"x": 1}}, {}),
            "split": ({"text": "a b"}, {}), "join": ({"items": ["a", "b"]}, {}), "partition": ({"text": "a/b/c"}, {}),
            "find": ({"text": "alpha"}, {"keyword": "ph"}), "text_count": ({"text": "aaa"}, {"keyword": "a"}),
            "padding": ({"text": "a"}, {}), "zfill": ({"text": "7"}, {}), "math": ({"value": -2}, {}),
            "power": ({"a": 2, "b": 3}, {}), "aggregate": ({"items": [1, 2, 3]}, {}),
            "clamp": ({"value": 20, "minimum": 1, "maximum": 10}, {}), "date_shift": ({"text": "2026-12-31"}, {}),
            "date_diff": ({"a": "2026-10-01", "b": "2026-10-11"}, {}), "url_parts": ({"text": "https://example.test/p?a=1#f"}, {}),
            "url_quote": ({"text": "a b/中文"}, {}), "url_unquote": ({"text": "a%20b"}, {}),
            "compare": ({"a": "alpha", "b": "beta"}, {}), "is_null": ({"value": data}, {}),
            "is_empty": ({"value": []}, {}), "truth_reduce": ({"items": [True, False]}, {}),
            "list_edit": ({"items": [1, 2], "value": data}, {}), "list_pop": ({"items": [1, 2]}, {}),
            "chunks": ({"items": [1, 2, 3]}, {"count": 2}), "flatten": ({"items": [[1], [2]]}, {}),
            "pluck": ({"items": [{"id": 1}]}, {"field": "id"}),
            "group_by": ({"items": [{"admin": "Maria", "id": 1}]}, {"field": "admin"}),
            "sort_by": ({"items": [{"id": 2}, {"id": 1}]}, {"field": "id"}),
            "dict_from_pairs": ({"items": [["admin", "Maria"]]}, {}),
            "nested_get": ({"value": PythonValue.capture({"items": [{"id": 1}]})}, {"route": '["items",0,"id"]'}),
            "template": ({"text": "${date}-${name}.mp4", "items": {"date": "1002", "name": "reels"}}, {}),
            "coalesce": ({"value": data, "fallback": PythonValue.capture("fallback")}, {}),
        }
        self.assertEqual(set(cases), {k.removeprefix("core.py_") for k in self.registry.definitions if k.startswith("core.py_")})
        for name, (values, settings) in cases.items():
            with self.subTest(name=name):
                result = self.call(name, values, **settings)
                definition = self.registry.definitions["core.py_" + name]
                self.assertEqual(set(result), {p.key for p in definition.outputs})
                # Run through the actual DAG, not just the function adapter.
                supplied = []
                for port in definition.inputs:
                    value = values.get(port.key)
                    if value is None:
                        continue
                    multiple = value if port.multiple else (value,)
                    for item in multiple:
                        dtype = port.data_type
                        if dtype == "any":
                            dtype = ("data" if isinstance(item, PythonValue) else "text" if type(item) is str else
                                     "list" if type(item) is list else "mapping" if type(item) is dict else
                                     "boolean" if type(item) is bool else "number")
                        supplied.append((port.key, dtype, item))
                self.graph_call(name, supplied, **settings)

    def test_null_is_real_data_not_missing_media(self):
        result = self.graph_call("json", [], content="null")["value"]
        self.assertIsNone(result.value)
        self.assertFalse(self.graph_call("bool", [("value", "data", result)])["boolean"])
        self.assertEqual(describe(result), "null")

    def test_list_get_negative_and_bounds(self):
        self.assertEqual(self.call("list_get", {"items": [1, 2], "index": -1})["value"].value, 2)
        for index in (2, -3, .5, True):
            with self.subTest(index=index), self.assertRaises((ValueError, IndexError)):
                self.call("list_get", {"items": [1, 2], "index": index})

    def test_list_slicing_sort_unique_zip(self):
        self.assertEqual(self.call("list_slice", {"items": [1, 2, 3]}, start=-1, stop="", step=-1)["items"], [3, 2, 1])
        with self.assertRaises(ValueError): self.call("list_slice", {"items": []}, step=0)
        with self.assertRaises(TypeError): self.call("list_sort", {"items": [1, "1"]})
        self.assertEqual(self.call("list_unique", {"items": [1, True, "1", 1]})["items"], [1, True, "1"])
        self.assertEqual(self.call("zip", {"groups": ([1, 2], [3])})["items"], [[1, 3]])

    def test_list_filter_dicts_and_text(self):
        rows = [{"admin": "Maria", "n": 2}, {"admin": "JOHN", "n": 9}, {"n": 5}]
        self.assertEqual(self.call("list_filter", {"items": rows, "target": PythonValue.capture("maria")}, field="admin")["items"], rows[:1])
        self.assertEqual(self.call("list_filter", {"items": rows, "target": PythonValue.capture(5)}, operation="ge", field="n")["items"], rows[1:])
        with self.assertRaises(TypeError): self.call("list_filter", {"items": [1], "target": PythonValue.capture("x")})

    def test_dict_defaults_conflicts_and_no_source_mutation(self):
        original = {"x": [1]}
        result = self.call("dict_set", {"items": original, "value": PythonValue.capture([2]), "key": "y"})["items"]
        result["x"].append(3)
        self.assertEqual(original, {"x": [1]})
        missing = self.call("dict_get", {"items": original}, name="absent", fallback='"fallback"')
        self.assertFalse(missing["found"])
        self.assertEqual(missing["value"].value, "fallback")
        groups = {"dictionaries": ({"x": 1}, {"x": 2})}
        self.assertEqual(self.call("dict_merge", groups, conflicts="first")["items"], {"x": 1})
        self.assertEqual(self.call("dict_merge", groups, conflicts="last")["items"], {"x": 2})
        with self.assertRaises(ValueError): self.call("dict_merge", groups, conflicts="error")

    def test_str_conversion_and_truthiness_are_explicit(self):
        self.assertEqual(self.call("int", {"value": "239"})["number"], 239)
        self.assertEqual(self.call("float", {"value": "1.25"})["number"], 1.25)
        self.assertTrue(self.call("bool", {"value": "false"})["boolean"])
        with self.assertRaises(ValueError): self.call("int", {"value": "239-a"})
        with self.assertRaises(ValueError): self.call("float", {"value": "nan"})
        self.assertEqual(self.call("zfill", {"text": "-7"}, width=3)["text"], "-07")

    def test_math_domain_range_and_statistics(self):
        self.assertEqual(self.call("math", {"value": 2.345}, operation="round", digits=2)["number"], 2.35)
        with self.assertRaises(ValueError): self.call("math", {"value": -1}, operation="sqrt")
        with self.assertRaises((ValueError, OverflowError)): self.call("power", {"a": 1e100, "b": 1e100})
        with self.assertRaises(ValueError): self.call("aggregate", {"items": []}, operation="mean")
        self.assertEqual(self.call("aggregate", {"items": [1, 3, 2]}, operation="median")["number"], 2)
        self.assertEqual(self.call("range", {}, start=3, end=0, step=-1)["items"], [3, 2, 1])
        with self.assertRaises(ValueError): self.call("range", {}, step=0)
        with self.assertRaises(ValueError): self.call("range", {}, end=100001)

    def test_dates_and_url_parse_do_not_network(self):
        self.assertEqual(self.call("date_shift", {"text": "2026-12-31"})["text"], "2027-01-01")
        self.assertEqual(self.call("date_diff", {"a": "2026-10-01", "b": "2026-10-11"})["number"], 10)
        self.assertEqual(self.call("url_parts", {"text": "https://example.test/a?x=1#f"})["host"], "example.test")

    def test_json_duplicates_size_depth_cycles_and_objects_rejected(self):
        for text in ('{"a":1,"a":2}', 'NaN', 'Infinity', '__import__("os").system("whoami")', '[' * 40 + '0' + ']' * 40):
            with self.subTest(text=text), self.assertRaises(ValueError): parse_json(text)
        with self.assertRaises(ValueError): validate_data("x" * (1024 * 1024 + 1))
        with self.assertRaises(ValueError): validate_data(float("inf"))
        with self.assertRaises(TypeError): validate_data({1: "x"})
        with self.assertRaises(TypeError): validate_data(object())
        cycle = []; cycle.append(cycle)
        with self.assertRaises(ValueError): validate_data(cycle)

    def test_container_output_contract_checked_on_actual_graph(self):
        bad = NodeDefinition("test.bad", "bad", outputs=(NodePort("value", "值", "list"),), build=lambda *_: {"value": [object()]})
        self.registry.register("test", bad)
        node = new_node(bad)
        with self.assertRaises(GraphError):
            evaluate_graph({**empty_graph(), "nodes": [node]}, self.registry, self.context, root_id=node["id"])

    def test_plugin_decorator_registration_signature_namespace_and_multioutput(self):
        @node_function(kind="sample.upper", title="插件函数", inputs=(NodePort("text", "文本", "text"),),
            outputs=(NodePort("text", "文本", "text"),), category="示例插件/文字")
        def upper(text):
            return text.upper()
        self.registry.register_function("sample", upper)
        self.assertEqual(upper("a"), "A")
        self.assertEqual(upper.__node_definition__.build({"text": "a"}, {}, self.context), {"text": "A"})
        with self.assertRaises(ValueError): NodeRegistry().register_function("other", upper)
        with self.assertRaises(ValueError):
            node_function(kind="sample.bad", title="bad", inputs=(NodePort("x", "x", "text"),), outputs=(NodePort("x", "x", "text"),))(lambda y: y)
        with self.assertRaises(TypeError): self.registry.register_function("sample", lambda: 1)
        @node_function(kind="sample.multi", title="multi", outputs=(NodePort("a", "a", "number"), NodePort("b", "b", "number")))
        def broken():
            return {"a": 1}
        with self.assertRaises(TypeError): broken.__node_definition__.build({}, {}, self.context)

    def test_branch_container_mutation_does_not_change_other_upstream_consumer(self):
        source = self.source("list", [1])
        mutate = NodeDefinition("test.mutate", "mutate", inputs=(NodePort("items", "列表", "list"),),
            outputs=(NodePort("items", "列表", "list"),), build=lambda i, _p, _c: {"items": i["items"] + [2]})
        self.registry.register("test", mutate)
        node = new_node(mutate)
        concat = new_node(self.registry.definitions["core.py_list_concat"])
        graph = {**empty_graph(), "nodes": [source, node, concat], "connections": [
            [source["id"], "value", node["id"], "items"], [node["id"], "items", concat["id"], "groups"],
            [source["id"], "value", concat["id"], "groups"]]}
        # Deliberately mutate the actual incoming object, mimicking a careless plugin.
        def mutator(i, _p, _c):
            i["items"].append(2)
            return {"items": i["items"]}
        self.registry.definitions["test.mutate"] = NodeDefinition("test.mutate", "mutate", inputs=mutate.inputs, outputs=mutate.outputs, build=mutator)
        self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=concat["id"])["items"], [1, 2, 1])

    def test_data_converts_to_plugin_text_and_retains_type_checks(self):
        source = new_node(self.registry.definitions["core.py_json"])
        source["params"]["content"] = '"慕雪"'
        convert = new_node(self.registry.definitions["core.py_str"])
        target = new_node(self.registry.definitions["core.text_test"])
        target["params"]["needle"] = "慕雪"
        graph = {**empty_graph(), "nodes": [source, convert, target], "connections": [
            [source["id"], "value", convert["id"], "value"], [convert["id"], "text", target["id"], "text"]]}
        self.assertTrue(evaluate_graph(graph, self.registry, self.context, root_id=target["id"])["boolean"])
        graph["connections"][-1] = [source["id"], "value", target["id"], "text"]
        self.assertTrue(validate(graph, self.registry, root_id=target["id"])[0])

    def test_metadata_is_not_serialized_code_and_preview_handles_lists(self):
        node = new_node(self.registry.definitions["core.py_dict_input"])
        saved = json.dumps(node)
        for key in ("build", "function", "category", "keywords"):
            self.assertNotIn('"' + key + '"', saved)
        self.assertEqual(json.loads(describe([1, {"x": "中文"}])), [1, {"x": "中文"}])
        self.assertEqual(json.loads(describe({"x": None})), {"x": None})

    def test_file_list_bridge_preserves_order_task_identity_and_snapshot(self):
        directory = DirectoryRef("C:/source", "task-a")
        a, b = ResourceRef("C:/source/a.mp4", "video", 1, 2), ResourceRef("C:/source/b.mp4", "video", 3, 4)
        files = FileList(directory, (a, b))
        self.assertEqual(self.call("files_paths", {"files": files})["items"], [a.path, b.path])
        result = self.call("paths_files", {"files": files, "items": [b.path, a.path]})["files"]
        self.assertEqual(result.directory, directory)
        self.assertEqual(result.files, (b, a))
        for items in ([a.path, a.path], ["C:/other/task.mp4"], [1]):
            with self.subTest(items=items), self.assertRaises((ValueError, TypeError)):
                self.call("paths_files", {"files": files, "items": items})

    def test_python_example_runs_without_ffmpeg_and_is_not_preview_execution(self):
        from unittest import mock
        from model.node_workflow.python_nodes import python_example_graph
        from model.node_workflow.tasks import prepare_job
        from model.node_workflow.preview import run_data_workflow
        graph = python_example_graph(self.registry)
        self.assertFalse(validate(graph, self.registry)[0])
        payload, resources = prepare_job(graph, self.registry, None, None)
        self.assertEqual(resources, ())
        with mock.patch("subprocess.Popen", side_effect=AssertionError("纯数据节点不应启动 FFmpeg")):
            result = run_data_workflow(payload, self.context, registry=self.registry)
        self.assertEqual(result["result_kind"], "data")
        self.assertEqual(json.loads(result["data_json"]), ["Maria_001.mp4", "Maria_003.mp4"])
        self.assertFalse(self.context.workflow_globals.is_preview)
        self.assertFalse(hasattr(self.context, "media"))
        graph["execution_mode"] = "line"
        self.assertTrue(validate(graph, self.registry)[0])


if __name__ == "__main__":
    unittest.main()

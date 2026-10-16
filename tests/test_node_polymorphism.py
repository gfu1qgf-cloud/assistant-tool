"""A polymorphic type group is one concrete type, not an unchecked Any."""
import threading
import time
import unittest

from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef, DirectoryRef, FileList
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, validate, evaluate_graph, GraphError
from model.node_workflow.typing import resolve_port_types
from model.node_workflow.reroute import insert_reroute


class PolymorphicTests(unittest.TestCase):
    def setUp(self):
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def source(self, kind, dtype, value):
        definition = NodeDefinition("test." + kind, kind, outputs=(NodePort("value", "值", dtype),),
            build=lambda _i, _p, _c: {"value": value})
        self.registry.register("test", definition)
        return new_node(definition)

    def switch_graph(self, dtype, yes_value, no_value):
        yes, no = self.source("yes", dtype, yes_value), self.source("no", dtype, no_value)
        condition = new_node(self.registry.definitions["core.boolean"])
        switch = new_node(self.registry.definitions["core.switch"])
        preview = new_node(self.registry.definitions["core.preview"])
        graph = {**empty_graph(), "nodes": [condition, yes, no, switch, preview], "connections": [
            [condition["id"], "boolean", switch["id"], "condition"],
            [yes["id"], "value", switch["id"], "yes"], [no["id"], "value", switch["id"], "no"],
            [switch["id"], "value", preview["id"], "value"]]}
        return graph, condition, switch, preview

    def test_same_switch_kind_handles_text_number_boolean_and_resources_by_identity(self):
        values = (("text", "甲", "乙"), ("number", 239, 1.5), ("boolean", True, False),
            ("video", ResourceRef("a.mp4", "video", 0, 0), ResourceRef("b.mp4", "video", 0, 0)),
            ("audio", ResourceRef("a.wav", "audio", 0, 0), ResourceRef("b.wav", "audio", 0, 0)),
            ("directory", DirectoryRef("a"), DirectoryRef("b")),
            ("files", FileList(DirectoryRef("a"), ()), FileList(DirectoryRef("b"), ())))
        for dtype, yes, no in values:
            with self.subTest(dtype=dtype):
                self.setUp()
                graph, condition, switch, preview = self.switch_graph(dtype, yes, no)
                self.assertFalse(validate(graph, self.registry, root_id=preview["id"])[0])
                types, conflicts = resolve_port_types(graph, self.registry)
                self.assertFalse(conflicts)
                for out, key in ((False, "yes"), (False, "no"), (True, "value")):
                    self.assertEqual(types[(switch["id"], out, key)], dtype)
                self.assertEqual(types[(switch["id"], False, "condition")], "boolean")
                for choice, expected in (("true", yes), ("false", no)):
                    condition["params"]["value"] = choice
                    self.assertIs(evaluate_graph(graph, self.registry, self.context, root_id=preview["id"])["value"], expected)

    def test_mixed_branch_types_fail_even_when_bad_branch_not_selected(self):
        graph, _condition, switch, preview = self.switch_graph("number", 1, 2)
        text = self.source("text", "text", "2")
        graph["nodes"].append(text)
        graph["connections"][2] = [text["id"], "value", switch["id"], "no"]
        self.assertTrue(any("同类型组不一致" in message for _, message in validate(graph, self.registry, root_id=preview["id"])[0]))
        with self.assertRaisesRegex(GraphError, "不匹配"):
            evaluate_graph(graph, self.registry, self.context, root_id=preview["id"])

    def test_downstream_mismatch_detected_across_generic_switch_dot_preview_chain(self):
        graph, _condition, switch, preview = self.switch_graph("number", 1, 2)
        insert_reroute(graph, self.registry, graph["connections"][-1], [300, 0])
        target = new_node(self.registry.definitions["core.text_replace"])
        graph["nodes"].append(target)
        graph["connections"].append([preview["id"], "value", target["id"], "text"])
        self.assertTrue(validate(graph, self.registry, root_id=target["id"])[0])

    def test_type_is_not_saved_or_stuck_after_rewiring(self):
        graph, _condition, switch, preview = self.switch_graph("number", 1, 2)
        types, _ = resolve_port_types(graph, self.registry)
        self.assertEqual(types[(preview["id"], True, "value")], "number")
        a, b = self.source("text_a", "text", "A"), self.source("text_b", "text", "B")
        graph["nodes"].extend([a, b])
        graph["connections"][1:3] = [[a["id"], "value", switch["id"], "yes"], [b["id"], "value", switch["id"], "no"]]
        types, _ = resolve_port_types(graph, self.registry)
        self.assertEqual(types[(preview["id"], True, "value")], "text")
        self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=preview["id"])["value"], "A")
        self.assertNotIn("data_type", preview["params"])

    def test_unresolved_or_unconnected_generic_nodes_cannot_execute(self):
        for kind in ("core.preview", "core.switch", "core.reroute"):
            node = new_node(self.registry.definitions[kind])
            graph = {**empty_graph(), "nodes": [node]}
            messages = validate(graph, self.registry, root_id=node["id"])[0]
            self.assertTrue(any("未连接" in text for _, text in messages))
            self.assertTrue(any("尚未确定" in text for _, text in messages))

    def test_runtime_rejects_wrong_value_from_plugin_despite_matching_declaration(self):
        graph, _condition, _switch, preview = self.switch_graph("number", "not-a-number", 2)
        with self.assertRaisesRegex(GraphError, "数据类型不符合"):
            evaluate_graph(graph, self.registry, self.context, root_id=preview["id"])

    def test_generic_plugin_group_cannot_register_without_explicit_type_group(self):
        for port in (NodePort("value", "值", "any"), NodePort("value", "值", "number", type_group="value")):
            with self.assertRaisesRegex(ValueError, "多态端口"):
                self.registry.register("test", NodeDefinition("test.bad", "bad", outputs=(port,), build=lambda *_: {}))

    def test_seek_requirement_is_not_lost_through_polymorphic_choice(self):
        graph, _condition, switch, preview = self.switch_graph("video", ResourceRef("a.mp4", "video", 0, 0), ResourceRef("b.mp4", "video", 0, 0))
        transport = new_node(self.registry.definitions["core.transport"])
        transport["params"]["mode"] = "stream"
        graph["nodes"].append(transport)
        yes_edge = graph["connections"][1]
        graph["connections"][1] = [yes_edge[0], yes_edge[1], transport["id"], "video"]
        graph["connections"].append([transport["id"], "video", switch["id"], "yes"])
        insert_reroute(graph, self.registry, graph["connections"][-1], [300, 0])
        self.assertTrue(any("顺序流" in text for _, text in validate(graph, self.registry, root_id=preview["id"])[0]))
        transport["params"]["mode"] = "memory"
        self.assertFalse(any("顺序流" in text for _, text in validate(graph, self.registry, root_id=preview["id"])[0]))

    def test_independent_polymorphic_groups_do_not_bind_unrelated_node_types(self):
        a, b = self.source("number", "number", 239), self.source("text", "text", "abc")
        first, second = (new_node(self.registry.definitions["core.preview"]) for _ in range(2))
        graph = {**empty_graph(), "nodes": [a, b, first, second], "connections": [
            [a["id"], "value", first["id"], "value"], [b["id"], "value", second["id"], "value"]]}
        types, conflicts = resolve_port_types(graph, self.registry)
        self.assertFalse(conflicts)
        self.assertEqual(types[(first["id"], True, "value")], "number")
        self.assertEqual(types[(second["id"], True, "value")], "text")

    def test_shared_switch_branches_do_not_cause_exponential_validation(self):
        source = self.source("video", "video", ResourceRef("synthetic.mp4", "video", 0, 0))
        condition = new_node(self.registry.definitions["core.boolean"])
        graph = {**empty_graph(), "nodes": [source, condition]}
        previous, out = source["id"], "value"
        for _ in range(60):
            switch = new_node(self.registry.definitions["core.switch"])
            graph["nodes"].append(switch)
            graph["connections"].extend([[condition["id"], "boolean", switch["id"], "condition"],
                [previous, out, switch["id"], "yes"], [previous, out, switch["id"], "no"]])
            previous, out = switch["id"], "value"
        preview = new_node(self.registry.definitions["core.preview"])
        graph["nodes"].append(preview)
        graph["connections"].append([previous, out, preview["id"], "value"])
        started = time.monotonic()
        self.assertFalse(validate(graph, self.registry, root_id=preview["id"])[0])
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()

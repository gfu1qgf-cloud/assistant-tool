import copy
import ast
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import (NodeDefinition, NodePort, FilterOperation, ResourceRef, RenderPlan)
from model.node_workflow.graph import (GraphError, NodeRegistry, register_core, example_graph, validate,
                                       snapshot, build_plan, load_graph, save_graph, new_node)
from model.node_workflow.render import compile_filters, render_plan


class NodeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root/"source.mp4"
        self.subtitle = self.root/"source.srt"
        self.video.write_bytes(b"synthetic video")
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nAhoj svet.\n",encoding="utf-8")
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.registry.register("batch_text_video",caption_node())
        self.graph = example_graph(self.registry)
        self.graph["nodes"][0]["params"].update(video=str(self.video),subtitles=str(self.subtitle))
        self.graph["nodes"][-1]["params"]["output"] = str(self.root/"result.mp4")
        self.messages = []
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel,lambda text,percent:self.messages.append((text,percent)),lambda *_:None)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self,graph=None):
        with mock.patch("model.node_workflow.render.resolve_tools",return_value=("ffmpeg","ffprobe")), \
             mock.patch("model.node_workflow.render.probe",return_value={"duration":2}):
            return build_plan(snapshot(graph or self.graph,self.registry),self.registry,self.context)

    def test_minimal_chain_produces_deferred_plan_not_video(self):
        plan = self.plan()
        self.assertIsInstance(plan,RenderPlan)
        self.assertEqual(plan.video.path,str(self.video.resolve()))
        self.assertEqual(len(plan.operations),1)
        self.assertFalse(Path(plan.output).exists())
        self.assertEqual(set(p.name for p in self.root.iterdir()),{"source.mp4","source.srt"})

    def test_round_required_ports_have_explicit_types(self):
        definition = self.registry.definitions["batch_text_video.caption"]
        self.assertEqual([p.data_type for p in definition.inputs if p.required],["subtitles"])
        self.assertEqual([p.data_type for p in definition.inputs if not p.required], ['color']*4)

    def test_missing_required_link_blocks_execution(self):
        self.graph["connections"].pop(0)
        errors,_,_ = validate(self.graph,self.registry)
        self.assertTrue(any("未连接" in message for _,message in errors))
        with self.assertRaises(GraphError): snapshot(self.graph,self.registry)

    def test_type_mismatch_blocks_execution(self):
        self.graph["connections"][0][1] = "video"
        errors,_,_ = validate(self.graph,self.registry)
        self.assertTrue(any("类型不匹配" in message for _,message in errors))

    def test_cycle_is_rejected(self):
        merge = self.graph["nodes"][2]["id"]
        self.graph["connections"].append([merge,"operations",merge,"operations"])
        self.assertTrue(any("循环" in message for _,message in validate(self.graph,self.registry)[0]))

    def test_single_input_cannot_have_multiple_sources(self):
        self.graph["connections"].append(copy.deepcopy(self.graph["connections"][0]))
        self.assertTrue(any("重复" in message for _,message in validate(self.graph,self.registry)[0]))

    def test_merge_preserves_connection_order(self):
        definition = caption_node()
        second = new_node(definition)
        self.graph["nodes"].append(second)
        first = self.graph["nodes"][1]
        second["params"]["size"] = 42
        imported,merge = self.graph["nodes"][0]["id"],self.graph["nodes"][2]["id"]
        self.graph["connections"] += [[imported,"subtitles",second["id"],"subtitles"],
                                       [second["id"],"operations",merge,"operations"]]
        plan = self.plan()
        self.assertIn("Fontsize=24",plan.operations[0].options[0][1])
        self.assertIn("Fontsize=42",plan.operations[1].options[0][1])

    def test_changed_source_is_not_silently_processed_while_queued(self):
        frozen = snapshot(self.graph,self.registry)
        self.video.write_bytes(b"modified video and different size")
        with self.assertRaisesRegex(GraphError,"发生变化"):
            build_plan(frozen,self.registry,self.context)

    def test_unbound_subtitle_operation_can_apply_to_another_video(self):
        other = self.root/"other.mp4"
        other.write_bytes(b"other")
        imported = new_node(self.registry.definitions["core.import"])
        imported["params"]["video"] = str(other)
        self.graph["nodes"].append(imported)
        self.graph["connections"][-1][0] = imported["id"]
        plan = self.plan()
        self.assertEqual(plan.video.path, str(other.resolve()))
        self.assertEqual(plan.operations[0].video, plan.video)

    def test_existing_output_is_never_overwritten(self):
        output = self.root/"result.mp4"
        output.write_bytes(b"keep")
        with self.assertRaisesRegex(GraphError,"已存在"):
            self.plan()
        self.assertEqual(output.read_bytes(),b"keep")

    def test_missing_subtitle_is_not_auto_generated_or_ignored(self):
        self.graph["nodes"][0]["params"]["subtitles"] = ""
        with self.assertRaisesRegex(GraphError,"未指定字幕"):
            self.plan()

    def test_cancel_before_build(self):
        self.cancel.set()
        with self.assertRaises(BackgroundTaskCanceled): self.plan()

    def test_file_roundtrip_keeps_data_not_callbacks_or_handles(self):
        path = self.root/"flow.json"
        save_graph(path,self.graph)
        self.assertEqual(load_graph(path),self.graph)
        self.assertNotIn("build",path.read_text(encoding="utf-8"))

    def test_unknown_schema_is_rejected(self):
        path = self.root/"flow.json"
        path.write_text(json.dumps({"schema":99,"nodes":[],"connections":[]}),encoding="utf-8")
        with self.assertRaises(GraphError): load_graph(path)

    def test_disabled_plugin_has_unavailable_node_not_fallback(self):
        registry = self.registry.subset(lambda owner:owner=="core")
        self.assertTrue(validate(self.graph,registry)[0])

    def test_version_mismatch(self):
        self.graph["nodes"][1]["version"] = 999
        self.assertTrue(any("版本" in message for _,message in validate(self.graph,self.registry)[0]))

    def test_unused_import_does_not_require_source_file(self):
        unused = new_node(self.registry.definitions["core.import"])
        self.graph["nodes"].append(unused)
        self.assertIsInstance(self.plan(),RenderPlan)

    def test_asset_compilation_copies_only_small_subtitle_and_safe_names(self):
        directory = self.root/"tmp"
        directory.mkdir()
        plan = self.plan()
        value = compile_filters(plan,directory)
        self.assertIn("filename='asset_0.srt'",value)
        self.assertNotIn(str(self.subtitle),value)
        self.assertEqual([p.name for p in directory.iterdir()],["asset_0.srt"])

    def test_raw_filter_injection_is_rejected(self):
        plan = self.plan()
        operation = FilterOperation("bad",plan.video,"subtitles",(("filename","x';movie=http://bad"),))
        with self.assertRaisesRegex(ValueError,"参数无效"):
            compile_filters(RenderPlan(plan.video,(operation,),plan.output),self.root)

    def test_registry_rejects_plugin_impersonation_and_duplicate(self):
        with self.assertRaises(ValueError): self.registry.register("evil",caption_node())
        with self.assertRaises(ValueError): self.registry.register("batch_text_video",caption_node())

    def test_shared_workflow_core_does_not_import_plugin_or_gui_internals(self):
        root = Path(__file__).resolve().parents[1]/"model"/"node_workflow"
        for source in root.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([node.module or ""] if isinstance(node,ast.ImportFrom) else
                         [item.name for item in node.names] if isinstance(node,ast.Import) else [])
                self.assertFalse(any(name.startswith(("app_plugins","PYUI")) for name in names),source.name)

    def test_caption_adapter_does_not_load_alignment_model(self):
        with mock.patch("app_plugins.builtin.batch_text_video.captions.whisper_inference",
                        side_effect=AssertionError("模型不应被调用")):
            self.assertIsInstance(self.plan(),RenderPlan)


if __name__ == "__main__": unittest.main()

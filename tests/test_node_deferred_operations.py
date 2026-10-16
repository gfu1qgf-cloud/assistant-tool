"""Video belongs to the settler; operation nodes remain reusable descriptors."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from qt_compat import QtCore, QtWidgets
from PYUI.node_graph_canvas import NodeScene
from app_plugins.builtin.batch_text_video.workflow_nodes import build_caption, caption_node
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.basic import settle
from model.node_workflow.contracts import (FilterOperation, ResourceRef, SubtitleCue,
    SubtitleTrack, VideoSection, VideoTimeline, RenderPlan)
from model.node_workflow.graph import (NodeRegistry, register_core, new_node, empty_graph,
    snapshot, build_plan, export_example_graph, validate)
from model.node_workflow.operations import bind_operations, flatten_operations, validate_subtitle
from model.node_workflow.render import compile_filters


def registry():
    result = NodeRegistry()
    register_core(result)
    result.register("batch_text_video", caption_node())
    return result


class DeferredOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic")
        self.ref = ResourceRef.capture(self.source, "video")
        self.video = VideoTimeline((VideoSection(self.ref, 0, 2),), 360, 640, "25", "task")
        self.track = SubtitleTrack((SubtitleCue(.1, 1.5, "Ahoj svet."),), "task", 2)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def caption(self, subtitle=None, **params):
        return build_caption({"subtitles": subtitle or self.track}, params, self.context)["operations"]

    def srt(self, text, encoding="utf-8"):
        path = self.root / "captions.srt"
        path.write_text(text, encoding=encoding)
        return ResourceRef.capture(path, "subtitles")

    def test_caption_has_no_video_input_or_media_probe(self):
        self.assertEqual([port.key for port in caption_node().inputs if port.required], ["subtitles"])
        self.assertEqual([p.data_type for p in caption_node().inputs if not p.required], ['color']*4)
        with mock.patch("model.node_workflow.render.probe_resource", side_effect=AssertionError("no video")):
            operations = self.caption()
        self.assertIsNone(operations[0].video)
        self.assertEqual(operations[0].assets, (("filename", self.track),))
        self.assertEqual(set(self.root.iterdir()), {self.source})

    def test_caption_does_not_require_a_video_context(self):
        operations = build_caption({"subtitles": self.track}, {}, None)["operations"]
        self.assertIsNone(operations[0].video)

    def test_multiple_operations_bind_in_wire_order_without_mutating_originals(self):
        first, second = self.caption(size=24), self.caption(size=42)
        video_effect = (FilterOperation("反相", None, "negate"),)
        originals = copy.deepcopy((first, video_effect, second))
        plan = settle({"video": self.video, "operations": (first, video_effect, second)}, {}, self.context)["plan"]
        self.assertIn("Fontsize=24", plan.operations[0].options[0][1])
        self.assertEqual(plan.operations[1].name, "negate")
        self.assertIn("Fontsize=42", plan.operations[2].options[0][1])
        self.assertTrue(all(op.video == self.video for op in plan.operations))
        self.assertEqual((first, video_effect, second), originals)
        self.assertTrue(all(op.video is None for group in originals for op in group))

    def test_unbound_file_caption_can_be_applied_to_two_videos(self):
        subtitles = self.srt("1\n00:00:00,100 --> 00:00:01,500\nAhoj svet.\n")
        operations = self.caption(subtitles)
        for duration in (2, 4):
            video = VideoTimeline((VideoSection(self.ref, 0, duration),), 360, 640, "25", "task")
            plan = settle({"video": video, "operations": (operations,)}, {}, self.context)["plan"]
            self.assertEqual(plan.operations[0].video, video)
        self.assertIsNone(operations[0].video)

    def test_video_specific_operation_cannot_be_applied_to_another_video(self):
        wrong = FilterOperation("反相", self.ref, "negate")
        with self.assertRaisesRegex(ValueError, "另一个视频"):
            settle({"video": self.video, "operations": ((wrong,),)}, {}, self.context)

    def test_caption_accepts_valid_track_but_settler_checks_timeline_duration(self):
        track = SubtitleTrack(self.track.cues, "task", 3)
        operations = self.caption(track)
        with self.assertRaisesRegex(ValueError, "时间线"):
            settle({"video": self.video, "operations": (operations,)}, {}, self.context)

    def test_caption_accepts_valid_track_but_settler_checks_task_ownership(self):
        track = SubtitleTrack(self.track.cues, "other-task", 2)
        operations = self.caption(track)
        with self.assertRaisesRegex(ValueError, "不同任务"):
            settle({"video": self.video, "operations": (operations,)}, {}, self.context)

    def test_srt_timing_is_checked_only_when_the_video_is_known(self):
        operations = self.caption(self.srt("1\n00:00:01,000 --> 00:00:03,000\nToo late.\n"))
        with self.assertRaisesRegex(ValueError, "超出视频时长"):
            settle({"video": self.video, "operations": (operations,)}, {}, self.context)
        self.assertFalse((self.root / "final.mp4").exists())

    def test_invalid_srt_is_not_silently_ignored(self):
        for text in ("", "bad subtitle", "1\n00:00:02,000 --> 00:00:01,000\nWrong.\n",
                     "1\n00:61:00,000 --> 00:61:01,000\nWrong.\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.caption(self.srt(text))

    def test_valid_utf16_and_multiline_srt_uses_the_same_cue(self):
        ref = self.srt("1\n00:00:00,100 --> 00:00:01,000\n<b>Ahoj</b>\nsvet.\n", "utf-16")
        validate_subtitle(ref, 2)
        self.assertIsNone(self.caption(ref)[0].video)

    def test_changed_subtitles_are_rejected_at_settlement(self):
        ref = self.srt("1\n00:00:00,100 --> 00:00:01,000\nAhoj.\n")
        operations = self.caption(ref)
        Path(ref.path).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            settle({"video": self.video, "operations": (operations,)}, {}, self.context)

    def test_functions_and_non_operations_cannot_be_submitted(self):
        for value in ((lambda: None,), (("arbitrary code",),), (42,)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                flatten_operations(value)

    def test_compiler_handles_unbound_operations_and_rechecks_timing(self):
        output = self.root / "assets"
        output.mkdir()
        operations = self.caption()
        chain = compile_filters(RenderPlan(self.video, operations, ""), output, self.context)
        self.assertIn("subtitles=", chain)
        self.assertIsNone(operations[0].video)
        wrong = SubtitleTrack(self.track.cues, "task", 3)
        with self.assertRaisesRegex(ValueError, "时间线"):
            compile_filters(RenderPlan(self.video, self.caption(wrong), ""), output, self.context,
                            video_info={"duration": 2})

    def test_direct_multi_node_graph_needs_no_merge(self):
        reg = registry()
        source = new_node(reg.definitions["core.import"])
        source["params"].update(video=str(self.source), subtitles=str(self.srt(
            "1\n00:00:00,100 --> 00:00:01,000\nAhoj.\n").path))
        captions = [new_node(caption_node()) for _ in range(2)]
        captions[1]["params"]["size"] = 42
        settling = new_node(reg.definitions["core.settle"])
        exporting = new_node(reg.definitions["core.export"])
        exporting["params"].update(destination="file", output=str(self.root / "final.mp4"))
        graph = empty_graph()
        graph.update(nodes=[source, *captions, settling, exporting], output_node=exporting["id"])
        graph["connections"] = [[source["id"], "video", settling["id"], "video"],
            [settling["id"], "plan", exporting["id"], "plan"]]
        for caption in captions:
            graph["connections"] += [[source["id"], "subtitles", caption["id"], "subtitles"],
                                     [caption["id"], "operations", settling["id"], "operations"]]
        self.assertFalse(validate(graph, reg)[0])
        with mock.patch("model.node_workflow.render.probe_resource", return_value={"duration": 2}) as probe:
            plan = build_plan(snapshot(graph, reg), reg, self.context)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(plan.operations), 2)
        self.assertIn("Fontsize=42", plan.operations[1].options[0][1])
        self.assertFalse(Path(plan.output).exists())

    def test_default_example_sends_video_only_to_settler(self):
        reg = registry()
        graph = export_example_graph(reg)
        captions = {node["id"] for node in graph["nodes"] if node["kind"] == caption_node().kind}
        self.assertFalse(any(edge[2] in captions and edge[3] == "video" for edge in graph["connections"]))
        port = next(port for port in reg.definitions["core.settle"].inputs if port.key == "operations")
        self.assertTrue(port.multiple)
        self.assertFalse(port.required)


class MultiInputCapsuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = registry()
        self.graph = empty_graph()
        self.graph["nodes"] = [new_node(self.registry.definitions[kind], (index * 300, 0))
            for index, kind in enumerate(("core.settle", "core.merge", "batch_text_video.caption"))]
        self.scene = NodeScene(self.graph, self.registry)

    def tearDown(self):
        self.scene.refresh_timer.stop()
        self.scene.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_multi_inputs_are_long_capsules_with_hit_test_on_both_ends(self):
        for node in self.graph["nodes"][:2]:
            port = self.scene.nodes[node["id"]].ports[(False, "operations")]
            self.assertGreater(port.boundingRect().height(), port.boundingRect().width() * 1.7)
            self.assertTrue(port.shape().contains(QtCore.QPointF(0, -16)))
            self.assertTrue(port.shape().contains(QtCore.QPointF(0, 16)))
            self.assertIn(port, self.scene.items(port.mapToScene(QtCore.QPointF(0, -16))))
            self.assertIn("可连接多个", port.toolTip())
        optional = self.scene.nodes[self.graph["nodes"][0]["id"]].ports[(False, "operations")]
        required = self.scene.nodes[self.graph["nodes"][1]["id"]].ports[(False, "operations")]
        self.assertIn("可选输入", optional.toolTip())
        self.assertIn("必须输入", required.toolTip())

    def test_single_required_ports_keep_circle_sized_bounds(self):
        caption = self.scene.nodes[self.graph["nodes"][2]["id"]]
        port = caption.ports[(False, "subtitles")]
        self.assertEqual(port.boundingRect().width(), port.boundingRect().height())
        self.assertNotIn((False, "video"), caption.ports)

    def test_capsule_accepts_more_than_one_operation_connection(self):
        settling, merging, caption = [self.scene.nodes[node["id"]] for node in self.graph["nodes"]]
        for node in (merging, caption):
            self.scene.port_pressed(node.ports[(True, "operations")])
            self.scene.port_pressed(settling.ports[(False, "operations")])
        self.assertEqual([edge[0] for edge in self.graph["connections"]],
                         [merging.data["id"], caption.data["id"]])


if __name__ == "__main__":
    unittest.main()

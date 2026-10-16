import base64
import io
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.basic import arithmetic, boolean_operation, compare, parse_number, finite_number, file_sets, list_video
from model.node_workflow.contracts import (ResourceRef, VideoTimeline, VideoSection, RenderPlan, FilterOperation, SubtitleTrack, SubtitleCue, DirectoryRef, FileList)
from model.node_workflow.graph import (NodeRegistry, register_core, new_node, empty_graph, evaluate_graph, build_plan, snapshot, validate, _task)
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.preview import run_preview, frame_preview


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.event = threading.Event()
        self.context = TaskRunContext(self.event, lambda *_: None, lambda *_: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_arithmetic_operations(self):
        expected = {"add": 10, "subtract": 4, "multiply": 21, "divide": 7 / 3, "modulo": 1, "min": 3, "max": 7}
        for operation, result in expected.items():
            self.assertEqual(arithmetic({"a": 7, "b": 3}, {"operation": operation}, None)["number"], result)

    def test_zero_division_and_non_finite_values_are_rejected(self):
        for operation in ("divide", "modulo"):
            with self.assertRaisesRegex(ValueError, "零"):
                arithmetic({"a": 1, "b": 0}, {"operation": operation}, None)
        for value in (True, math.nan, math.inf, 10**200):
            with self.assertRaises(ValueError):
                finite_number(value)
        with self.assertRaises(ValueError):
            arithmetic({"a": 1e100, "b": 1e100}, {"operation": "multiply"}, None)

    def test_strict_number_conversion_preserves_original_task_identifier(self):
        for text, expected in (("0012", 12), ("-2", -2), (" 3.5 ", 3.5), ("1e3", 1000)):
            self.assertEqual(parse_number(text), expected)
        for text in ("LZX-239", "239a", "", "nan", "1e999", "1,2", "__import__('os')"):
            with self.assertRaises(ValueError):
                parse_number(text)
        task = capture_task({"task": {"task_id": "00239"}, "target_dir": self.root})
        values = _task({}, {}, SimpleNamespace(task=task))
        self.assertEqual(values["task_id"], "00239")
        self.assertEqual(values["task_id_number"], 239)
        task = capture_task({"task": {"task_id": "LZX-239"}, "target_dir": self.root})
        self.assertIsNone(_task({}, {}, SimpleNamespace(task=task))["task_id_number"])

    def test_boolean_truth_table_and_comparisons(self):
        for a in (False, True):
            for b in (False, True):
                for op, expected in (("and", a and b), ("or", a or b), ("xor", a != b)):
                    self.assertIs(boolean_operation({"a": a, "b": b}, {"operation": op}, None)["boolean"], expected)
        self.assertTrue(compare({"a": 239, "b": 200}, {"operation": "gt"}, None)["boolean"])

    def test_typed_math_graph_can_be_previewed_without_export(self):
        graph = empty_graph()
        a = new_node(self.registry.definitions["core.number"])
        b = new_node(self.registry.definitions["core.number"])
        add = new_node(self.registry.definitions["core.arithmetic"])
        preview = new_node(self.registry.definitions["core.preview_number"])
        a["params"]["value"], b["params"]["value"] = 200, 39
        graph["nodes"] = [a, b, add, preview]
        graph["connections"] = [[a["id"], "number", add["id"], "a"], [b["id"], "number", add["id"], "b"],
                                [add["id"], "number", preview["id"], "value"]]
        self.assertFalse(validate(graph, self.registry, root_id=preview["id"])[0])
        self.assertEqual(evaluate_graph(graph, self.registry, self.context, root_id=preview["id"]), {"value": 239})
        payload, resources = prepare_job(graph, self.registry, None, None, root_id=preview["id"])
        payload["preview_node"] = preview["id"]
        result = run_preview(payload, self.context, registry=self.registry)
        self.assertIn("239", result["text"])
        self.assertEqual(result["png"], "")
        self.assertEqual(resources, ())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_required_ports_type_checks_and_cancellation_still_apply_to_preview(self):
        graph = empty_graph()
        preview = new_node(self.registry.definitions["core.preview_number"])
        text = new_node(self.registry.definitions["core.text"])
        graph["nodes"] = [text, preview]
        self.assertTrue(any("未连接" in text for _, text in validate(graph, self.registry, root_id=preview["id"])[0]))
        graph["connections"] = [[text["id"], "text", preview["id"], "value"]]
        self.assertTrue(any("类型不匹配" in text for _, text in validate(graph, self.registry, root_id=preview["id"])[0]))
        graph["connections"] = []
        self.event.set()
        with self.assertRaises(BackgroundTaskCanceled):
            evaluate_graph({**graph, "nodes": [text]}, self.registry, self.context, root_id=text["id"])

    def export_graph(self):
        graph = empty_graph()
        imported = new_node(self.registry.definitions["core.import"])
        settle = new_node(self.registry.definitions["core.settle"])
        export = new_node(self.registry.definitions["core.export"])
        source = self.root / "video.mp4"
        source.write_bytes(b"synthetic")
        imported["params"]["video"] = str(source)
        export["params"].update(destination="file", output=str(self.root / "final.mp4"))
        graph["nodes"] = [imported, settle, export]
        graph["connections"] = [[imported["id"], "video", settle["id"], "video"], [settle["id"], "plan", export["id"], "plan"]]
        graph["output_node"] = export["id"]
        return graph

    def test_settlement_does_not_export_or_encode_and_export_accepts_no_operations(self):
        graph = self.export_graph()
        root = graph["nodes"][1]["id"]
        before = set(self.root.iterdir())
        result = evaluate_graph(snapshot(graph, self.registry, root_id=root), self.registry, self.context, root_id=root)
        self.assertEqual(result["plan"].output, "")
        self.assertEqual(set(self.root.iterdir()), before)
        plan = build_plan(snapshot(graph, self.registry), self.registry, self.context)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.output, str(self.root / "final.mp4"))
        self.assertFalse(Path(plan.output).exists())

    def test_export_does_not_overwrite_and_task_mode_requires_binding(self):
        graph = self.export_graph()
        output = graph["nodes"][-1]
        (self.root / "final.mp4").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "覆盖"):
            build_plan(snapshot(graph, self.registry), self.registry, self.context)
        output["params"]["destination"] = "task"
        graph["execution_mode"] = "line"
        with self.assertRaisesRegex(ValueError, "载入任务"):
            build_plan(snapshot(graph, self.registry), self.registry, self.context)

    def test_export_empty_path_is_preflight_error_not_background_failure(self):
        graph = self.export_graph()
        graph["nodes"][-1]["params"]["output"] = ""
        self.assertTrue(any("导出文件路径" in text for _, text in validate(graph, self.registry)[0]))

    def test_switch_text_and_text_processing(self):
        switch = self.registry.definitions["core.switch_text"]
        self.assertEqual(switch.build({"condition": False, "yes": "A", "no": "B"}, {}, None), {"value": "B"})
        self.assertEqual(self.registry.definitions["core.text_join"].build({"texts": ("A", "B")}, {"separator": "-"}, None), {"text": "A-B"})
        self.assertEqual(self.registry.definitions["core.text_replace"].build({"text": "A.B"}, {"find": ".", "replacement": "!"}, None), {"text": "A!B"})

    def test_file_sets_preserve_order_and_never_duplicate_or_change_sources(self):
        refs = []
        for name in ("1.mp4", "2.mp4", "3.mp4"):
            path = self.root / name
            path.write_bytes(b"synthetic")
            refs.append(ResourceRef.capture(path, "video"))
        directory = DirectoryRef(str(self.root), "owner")
        a, b = FileList(directory, tuple(refs[:2])), FileList(directory, tuple(refs[1:]))
        for operation, expected in (("union", refs), ("intersection", [refs[1]]), ("difference", [refs[0]]), ("xor", [refs[0], refs[2]])):
            result = file_sets({"a": a, "b": b}, {"operation": operation}, self.context)
            self.assertEqual(result["files"].files, tuple(expected))
        self.assertEqual(len(list(self.root.iterdir())), 3)

    def test_list_video_uses_one_based_index_and_rejects_out_of_range(self):
        path = self.root / "1.mp4"
        path.write_bytes(b"synthetic")
        ref = ResourceRef.capture(path, "video")
        files = FileList(DirectoryRef(str(self.root)), (ref,))
        self.assertEqual(list_video({"files": files, "index": 1.0}, {"index": 2}, self.context), {"video": ref})
        for index in (0, 2, 1.5):
            with self.assertRaises(ValueError):
                list_video({"files": files, "index": index}, {"index": 1}, self.context)

    def test_file_sets_reject_cross_task_mixing(self):
        a = FileList(DirectoryRef(str(self.root), "a"), ())
        b = FileList(DirectoryRef(str(self.root), "b"), ())
        with self.assertRaisesRegex(ValueError, "另一任务"):
            file_sets({"a": a, "b": b}, {"operation": "union"}, self.context)


FFMPEG = os.environ.get("NODE_FFMPEG", "")


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实 FFmpeg")
class PreviewMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.context.ffmpeg = FFMPEG
        self.red, self.blue = self.root / "red.mp4", self.root / "blue.mp4"
        for path, color in ((self.red, "red"), (self.blue, "blue")):
            result = subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=240x320:r=25:d=2",
                "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(path)], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.temp.cleanup()

    def image(self, plan, at):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            png, actual = frame_preview(plan, at, self.context, directory)
            image = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGB")
        return image, actual

    def test_file_frame_preview_has_correct_aspect_and_no_final_video(self):
        plan = RenderPlan(ResourceRef.capture(self.red, "video"), (), "")
        before = set(self.root.iterdir())
        image, at = self.image(plan, 1)
        self.assertEqual(image.size, (240, 320))
        self.assertEqual(at, 1)
        self.assertGreater(image.getpixel((0, 0))[0], 200)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_timeline_preview_seeks_correct_section_and_applies_processing(self):
        timeline = VideoTimeline((VideoSection(ResourceRef.capture(self.red, "video"), 0, 1),
                                  VideoSection(ResourceRef.capture(self.blue, "video"), 0, 1)), 240, 320, "25", "test")
        plan = RenderPlan(timeline, (FilterOperation("反相", timeline, "negate"),), "")
        image, at = self.image(plan, 1.2)
        r, g, b = image.getpixel((20, 20))
        self.assertGreater(r, 200)
        self.assertGreater(g, 200)
        self.assertLess(b, 40)
        self.assertEqual(at, 1.2)

    def test_preview_subtitles_use_absolute_edited_time_not_local_clip_time(self):
        timeline = VideoTimeline((VideoSection(ResourceRef.capture(self.red, "video"), 0, 1),
                                  VideoSection(ResourceRef.capture(self.blue, "video"), 0, 1)), 240, 320, "25", "test")
        track = SubtitleTrack((SubtitleCue(1.1, 1.8, "HELLO WORLD"),), "test", 2)
        op = FilterOperation("字幕", timeline, "subtitles", (("force_style", "Fontsize=28,Alignment=2"),), (("filename", track),))
        before, _ = self.image(RenderPlan(timeline, (op,), ""), .5)
        after, _ = self.image(RenderPlan(timeline, (op,), ""), 1.3)
        def white(image):
            return sum(1 for r, g, b in image.getdata() if min(r, g, b) > 200)
        self.assertLess(white(before), 20)
        self.assertGreater(white(after), 100)

    def test_memory_resource_can_be_previewed_in_its_session(self):
        from model.node_workflow.media import MediaSession
        with MediaSession(self.context, FFMPEG) as session:
            self.context.media = session
            video = session.convert(ResourceRef.capture(self.red, "video"), "memory", 16)
            image, _ = self.image(RenderPlan(video, (), ""), .6)
            self.assertGreater(image.getpixel((0, 0))[0], 200)

    def test_explicit_settle_export_produces_real_video_once(self):
        from model.node_workflow.render import run_workflow, probe
        registry = NodeRegistry()
        register_core(registry)
        imported = new_node(registry.definitions["core.import"])
        settle = new_node(registry.definitions["core.settle"])
        export = new_node(registry.definitions["core.export"])
        imported["params"]["video"] = str(self.red)
        export["params"].update(destination="file", output=str(self.root / "final.mp4"))
        graph = {"schema": 1, "nodes": [imported, settle, export], "connections": [
            [imported["id"], "video", settle["id"], "video"], [settle["id"], "plan", export["id"], "plan"]], "output_node": export["id"]}
        payload, _ = prepare_job(graph, registry, None, None)
        payload["ffmpeg"] = FFMPEG
        result = run_workflow(payload, self.context, registry=registry)
        self.assertTrue(Path(result["video"]).is_file())
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        self.assertAlmostEqual(result["duration"], 2, delta=.05)

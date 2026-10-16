"""Rerouting must preserve values, capability checks and Merge ordering."""
import base64
import copy
import io
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import (NodeDefinition, NodePort, ResourceRef, DirectoryRef,
    FileList, FilterOperation, RenderPlan, VideoTimeline, VideoSection, SubtitleTrack, SubtitleCue)
from model.node_workflow.graph import (NodeRegistry, register_core, new_node, empty_graph,
    evaluate_graph, validate, save_graph, load_graph, check_document, GraphError, MAX_NODES)
from model.node_workflow.reroute import ROUTE_TYPES, insert_reroute, remove_reroute
from model.node_workflow.preview import run_preview
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.typing import resolve_port_types
from PYUI.node_graph_canvas import NodeScene, NodeView, JunctionItem
from PYUI.node_connection_paths import WIRE_STYLES, connection_path, closest_point


def registry():
    result = NodeRegistry()
    register_core(result)
    return result


def text_graph(reg):
    graph = empty_graph()
    a = new_node(reg.definitions["core.text"], (0, 60))
    a["params"]["text"] = "内容保持不变"
    b = new_node(reg.definitions["core.preview"], (580, 160))
    graph["nodes"] = [a, b]
    graph["connections"] = [[a["id"], "text", b["id"], "value"]]
    return graph


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = registry()
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_each_contract_type_is_forwarded_by_identity_not_copied(self):
        video = ResourceRef("synthetic.mp4", "video", 0, 0, storage="memory", resource_id="test")
        values = {"text": "abc", "number": 239, "boolean": True,
            "directory": DirectoryRef(str(self.root)), "files": FileList(DirectoryRef(str(self.root)), ()),
            "video": video, "subtitles": SubtitleTrack((SubtitleCue(0, 1, "abc"),), "test", 1),
            "audio": ResourceRef("synthetic.wav", "audio", 0, 0),
            "operations": (FilterOperation("反相", video, "negate"),),
            "render_plan": RenderPlan(video, (), ""),
            "task": capture_task({"task": {"task_id": "239"}, "target_dir": self.root})}
        for dtype, _title in ROUTE_TYPES:
            with self.subTest(dtype=dtype):
                value = values[dtype]
                source = NodeDefinition("test.source_" + dtype, dtype, outputs=(NodePort("value", dtype, dtype),),
                    build=lambda _i, _p, _c, value=value: {"value": value})
                self.registry.register("test", source)
                a, b = new_node(source), new_node(self.registry.definitions["core.reroute"])
                graph = {**empty_graph(), "nodes": [a, b], "connections": [[a["id"], "value", b["id"], "value"]]}
                result = evaluate_graph(graph, self.registry, self.context, root_id=b["id"])
                self.assertIs(result["value"], value)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_insert_save_load_remove_preserves_old_graph_and_style(self):
        graph = text_graph(self.registry)
        original = copy.deepcopy(graph)
        edge = graph["connections"][0]
        dot = insert_reroute(graph, self.registry, edge, [400, 120])
        self.assertEqual(dot["kind"], "core.reroute")
        self.assertEqual(resolve_port_types(graph, self.registry)[0][(dot["id"], True, "value")], "text")
        graph["view"] = {"connection_style": "rounded"}
        path = self.root / "graph.json"
        save_graph(path, graph)
        loaded = load_graph(path)
        self.assertEqual(loaded, graph)
        root = original["nodes"][-1]["id"]
        self.assertEqual(evaluate_graph(loaded, self.registry, self.context, root_id=root), {"value": "内容保持不变"})
        remove_reroute(loaded, dot["id"])
        loaded.pop("view")
        self.assertEqual(loaded, original)
        self.assertNotIn("view", original)

    def test_merge_order_stays_identical_when_inserting_moving_or_removing_dots(self):
        video = ResourceRef("video.mp4", "video", 0, 0)
        graph = empty_graph()
        for index in range(3):
            op = FilterOperation(str(index), video, "negate")
            definition = NodeDefinition("test.op_" + str(index), str(index),
                outputs=(NodePort("value", "操作", "operations"),),
                build=lambda _i, _p, _c, op=op: {"value": (op,)})
            self.registry.register("test", definition)
            graph["nodes"].append(new_node(definition))
        merge = new_node(self.registry.definitions["core.merge"])
        graph["connections"] = [[n["id"], "value", merge["id"], "operations"] for n in graph["nodes"]]
        graph["nodes"].append(merge)
        dots = [insert_reroute(graph, self.registry, edge, [300, 50]) for edge in list(graph["connections"])]
        read = lambda: [op.title for op in evaluate_graph(graph, self.registry, self.context, root_id=merge["id"])["operations"]]
        self.assertEqual(read(), ["0", "1", "2"])
        for dot in reversed(dots):
            dot["position"] = [999, -100]
            remove_reroute(graph, dot["id"])
            self.assertEqual(read(), ["0", "1", "2"])

    def test_seek_preflight_follows_reroutes_and_preview_nodes(self):
        imported = new_node(self.registry.definitions["core.import"])
        imported["params"]["video"] = "synthetic.mp4"
        transport = new_node(self.registry.definitions["core.transport"])
        transport["params"]["mode"] = "stream"
        preview = new_node(self.registry.definitions["core.preview_video"])
        graph = {**empty_graph(), "nodes": [imported, transport, preview], "connections": [
            [imported["id"], "video", transport["id"], "video"],
            [transport["id"], "video", preview["id"], "value"]]}
        first = insert_reroute(graph, self.registry, graph["connections"][-1], [400, 0])
        insert_reroute(graph, self.registry, graph["connections"][-1], [450, 0])
        for mode, expected in (("stream", True), ("memory", False)):
            transport["params"]["mode"] = mode
            self.assertEqual(any("顺序流" in text for _, text in validate(graph, self.registry, root_id=preview["id"])[0]), expected)
        # A cycle introduced by hand must be rejected, not recurse indefinitely.
        graph["connections"][1] = [first["id"], "value", first["id"], "value"]
        self.assertTrue(validate(graph, self.registry, root_id=preview["id"])[0])

    def test_missing_subtitle_and_incomplete_preview_are_not_hidden_by_dot(self):
        graph = empty_graph()
        a = new_node(self.registry.definitions["core.import"])
        a["params"]["video"] = "synthetic.mp4"
        b = new_node(self.registry.definitions["core.preview_subtitles"])
        graph["nodes"] = [a, b]
        self.assertTrue(any("未连接" in text for _, text in validate(graph, self.registry, root_id=b["id"])[0]))
        graph["connections"] = [[a["id"], "subtitles", b["id"], "value"]]
        insert_reroute(graph, self.registry, graph["connections"][0], [300, 0])
        self.assertTrue(any("未指定字幕" in text for _, text in validate(graph, self.registry, root_id=b["id"])[0]))

    def test_failed_insert_is_atomic_for_limits_invalid_position_and_bad_types(self):
        for mode in ("limit", "edges", "nan", "mismatch"):
            graph = text_graph(self.registry)
            if mode == "limit":
                graph["nodes"].extend(new_node(self.registry.definitions["core.number"]) for _ in range(MAX_NODES - 2))
            if mode == "edges":
                graph["connections"] *= 512
            if mode == "mismatch":
                graph["nodes"][-1]["kind"] = "core.preview_number"
            before = copy.deepcopy(graph)
            with self.assertRaises(GraphError):
                insert_reroute(graph, self.registry, graph["connections"][0], [math.nan if mode == "nan" else 300, 50])
            self.assertEqual(graph, before)

    def test_deleting_dot_does_not_silently_deduplicate_merge_operations(self):
        graph = text_graph(self.registry)
        original_edge = list(graph["connections"][0])
        dot = insert_reroute(graph, self.registry, graph["connections"][0], [300, 0])
        graph["connections"].append(original_edge)
        before = copy.deepcopy(graph)
        with self.assertRaisesRegex(GraphError, "重复连接"):
            remove_reroute(graph, dot["id"])
        self.assertEqual(graph, before)

    def test_view_metadata_is_optional_and_invalid_styles_rejected(self):
        graph = text_graph(self.registry)
        check_document(graph)
        for style, _title in WIRE_STYLES:
            check_document({**graph, "view": {"connection_style": style}})
        for view in ("smooth", {"connection_style": "bad"}, {"connection_style": ["smooth"]}):
            with self.assertRaises(GraphError):
                check_document({**graph, "view": view})


class RoutingCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.registry = registry()
        self.graph = text_graph(self.registry)
        self.scene = NodeScene(self.graph, self.registry)
        self.view = NodeView(self.scene)
        self.view.resize(1050, 550)
        self.view.show()
        self.settle()
        self.view.centerOn(self.scene.itemsBoundingRect().center())

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def tearDown(self):
        self.scene.refresh_timer.stop()
        self.view.close()
        self.view.deleteLater()
        self.scene.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_curve_endpoints_finite_no_fixed_detour_for_long_horizontal_wire(self):
        for style, _title in WIRE_STYLES:
            for end in (QtCore.QPointF(900, 0), QtCore.QPointF(60, 100), QtCore.QPointF(-200, 10),
                        QtCore.QPointF(0, 0), QtCore.QPointF(0, -300), QtCore.QPointF(10000, 10000)):
                with self.subTest(style=style, end=end):
                    path = connection_path(QtCore.QPointF(), end, style)
                    self.assertEqual(path.pointAtPercent(0), QtCore.QPointF())
                    self.assertEqual(path.currentPosition(), end)
                    self.assertTrue(all(math.isfinite(v) for t in range(21) for v in
                        (path.pointAtPercent(t / 20).x(), path.pointAtPercent(t / 20).y())))
            if style == "smooth":
                path = connection_path(QtCore.QPointF(), QtCore.QPointF(900, 0), style)
                self.assertEqual(path.boundingRect().height(), 0)

    def test_closest_point_projection_and_wide_hit_area_for_all_styles(self):
        for style, _title in WIRE_STYLES:
            self.scene.set_connection_style(style)
            item = self.scene.edges[0]
            mid = item.path().pointAtPercent(.5)
            self.assertTrue(item.shape().contains(mid + QtCore.QPointF(0, 3)))
            point = closest_point(item.path(), mid + QtCore.QPointF(0, 3))
            self.assertLess((point - mid).manhattanLength(), 10)

    def test_backward_smooth_wire_uses_outer_lane_not_self_overlapping_line(self):
        start, end = QtCore.QPointF(700, 150), QtCore.QPointF(0, 150)
        path = connection_path(start, end)
        self.assertLess(path.boundingRect().top(), 100)
        self.assertGreater(path.pointAtPercent(.01).x(), start.x())
        self.assertLess(path.pointAtPercent(.99).x(), end.x())

    def test_short_forward_gap_never_creates_reverse_lobes_or_above_port_detour(self):
        # The screenshot regression: dx < 48 was incorrectly treated as backward.
        for style, _title in WIRE_STYLES:
            for dx in (.1, 1, 5, 20, 40, 47, 48, 49, 100, 1000):
                for dy in (-300, -81, -79, -50, -1, 0, 1, 50, 79, 81, 300):
                    start, end = QtCore.QPointF(20, 145), QtCore.QPointF(20 + dx, 145 + dy)
                    path = connection_path(start, end, style)
                    points = [path.pointAtPercent(i / 100) for i in range(101)]
                    with self.subTest(style=style, dx=dx, dy=dy):
                        self.assertTrue(all(a.x() <= b.x() + 1e-7 for a, b in zip(points, points[1:])))
                        self.assertTrue(all(start.x() - 1e-7 <= point.x() <= end.x() + 1e-7 for point in points))
                        self.assertTrue(all(min(start.y(), end.y()) - 1e-7 <= point.y() <= max(start.y(), end.y()) + 1e-7
                                            for point in points))

    def test_vertical_wires_are_direct_and_have_no_loop(self):
        start, end = QtCore.QPointF(84, 27), QtCore.QPointF(84, 145)
        for style, _title in WIRE_STYLES:
            path = connection_path(start, end, style)
            self.assertEqual(path.boundingRect().width(), 0)
            self.assertEqual(path.currentPosition(), end)

    def test_curves_have_no_self_crossing_for_forward_and_backward_close_ports(self):
        def cross(a, b, c):
            return (b.x() - a.x()) * (c.y() - a.y()) - (b.y() - a.y()) * (c.x() - a.x())
        def intersects(a, b, c, d):
            return cross(a, b, c) * cross(a, b, d) < -1e-8 and cross(c, d, a) * cross(c, d, b) < -1e-8
        for style, _title in WIRE_STYLES:
            for dx, dy in ((40, 50), (40, -50), (1, 1), (-1, 1), (-1, -1),
                           (-20, 50), (-40, -50), (-700, 0), (-700, 300), (-700, -300)):
                path = connection_path(QtCore.QPointF(), QtCore.QPointF(dx, dy), style)
                points = [path.pointAtPercent(i / 100) for i in range(101)]
                with self.subTest(style=style, dx=dx, dy=dy):
                    self.assertFalse(any(intersects(points[i], points[i + 1], points[j], points[j + 1])
                        for i in range(99) for j in range(i + 2, 100)))

    def test_alt_click_native_qt_event_inserts_typed_dot_safely_in_every_style(self):
        for style, _title in WIRE_STYLES:
            self.graph = text_graph(self.registry)
            self.scene.graph = self.graph
            self.scene.rebuild()
            self.scene.set_connection_style(style)
            edge = self.scene.edges[0]
            point = edge.path().pointAtPercent(.5)
            QTest.mouseClick(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.KeyboardModifier.AltModifier, self.view.mapFromScene(point))
            self.settle()
            dots = [n for n in self.graph["nodes"] if n["kind"] == "core.reroute"]
            self.assertEqual(len(dots), 1, style)
            self.assertEqual(len(self.graph["connections"]), 2)
            self.assertIsInstance(self.scene.nodes[dots[0]["id"]], JunctionItem)
            self.assertTrue(self.scene.nodes[dots[0]["id"]].isSelected())
            self.assertIsNone(self.scene.pending)
            self.scene.remove_node(dots[0]["id"])
            self.settle()
            self.assertEqual(len(self.graph["connections"]), 1)

    def test_regular_wire_click_does_not_insert_dot(self):
        point = self.scene.edges[0].path().pointAtPercent(.5)
        QTest.mouseClick(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=self.view.mapFromScene(point))
        self.settle()
        self.assertEqual(len(self.graph["nodes"]), 2)
        self.assertTrue(self.scene.edges[0].isSelected())

    def test_junction_drag_updates_saved_position_and_delete_restores_wire(self):
        node = self.scene.insert_junction(self.graph["connections"][0], QtCore.QPointF(400, 150))
        self.settle()
        item = self.scene.nodes[node["id"]]
        start = self.view.mapFromScene(item.scenePos())
        end = start + QtCore.QPoint(65, -40)
        QTest.mousePress(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.view.viewport(), end)
        QTest.mouseRelease(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=end)
        self.settle()
        self.assertAlmostEqual(node["position"][0], 465, delta=2)
        self.assertAlmostEqual(node["position"][1], 110, delta=2)
        QTest.keyClick(self.view, QtCore.Qt.Key.Key_Delete)
        self.settle()
        self.assertNotIn(node, self.graph["nodes"])
        self.assertEqual(len(self.graph["connections"]), 1)

    def test_double_click_preview_only_emits_content_request(self):
        requests = []
        self.scene.previewRequested.connect(requests.append)
        for node, expected in zip(self.graph["nodes"], (0, 1)):
            item = self.scene.nodes[node["id"]]
            point = self.view.mapFromScene(item.scenePos() + QtCore.QPointF(100, 25))
            QTest.mouseClick(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseDClick(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton,
                pos=point)
            QTest.mouseRelease(self.view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
            self.settle()
            self.assertEqual(len(requests), expected)
        self.assertEqual(requests, [self.graph["nodes"][-1]["id"]])

    def test_changing_wire_style_keeps_nodes_and_connections_not_rebuilt(self):
        original_nodes, original_edges = dict(self.scene.nodes), list(self.scene.edges)
        for style, _title in WIRE_STYLES:
            self.scene.set_connection_style(style)
            self.assertEqual(self.scene.connection_style(), style)
            self.assertEqual(self.scene.nodes, original_nodes)
            self.assertEqual(self.scene.edges, original_edges)
        self.assertEqual(self.graph["view"]["connection_style"], "rounded")


FFMPEG = os.environ.get("NODE_FFMPEG", "")


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要实际 FFmpeg")
class RoutedMediaPreviewTests(unittest.TestCase):
    def test_plan_preview_after_reroute_has_processed_frame_no_final_export(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "red.mp4"
            completed = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                "color=c=red:s=240x320:r=25:d=2", "-c:v", "libx264", "-threads", "2", str(video_path)],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reg = registry()
            video = ResourceRef.capture(video_path, "video")
            timeline = VideoTimeline((VideoSection(video, .1, 1.6),), 240, 320, "25", "test")
            plan = RenderPlan(timeline, (FilterOperation("反相", timeline, "negate"),), "")
            source = NodeDefinition("test.visual", "计划", outputs=(NodePort("value", "计划", "render_plan"),),
                build=lambda _i, _p, _c: {"value": plan})
            reg.register("test", source)
            a, b = new_node(source), new_node(reg.definitions["core.preview"])
            graph = {**empty_graph(), "nodes": [a, b], "connections": [[a["id"], "value", b["id"], "value"]]}
            insert_reroute(graph, reg, graph["connections"][0], [300, 0])
            payload, _resources = prepare_job(graph, reg, None, None, root_id=b["id"])
            payload.update(preview_node=b["id"], ffmpeg=FFMPEG)
            context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
            result = run_preview(payload, context, registry=reg)
            image = Image.open(io.BytesIO(base64.b64decode(result["png"]))).convert("RGB")
            self.assertEqual(image.size, (240, 320))
            r, g, blue = image.getpixel((20, 20))
            self.assertLess(r, 40)
            self.assertGreater(g, 200)
            self.assertGreater(blue, 200)
            self.assertIn("反相", result["text"])
            self.assertEqual(list(root.iterdir()), [video_path])


if __name__ == "__main__":
    unittest.main()

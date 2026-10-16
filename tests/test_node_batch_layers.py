"""Shared-code boundary, read-only pools and real multi-input node renders."""
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtGui, QtWidgets
from PIL import Image, ImageChops, ImageStat
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ResourceRef, ResourcePool, FilterOperation, RenderPlan, VideoTimeline, VideoSection
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, validate, evaluate_graph
from model.node_workflow.tasks import prepare_job
from model.node_workflow.global_data import WorkflowGlobals
from model.node_workflow.render import compile_filters, run_workflow, render_plan, probe
from model.node_workflow.preview import run_preview
from model.node_workflow.filter_graph import VideoFilterChain
from app_plugins.builtin.batch_text_video.workflow_nodes import canvas_nodes
from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, build_layer, prepare_layer, new_layer
from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
from app_plugins.builtin.batch_text_video.workflow_pool_ui import PoolNodeEditor
from app_plugins.builtin.batch_text_video.workflow_pools import prepare_pool, build_pool, pool_records
from app_plugins.builtin.batch_text_video.store import Store, fresh_state, DEFAULTS
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.captions import static_planes
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def context():
    c = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
    c.node_prepared, c.prepared, c.notes = {}, {}, []
    c.workflow_globals = WorkflowGlobals.create("line", 5)
    return c


class BatchLayerNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        for node in canvas_nodes(): self.registry.register("batch_text_video", node)
        self.c = context()
        self.c.workflow_registry = self.registry
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic")
        self.video = ResourceRef.capture(self.source, "video")
        self.image = self.root / "图片,[分号;].png"
        im = QtGui.QImage(80, 50, QtGui.QImage.Format.Format_ARGB32)
        im.fill(QtGui.QColor("red")); self.assertTrue(im.save(str(self.image)))
        self.store = Store(self.root / "BatchTextVideo")
        state = fresh_state()
        state["copy_pool"] = [{"id": "a", "title": "标题A", "body": "正文A"}, {"id": "b", "title": "标题B", "body": "正文B"}]
        state["image_pool"] = [{"id": "im", "path": str(self.image), "name": "图片"}]
        self.state = self.store.save(state, 0)

    def params(self, media="text", **layer):
        p = {param.key: copy.deepcopy(param.default) for param in layer_node(media).parameters}
        p["layer"].update(layer)
        return p

    def pool(self, pool_id="builtin:copy", profile_id=""):
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        runtime=self.store.load()
        if profile_id:
            _name,data=self.store.read_profile(profile_id)
            runtime.update(data,active_profile=profile_id)
        self.c.node_prepared = prepare_pool(None, {"pool_id": pool_id}, ProjectServices(None,runtime))
        return build_pool({}, {}, self.c)["pool"]

    def test_registration_and_optional_ports_keep_video_at_settler(self):
        self.assertEqual(len(canvas_nodes()), 8)
        self.assertIn('batch_text_video.component_source', {node.kind for node in canvas_nodes()})
        for kind in ("text", "image", "animated", "visual"):
            definition = layer_node(kind)
            self.assertTrue(all(not p.required for p in definition.inputs))
            self.assertEqual(next(p for p in definition.inputs if p.key == "pool").data_type, "resource_pool")
            self.assertNotIn("video", [p.key for p in definition.inputs])
            self.assertIsNotNone(definition.compile_operation)

    def test_pool_is_readonly_and_does_not_change_active_profile(self):
        other = self.store.create_profile(self.state, "另一项目", clone=False)
        active = other["active_profile"]
        original = self.store.path.read_bytes()
        pool = self.pool(profile_id="default")
        self.assertEqual(pool.data["items"][0]["title"], "标题A")
        self.assertEqual(self.store.load()["active_profile"], active)
        self.assertEqual(self.store.path.read_bytes(), original)
        with patch.object(Store, "load", side_effect=AssertionError("worker must not reread project")):
            self.assertEqual(build_pool({}, {}, self.c)["pool"], pool)

    def test_pool_rotates_by_current_iteration_not_completion_order(self):
        pool = self.pool()
        original = self.store.path.read_bytes()
        params = self.params(pool_field="title")
        for iteration, expected in ((3, "标题A"), (2, "标题B"), (1, "标题A"), (5, "标题A")):
            self.c.workflow_globals = self.c.workflow_globals.at_iteration(iteration)
            result = build_layer({"pool": pool, "content": "忽略"}, params, self.c)
            layer = json.loads(dict(result["operations"][0].options)["layer"])
            self.assertEqual(layer["text"], expected)
        self.assertEqual(self.store.path.read_bytes(), original)
        result = build_layer({"pool": pool, "index": 2}, params, self.c)
        self.assertEqual(json.loads(dict(result["operations"][0].options)["layer"])["text"], "标题B")
        with self.assertRaisesRegex(ValueError, "从1开始"):
            build_layer({"pool": pool, "index": 1.5}, params, self.c)

    def test_custom_text_pool_uses_original_binder_and_utf8_rules(self):
        file = self.root / "文案.txt"
        file.write_text("标题\n正文两行\n下一行", encoding="utf-8-sig")
        state = self.store.load()
        state["resource_pools"] = [{"id": "texts", "name": "自建文字", "kind": "text",
                                   "items": [{"id": "t", "path": str(file)}], "next_id": ""}]
        self.store.save(state, state["revision"])
        pool = self.pool("texts")
        params = self.params(pool_field="body")
        with patch("app_plugins.builtin.batch_text_video.pools.bind_pool_layers", wraps=__import__(
                "app_plugins.builtin.batch_text_video.pools", fromlist=["bind_pool_layers"]).bind_pool_layers) as binder:
            result = build_layer({"pool": pool}, params, self.c)
            binder.assert_called_once()
        self.assertEqual(json.loads(dict(result["operations"][0].options)["layer"])["text"], "正文两行\n下一行")
        self.assertEqual(len(result["operations"][0].assets), 1)

    def test_pool_type_and_selected_file_stamp_are_checked(self):
        pool = self.pool("builtin:image")
        with self.assertRaisesRegex(ValueError, "文案池"):
            build_layer({"pool": pool}, self.params(), self.c)
        op = build_layer({"pool": pool}, self.params("image"), self.c, media="image")["operations"][0]
        self.assertIsNone(op.video)
        self.image.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            build_layer({"pool": pool}, self.params("image"), self.c, media="image")

    def test_inspectors_reuse_full_editors_and_save_without_qt_exceptions(self):
        values = []
        editor = LayerNodeEditor(self.params(text="测试"), values.append)
        self.addCleanup(editor.deleteLater)
        self.assertTrue(('font' in editor.panel.editor.sections))
        self.assertEqual(editor.panel.content_editor.source.count(), 2)
        self.assertIn("font_weight", editor.panel.text_properties.controls)
        editor.panel.properties["x"].setValue(40)
        self.assertEqual(values[-1]["layer"]["x"], 40)
        params = self.params("animated")
        visual = LayerNodeEditor(params, values.append)
        self.addCleanup(visual.deleteLater)
        self.assertTrue(('visual' in visual.panel.editor.sections))
        self.assertIn("key_mode", visual.panel.visual_properties.controls)
        self.assertIn("blend_mode", visual.panel.visual_properties.controls)
        bad = LayerNodeEditor(self.params(), lambda _: (_ for _ in ()).throw(ValueError("test")))
        self.addCleanup(bad.deleteLater)
        bad.offset.setValue(2)
        self.assertIn("test", bad.error.text())

    def test_pool_editor_lists_profiles_and_pools_without_saving(self):
        original = self.store.path.read_bytes()
        changes = []
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        parent=QtWidgets.QWidget();parent.workflow_services=ProjectServices(None,self.state)
        self.addCleanup(parent.deleteLater)
        editor = PoolNodeEditor({"pool_id": "builtin:image"}, changes.append,parent)
        self.addCleanup(editor.deleteLater)
        self.assertEqual(editor.pools.currentData(), "builtin:image")
        self.assertFalse(hasattr(editor,'profiles'))
        self.assertEqual(editor.pools.count(), 4)
        editor.pools.setCurrentIndex(editor.pools.findData("builtin:copy"))
        self.assertEqual(changes[-1]["pool_id"], "builtin:copy")
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_text_plane_is_identical_to_original_batch_renderer(self):
        params = self.params(text="Božia láska", font="Open Sans", font_weight=750, outline_color="#8b0000", shadow_enabled=True)
        self.c.node_prepared = prepare_layer(None, params, None)
        op = build_layer({}, params, self.c)["operations"][0]
        plan = RenderPlan(self.video, (op,), "")
        info = {"duration": 2, "video": {"width": 360, "height": 640, "avg_frame_rate": "10/1"}, "audio": False}
        chain = compile_filters(plan, self.root, self.c, video_info=info)
        self.assertIsInstance(chain, VideoFilterChain)
        rendered = QtGui.QImage(chain.steps[0].inputs[0][-1])
        layer = json.loads(dict(op.options)["layer"])
        settings = {**copy.deepcopy(DEFAULTS), "layers": [layer]+[dict(d, enabled=False) for d in DEFAULT_LAYERS],
                    "darkness": 0, "background_tint_strength": 0}
        expected = static_planes("", "", 360, 640, settings, ImageCache())[0][1]
        expected_path = self.root / "expected.png"
        self.assertTrue(expected.save(str(expected_path), "PNG"))
        # QImage's decoded PNG uses straight alpha, while the painter uses
        # premultiplied alpha. Compare their actual exported assets instead.
        self.assertEqual(Path(chain.steps[0].inputs[0][-1]).read_bytes(), expected_path.read_bytes())
        self.assertEqual(list(self.root.glob("layer_*.mp4")), [])


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实FFmpeg")
class RealBatchLayerNodeTests(BatchLayerNodeTests):
    def ffmpeg(self, args):
        run = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *args], capture_output=True, timeout=40)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        return run.stdout

    def make_media(self):
        self.source.unlink()
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=gray:s=360x640:r=10:d=2",
                     "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.source)])
        self.video = ResourceRef.capture(self.source, "video")
        self.gif = self.root / "动画,[;].gif"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc2=s=80x80:r=10:d=1", str(self.gif)])
        self.mp4 = self.root / "绿幕.mp4"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=0x00ff00:s=80x80:r=10:d=1,drawbox=x=20:y=20:w=40:h=40:color=red:t=fill",
                     "-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.mp4)])

    def graph(self):
        graph = empty_graph()
        nodes = {}
        for name, kind in (("import", "core.import"), ("pool", "batch_text_video.load_pool"),
                           ("text", "batch_text_video.text_layer"), ("image", "batch_text_video.image_layer"),
                           ("gif", "batch_text_video.animated_layer"), ("mp4", "batch_text_video.animated_layer"),
                           ("settle", "core.settle"), ("preview", "core.preview"), ("export", "core.export")):
            nodes[name] = new_node(self.registry.definitions[kind]); graph["nodes"].append(nodes[name])
        nodes["import"]["params"]["video"] = str(self.source)
        nodes["pool"]["params"].update(store_file=str(self.store.path), profile_id="default", pool_id="builtin:copy")
        nodes["text"]["params"]["layer"].update(font_min=20, font_max=20, width=90, height=20, y=15)
        nodes["image"]["params"]["layer"].update(source_file=str(self.image), path=str(self.image), x=25, y=50)
        nodes["gif"]["params"]["layer"].update(source_file=str(self.gif), path=str(self.gif), x=75, y=50)
        nodes["mp4"]["params"]["layer"].update(source_file=str(self.mp4), path=str(self.mp4), x=50, y=80, key_mode="green", key_similarity=.2)
        nodes["export"]["params"].update(destination="file", output=str(self.root / "成品.mp4"))
        def wire(a, out, b, inp): graph["connections"].append([nodes[a]["id"], out, nodes[b]["id"], inp])
        wire("import", "video", "settle", "video")
        wire("pool", "pool", "text", "pool")
        for name in ("text", "image", "gif", "mp4"): wire(name, "operations", "settle", "operations")
        wire("settle", "plan", "preview", "value")
        wire("preview", "value", "export", "plan")
        graph["output_node"] = nodes["export"]["id"]
        return graph, nodes

    def test_actual_multi_layer_graph_export_preview_and_source_audio(self):
        self.make_media()
        graph, nodes = self.graph()
        self.assertEqual(validate(graph, self.registry)[0], [])
        original = self.store.path.read_bytes()
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        payload, _ = prepare_job(graph, self.registry, None, ProjectServices(None,self.store.load()))
        payload["ffmpeg"] = FFMPEG
        preview_payload = {**payload, "preview_node": nodes["preview"]["id"]}
        with ThreadPoolExecutor(max_workers=1) as executor:
            preview = executor.submit(run_preview, preview_payload, context(), registry=self.registry).result(timeout=60)
            result = executor.submit(run_workflow, payload, context(), registry=self.registry).result(timeout=60)
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        info = probe(result["video"], str(Path(FFMPEG).with_name("ffprobe.exe")))
        self.assertTrue(info["audio"])
        self.assertAlmostEqual(info["duration"], 2, delta=.15)
        def pcm(path):
            return self.ffmpeg(["-i", str(path), "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"])
        self.assertEqual(pcm(result["video"]), pcm(self.source))
        frame = self.ffmpeg(["-ss", "1", "-i", result["video"], "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
        import io
        a = Image.open(io.BytesIO(base64.b64decode(preview["png"]))).convert("RGB")
        b = Image.open(io.BytesIO(frame)).convert("RGB")
        self.assertEqual(a.size, b.size)
        self.assertLess(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)/3, 8)
        r, g, blue = b.getpixel((90, 320))  # Native-resolution image layer center.
        self.assertGreater(r, 180); self.assertLess(g, 70)
        r, g, blue = b.getpixel((180, 512))  # Native-resolution center of keyed video.
        self.assertGreater(r, 180); self.assertLess(g, 70)
        r, g, blue = b.getpixel((145, 477))  # Interior green corner is transparent.
        self.assertLess(abs(r-g), 20); self.assertLess(abs(g-blue), 20)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".node-render-*")), [])

    def test_memory_stream_and_timeline_sources_support_same_visual_compiler(self):
        self.make_media()
        from model.node_workflow.media import MediaSession
        params = self.params("animated", source_file=str(self.gif), path=str(self.gif), blend_mode="screen")
        self.c.node_prepared = prepare_layer(None, params, None)
        operation = build_layer({}, params, self.c, media="animated")["operations"][0]
        self.c.ffmpeg = FFMPEG
        with MediaSession(self.c, FFMPEG) as session:
            self.c.media = session
            for mode in ("memory", "stream", "timeline"):
                if mode == "timeline":
                    video = VideoTimeline((VideoSection(self.video, 0, 1), VideoSection(self.video, 1, 2)), 360, 640, "10/1", "")
                else:
                    video = session.convert(self.video, mode)
                result = render_plan(RenderPlan(video, (operation,), str(self.root / (mode + ".mp4"))), self.c, FFMPEG)
                self.assertEqual(result["video_encodings"], 1)
                self.assertEqual(result["intermediate_videos"], 0)

    def test_layer_order_caption_interleaving_and_nonlooping_video(self):
        self.make_media()
        self.c.ffmpeg = FFMPEG
        blue = self.root / "上层.png"
        image = QtGui.QImage(80, 50, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor("blue")); self.assertTrue(image.save(str(blue)))
        operations = []
        for path in (self.image, blue):
            params = self.params("image", path=str(path), source_file=str(path), x=25, y=50)
            self.c.node_prepared = prepare_layer(None, params, None)
            operations.extend(build_layer({}, params, self.c, media="image")["operations"])
        srt = self.root / "字幕.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nSpoločné titulky\n", encoding="utf-8")
        from app_plugins.builtin.batch_text_video.workflow_nodes import build_caption
        operations.extend(build_caption({"subtitles": ResourceRef.capture(srt, "subtitles")}, {}, self.c)["operations"])
        params = self.params("animated", path=str(self.mp4), source_file=str(self.mp4), x=75, y=50, media_loop=False)
        self.c.node_prepared = prepare_layer(None, params, None)
        operations.extend(build_layer({}, params, self.c, media="animated")["operations"])
        output = self.root / "顺序和隐藏.mp4"
        result = render_plan(RenderPlan(self.video, tuple(operations), str(output)), self.c, FFMPEG)
        import io
        def frame(at):
            data = self.ffmpeg(["-ss", str(at), "-i", str(output), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
            return Image.open(io.BytesIO(data)).convert("RGB")
        early, late = frame(.3), frame(1.5)
        self.assertGreater(early.getpixel((90, 320))[2], 180)  # Last image wire is above red image.
        self.assertLess(early.getpixel((90, 320))[0], 70)
        self.assertGreater(early.getpixel((224, 274))[1], 160)  # MP4 is visible before EOF.
        r, g, b = late.getpixel((224, 274))
        self.assertLess(abs(r-g), 20); self.assertLess(abs(g-b), 20)  # No frozen MP4 after EOF.
        self.assertGreater(late.crop((20, 520, 340, 635)).getextrema()[0][1], 230)  # Caption stayed in the mixed chain.
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)


if __name__ == "__main__": unittest.main()

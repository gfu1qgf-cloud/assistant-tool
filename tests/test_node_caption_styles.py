"""Full caption-node parity, editor controls, deferred compile and real export."""
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from PIL import Image, ImageChops, ImageStat
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import FilterOperation, ResourceRef, RenderPlan, SubtitleCue, SubtitleTrack
from model.node_workflow.graph import NodeRegistry, register_core, example_graph, validate, new_node, empty_graph
from model.node_workflow.tasks import prepare_job
from model.node_workflow.render import compile_filters, run_workflow, probe
from model.node_workflow.preview import run_preview
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node, build_caption
from app_plugins.builtin.batch_text_video.workflow_caption import normalize_node_caption, subtitle_words
from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
from app_plugins.builtin.batch_text_video.caption_styles import preset_choices, apply_style
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG = os.environ.get("NODE_FFMPEG", "")
STYLES = ["pop", "appear"] + [key for _name, key in preset_choices()]


class CaptionStyleNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subtitle = self.root / "字幕,特殊字符.srt"
        self.subtitle.write_text("1\n00:00:00,250 --> 00:00:00,800\nBožia\n\n"
            "2\n00:00:00,950 --> 00:00:01,500\nláska\n\n"
            "3\n00:00:01,650 --> 00:00:03,200\nnami.\n", encoding="utf-8")
        self.asset = ResourceRef.capture(self.subtitle, "subtitles")
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic")
        self.video = ResourceRef.capture(self.source, "video")
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.registry.register("batch_text_video", caption_node())
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.context.workflow_registry = self.registry
        self.info = {"duration": 3.5, "video": {"width": 360, "height": 640}, "audio": True}

    def layer(self, preset="pop", **values):
        layer = apply_style(new_caption(), preset)
        layer.update(caption_timing="task", **values)
        return normalize_node_caption(layer)

    def operation(self, layer):
        return build_caption({"subtitles": self.asset}, {"caption": layer}, self.context)["operations"][0]

    def test_all_68_styles_compile_to_identical_batch_ass(self):
        self.assertEqual(len(STYLES), 68)
        words = subtitle_words(self.asset, 3.5)
        for index, preset in enumerate(STYLES):
            with self.subTest(preset=preset):
                layer = self.layer(preset)
                operation = self.operation(layer)
                self.assertIsNone(operation.video)
                folder = self.root / str(index)
                folder.mkdir()
                chain = compile_filters(RenderPlan(self.video, (operation,), ""), folder, self.context, video_info=self.info)
                name = re.search(r"ass=filename='([^']+)'", chain).group(1)
                expected = folder / "expected.ass"
                write_caption_ass(expected, words, layer, 360, 640, 3.5)
                self.assertEqual((folder / name).read_bytes(), expected.read_bytes())
                self.assertFalse(list(folder.glob("*.mp4")))
                json.dumps(dict(operation.options), ensure_ascii=False)

    def test_cue_timestamps_remain_authoritative_and_uniform_is_explicit(self):
        track = SubtitleTrack((SubtitleCue(.2, 2.8, "Božia láska nami."),), "", 3.5)
        exact = subtitle_words(track, 3.5)
        self.assertEqual([word["start"] for word in exact], [.2, .2, .2])
        with mock.patch("app_plugins.builtin.batch_text_video.captions.load_word_timings", side_effect=AssertionError("不得加载模型")):
            uniform = subtitle_words(track, 3.5, True)
        self.assertEqual(uniform[0]["start"], .2)
        self.assertEqual(uniform[-1]["end"], 2.8)
        self.assertGreater(uniform[1]["start"], uniform[0]["start"])

    def test_missing_compiler_fails_closed_not_as_raw_ffmpeg(self):
        self.context.workflow_registry = None
        with self.assertRaisesRegex(ValueError, "未注册"):
            compile_filters(RenderPlan(self.video, (self.operation(self.layer()),), ""), self.root,
                            self.context, video_info=self.info)

    def test_node_hooks_must_be_callable(self):
        for field in ("inspector_factory", "compile_operation", "validate_parameters"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.registry.register("test", replace(caption_node(), kind="test.caption", **{field: "not callable"}))

    def test_invalid_full_style_disables_graph_before_submission(self):
        graph = example_graph(self.registry)
        graph["nodes"][0]["params"].update(video=str(self.source), subtitles=str(self.subtitle))
        graph["nodes"][-1]["params"]["output"] = str(self.root / "final.mp4")
        layer = self.layer()
        layer["font_min"], layer["font_max"] = 100, 20
        graph["nodes"][1]["params"]["caption"] = layer
        errors = validate(graph, self.registry)[0]
        self.assertTrue(any("最小字号" in message for _, message in errors))
        with self.assertRaisesRegex(ValueError, "最小字号"):
            prepare_job(graph, self.registry, None, None)

    def test_node_never_adds_model_or_competing_source_modes(self):
        for mode in ("model", "auto", "file"):
            layer = self.layer()
            layer["caption_timing"] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "输入字幕"):
                self.operation(layer)

    def test_editor_reuses_all_styles_gallery_font_and_effect_controls(self):
        updates = []
        editor = CaptionNodeEditor({}, updates.append)
        try:
            editor.mode.setCurrentIndex(1)
            properties = editor.panel.caption_properties
            self.assertEqual(properties.controls["caption_preset"].count(), 68)
            self.assertEqual(properties.controls["caption_timing"].count(), 2)
            self.assertFalse(properties.gallery_button.isHidden())
            properties.controls["caption_preset"].setCurrentIndex(
                properties.controls["caption_preset"].findData("videokit:渐变蓝色框"))
            self.assertEqual(updates[-1]["caption"]["caption_preset"], "videokit:渐变蓝色框")
            self.assertTrue(properties.effect_button.isEnabled())
            self.assertTrue(('font' in editor.panel.editor.sections))
            self.assertTrue(('position' in editor.panel.editor.sections))
            self.assertFalse(('source' in editor.panel.editor.sections))
            self.assertIn("font_weight", editor.panel.text_properties.controls)
            self.assertIn("shadow_color", editor.panel.text_properties.controls)
            editor.panel.properties["y"].setValue(33)
            self.assertEqual(updates[-1]["caption"]["y"], 33)
        finally:
            editor.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_gui_callback_failure_is_displayed_not_escaped(self):
        editor = CaptionNodeEditor({}, lambda _values: (_ for _ in ()).throw(ValueError("synthetic error")))
        try:
            editor.mode.setCurrentIndex(1)
            self.assertIn("synthetic error", editor.error_label.text())
            editor.panel.current()["font_min"] = 199
            editor.panel.current()["font_max"] = 10
            editor.caption_changed()
            self.assertFalse(editor.error_label.isHidden())
        finally:
            editor.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_main_node_inspector_saves_full_style_without_global_config_changes(self):
        from test_node_workspace import NodeWorkspaceTests
        case = NodeWorkspaceTests("test_workspace_is_lazy_and_does_not_change_original_panes")
        case.app = self.app
        case.setUp()
        try:
            panel = case.panel()
            caption = next(node for node in panel.graph["nodes"] if node["kind"] == "batch_text_video.caption")
            panel.scene.nodes[caption["id"]].setSelected(True)
            case.settle()
            editor = panel.inspector.widget().findChild(CaptionNodeEditor)
            self.assertIsNotNone(editor)
            editor.mode.setCurrentIndex(1)
            preset = editor.panel.caption_properties.controls["caption_preset"]
            preset.setCurrentIndex(preset.findData("videokit:圣光降临"))
            editor.panel.text_properties.controls["font_weight"].setValue(900)
            self.assertEqual(caption["params"]["caption"]["caption_preset"], "videokit:圣光降临")
            self.assertEqual(caption["params"]["caption"]["font_weight"], 900)
            panel.path = Path(case.temp.name) / "full-style.json"
            panel.export_file()
            saved = json.loads(panel.path.read_text(encoding="utf-8"))
            stored = next(node for node in saved["nodes"] if node["kind"] == "batch_text_video.caption")
            self.assertEqual(stored["params"]["caption"], caption["params"]["caption"])
            self.assertEqual(json.loads(Path("config.json").read_text(encoding="utf-8")), case.config)
        finally:
            case.tearDown()

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实FFmpeg")
    def test_real_styled_preview_and_export_are_single_pass_and_match(self):
        def execute(args):
            result = subprocess.run(args, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            return result.stdout
        execute([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x263748:s=360x640:r=10:d=3.5",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3.5", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source)])
        for index, preset in enumerate(("pop", "videokit:渐变蓝色框", "videokit:霓虹多层描边")):
            with self.subTest(preset=preset):
                imported = new_node(self.registry.definitions["core.import"])
                imported["params"].update(video=str(self.source), subtitles=str(self.subtitle))
                caption = new_node(caption_node())
                caption["params"]["caption"] = self.layer(preset)
                settle = new_node(self.registry.definitions["core.settle"])
                export = new_node(self.registry.definitions["core.export"])
                output = self.root / f"final{index}.mp4"
                export["params"].update(destination="file", output=str(output))
                graph = {**empty_graph(), "nodes": [imported, caption, settle, export], "output_node": export["id"],
                    "connections": [[imported["id"], "video", settle["id"], "video"],
                        [imported["id"], "subtitles", caption["id"], "subtitles"],
                        [caption["id"], "operations", settle["id"], "operations"],
                        [settle["id"], "plan", export["id"], "plan"]]}
                payload, _ = prepare_job(graph, self.registry, None, None)
                payload["ffmpeg"] = FFMPEG
                preview = run_preview({**payload, "preview_node": settle["id"]}, self.context, registry=self.registry)
                self.assertFalse(output.exists())
                with ThreadPoolExecutor(max_workers=1) as workers:
                    result = workers.submit(run_workflow, payload, self.context, registry=self.registry).result(timeout=30)
                self.assertEqual((result["video_encodings"], result["intermediate_videos"]), (1, 0))
                info = probe(output, str(Path(FFMPEG).with_name("ffprobe.exe")))
                self.assertTrue(info["audio"])
                self.assertAlmostEqual(info["duration"], 3.5, places=1)
                rendered = execute([FFMPEG, "-v", "error", "-ss", "1", "-i", str(output), "-frames:v", "1",
                    "-vf", "scale=w='min(iw,1080)':h='min(ih,1080)':force_original_aspect_ratio=decrease", "-f", "image2pipe", "-vcodec", "png", "-"])
                a = Image.open(io.BytesIO(base64.b64decode(preview["png"]))).convert("RGB")
                b = Image.open(io.BytesIO(rendered)).convert("RGB")
                self.assertEqual(a.size, b.size)
                self.assertLess(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3, 5)
                self.assertFalse(any(path.name.startswith(".node-render-") for path in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()

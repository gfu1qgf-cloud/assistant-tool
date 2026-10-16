"""Vertical typography and reusable image motion: UI, persistence, real FFmpeg."""
import copy
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, TextDoesNotFit
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.vertical_text import graphemes, text_layout
from app_plugins.builtin.batch_text_video.visual_animation import normalize_animation, motion_at, expressions
from app_plugins.builtin.batch_text_video.visual_media import add_plane, needs_plane
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.playback import preview_key
from app_plugins.builtin.batch_text_video.engine import resolve_tools, render_static_text, probe
from app_plugins.builtin.batch_text_video.captions import static_planes, new_caption
from app_plugins.builtin.batch_text_video.scrolling_text import render_scroll_assets

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def text(**updates):
    value = new_text_component()
    value.update(text="平安喜乐\n日日感恩", font="Microsoft YaHei UI", font_min=48, font_max=70,
                 width=80, height=80, writing_mode="vertical_rl", outline_width=0, padding=4)
    value.update(updates)
    return normalize_layers([value])[0]


def visual(path="placeholder.png", **updates):
    value = {"id": "picture", "kind": "image", "name": "图片", "path": str(path),
        "source": "fixed", "enabled": True, "opacity": 100, "x": 50, "y": 50,
        "width": 40, "height": 50, "visual_animation": "pulse", "animation_strength": 40}
    value.update(updates)
    return normalize_layers([value])[0]


class VerticalMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_legacy_defaults_remain_horizontal_and_static(self):
        value = normalize_layers([new_text_component()])[0]
        self.assertEqual(value["writing_mode"], "horizontal")
        picture = visual(visual_animation="none")
        self.assertFalse(needs_plane(picture))
        self.assertTrue(needs_plane(visual()))

    def test_unknown_modes_nonfinite_and_boolean_parameters_are_rejected(self):
        for mode in ("diagonal", "", 123):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                text(writing_mode=mode)
        for key, bad in (("visual_animation", "unknown"), ("animation_strength", float("nan")),
                         ("animation_strength", 101), ("animation_period", 0), ("animation_period", True),
                         ("animation_end_early", -1), ("animation_start_delay", float("inf"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                visual(**{key: bad})
        caption = new_caption(); caption["writing_mode"] = "vertical_rl"
        with self.assertRaisesRegex(ValueError, "逐词字幕"):
            normalize_layers([caption])

    def test_unicode_graphemes_do_not_split_accent_emoji_or_zwj(self):
        value = "A\u0301🧑‍💻🇮🇹B"
        clusters = list(graphemes(value))
        self.assertEqual(clusters, ["A\u0301", "🧑‍💻", "🇮🇹", "B"])
        self.assertEqual("".join(clusters), value)
        self.assertEqual(list(graphemes("🇮🇹🇫🇷👍🏽1️⃣")), ["🇮🇹", "🇫🇷", "👍🏽", "1️⃣"])

    def test_vertical_column_order_and_explicit_linebreaks(self):
        for mode, right in (("vertical_rl", True), ("vertical_lr", False)):
            groups, height, width = text_layout("AB\nCD", "Segoe UI", 24, 200, 200, "left", 1.15,
                {"writing_mode": mode}, 1)
            positions = {layout.text(): (x, y) for layout, x, y in groups[0][0].placements}
            self.assertGreater(positions["A"][0], positions["C"][0]) if right else self.assertLess(
                positions["A"][0], positions["C"][0])
            self.assertGreater(positions["B"][1], positions["A"][1])
            self.assertGreater(width, 24); self.assertGreater(height, 24)

    def test_punctuation_uses_vertical_forms_without_changing_original(self):
        value = text(text="「平安」，感恩。")
        original = copy.deepcopy(value)
        groups, _, _ = text_layout(value["text"], value["font"], 20, 300, 300, "left", 1.15, value)
        displayed = "".join(layout.text() for layout, _, _ in groups[0][0].placements)
        self.assertIn("﹁", displayed); self.assertIn("︐", displayed); self.assertIn("︒", displayed)
        self.assertEqual(value, original)

    def test_auto_wrap_and_auto_size_fit_both_dimensions(self):
        value = text(text="ABCDEFGH"*3, font="Segoe UI", width=22, height=34,
                     font_min=16, font_max=150, x=50, y=50)
        image, size = render_text_box(value, 360, 640)
        self.assertGreaterEqual(size, 16); self.assertLess(size, 150)
        self.assertFalse(image.isNull())
        with self.assertRaises(TextDoesNotFit):
            render_text_box(text(text="A"*400, width=2, height=2, font_min=100, font_max=100), 360, 640)

    def test_nine_alignments_and_column_direction_affect_pixels(self):
        signatures = set()
        for mode in ("vertical_rl", "vertical_lr"):
            for align in ("left", "center", "right"):
                for vertical in ("top", "center", "bottom"):
                    image, _ = render_text_box(text(text="AB\nC", font="Segoe UI", font_min=54, font_max=54,
                        writing_mode=mode, align=align, vertical=vertical), 360, 640)
                    signatures.add(bytes(image.constBits().asstring(image.sizeInBytes())))
        self.assertEqual(len(signatures), 18)

    def test_vertical_scroll_keeps_columns_and_rejects_width_overflow(self):
        assets = render_scroll_assets(text(text="ABCDEFGHIJKLMNO", font="Segoe UI", text_animation="up",
            height=15, font_min=72, font_max=72), 360, 640)
        self.assertGreater(assets["text_height"], assets["height"])
        with self.assertRaisesRegex(TextDoesNotFit, "列宽"):
            render_scroll_assets(text(text="A\nB\nC\nD\nE", text_animation="up", width=5, font_max=100), 360, 640)

    def test_pulse_returns_to_rest_breath_is_smooth_and_does_not_drift(self):
        layer = visual(animation_strength=20, animation_period=2)
        self.assertAlmostEqual(motion_at(layer, 20, 0)[0], 1)
        self.assertAlmostEqual(motion_at(layer, 20, .45)[0], 1.2)
        self.assertAlmostEqual(motion_at(layer, 20, 1.5)[0], 1)
        self.assertEqual(motion_at(layer, 20, .45), motion_at(layer, 20, 10.45))
        layer["visual_animation"] = "breathe"
        self.assertAlmostEqual(motion_at(layer, 20, 1)[0], 1.2)
        self.assertAlmostEqual(motion_at(layer, 20, 2)[0], 1)

    def test_motion_timing_holds_use_final_duration(self):
        layer = visual(visual_animation="zoom_in", animation_start_delay=1, animation_end_early=2)
        self.assertEqual(motion_at(layer, 6, 0), motion_at(layer, 6, 1))
        self.assertEqual(motion_at(layer, 6, 4), motion_at(layer, 6, 6))
        self.assertAlmostEqual(motion_at(layer, 6, 2.5)[0], 1.2)
        layer["visual_animation"] = "zoom_out"
        self.assertAlmostEqual(motion_at(layer, 6, 1)[0], 1.4)
        self.assertAlmostEqual(motion_at(layer, 6, 4)[0], 1)
        layer["visual_animation"] = "float"
        self.assertAlmostEqual(motion_at(layer, 6, 1.5)[1], .4)
        with self.assertRaisesRegex(ValueError, "最终视频时长"):
            expressions(layer, 3)

    def test_shared_plane_routes_animation_and_preserves_layer_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"picture.png"
            image = QtGui.QImage(128, 64, QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor("red")); image.save(str(path))
            layer = visual(path)
            settings = fresh_state()["settings"]
            settings["layers"] = normalize_layers([layer, text(text="TOP")]+[dict(d, enabled=False) for d in DEFAULT_LAYERS])
            planes = static_planes("", "", 240, 160, settings, ImageCache())
            self.assertEqual([kind for kind, _ in planes], ["image", "visual", "image"])
            args, filters = [], []
            add_plane(args, filters, layer, 3, "in", "out", 240, 160, 4, Fraction(20))
            self.assertEqual(args.count("-i"), 1)
            self.assertIn("eval=frame", ";".join(filters))
            self.assertIn("overlay_w/2", ";".join(filters))
            self.assertIn("[3:v]", ";".join(filters))

    def test_ui_entry_points_apply_and_old_caption_has_direction_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            panel = LayerPanel(None, root)
            panel.add_text()
            control = panel.text_properties.controls["writing_mode"]
            control.setCurrentIndex(control.findData("vertical_lr"))
            self.assertEqual(panel.current()["writing_mode"], "vertical_lr")
            picture = visual()
            panel._layers.append(picture); panel.rebuild(picture["id"])
            properties = panel.visual_properties
            self.assertEqual(properties.sections.sections['animation'].toggle.text(), "动画")
            properties.controls["visual_animation"].setCurrentIndex(properties.controls["visual_animation"].findData("breathe"))
            properties.controls["animation_strength"].setValue(12)
            self.assertEqual(panel.current()["visual_animation"], "breathe")
            self.assertEqual(panel.current()["animation_strength"], 12)
            properties.controls["visual_animation"].setCurrentIndex(properties.controls["visual_animation"].findData("zoom_in"))
            self.assertFalse(properties.controls["animation_period"].isEnabled())
            panel.add_caption()
            self.assertFalse(panel.text_properties.controls["writing_mode"].isEnabled())
            panel.deleteLater(); self.app.processEvents()

    def test_profile_roundtrip_and_preview_cache_invalidate(self):
        with tempfile.TemporaryDirectory() as root:
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([text(), visual()])
            store = Store(root); saved = store.save(state, 0)
            loaded = store.load()
            self.assertEqual(loaded["settings"]["layers"], saved["settings"]["layers"])
            path = Path(root)/"bg.mp4"; path.touch()
            job = {"path": str(path), "title": "", "body": ""}
            before = preview_key(job, loaded)
            loaded["settings"]["layers"][0]["writing_mode"] = "vertical_lr"
            self.assertNotEqual(before, preview_key(job, loaded))
            before = preview_key(job, loaded)
            loaded["settings"]["layers"][1]["animation_strength"] = 16
            self.assertNotEqual(before, preview_key(job, loaded))

    def test_animation_and_direction_use_existing_undo(self):
        from unittest.mock import patch
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.layer_panel.select_layer_id("title")
            control = dialog.layer_panel.text_properties.controls["writing_mode"]
            control.setCurrentIndex(control.findData("vertical_rl")); dialog.persist()
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(next(d for d in dialog.layer_panel.values() if d["id"] == "title")["writing_mode"], "horizontal")
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(next(d for d in dialog.layer_panel.values() if d["id"] == "title")["writing_mode"], "vertical_rl")
            dialog.close(); dialog.deleteLater(); self.app.processEvents()


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实 FFmpeg")
class NativeVerticalMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, "-y", "-v", "error", *args], capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf8", "replace"))
        return result.stdout

    def frame(self, path, time):
        return QtGui.QImage.fromData(self.ffmpeg(["-ss", str(time), "-i", str(path), "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1"]))

    def red_box(self, image):
        points = [(x,y) for y in range(image.height()) for x in range(image.width())
                  if image.pixelColor(x,y).red() > 150 and image.pixelColor(x,y).green() < 100 and image.pixelColor(x,y).blue() < 100]
        self.assertTrue(points)
        return min(x for x,y in points), min(y for x,y in points), max(x for x,y in points), max(y for x,y in points)

    def difference(self, a, b):
        from PIL import Image, ImageChops, ImageStat
        def pillow(value):
            image = value.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            return Image.frombytes("RGBA", (image.width(), image.height()),
                image.constBits().asstring(image.sizeInBytes())).convert("RGB")
        return sum(ImageStat.Stat(ImageChops.difference(pillow(a), pillow(b))).mean)/3

    def state(self, root, layers, duration=2):
        background, picture = Path(root)/"background.mp4", Path(root)/"picture.png"
        self.ffmpeg(["-f", "lavfi", "-i", f"color=c=0x203060:s=240x160:r=20:d={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(background)])
        image = QtGui.QImage(128, 64, QtGui.QImage.Format.Format_RGBA8888)
        image.fill(QtGui.QColor("red")); image.save(str(picture))
        state = fresh_state()
        state["settings"].update(ffmpeg_path=FFMPEG, aspect="source", darkness=0, music_enabled=False, overwrite=True)
        layers = [dict(layer, path=str(picture)) if layer["kind"] == "image" else layer for layer in layers]
        state["settings"]["layers"] = normalize_layers(layers+[dict(d, enabled=False) for d in DEFAULT_LAYERS])
        job = {"id": "job", "path": str(background), "name": "finished", "title": "", "body": "", "task_dir": str(root)}
        state["jobs"] = [job]
        return state, job, picture

    def test_real_pulse_preserves_aspect_center_and_preview_export(self):
        with tempfile.TemporaryDirectory() as root:
            state, job, picture = self.state(root, [visual()])
            before = copy.deepcopy(state); original = picture.read_bytes()
            output, _ = render_static_text(job, state, root, preview=True)
            rest = self.red_box(self.frame(output, 0))
            peak_frame = self.frame(output, .45)
            peak = self.red_box(peak_frame)
            later = self.red_box(self.frame(output, 1.5))
            self.assertGreater(peak[2]-peak[0], rest[2]-rest[0]+25)
            self.assertAlmostEqual((peak[2]-peak[0])/(peak[3]-peak[1]), 2, delta=.12)
            self.assertAlmostEqual((peak[0]+peak[2])/2, 120, delta=2)
            self.assertAlmostEqual((peak[1]+peak[3])/2, 80, delta=2)
            self.assertAlmostEqual(later[2]-later[0], rest[2]-rest[0], delta=3)
            output, _ = render_static_text(job, state, root)
            exported_frame = self.frame(output, .45)
            # Preview CRF25 and final CRF20 are lossy: a thresholded antialias
            # edge can differ by one pixel without any geometry difference.
            for actual, expected in zip(self.red_box(exported_frame), peak):
                self.assertAlmostEqual(actual, expected, delta=1)
            self.assertLess(self.difference(exported_frame, peak_frame), 3)
            self.assertEqual(state, before); self.assertEqual(picture.read_bytes(), original)

    def test_real_all_modes_alpha_and_blend(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in ("breathe", "float", "zoom_in", "zoom_out"):
                with self.subTest(mode=mode):
                    state, job, _ = self.state(root, [visual(visual_animation=mode)])
                    output, _ = render_static_text(job, state, root)
                    first, later = self.red_box(self.frame(output, 0)), self.red_box(self.frame(output, .5 if mode=="float" else 1))
                    self.assertNotEqual(first, later)
            for blend in ("add", "screen"):
                with self.subTest(blend=blend):
                    state, job, _ = self.state(root, [visual(blend_mode=blend)])
                    output, _ = render_static_text(job, state, root)
                    first, peak = self.red_box(self.frame(output, 0)), self.red_box(self.frame(output, .45))
                    self.assertGreater(peak[2]-peak[0], first[2]-first[0]+25)
                    pixel = self.frame(output, .45).pixelColor(0, 0)
                    self.assertLess(pixel.red(), 50); self.assertGreater(pixel.blue(), 80)

    def test_real_vertical_text_visible_and_matches_playback(self):
        with tempfile.TemporaryDirectory() as root:
            state, job, _ = self.state(root, [text(text="AB\nCD", font="Segoe UI", font_min=130, font_max=130)])
            output, _ = render_static_text(job, state, root, preview=True)
            frame = self.frame(output, .5)
            whites = sum(min(frame.pixelColor(x,y).red(), frame.pixelColor(x,y).green(),
                frame.pixelColor(x,y).blue()) > 160 for y in range(frame.height()) for x in range(frame.width()))
            self.assertGreater(whites, 100)
            before = frame
            output, _ = render_static_text(job, state, root)
            frame = self.frame(output, .5)
            self.assertLess(self.difference(frame, before), 3)

    def test_real_motion_video_gif_and_key_alpha(self):
        with tempfile.TemporaryDirectory() as root:
            state, job, _ = self.state(root, [visual()])
            video, gif = Path(root)/"element.mp4", Path(root)/"element.gif"
            self.ffmpeg(["-f", "lavfi", "-i", "color=c=0x00ff00:s=128x64:r=20:d=0.3",
                "-vf", "drawbox=x=24:y=8:w=80:h=48:color=red:t=fill",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
            self.ffmpeg(["-i", str(video), "-loop", "0", str(gif)])
            for path in (video, gif):
                for blend in ("normal", "add", "screen"):
                    with self.subTest(path=path.suffix, blend=blend):
                        state["settings"]["layers"][0].update(path=str(path), key_mode="green", blend_mode=blend)
                        output, _ = render_static_text(job, state, root)
                        before, peak = self.red_box(self.frame(output, 0)), self.red_box(self.frame(output, .45))
                        self.assertGreater(peak[2]-peak[0], before[2]-before[0]+15)
                        pixel = self.frame(output, .45).pixelColor(4, 4)
                        self.assertLess(pixel.red(), 50); self.assertGreater(pixel.blue(), 80)
            state["settings"]["layers"][0].update(path=str(video), media_loop=False)
            output, _ = render_static_text(job, state, root)
            pixel = self.frame(output, 1).pixelColor(120, 80)
            self.assertLess(pixel.red(), 70); self.assertGreater(pixel.blue(), 80)

    def test_real_voice_duration_holds_and_invalid_timing_keeps_old_output(self):
        with tempfile.TemporaryDirectory() as root:
            state, job, _ = self.state(root, [visual(visual_animation="zoom_in",
                animation_start_delay=1, animation_end_early=1)])
            voice = Path(root)/"task_audio.wav"
            self.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=400:duration=4", str(voice)])
            state["settings"].update(use_voice=True, loop_transition="none")
            job["voice_path"] = str(voice)
            output, _ = render_static_text(job, state, root)
            _, ffprobe = resolve_tools(FFMPEG)
            self.assertAlmostEqual(probe(output, ffprobe)["duration"], 4, delta=.1)
            self.assertEqual(self.red_box(self.frame(output, .1)), self.red_box(self.frame(output, .8)))
            self.assertEqual(self.red_box(self.frame(output, 3.1)), self.red_box(self.frame(output, 3.8)))
            self.assertGreater(self.red_box(self.frame(output, 3.1))[2]-self.red_box(self.frame(output, 3.1))[0],
                self.red_box(self.frame(output, .1))[2]-self.red_box(self.frame(output, .1))[0]+25)
            before = output.read_bytes()
            state["settings"]["layers"][0]["animation_start_delay"] = 4
            with self.assertRaisesRegex(ValueError, "最终视频时长"):
                render_static_text(job, state, root)
            self.assertEqual(output.read_bytes(), before)

    def test_real_node_uses_same_animation_and_vertical_compositor(self):
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.contracts import ResourceRef, RenderPlan
        from model.node_workflow.render import render_plan
        from model.node_workflow.graph import NodeRegistry, register_core
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, prepare_layer, build_layer
        with tempfile.TemporaryDirectory() as root:
            state, job, picture = self.state(root, [visual()])
            context = TaskRunContext(threading.Event(), lambda *_:None, lambda *_:None)
            context.ffmpeg = FFMPEG
            registry = NodeRegistry(); register_core(registry)
            operations = []
            for kind, value in (("image", visual(picture)), ("text", text(text="AB", font="Segoe UI", x=90, width=15,
                font_min=40, font_max=40))):
                definition = layer_node(kind); registry.register("batch_text_video", definition)
                params = {parameter.key:copy.deepcopy(parameter.default) for parameter in definition.parameters}
                params["layer"] = value
                context.node_prepared = prepare_layer(None, params, None)
                operations.extend(build_layer({}, params, context, media=kind)["operations"])
            context.workflow_registry = registry
            output = Path(root)/"node.mp4"
            result = render_plan(RenderPlan(ResourceRef.capture(job["path"], "video"), tuple(operations), str(output)),
                context, FFMPEG)
            self.assertEqual(result["video_encodings"], 1); self.assertEqual(result["intermediate_videos"], 0)
            rest, peak = self.red_box(self.frame(output, 0)), self.red_box(self.frame(output, .45))
            self.assertGreater(peak[2]-peak[0], rest[2]-rest[0]+25)
            self.assertEqual(list(Path(root).glob(".node-render-*")), [])

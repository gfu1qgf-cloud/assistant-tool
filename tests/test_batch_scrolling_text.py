"""Whole-text motion, source/preset compatibility, clipping and real encoding."""
import copy
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.scrolling_text import (
    normalize_scroll, new_scrolling_component, box_geometry, scroll_position,
    render_scroll_assets, render_scroll_preview, prepare_scroll_plane, add_scroll_plane,
)
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, TextDoesNotFit, render_overlay
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources, commit_sequences
from app_plugins.builtin.batch_text_video.captions import static_planes, new_caption
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.playback import preview_key
from app_plugins.builtin.batch_text_video.engine import render_static_text, resolve_tools, probe

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def layer(**values):
    value = new_scrolling_component()
    value.update(text="One\nTwo\nThree\nFour\nFive\nSix", font="Segoe UI", font_max=72,
                 width=80, height=50, outline_width=0, padding=4, scroll_edge_fade=0,
                 scroll_start_anchor='outside')
    value.update(values)
    return normalize_layers([value])[0]


class ScrollTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_old_text_boxes_stay_static_and_caption_is_unaffected(self):
        original = new_text_component()
        normalized = normalize_layers([original])[0]
        self.assertEqual(normalized["text_animation"], "none")
        self.assertEqual(original, new_text_component() | {"id": original["id"]})
        from app_plugins.builtin.batch_text_video.scrolling_text import is_scrolling
        caption = new_caption(); caption["text_animation"] = "up"
        self.assertFalse(is_scrolling(caption))

    def test_parameter_validation_and_nonfinite_values(self):
        for key, bad in (("text_animation", "bad"), ("scroll_start_delay", -1),
                         ("scroll_end_early", float("nan")), ("scroll_start_offset", float("inf")),
                         ("scroll_edge_fade", 51), ("scroll_offset_units", "bad")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                layer(**{key: bad})

    def test_timing_offsets_holds_and_both_directions(self):
        value = layer(scroll_start_delay=2, scroll_end_early=3, scroll_start_offset=-20, scroll_end_offset=40)
        self.assertEqual(scroll_position(value, 10, 100, 200, 0), 80)
        self.assertEqual(scroll_position(value, 10, 100, 200, 2), 80)
        self.assertEqual(scroll_position(value, 10, 100, 200, 4.5), -40)
        self.assertEqual(scroll_position(value, 10, 100, 200, 10), -160)
        value["text_animation"] = "down"
        self.assertEqual(scroll_position(value, 10, 100, 200, 0), -220)
        self.assertEqual(scroll_position(value, 10, 100, 200, 10), 140)
        with self.assertRaisesRegex(ValueError, "最终视频时长"):
            scroll_position(value, 5, 100, 200, 0)

    def test_long_text_does_not_shrink_and_does_not_escape_box(self):
        value = layer(text=("Text remains readable.\n"*30), height=15)
        assets = render_scroll_assets(value, 360, 640)
        self.assertEqual(assets["font_size"], 72)
        self.assertGreater(assets["text_height"], assets["height"])
        image, size = render_text_box(dict(value,scroll_start_anchor='top'), 360, 640)
        x, y, w, h = box_geometry(value, 360, 640)
        self.assertEqual(size, 72)
        self.assertTrue(any(image.pixelColor(px, py).alpha() > 100 for py in range(y, y+h) for px in range(x, x+w)))
        self.assertEqual(max(image.pixelColor(px, py).alpha() for px, py in ((x-1, y+h//2), (x+w, y+h//2),
                         (x+w//2, y-1), (x+w//2, y+h))), 0)
        value["text_animation"] = "none"
        with self.assertRaises(TextDoesNotFit):
            render_text_box(value, 360, 640)

    def test_strip_allocation_is_bounded(self):
        with self.assertRaisesRegex(TextDoesNotFit, "过大"):
            render_scroll_assets(layer(text="line\n"*7000, font_max=200), 1080, 1920)

    def test_gradient_fades_edges_not_background(self):
        plain = layer(text="A\n"*20, font_max=120, scroll_start_offset=-100)
        fade = dict(plain, scroll_edge_fade=25)
        a, _ = render_scroll_preview(plain, 360, 640, duration=10, time=0)
        b, _ = render_scroll_preview(fade, 360, 640, duration=10, time=0)
        x, y, w, h = box_geometry(plain, 360, 640)
        row = next(py for py in range(y+1, y+int(h*.2)) if any(a.pixelColor(px, py).alpha()>100 for px in range(x, x+w)))
        self.assertLess(sum(b.pixelColor(px, row).alpha() for px in range(x, x+w)),
                        sum(a.pixelColor(px, row).alpha() for px in range(x, x+w)))
        fade.update(background_enabled=True, background_color="#aa3300", background_opacity=100, background_radius=0)
        c, _ = render_scroll_preview(fade, 360, 640, duration=10, time=0)
        self.assertEqual(c.pixelColor(x+1, y+1).alpha(), 255)

    def test_add_menu_and_units_conversion_do_not_move_text(self):
        with tempfile.TemporaryDirectory() as root:
            panel = LayerPanel(None, root)
            self.assertIn("添加滚动文字框", [action.text() for action in panel.add_button.menu().actions()])
            panel.add_scrolling_text()
            value = panel.current()
            self.assertEqual(value["text_animation"], "up")
            self.assertTrue(panel.editor.sections['scroll'].toggle.isChecked())
            self.assertEqual(panel.editor.sections["scroll"].toggle.text(), "文字滚动动画")
            controls = panel.scroll_properties
            controls.controls["scroll_start_offset"].setValue(-50)
            saved = panel.current()["scroll_start_offset"]
            controls.units.setCurrentIndex(1)
            self.assertEqual(panel.current()["scroll_start_offset"], saved)
            self.assertAlmostEqual(controls.controls["scroll_start_offset"].value(), -288)
            panel.set_canvas_size(1920, 1080)
            self.assertAlmostEqual(controls.controls["scroll_start_offset"].value(), -162)
            self.assertEqual(panel.current()["scroll_start_offset"], saved)
            controls.units.setCurrentIndex(0)
            self.assertEqual(panel.current()["scroll_start_offset"], saved)
            panel.select_layer_id("title")
            self.assertEqual(panel.current()["text_animation"], "none")
            panel.deleteLater(); self.app.processEvents()

    def test_file_pool_sources_and_preset_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"text.txt"; path.write_text("Text from file", encoding="utf8")
            value = layer(source="file", source_file=str(path), scroll_start_offset=-45, scroll_end_early=.7)
            resolved, _ = resolve_sources([value])
            self.assertEqual(resolved[0]["text"], "Text from file")
            value.update(source="sequence", sequence_items=[{"text":"first"},{"text":"second"}], sequence_cursor=0)
            resolved, plans = resolve_sources([value])
            self.assertEqual(resolved[0]["text"], "first")
            self.assertEqual(value["sequence_cursor"], 0)
            value = commit_sequences([value], plans)[0]
            self.assertEqual(value["sequence_cursor"], 1)
            store = Store(root)
            state = fresh_state(); state["settings"]["layers"] = normalize_layers([value])
            store.save(state, 0)
            loaded = store.load()["settings"]["layers"][0]
            self.assertEqual(loaded["scroll_start_offset"], -45)
            self.assertEqual(loaded["scroll_end_early"], .7)
            self.assertEqual(loaded["sequence_cursor"], 1)

    def test_add_and_motion_parameters_support_existing_undo(self):
        from unittest.mock import patch
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            dialog.layer_panel.add_scrolling_text(); dialog.persist()
            identifier = dialog.layer_panel.current()["id"]
            dialog.layer_panel.scroll_properties.controls["scroll_start_delay"].setValue(1.25)
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            value = next(item for item in dialog.layer_panel.values() if item["id"] == identifier)
            self.assertEqual(value["scroll_start_delay"], 0)
            self.assertTrue(dialog.redo_edit())
            value = next(item for item in dialog.layer_panel.values() if item["id"] == identifier)
            self.assertEqual(value["scroll_start_delay"], 1.25)
            dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_layer_order_and_preview_cache_include_animation(self):
        state = fresh_state()
        value = layer()
        fixed = new_text_component(); fixed.update(text="TOP")
        state["settings"]["layers"] = normalize_layers([value, fixed])
        planes = static_planes("", "", 360, 640, state["settings"], ImageCache())
        self.assertEqual([kind for kind, _ in planes], ["image", "scroll", "image"])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"bg.mp4"; path.touch()
            job = {"path":str(path),"title":"","body":""}
            before = preview_key(job, state)
            state["settings"]["layers"][0]["scroll_start_delay"] = .5
            self.assertNotEqual(before, preview_key(job, state))

    def test_ffmpeg_plane_uses_final_duration_and_two_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            value = layer(scroll_start_delay=1, scroll_end_early=2)
            assets = prepare_scroll_plane(value, 360, 640, 6, root)
            args, filters = [], []
            self.assertEqual(add_scroll_plane(args, filters, assets, 4, "input", "output", 6, Fraction(25)), 2)
            self.assertEqual(args.count("-i"), 2)
            self.assertIn("(t-1.000000000)/3.000000000", "\n".join(filters))
            self.assertIn("[5:v]", "\n".join(filters))
            self.assertIn("d=6.000000000", "\n".join(filters))


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实 FFmpeg")
class RealScrollTextTests(unittest.TestCase):
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
        return QtGui.QImage.fromData(self.ffmpeg(["-ss", str(time), "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]))

    def whites(self, image):
        return [(x, y) for y in range(image.height()) for x in range(image.width())
                if min(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(), image.pixelColor(x, y).blue()) > 160]

    def image_difference(self, a, b):
        self.assertEqual(a.size(), b.size())
        from PIL import Image, ImageChops, ImageStat
        def pillow(image):
            image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            return Image.frombytes("RGBA", (image.width(), image.height()), image.constBits().asstring(image.sizeInBytes())).convert("RGB")
        return sum(ImageStat.Stat(ImageChops.difference(pillow(a), pillow(b))).mean)/3

    def state(self, root, value, duration=4):
        bg = Path(root)/"bg.mp4"
        self.ffmpeg(["-f", "lavfi", "-i", f"color=c=0x203060:s=240x160:r=20:d={duration}",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg)])
        state = fresh_state()
        state["settings"].update(ffmpeg_path=FFMPEG, aspect="source", darkness=0, music_enabled=False, overwrite=True)
        state["settings"]["layers"] = normalize_layers([value]+[dict(item, enabled=False) for item in DEFAULT_LAYERS])
        job = {"id":"job", "path":str(bg), "title":"", "body":"", "name":"finished", "task_dir":str(root)}
        state["jobs"] = [job]
        return state, job

    def test_motion_clipping_both_directions_preview_and_export(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in ("up", "down"):
                with self.subTest(mode=mode):
                    value = layer(text="SCROLL", text_animation=mode, font_max=90, height=50)
                    state, job = self.state(root, value)
                    original = copy.deepcopy(state)
                    output, _ = render_static_text(job, state, root, preview=True)
                    a, b = self.whites(self.frame(output, 1.5)), self.whites(self.frame(output, 2.5))
                    self.assertTrue(a and b)
                    ya, yb = sum(y for _, y in a)/len(a), sum(y for _, y in b)/len(b)
                    self.assertGreater(ya, yb) if mode == "up" else self.assertLess(ya, yb)
                    x, y, w, h = box_geometry(value, 240, 160)
                    for frame_time in (.7, 1.5, 2.5, 3.3):
                        self.assertTrue(all(x-2 <= px < x+w+2 and y-2 <= py < y+h+2 for px, py in self.whites(self.frame(output, frame_time))))
                    preview_frame = self.frame(output, 2)
                    output, _ = render_static_text(job, state, root, preview=False)
                    self.assertLess(self.image_difference(self.frame(output, 2), preview_frame), 3)
                    self.assertEqual(state, original)

    def test_voice_duration_drives_motion_and_time_offsets_hold(self):
        with tempfile.TemporaryDirectory() as root:
            value = layer(text="VOICE", font_max=90, scroll_start_delay=1, scroll_end_early=1,
                          scroll_start_offset=-60, scroll_end_offset=60, scroll_edge_fade=12)
            state, job = self.state(root, value, duration=2)
            voice = Path(root)/"task_audio.wav"
            self.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=400:duration=6", str(voice)])
            state["settings"].update(use_voice=True, loop_transition="none")
            job["voice_path"] = str(voice)
            output, _ = render_static_text(job, state, root)
            _, ffprobe = resolve_tools(FFMPEG)
            self.assertAlmostEqual(probe(output, ffprobe)["duration"], 6, delta=.1)
            self.assertLess(self.image_difference(self.frame(output, .1), self.frame(output, .8)), 1)
            self.assertLess(self.image_difference(self.frame(output, 5.1), self.frame(output, 5.8)), 1)
            self.assertGreater(self.image_difference(self.frame(output, 2), self.frame(output, 4)), .5)

    def test_invalid_timing_does_not_replace_existing_result(self):
        with tempfile.TemporaryDirectory() as root:
            state, job = self.state(root, layer(scroll_start_delay=3, scroll_end_early=2))
            output = Path(root)/"finished.mp4"; output.write_bytes(b"existing result")
            with self.assertRaisesRegex(ValueError, "最终视频时长"):
                render_static_text(job, state, root)
            self.assertEqual(output.read_bytes(), b"existing result")
            self.assertEqual(list(Path(root).glob(".text-video-*")), [])

    def test_odd_box_size_off_canvas_and_transparent_background(self):
        with tempfile.TemporaryDirectory() as root:
            value = layer(text="ODD\nBOX", x=8, y=15, width=83, height=47, font_max=90,
                          scroll_edge_fade=20, background_enabled=True,
                          background_color="#882233", background_opacity=40,
                          scroll_start_offset=-30, scroll_end_offset=20)
            state, job = self.state(root, value)
            output, _ = render_static_text(job, state, root)
            frame = self.frame(output, 2)
            self.assertEqual((frame.width(), frame.height()), (240, 160))
            # Large portions of the box are outside the canvas; no black mat
            # or color should appear outside its visible bounds.
            x, y, w, h = box_geometry(value, 240, 160)
            outside = frame.pixelColor(239, 159)
            self.assertLess(outside.red(), 45)
            self.assertGreater(outside.blue(), 85)
            self.assertTrue(all(px < x+w+2 and py < y+h+2 for px, py in self.whites(frame)))

    def test_node_uses_same_animation_compiler_without_intermediate_video(self):
        from model.BackgroundTasks import TaskRunContext
        from model.node_workflow.contracts import ResourceRef, RenderPlan
        from model.node_workflow.render import render_plan
        from model.node_workflow.graph import NodeRegistry, register_core
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, prepare_layer, build_layer
        from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
        with tempfile.TemporaryDirectory() as root:
            value = layer(text="NODE", font_max=90, scroll_edge_fade=10)
            state, job = self.state(root, value)
            params = {param.key:copy.deepcopy(param.default) for param in layer_node("text").parameters}
            params["layer"] = value
            editor = LayerNodeEditor(params, lambda *_:None)
            self.assertTrue(('scroll' in editor.panel.editor.sections))
            editor.deleteLater(); self.app.processEvents()
            context = TaskRunContext(threading.Event(), lambda *_:None, lambda *_:None)
            context.ffmpeg = FFMPEG
            context.node_prepared = prepare_layer(None, params, None)
            registry = NodeRegistry(); register_core(registry); registry.register("batch_text_video", layer_node("text"))
            context.workflow_registry = registry
            operation = build_layer({}, params, context, media="text")["operations"][0]
            target = Path(root)/"node.mp4"
            result = render_plan(RenderPlan(ResourceRef.capture(job["path"], "video"), (operation,), str(target)), context, FFMPEG)
            self.assertEqual(result["video_encodings"], 1)
            self.assertEqual(result["intermediate_videos"], 0)
            self.assertGreater(self.image_difference(self.frame(target, 1.5), self.frame(target, 2.5)), .5)
            self.assertEqual(list(Path(root).glob(".node-render-*")), [])

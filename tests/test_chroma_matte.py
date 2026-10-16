"""Matte math, Qt main-thread controls and the actual shared FFmpeg pipeline."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageChops, ImageStat
from qt_compat import QtCore, QtGui, QtWidgets, QtTest
from app_plugins.builtin.batch_text_video.chroma_matte import levels, remap, key_filters, MatteLevels
from app_plugins.builtin.batch_text_video.visual_media import normalize_visual, keyed_image, add_plane, VisualProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.workflow_layer_ui import LayerNodeEditor
from app_plugins.builtin.batch_text_video.workflow_layers import layer_node

FFMPEG = os.environ.get("NODE_FFMPEG", "")


class MatteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_old_settings_gain_identity_levels_without_changing_key_parameters(self):
        old = {"key_mode": "green", "key_similarity": .15, "key_softness": .08}
        layer = copy.deepcopy(old)
        normalize_visual(layer)
        self.assertEqual((layer["key_alpha_low"], layer["key_alpha_high"]), (0, 100))
        self.assertTrue(all(layer[key] == value for key, value in old.items()))
        self.assertFalse(any("lut" in text for text in key_filters(layer)))
        alpha = np.array([0., .2, .5, 1.])
        np.testing.assert_array_equal(remap(alpha, layer), alpha)

    def test_clipped_transfer_fills_foreground_and_clears_residue(self):
        layer = {"key_alpha_low": 20, "key_alpha_high": 70}
        np.testing.assert_allclose(remap(np.array([0., .1, .2, .45, .7, .9, 1]), layer),
                                   [0, 0, 0, .5, 1, 1, 1])
        for low, high in ((30, 30), (31, 30), (-1, 99), (0, 101), (False, 100), (0, float("nan")), (0, 1e-308)):
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                levels({"key_alpha_low": low, "key_alpha_high": high})

    def test_black_mode_and_native_alpha_are_not_despilled(self):
        image = QtGui.QImage(4, 1, QtGui.QImage.Format.Format_RGBA8888)
        for x, color in enumerate(((0, 0, 0, 255), (4, 4, 4, 255), (240, 160, 80, 255), (255, 255, 255, 90))):
            image.setPixelColor(x, 0, QtGui.QColor(*color))
        layer = {"key_mode": "black", "key_similarity": .03, "key_softness": .03,
                 "key_alpha_low": 5, "key_alpha_high": 70, "key_despill": True}
        normalize_visual(layer)
        result = keyed_image(image, layer)
        self.assertEqual(result.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(result.pixelColor(1, 0).alpha(), 0)
        self.assertEqual(result.pixelColor(2, 0), image.pixelColor(2, 0))
        self.assertEqual(result.pixelColor(3, 0).alpha(), 90)
        self.assertFalse(any("despill" in text for text in key_filters(layer)))
        self.assertIn("color=0x000000", key_filters(layer)[0])
        layer["key_mode"] = "none"
        self.assertEqual(key_filters(layer), [])
        self.assertEqual(keyed_image(image, layer), image)

    def test_matte_applies_before_user_opacity_and_has_no_second_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"element.mp4"; path.touch()
            layer = normalize_layers([{"id": "visual", "kind": "image", "path": str(path),
                "key_mode": "green", "opacity": 50, "key_alpha_low": 10, "key_alpha_high": 70}])[0]
            args, filters = [], []
            add_plane(args, filters, layer, 1, "background", "final", 160, 90, 1, 10)
            source = ";".join(filters)
            self.assertLess(source.index("colorkey="), source.index("lutrgb="))
            self.assertLess(source.index("lutrgb="), source.index("scale="))
            self.assertLess(source.index("lutrgb="), source.index("colorchannelmixer=aa=0.50000000"))
            self.assertIn("blend=all_mode=multiply", source)
            self.assertIn("alphamerge", source)

    def test_widget_clamps_crossed_handles_and_loading_preserves_levels(self):
        editor = VisualProperties()
        self.addCleanup(editor.deleteLater)
        values = []; editor.changed.connect(values.append)
        editor.set_layer({"key_mode": "green", "key_alpha_low": 80, "key_alpha_high": 90})
        self.assertFalse(values)
        self.assertEqual(editor.controls["key_alpha_low"].value(), 80)
        editor.controls["key_alpha_high"].setValue(30)
        self.assertAlmostEqual(values[-1]["key_alpha_high"], 80.1)
        editor.controls["key_alpha_low"].setValue(99)
        self.assertAlmostEqual(values[-1]["key_alpha_low"], 80)
        editor.matte_levels.reset.click()
        self.assertEqual((values[-1]["key_alpha_low"], values[-1]["key_alpha_high"]), (0, 100))
        editor.controls["key_mode"].setCurrentIndex(editor.controls["key_mode"].findData("black"))
        self.assertFalse(editor.controls["key_despill"].isEnabled())
        self.assertTrue(editor.matte_levels.isEnabled())
        editor.controls["key_mode"].setCurrentIndex(editor.controls["key_mode"].findData("none"))
        self.assertFalse(editor.matte_levels.isEnabled())

    def test_node_inspector_uses_same_levels_and_settings_survive_normalization(self):
        params = {param.key: copy.deepcopy(param.default) for param in layer_node("animated").parameters}
        params["layer"].update(key_mode="black", key_alpha_low=12, key_alpha_high=65)
        changes = []
        editor = LayerNodeEditor(params, changes.append)
        self.addCleanup(editor.deleteLater)
        visual = editor.panel.visual_properties
        self.assertEqual(visual.controls["key_mode"].currentData(), "black")
        visual.controls["key_alpha_high"].setValue(55)
        self.assertEqual(changes[-1]["layer"]["key_alpha_high"], 55)
        saved = normalize_layers(json.loads(json.dumps(editor.panel.values())))
        self.assertEqual(saved[0]["key_alpha_low"], 12)
        self.assertEqual(saved[0]["key_alpha_high"], 55)

    def test_dragging_curve_updates_numbers_and_never_crosses(self):
        editor = MatteLevels(); editor.resize(300, 190); editor.show()
        self.addCleanup(editor.deleteLater); self.app.processEvents()
        curve = editor.curve
        left, right = curve.endpoints()
        target = QtCore.QPoint(round(curve.plot().left()+curve.plot().width()*.7), round(right.y()))
        QtTest.QTest.mousePress(curve, QtCore.Qt.MouseButton.LeftButton, pos=right.toPoint())
        QtTest.QTest.mouseMove(curve, target)
        QtTest.QTest.mouseRelease(curve, QtCore.Qt.MouseButton.LeftButton, pos=target)
        self.assertAlmostEqual(curve.high, 70, delta=.5)
        self.assertAlmostEqual(editor.controls["key_alpha_high"].value(), curve.high)
        curve.active = 0; curve.move_endpoint(95)
        self.assertLess(curve.low, curve.high)
        pixmap = editor.grab(); self.assertFalse(pixmap.isNull())

    def test_high_precision_external_settings_remain_editable(self):
        editor = MatteLevels(); self.addCleanup(editor.deleteLater)
        for low, high in ((99.99, 100), (0, .01), (20.01, 20.02)):
            editor.set_levels(low, high)
            self.assertLess(editor.controls["key_alpha_low"].value(), editor.controls["key_alpha_high"].value())

    def test_alpha_changes_invalidate_existing_preview_cache(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from app_plugins.builtin.batch_text_video.store import fresh_state
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"sample.png"; path.touch()
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([{"id":"visual", "kind":"image", "path":str(path), "key_mode":"green"}])
            first = preview_key({},state)
            state["settings"]["layers"][0]["key_alpha_high"] = 70
            self.assertNotEqual(first,preview_key({},state))

    def test_ui_capture(self):
        if not os.environ.get("CHROMA_QA"):
            return
        editor = VisualProperties(); editor.resize(440, 740)
        editor.set_layer({"key_mode":"green", "key_alpha_low":10, "key_alpha_high":70})
        editor.show(); self.app.processEvents()
        directory = Path(os.environ["CHROMA_QA"]); directory.mkdir(parents=True,exist_ok=True)
        self.assertTrue(editor.grab().save(str(directory/"抠像透明度.png")))
        editor.close(); editor.deleteLater(); self.app.processEvents()


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实FFmpeg")
class MatteRenderTests(MatteTests):
    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf8", "replace"))
        return result.stdout

    def sample_image(self, mode):
        image = QtGui.QImage(80, 40, QtGui.QImage.Format.Format_RGBA8888)
        foreground = {"green": (65, 210, 100), "blue": (90, 65, 210), "black": (75, 75, 75)}[mode]
        background = {"green": (0, 255, 0), "blue": (0, 0, 255), "black": (0, 0, 0)}[mode]
        image.fill(QtGui.QColor(*background))
        painter = QtGui.QPainter(image)
        painter.fillRect(20, 10, 40, 20, QtGui.QColor(*foreground)); painter.end()
        return image

    def test_real_lut_and_poster_agree_for_green_blue_black(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in ("green", "blue", "black"):
                path = Path(root)/(mode+".png")
                image = self.sample_image(mode); self.assertTrue(image.save(str(path)))
                layer = {"key_mode": mode, "key_similarity": .05, "key_softness": .5,
                         "key_alpha_low": 5, "key_alpha_high": 35}
                normalize_visual(layer)
                raw = self.ffmpeg(["-i", str(path), "-vf", "format=rgba,"+",".join(key_filters(layer)),
                    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
                actual = QtGui.QImage.fromData(raw); poster = keyed_image(image, layer)
                for x, y in ((0, 0), (30, 15)):
                    self.assertAlmostEqual(actual.pixelColor(x,y).alpha(), poster.pixelColor(x,y).alpha(), delta=2)
                    self.assertEqual(actual.pixelColor(x,y).getRgb()[:3], poster.pixelColor(x,y).getRgb()[:3])
                self.assertEqual(actual.pixelColor(0,0).alpha(), 0)
                self.assertEqual(actual.pixelColor(30,15).alpha(), 255)

    def test_shared_compositor_recovers_foreground_and_retains_user_opacity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"green.png"; image = self.sample_image("green"); image.save(str(path))
            def composite(high, opacity=100, blend="normal"):
                layer = normalize_layers([{"id":"v", "kind":"image", "path":str(path),
                    "x":50, "y":50, "width":100, "height":100, "opacity":opacity,
                    "key_mode":"green", "key_similarity":.05, "key_softness":.5,
                    "key_alpha_low":0, "key_alpha_high":high, "blend_mode":blend}])[0]
                args, filters = [], ["[0:v]format=rgba[base]"]
                add_plane(args, filters, layer, 1, "base", "out", 80, 40, .5, 10)
                raw = self.ffmpeg(["-f","lavfi","-i","color=c=red:s=80x40:r=10:d=0.5", *args,
                    "-filter_complex", ";".join(filters), "-map","[out]","-frames:v","1",
                    "-f","image2pipe","-vcodec","png","pipe:1"])
                return QtGui.QImage.fromData(raw)
            weak, recovered, half = composite(100), composite(35), composite(35, 50)
            self.assertGreater(recovered.pixelColor(40,20).green(), weak.pixelColor(40,20).green()+80)
            self.assertLess(recovered.pixelColor(40,20).red(), weak.pixelColor(40,20).red()-60)
            self.assertGreater(half.pixelColor(40,20).red(), recovered.pixelColor(40,20).red()+70)
            self.assertLess(half.pixelColor(40,20).green(), recovered.pixelColor(40,20).green()-70)
            self.assertGreater(recovered.pixelColor(0,0).red(), 245)
            self.assertLess(recovered.pixelColor(0,0).green(), 5)
            for blend in ("add", "screen"):
                result = composite(35, blend=blend)
                self.assertLess(result.pixelColor(0,0).green(), 5)

    def test_real_compositor_preserves_native_transparency(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"transparent.png"
            source = QtGui.QImage(80,40,QtGui.QImage.Format.Format_RGBA8888)
            source.fill(QtGui.QColor(180,80,20,0))
            painter = QtGui.QPainter(source)
            painter.setCompositionMode(painter.CompositionMode.CompositionMode_Source)
            painter.fillRect(20,10,40,20,QtGui.QColor(240,160,80,128)); painter.end()
            source.save(str(path))
            layer = normalize_layers([{"id":"v", "kind":"image", "path":str(path), "opacity":100,
                "width":100, "height":100, "key_mode":"black", "key_similarity":.03,
                "key_alpha_low":10, "key_alpha_high":35}])[0]
            args, filters = [], []
            add_plane(args,filters,layer,1,"base","out",80,40,.5,10)
            raw = self.ffmpeg(["-f","lavfi","-i","color=c=blue:s=80x40:r=10:d=0.5",*args,
                "-filter_complex","[0:v]format=rgba[base];"+";".join(filters),"-map","[out]",
                "-frames:v","1","-f","image2pipe","-vcodec","png","-"])
            actual = QtGui.QImage.fromData(raw)
            self.assertLess(actual.pixelColor(0,0).red(),5)
            self.assertGreater(actual.pixelColor(0,0).blue(),245)
            foreground = actual.pixelColor(40,20)
            self.assertAlmostEqual(foreground.red(),120,delta=3)
            self.assertAlmostEqual(foreground.green(),80,delta=3)
            self.assertGreater(foreground.blue(),160)

    def test_real_node_preview_and_export_use_levels_and_black_mode(self):
        from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node
        from model.node_workflow.tasks import prepare_job
        from model.node_workflow.preview import run_preview
        from model.node_workflow.render import run_workflow
        from app_plugins.builtin.batch_text_video.workflow_nodes import canvas_nodes
        from test_node_batch_layers import context
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source, overlay = root/"source.mp4", root/"overlay.mp4"
            self.ffmpeg(["-y","-f","lavfi","-i","color=c=red:s=160x90:r=10:d=1","-c:v","libx264",str(source)])
            png = root/"black.png"; self.sample_image("black").save(str(png))
            self.ffmpeg(["-y","-loop","1","-i",str(png),"-t","1","-r","10","-c:v","libx264",str(overlay)])
            registry = NodeRegistry(); register_core(registry)
            for definition in canvas_nodes(): registry.register("batch_text_video", definition)
            graph = empty_graph(); nodes = {}
            for name, kind in (("input","core.import"),("layer","batch_text_video.animated_layer"),
                               ("settle","core.settle"),("preview","core.preview"),("export","core.export")):
                nodes[name] = new_node(registry.definitions[kind]); graph["nodes"].append(nodes[name])
            nodes["input"]["params"]["video"] = str(source)
            nodes["layer"]["params"]["layer"].update(source_file=str(overlay), path=str(overlay), width=100, height=100,
                key_mode="black", key_similarity=.05, key_softness=.5, key_alpha_low=5, key_alpha_high=35)
            nodes["export"]["params"].update(destination="file", output=str(root/"output.mp4"))
            for a, out, b, inp in (("input","video","settle","video"),("layer","operations","settle","operations"),
                                   ("settle","plan","preview","value"),("preview","value","export","plan")):
                graph["connections"].append([nodes[a]["id"],out,nodes[b]["id"],inp])
            graph["output_node"] = nodes["export"]["id"]
            payload, _ = prepare_job(graph, registry, None, {}); payload["ffmpeg"] = FFMPEG
            preview = run_preview({**payload,"preview_node":nodes["preview"]["id"]}, context(), registry=registry)
            result = run_workflow(payload, context(), registry=registry)
            a = Image.open(io.BytesIO(base64.b64decode(preview["png"]))).convert("RGB")
            b = Image.open(io.BytesIO(self.ffmpeg(["-ss","0.5","-i",result["video"],"-frames:v","1","-f","image2pipe","-vcodec","png","-"]))).convert("RGB")
            self.assertEqual(a.size, b.size)
            self.assertLess(sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3, 4)
            red, green, blue = b.getpixel((80,45))
            self.assertLess(abs(red-green), 5); self.assertLess(abs(green-blue), 5)
            self.assertGreater(b.getpixel((0,0))[0], 245)
            self.assertEqual(result["video_encodings"], 1)
            self.assertEqual(result["intermediate_videos"], 0)


if __name__ == "__main__": unittest.main()

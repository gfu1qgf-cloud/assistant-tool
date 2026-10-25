"""Real FFmpeg/Qt regressions: protect same-coloured props, not just alpha."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.chroma_key import (
    normalize_key, key_color, despill_type, active_mask, inside_protection, enhanced_alpha, trimap_channels)
from app_plugins.builtin.batch_text_video.chroma_matte import key_filters
from app_plugins.builtin.batch_text_video.visual_media import normalize_visual, keyed_image, add_plane, VisualProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def sample(background=(0, 255, 0), connected=False):
    image = QtGui.QImage(80, 60, QtGui.QImage.Format.Format_RGBA8888)
    image.fill(QtGui.QColor(*background))
    painter = QtGui.QPainter(image)
    painter.fillRect(15, 10, 50, 40, QtGui.QColor(200, 70, 40))
    painter.fillRect(30, 20, 15, 20, QtGui.QColor(*background))
    if connected:
        painter.fillRect(30, 0, 15, 25, QtGui.QColor(*background))
    painter.end()
    return image


def layer(**values):
    result = {"key_mode": "green", "key_method": "enhanced", "key_similarity": .04,
              "key_softness": .03, "key_despill": True, **values}
    normalize_visual(result)
    return result


class EnhancedKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_old_configs_keep_normal_and_presets(self):
        old = {"key_mode": "blue", "key_similarity": .14, "key_softness": .09}
        value = dict(old); normalize_visual(value)
        self.assertEqual(value["key_method"], "normal")
        self.assertEqual(key_color(value), (0, 0, 255))
        self.assertTrue(all(value[key] == item for key, item in old.items()))
        self.assertIn("color=0x0000ff", key_filters(value)[0])

    def test_arbitrary_color_and_no_filter_injection(self):
        value = layer(key_mode="custom", key_color="#DE35a0")
        self.assertEqual(key_color(value), (222, 53, 160))
        self.assertIn("color=0xde35a0", key_filters(value)[0])
        self.assertEqual(despill_type(value), "")
        self.assertEqual(despill_type(layer(key_mode="custom", key_color="#11DD22")), "green")
        for key, bad in (("key_color", "red"), ("key_color", "#00ff00,evil"),
                         ("key_method", "unknown"), ("key_protect_inside", 1), ("key_mask_path", "bad\npath")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_key({key: bad})

    def test_enclosed_decoration_keeps_alpha_AND_color_with_despill(self):
        original = sample()
        ordinary = keyed_image(original, layer(key_method="normal"))
        enhanced = keyed_image(original, layer())
        self.assertEqual(ordinary.pixelColor(35, 30).alpha(), 0)
        self.assertEqual(enhanced.pixelColor(35, 30), original.pixelColor(35, 30))
        self.assertEqual(enhanced.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(enhanced.pixelColor(20, 20).alpha(), 255)

    def test_all_borders_and_four_connectivity_not_corners_only(self):
        alpha = np.ones((10, 10), np.float32)
        alpha[:, 3:7] = 0  # background touches top/bottom but not corners
        alpha[3:6, 1] = 0  # enclosed same-colour prop
        protection = inside_protection(alpha)
        self.assertEqual(protection[0, 5], 0)
        self.assertEqual(protection[4, 1], 1)
        self.assertEqual(protection[4, 2], 0)

    def test_connected_green_is_not_falsely_claimed_semantic_foreground(self):
        enhanced = keyed_image(sample(connected=True), layer())
        self.assertEqual(enhanced.pixelColor(35, 30).alpha(), 0)
        self.assertEqual(keyed_image(sample(), layer(key_protect_inside=False)).pixelColor(35, 30).alpha(), 0)

    def test_trimap_can_protect_connected_prop_and_remove_real_hole(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"trimap.png"
            mask = QtGui.QImage(80, 60, QtGui.QImage.Format.Format_RGBA8888)
            mask.fill(QtGui.QColor(128, 128, 128))
            painter = QtGui.QPainter(mask); painter.fillRect(30, 20, 15, 20, QtGui.QColor("white")); painter.end()
            mask.save(str(path))
            result = keyed_image(sample(connected=True), layer(key_mask_path=str(path)))
            self.assertEqual(result.pixelColor(35, 30), QtGui.QColor(0, 255, 0))
            self.assertEqual(result.pixelColor(35, 5).alpha(), 0)
            painter = QtGui.QPainter(mask); painter.fillRect(30, 20, 15, 20, QtGui.QColor("black")); painter.end()
            mask.save(str(path))
            result = keyed_image(sample(), layer(key_mask_path=str(path)))
            self.assertEqual(result.pixelColor(35, 30).alpha(), 0)

    def test_trimap_native_alpha_and_mid_gray_are_honored(self):
        rgba = np.array([[[255, 255, 255, 0], [128, 128, 128, 255], [255, 255, 255, 128], [0, 0, 0, 255]]], np.uint8)
        keep, remove = trimap_channels(rgba)
        np.testing.assert_allclose(keep, [[0, 0, 128/255, 0]])
        np.testing.assert_allclose(remove, [[0, 0, 0, 1]])
        value, protection = enhanced_alpha(np.zeros((1, 4), np.float32), layer(key_protect_inside=False), rgba)
        np.testing.assert_allclose(value, keep)
        np.testing.assert_allclose(protection, keep)
        image = sample(); color = image.pixelColor(35, 30); color.setAlpha(70); image.setPixelColor(35, 30, color)
        self.assertEqual(keyed_image(image, layer()).pixelColor(35, 30).alpha(), 70)

    def test_missing_mask_is_clear_error_only_when_enabled(self):
        value = layer(key_mask_path="missing.png", key_method="normal")
        self.assertEqual(active_mask(value), "")
        keyed_image(sample(), value)
        with self.assertRaisesRegex(ValueError, "辅助遮罩不存在"):
            keyed_image(sample(), layer(key_mask_path="missing.png"))
        self.assertEqual(keyed_image(sample(), layer(key_mode="none", key_mask_path="missing.png")), sample())

    def test_gui_preset_custom_enhanced_and_layer_roundtrip(self):
        editor = VisualProperties(); self.addCleanup(editor.deleteLater)
        values = []; editor.changed.connect(values.append)
        editor.set_layer(layer())
        self.assertFalse(values)
        editor.key_details.color.setText("#ee4411"); editor.key_details.commit_color()
        self.assertEqual(values[-1]["key_mode"], "custom")
        self.assertEqual(values[-1]["key_color"], "#ee4411")
        self.assertFalse(editor.controls["key_despill"].isEnabled())
        editor.controls["key_mode"].setCurrentIndex(editor.controls["key_mode"].findData("blue"))
        self.assertEqual(editor.key_details.color.text(), "#0000ff")
        self.assertTrue(editor.controls["key_despill"].isEnabled())
        editor.key_details.mask.setText("mask.png"); editor.key_details.commit_mask()
        editor.controls["key_method"].setCurrentIndex(editor.controls["key_method"].findData("normal"))
        self.assertEqual(values[-1]["key_mask_path"], "mask.png")
        self.assertFalse(editor.key_details.mask_group.isVisible())
        normalized = normalize_layers([{"id":"visual", "kind":"image", "path":"fixture.png", **values[-1]}])[0]
        self.assertEqual(normalized["key_mask_path"], "mask.png")

    def test_node_has_mask_input_and_asset_capture(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, prepare_layer, build_layer
        from types import SimpleNamespace
        definition = layer_node("animated")
        self.assertIn("key_mask", [port.key for port in definition.inputs])
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.mp4"; source.touch()
            mask = Path(root)/"mask.png"; mask.touch()
            params = {p.key: copy.deepcopy(p.default) for p in definition.parameters}
            params["layer"].update(path=str(source), source_file=str(source), **layer(key_mask_path=str(mask)))
            prepared = prepare_layer(None, params, None)
            c = SimpleNamespace(node_prepared=prepared, workflow_globals=None)
            operation = build_layer({}, params, c, media="animated")["operations"][0]
            self.assertEqual({ref.data_type for _, ref in operation.assets}, {"layer_file", "key_mask"})
            mask.write_bytes(b"changed")
            with self.assertRaises(ValueError): build_layer({}, params, c, media="animated")

    def test_connected_mask_can_override_missing_inspector_fallback(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, prepare_layer, build_layer
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.mp4"; source.touch()
            mask = Path(root)/"mask.png"; mask.touch()
            params = {p.key:copy.deepcopy(p.default) for p in layer_node("animated").parameters}
            params["layer"].update(path=str(source),source_file=str(source),**layer(key_mask_path="missing-fallback.png"))
            c = SimpleNamespace(node_prepared=prepare_layer(None,params,None),workflow_globals=None)
            operation = build_layer({"key_mask":str(mask)},params,c,media="animated")["operations"][0]
            self.assertTrue(any(ref.path == str(mask.resolve()) for _,ref in operation.assets))

    def test_color_and_mask_dialogs_cancel_cleanly_and_do_not_touch_other_values(self):
        editor = VisualProperties(); self.addCleanup(editor.deleteLater)
        editor.set_layer(layer(key_alpha_low=10,key_alpha_high=70))
        received = []; editor.changed.connect(received.append)
        with patch.object(QtWidgets.QColorDialog,"getColor",return_value=QtGui.QColor()):
            editor.key_details.pick.click()
        with patch.object(QtWidgets.QFileDialog,"getOpenFileName",return_value=("", "")):
            editor.key_details.browse.click()
        self.assertFalse(received)
        with patch.object(QtWidgets.QColorDialog,"getColor",return_value=QtGui.QColor("#0102ee")):
            editor.key_details.pick.click()
        self.assertEqual(received[-1]["key_color"],"#0102ee")
        with patch.object(QtWidgets.QFileDialog,"getOpenFileName",return_value=("fixture-mask.png", "")):
            editor.key_details.browse.click()
        self.assertEqual(received[-1]["key_mask_path"],"fixture-mask.png")
        editor.key_details.clear.click()
        self.assertEqual(received[-1]["key_mask_path"],"")
        self.assertEqual((received[-1]["key_alpha_low"],received[-1]["key_alpha_high"]),(10,70))

    def test_preview_cache_fingerprints_mask_content_stamp(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from app_plugins.builtin.batch_text_video.store import fresh_state
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.png"; source.touch()
            mask = Path(root)/"mask.png"; mask.touch()
            state = fresh_state()
            state["settings"]["layers"] = normalize_layers([{"id":"v", "kind":"image", "path":str(source),
                **layer(key_mask_path=str(mask))}])
            before = preview_key({}, state); mask.write_bytes(b"new")
            self.assertNotEqual(before, preview_key({}, state))

    def test_legs_gap_is_removed_not_convex_hull_filled(self):
        image = QtGui.QImage(80, 60, QtGui.QImage.Format.Format_RGBA8888)
        image.fill(QtGui.QColor("#00ff00"))
        painter = QtGui.QPainter(image)
        painter.fillRect(20, 10, 40, 15, QtGui.QColor("red"))
        painter.fillRect(20, 20, 10, 40, QtGui.QColor("red"))
        painter.fillRect(50, 20, 10, 40, QtGui.QColor("red"))
        painter.end()
        result = keyed_image(image, layer())
        self.assertEqual(result.pixelColor(40, 40).alpha(), 0)
        self.assertEqual(result.pixelColor(25, 40).alpha(), 255)

    def test_real_layer_panel_does_not_drop_new_settings_on_selection_change(self):
        from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
        values = normalize_layers([{"id":"v", "kind":"image", "path":"fixture.png"}])
        panel = LayerPanel(values, ""); self.addCleanup(panel.deleteLater)
        panel.select_layer_id("v")
        editor = panel.visual_properties
        editor.controls["key_mode"].setCurrentIndex(editor.controls["key_mode"].findData("green"))
        editor.controls["key_method"].setCurrentIndex(editor.controls["key_method"].findData("enhanced"))
        editor.key_details.color.setText("#22cc33"); editor.key_details.commit_color()
        editor.key_details.mask.setText("fixture-mask.png"); editor.key_details.commit_mask()
        panel.select_layer_id("title"); panel.select_layer_id("v")
        selected = next(item for item in panel.values() if item["id"] == "v")
        self.assertEqual(selected["key_method"], "enhanced")
        self.assertEqual(selected["key_color"], "#22cc33")
        self.assertEqual(selected["key_mask_path"], "fixture-mask.png")


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实FFmpeg")
class EnhancedRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def run_ffmpeg(self, args):
        result = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf8", "replace"))
        return result.stdout

    def composite(self, root, source, options, seconds=.2, raw=False):
        from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
        from app_plugins.builtin.batch_text_video.visual_media import is_animated
        if raw:
            args = ["-loop", "1", "-i", str(source)]; filters = []
            pad, consumed = add_keyed_source(args, filters, layer(**options), 0, "test", 10, is_animated, duration=seconds)
            out = "["+pad+"]"
        else:
            value = normalize_layers([{"id":"v", "kind":"image", "path":str(source), "width":100, "height":100,
                **layer(**options)}])[0]
            args, filters = [], ["[0:v]format=rgba[base]"]
            consumed = add_plane(args, filters, value, 1, "base", "out", 80, 60, seconds, 10)
            args = ["-f", "lavfi", "-i", f"color=c=blue:s=80x60:r=10:d={seconds}", *args]
            out = "[out]"
        data = self.run_ffmpeg([*args, "-filter_complex", ";".join(filters), "-map", out,
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
        return QtGui.QImage.fromData(data), consumed

    def test_native_colorkey_enhanced_custom_preserves_decoration_and_source_alpha(self):
        with tempfile.TemporaryDirectory() as root:
            for mode, color in (("green", (0, 255, 0)), ("blue", (0, 0, 255)), ("black", (0, 0, 0)),
                                ("custom", (240, 10, 180))):
                with self.subTest(mode=mode):
                    source = Path(root)/(mode+".png"); image = sample(color); image.save(str(source))
                    options = {"key_mode":mode, "key_color":"#f00ab4"}
                    actual, count = self.composite(root, source, options, raw=True)
                    self.assertEqual(count, 1)
                    self.assertEqual((actual.width(), actual.height()), (80, 60))
                    self.assertEqual(actual.pixelColor(0, 0).alpha(), 0)
                    self.assertAlmostEqual(actual.pixelColor(35, 30).alpha(), 255, delta=1)
                    self.assertEqual(actual.pixelColor(35, 30).getRgb()[:3], color)
                    poster = keyed_image(image, layer(**options))
                    self.assertEqual(actual.pixelColor(35, 30), poster.pixelColor(35, 30))
            transparent = sample()
            c = transparent.pixelColor(35, 30); c.setAlpha(70); transparent.setPixelColor(35, 30, c)
            source = Path(root)/"alpha.png"; transparent.save(str(source))
            actual, _ = self.composite(root, source, {}, raw=True)
            self.assertAlmostEqual(actual.pixelColor(35, 30).alpha(), 70, delta=1)

    def test_native_trimap_override_scale_and_rgb_protection(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.png"; sample(connected=True).save(str(source))
            mask = QtGui.QImage(40, 30, QtGui.QImage.Format.Format_RGBA8888); mask.fill(QtGui.QColor(128, 128, 128))
            painter = QtGui.QPainter(mask); painter.fillRect(15, 10, 8, 10, QtGui.QColor("white")); painter.end()
            mask_path = Path(root)/"mask.png"; mask.save(str(mask_path))
            actual, count = self.composite(root, source, {"key_mask_path":str(mask_path)}, raw=True)
            self.assertEqual(count, 2)
            self.assertEqual((actual.width(), actual.height()), (80, 60))
            self.assertGreater(actual.pixelColor(35, 30).alpha(), 250)
            self.assertGreater(actual.pixelColor(35, 30).green(), 250)
            self.assertLess(actual.pixelColor(0, 0).alpha(), 2)

    def test_ordinary_has_same_color_loss_and_enhanced_keeps_prop_on_composite(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"source.png"; sample().save(str(source))
            ordinary, _ = self.composite(root, source, {"key_method":"normal"})
            enhanced, _ = self.composite(root, source, {})
            self.assertGreater(ordinary.pixelColor(35, 30).blue(), 250)
            self.assertGreater(enhanced.pixelColor(35, 30).green(), 250)
            self.assertGreater(enhanced.pixelColor(0, 0).blue(), 250)

    def test_dynamic_mask_does_not_freeze_non_looping_overlay(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root)/"frame.png"; sample().save(str(image))
            clip = Path(root)/"overlay.mov"
            self.run_ffmpeg(["-loop","1","-i",str(image),"-t","0.4","-r","10","-c:v","png","-pix_fmt","rgba",str(clip)])
            mask = Path(root)/"mask.mov"
            self.run_ffmpeg(["-f","lavfi","-i","color=c=gray:s=80x60:r=10:d=0.3","-c:v","png","-pix_fmt","rgba",str(mask)])
            value = normalize_layers([{"id":"v","kind":"image","path":str(clip),"width":100,"height":100,
                **layer(key_mask_path=str(mask), media_loop=False)}])[0]
            args, filters = [], ["[0:v]format=rgba[base]"]
            count = add_plane(args, filters, value, 1, "base", "out", 80, 60, 1, 10)
            self.assertEqual(count, 2)
            raw = self.run_ffmpeg(["-f","lavfi","-i","color=c=blue:s=80x60:r=10:d=1",*args,
                "-filter_complex",";".join(filters),"-map","[out]","-t","1","-f","rawvideo","-pix_fmt","rgba","pipe:1"])
            frames = np.frombuffer(raw,np.uint8).reshape(-1,60,80,4)
            self.assertEqual(len(frames), 10)
            self.assertGreater(frames[0,30,35,1], 240)
            self.assertGreater(frames[8,30,35,2], 240)
            self.assertLess(frames[8,30,35,1], 5)

    def test_node_preview_export_mask_inputs_are_rebased_and_single_encoding(self):
        import base64
        from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node
        from model.node_workflow.tasks import prepare_job
        from model.node_workflow.preview import run_preview
        from model.node_workflow.render import run_workflow
        from app_plugins.builtin.batch_text_video.workflow_nodes import canvas_nodes
        from test_node_batch_layers import context
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source, overlay = root/"base.mp4", root/"overlay.mov"
            self.run_ffmpeg(["-f","lavfi","-i","color=c=blue:s=80x60:r=10:d=0.6","-c:v","libx264",str(source)])
            png = root/"source.png"; sample(connected=True).save(str(png))
            self.run_ffmpeg(["-loop","1","-i",str(png),"-t","0.6","-r","10","-c:v","png","-pix_fmt","rgba",str(overlay)])
            mask = QtGui.QImage(80,60,QtGui.QImage.Format.Format_RGBA8888); mask.fill(QtGui.QColor(128,128,128))
            painter = QtGui.QPainter(mask); painter.fillRect(30,20,15,20,QtGui.QColor("white")); painter.end()
            mask_path = root/"mask.png"; mask.save(str(mask_path))
            registry = NodeRegistry(); register_core(registry)
            for definition in canvas_nodes(): registry.register("batch_text_video", definition)
            graph = empty_graph(); nodes = {}
            for name, kind in (("input","core.import"),("layer","batch_text_video.animated_layer"),
                               ("settle","core.settle"),("preview","core.preview"),("export","core.export")):
                nodes[name] = new_node(registry.definitions[kind]); graph["nodes"].append(nodes[name])
            nodes["input"]["params"]["video"] = str(source)
            nodes["layer"]["params"]["layer"].update(source_file=str(overlay), path=str(overlay), width=100, height=100,
                **layer(key_mask_path=str(mask_path)))
            nodes["export"]["params"].update(destination="file", output=str(root/"output.mp4"))
            for a, out, b, inp in (("input","video","settle","video"),("layer","operations","settle","operations"),
                                   ("settle","plan","preview","value"),("preview","value","export","plan")):
                graph["connections"].append([nodes[a]["id"],out,nodes[b]["id"],inp])
            graph["output_node"] = nodes["export"]["id"]
            payload, _ = prepare_job(graph, registry, None, {}); payload["ffmpeg"] = FFMPEG
            preview = run_preview({**payload,"preview_node":nodes["preview"]["id"]}, context(), registry=registry)
            result = run_workflow(payload, context(), registry=registry)
            a = QtGui.QImage.fromData(base64.b64decode(preview["png"]))
            b = QtGui.QImage.fromData(self.run_ffmpeg(["-ss","0.2","-i",result["video"],"-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"]))
            self.assertEqual(a.size(), b.size())
            for image in (a,b):
                self.assertGreater(image.pixelColor(35,30).green(),230)
                self.assertGreater(image.pixelColor(0,0).blue(),230)
            self.assertEqual(result["video_encodings"],1)
            self.assertEqual(result["intermediate_videos"],0)

    def test_batch_playback_and_export_share_mask_pipeline_and_do_not_touch_inputs(self):
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root/"source.mp4"
            self.run_ffmpeg(["-f","lavfi","-i","color=c=blue:s=320x240:r=10:d=0.6","-c:v","libx264",str(source)])
            overlay = root/"visual.png"; sample(connected=True).save(str(overlay))
            mask = root/"trimap.png"
            image = QtGui.QImage(80,60,QtGui.QImage.Format.Format_RGBA8888); image.fill(QtGui.QColor(128,128,128))
            painter = QtGui.QPainter(image); painter.fillRect(30,20,15,20,QtGui.QColor("white")); painter.end(); image.save(str(mask))
            originals = {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in (source,overlay,mask)}
            state = fresh_state()
            state["settings"].update(ffmpeg_path=FFMPEG,aspect="source",music_enabled=False,keep_audio=False,darkness=0,
                auto_crop_black_bars=False,layers=normalize_layers([{"id":"v","kind":"image","path":str(overlay),"width":100,"height":100,
                    **layer(key_mask_path=str(mask))}] + [dict(item,enabled=False) for item in DEFAULT_LAYERS]))
            job = {"id":"j","path":str(source),"name":"finished","title":"","body":"","task_dir":str(root/"task")}
            state["jobs"] = [job]
            result, _ = render_static_text(job,state,root/"private")
            preview, _ = render_static_text({**job,"name":"preview"},state,root/"private",preview=True)
            for path in (result,preview):
                image = QtGui.QImage.fromData(self.run_ffmpeg(["-ss","0.2","-i",str(path),"-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"]))
                self.assertEqual((image.width(),image.height()),(320,240))
                self.assertGreater(image.pixelColor(140,120).green(),230)
            self.assertEqual(originals,{path:hashlib.sha256(path.read_bytes()).hexdigest() for path in originals})
            voice = root/"voice.wav"
            self.run_ffmpeg(["-f","lavfi","-i","sine=frequency=440:duration=0.6","-c:a","pcm_s16le",str(voice)])
            state["settings"]["use_voice"] = True
            spoken = {**job,"name":"spoken","voice_path":str(voice),"voice_override":True}
            state["jobs"] = [spoken]
            output, _ = render_static_text(spoken,state,root/"private")
            from app_plugins.builtin.batch_text_video.engine import probe
            self.assertTrue(probe(output,str(Path(FFMPEG).with_name("ffprobe.exe")))["audio"])

    def test_auxiliary_mask_is_protected_from_output_overwrite(self):
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); source=root/"source.mp4"; mask=root/"mask.mp4"; overlay=root/"visual.png"
            sample().save(str(overlay))
            for path,color in ((source,"blue"),(mask,"gray")):
                self.run_ffmpeg(["-f","lavfi","-i",f"color=c={color}:s=320x240:r=10:d=0.3","-c:v","libx264",str(path)])
            state=fresh_state()
            state["settings"].update(ffmpeg_path=FFMPEG,aspect="source",music_enabled=False,overwrite=True,darkness=0,
                auto_crop_black_bars=False,layers=normalize_layers([{"id":"v","kind":"image","path":str(overlay),
                    **layer(key_mask_path=str(mask))}] + [dict(item,enabled=False) for item in DEFAULT_LAYERS]))
            job={"id":"j","path":str(source),"name":"mask","title":"","body":"","task_dir":str(root)}
            state["jobs"]=[job]
            with self.assertRaisesRegex(ValueError,"覆盖源文件"):
                render_static_text(job,state,root/"private")

    def test_one_frame_preview_has_no_sink_that_drains_full_clip(self):
        from app_plugins.builtin.batch_text_video.chroma_key import add_keyed_source
        from app_plugins.builtin.batch_text_video.visual_media import is_animated
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/"source.png"; sample().save(str(source))
            for method,spill in (("normal",False),("enhanced",False),("enhanced",True)):
                with self.subTest(method=method,spill=spill):
                    args,filters=["-loop","1","-i",str(source)],[]
                    pad,_=add_keyed_source(args,filters,layer(key_method=method,key_despill=spill),0,"test",10,is_animated,duration=300)
                    self.assertNotIn("nullsink",";".join(filters))
                    image=QtGui.QImage.fromData(self.run_ffmpeg([*args,"-filter_complex",";".join(filters),"-map","["+pad+"]",
                        "-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"]))
                    self.assertFalse(image.isNull())


if __name__ == "__main__": unittest.main()

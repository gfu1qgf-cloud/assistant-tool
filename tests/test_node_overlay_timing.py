"""Timing contracts plus real FFmpeg visibility, alpha, clocks and preview parity."""
import base64
import copy
from dataclasses import replace
import io
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
from model.node_workflow.contracts import ResourceRef, FilterOperation, RenderPlan, VideoFilterFragment
from model.node_workflow.filter_graph import VideoFilterChain
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, validate, evaluate_graph
from model.node_workflow.render import compile_filters, render_plan, probe
from model.node_workflow.preview import frame_preview
from app_plugins.builtin.batch_text_video.workflow_nodes import canvas_nodes, build_caption
from app_plugins.builtin.batch_text_video.workflow_layers import layer_node, build_layer, prepare_layer
from app_plugins.builtin.batch_text_video.workflow_timing import KIND, timing_node, build_timing, read_operation
from app_plugins.builtin.batch_text_video.overlay_timing import DEFAULTS, EFFECTS, normalize_timing, resolve_window
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG = os.environ.get("NODE_FFMPEG", "")


def context(registry=None):
    c = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
    c.node_prepared, c.prepared, c.notes = {}, {}, []
    c.workflow_registry = registry
    c.ffmpeg = FFMPEG
    return c


class OverlayTimingTests(unittest.TestCase):
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
        for definition in canvas_nodes(): self.registry.register("batch_text_video", definition)
        self.c = context(self.registry)
        self.image = self.root / "图片,[特殊;].png"
        picture = QtGui.QImage(60, 40, QtGui.QImage.Format.Format_ARGB32)
        picture.fill(QtGui.QColor("red")); self.assertTrue(picture.save(str(self.image)))

    def layer(self, media="image", **settings):
        params = {p.key: copy.deepcopy(p.default) for p in layer_node(media).parameters}
        params["layer"].update(x=35, y=45, width=28, height=30)
        if media != "text": params["layer"].update(path=str(self.image), source_file=str(self.image))
        else: params["layer"].update(text="Božia láska", font_min=45, font_max=45, width=90, height=35)
        params["layer"].update(settings)
        self.c.node_prepared = prepare_layer(None, params, None)
        return build_layer({}, params, self.c, media=media)["operations"][0]

    def timed(self, operation, **settings):
        return build_timing({"operations": (operation,)}, {**DEFAULTS, **settings}, self.c)["operations"][0]

    def test_registration_ports_and_choices(self):
        definition = self.registry.definitions[KIND]
        self.assertEqual(definition.category, "视频与画面/时间控制")
        self.assertEqual([(p.key, p.required) for p in definition.inputs],
                         [("operations", True), ("start", False), ("end", False)])
        self.assertTrue(definition.inputs[0].multiple)
        self.assertNotIn("video", [p.key for p in definition.inputs])
        parameters = {p.key: p for p in definition.parameters}
        self.assertEqual({k for k, _ in parameters['enter'].choices}, EFFECTS)
        self.assertEqual({k for k, _ in parameters['exit'].choices}, EFFECTS)
        self.assertEqual(parameters['fade_in'].default, 0)
        self.assertEqual(parameters['fade_out'].default, 0)

    def test_no_render_or_asset_change_during_build_and_multiple_wire_order(self):
        image, text = self.layer(), self.layer("text")
        original_options, image_bytes = image.options, self.image.read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("构图不执行媒体")):
            result = build_timing({"operations": ((image,), (text,)), "start": 20, "end": 25}, DEFAULTS, self.c)
        ops = result["operations"]
        self.assertEqual(len(ops), 2)
        self.assertEqual([read_operation(op)[0].name for op in ops], [image.name, text.name])
        self.assertEqual(read_operation(ops[0])[1]["start"], 20)
        self.assertEqual(read_operation(ops[0])[1]["end"], 25)
        self.assertIs(ops[0].assets, image.assets)
        self.assertIsNone(ops[0].video)
        self.assertEqual(image.options, original_options)
        self.assertEqual(image_bytes, self.image.read_bytes())
        json.dumps(dict(ops[0].options), ensure_ascii=False, allow_nan=False)

    def test_repeated_window_replaces_instead_of_nesting_or_changing_owner(self):
        op = replace(self.layer(), video="synthetic_owner")
        once = self.timed(op, start=1, end=5)
        twice = self.timed(once, start=2, end=4)
        source, settings = read_operation(twice)
        self.assertEqual(source.name, op.name)
        self.assertEqual(source.options, op.options)
        self.assertEqual(source.video, "synthetic_owner")
        self.assertEqual(settings["start"], 2)
        self.assertEqual(settings["end"], 4)

    def test_unsupported_operations_and_untrusted_expressions_rejected(self):
        for name in ("scale", "lut3d", "other_plugin.operation"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "不是可控制"):
                self.timed(FilterOperation(name, None, name))
        for settings in ({"start": True}, {"start": float("nan")}, {"end": float("inf")},
                         {"start": -1}, {"start": 3, "end": 2}, {"enter": "fade;movie=remote"},
                         {"distance": 81}, {"clock": "eval"}, {"start": "20"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.timed(self.layer(), **settings)
        with self.assertRaises(ValueError): normalize_timing({"raw_expression": "x"})

    def test_actual_duration_sentinel_and_short_window_policy(self):
        messages = []
        w = resolve_window({"start": 20, "end": 0}, 23.8)
        self.assertEqual(w.end, 23.8)
        self.assertEqual(w.clock, "restart")
        w = resolve_window({"start": 20, "end": 30}, 24, report=lambda m, _: messages.append(m))
        self.assertEqual(w.end, 24)
        self.assertTrue(messages)
        short = resolve_window({"start": 1, "end": 1.1, "enter_duration": 1, "exit_duration": 1}, 3)
        self.assertLess(short.enter_duration + short.exit_duration, .1)
        with self.assertRaisesRegex(ValueError, "没有可显示"):
            resolve_window({"start": 20}, 8)
        global_window = resolve_window({"start": 2}, 4, automatic_clock="global")
        self.assertEqual(global_window.source_duration(4), 4)

    def test_graph_parameters_checked_before_execution(self):
        graph = empty_graph()
        text, timing, settle, imp = [new_node(self.registry.definitions[k]) for k in (
            "batch_text_video.text_layer", KIND, "core.settle", "core.import")]
        text["params"]["layer"]["text"] = "Hello"
        source = self.root / "source.mp4"; source.write_bytes(b"synthetic")
        imp["params"]["video"] = str(source)
        graph["nodes"] = [text, timing, settle, imp]
        graph["connections"] = [[text["id"], "operations", timing["id"], "operations"],
            [timing["id"], "operations", settle["id"], "operations"], [imp["id"], "video", settle["id"], "video"]]
        graph["output_node"] = settle["id"]
        timing["params"].update(start=4, end=3)
        self.assertTrue(any("结束秒" in m for _, m in validate(graph, self.registry)[0]))

    def test_real_graph_numeric_overrides_and_multiple_operation_order(self):
        graph = empty_graph()
        first, second, start, end, window = [new_node(self.registry.definitions[k]) for k in (
            "batch_text_video.text_layer", "batch_text_video.text_layer", "core.number", "core.number", KIND)]
        first['params']['layer']['text'], second['params']['layer']['text'] = 'First', 'Second'
        start['params']['value'], end['params']['value'] = 1.2, 2.4
        graph['nodes'] = [first, second, start, end, window]
        graph['connections'] = [[second['id'], 'operations', window['id'], 'operations'],
            [first['id'], 'operations', window['id'], 'operations'],
            [start['id'], 'number', window['id'], 'start'], [end['id'], 'number', window['id'], 'end']]
        graph['output_node'] = window['id']
        self.assertEqual(validate(graph, self.registry, root_id=window['id'])[0], [])
        result = evaluate_graph(graph, self.registry, self.c, root_id=window['id'])['operations']
        self.assertEqual(len(result), 2)
        self.assertEqual([json.loads(dict(read_operation(op)[0].options)['layer'])['text'] for op in result], ['Second', 'First'])
        self.assertTrue(all(read_operation(op)[1]['start'] == 1.2 and read_operation(op)[1]['end'] == 2.4 for op in result))

    def test_compile_static_plane_has_alpha_gate_not_whole_video_effect(self):
        source = self.root / "source.mp4"; source.write_bytes(b"synthetic")
        video = ResourceRef.capture(source, "video")
        operation = self.timed(self.layer(), start=20, end=23, enter="left", exit="small")
        info = {"duration": 24., "video": {"width": 256, "height": 144, "avg_frame_rate": "10/1"}, "audio": True}
        chain = compile_filters(RenderPlan(video, (operation,), ""), self.root, self.c, video_info=info)
        self.assertIsInstance(chain, VideoFilterChain)
        fragment = chain.steps[0]
        self.assertIsInstance(fragment, VideoFilterFragment)
        content = ";".join(fragment.filters)
        self.assertIn("gte(t,20.000000000)*lt(t,23.000000000)", content)
        self.assertIn("format=rgba", content)
        self.assertNotIn('fade=t=', content)
        self.assertEqual(sum("[input]" in f for f in fragment.filters), 1)
        self.assertIn("[input][", fragment.filters[-1])
        self.assertEqual(len(fragment.inputs), 1)
        self.assertEqual(list(self.root.glob("*.ass")), [])

    def test_disabled_layer_and_asset_snapshot_still_checked(self):
        operation = self.timed(self.layer(enabled=False), start=20)
        source, _ = read_operation(operation)
        self.assertEqual(source.name, "null")
        op = self.timed(self.layer(), start=1)
        self.image.write_bytes(b"changed")
        source = self.root / "source.mp4"; source.write_bytes(b"synthetic")
        video = ResourceRef.capture(source, "video")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            compile_filters(RenderPlan(video, (op,), ""), self.root, self.c,
                video_info={"duration": 3, "video": {"width": 256, "height": 144}, "audio": False})


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "需要真实FFmpeg")
class RealOverlayTimingTests(OverlayTimingTests):
    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def media(self, duration=3.2):
        source = self.root / "background.mp4"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", f"color=c=gray:s=256x144:r=20:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)])
        self.video = ResourceRef.capture(source, "video")
        return source

    def render(self, operation, name="output"):
        result = render_plan(RenderPlan(self.video, (operation,), str(self.root / (name + ".mp4"))), self.c, FFMPEG)
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        return result["video"]

    def image_at(self, video, at):
        raw = self.ffmpeg(["-ss", str(at), "-i", str(video), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def red_bounds(self, image):
        import numpy as np
        pixels = np.asarray(image)
        ys, xs = np.nonzero((pixels[:, :, 0].astype(int) - pixels[:, :, 1] > 25) & (pixels[:, :, 0] > 130))
        return None if not len(xs) else (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

    def test_real_twenty_second_delay_finish_audio_and_preview_match(self):
        source = self.media(24)
        op = self.timed(self.layer(), start=20, end=22, enter="cut", exit="cut")
        output = self.render(op, "twenty_seconds")
        self.assertIsNone(self.red_bounds(self.image_at(output, 19.5)))
        self.assertIsNotNone(self.red_bounds(self.image_at(output, 20.2)))
        self.assertIsNone(self.red_bounds(self.image_at(output, 22.1)))
        info = probe(output, str(Path(FFMPEG).with_name("ffprobe.exe")))
        self.assertAlmostEqual(info["duration"], 24, delta=.08)
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (256, 144))
        def pcm(path):
            return self.ffmpeg(["-i", str(path), "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"])
        self.assertEqual(pcm(source), pcm(output))
        folder = self.root / "preview"; folder.mkdir()
        for at in (19.5, 20.2, 22.1):
            with self.subTest(at=at):
                # The host makes a fresh request directory; do not accidentally
                # read the first request's PNG when FFmpeg is given -n.
                frame_folder = folder / str(at); frame_folder.mkdir()
                preview = frame_preview(RenderPlan(self.video, (op,), ""), at, self.c, frame_folder)
                image = Image.open(io.BytesIO(base64.b64decode(preview[0]))).convert("RGB")
                actual = self.image_at(output, at)
                self.assertEqual(image.size, actual.size)
                if os.environ.get('TIMING_QA_DIR'):
                    qa = Path(os.environ['TIMING_QA_DIR']); qa.mkdir(parents=True, exist_ok=True)
                    image.save(qa / f'preview_{at}.png'); actual.save(qa / f'export_{at}.png')
                self.assertLess(sum(ImageStat.Stat(ImageChops.difference(image, actual)).mean)/3, 4,
                    f'preview={self.red_bounds(image)} export={self.red_bounds(actual)} '
                    f'pixel={image.getpixel((89,64))}/{actual.getpixel((89,64))}')

    def test_all_effects_render_and_never_transform_background(self):
        self.media()
        original = self.image_at(self.video.path, 1.2)
        for mode in sorted(EFFECTS):
            with self.subTest(effect=mode):
                op = self.timed(self.layer(), start=.5, end=2.5, enter=mode, exit=mode,
                                enter_duration=.8, exit_duration=.8, distance=30)
                output = self.render(op, mode)
                self.assertIsNone(self.red_bounds(self.image_at(output, .2)))
                hold = self.image_at(output, 1.4)
                self.assertIsNotNone(self.red_bounds(hold))
                for at in (.8, 1.4, 2.2):
                    image = self.image_at(output, at)
                    self.assertLess(max(abs(a-b) for a, b in zip(image.getpixel((240, 130)), original.getpixel((240, 130)))), 4)
                self.assertIsNone(self.red_bounds(self.image_at(output, 2.8)))
                if mode in {"left", "right", "up", "down", "small", "large"}:
                    self.assertNotEqual(self.red_bounds(self.image_at(output, .8)), self.red_bounds(hold))

    def test_fade_alpha_and_existing_blend_and_key_modes(self):
        self.media()
        source = self.root / "green.mp4"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=0x00ff00:s=60x40:r=20:d=1,drawbox=x=15:y=10:w=30:h=20:color=red:t=fill",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        for mode in ("normal", "add", "screen"):
            with self.subTest(blend=mode):
                op = self.timed(self.layer("animated", path=str(source), source_file=str(source),
                    key_mode="green", key_similarity=.2, key_softness=.1, blend_mode=mode),
                    start=.5, end=2.5, enter="fade", exit="fade", enter_duration=.8, exit_duration=.8)
                output = self.render(op, "key_"+mode)
                middle = self.image_at(output, 1.4).getpixel((89, 64))
                fading = self.image_at(output, .7).getpixel((89, 64))
                self.assertGreater(middle[0]-middle[1], fading[0]-fading[1]+20)
                self.assertIsNone(self.red_bounds(self.image_at(output, .2)))
                self.assertIsNone(self.red_bounds(self.image_at(output, 2.8)))
                # The keyed green portion must not become black or green on entry.
                corner = self.image_at(output, 1.4).getpixel((68, 52))
                self.assertLess(max(corner)-min(corner), 8)

    def test_nonlooping_source_starts_on_appearance_global_clock_and_gif(self):
        self.media()
        source = self.root / "red_blue.mp4"
        self.ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=red:s=60x40:r=20:d=0.8,drawbox=color=blue:t=fill:enable='gte(t,0.4)'",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
        layer = self.layer("animated", path=str(source), source_file=str(source), media_loop=False)
        relative = self.render(self.timed(layer, start=1, end=2.8, enter="cut", exit="cut"), "restart")
        self.assertIsNone(self.red_bounds(self.image_at(relative, .5)))
        self.assertIsNotNone(self.red_bounds(self.image_at(relative, 1.2)))
        self.assertGreater(self.image_at(relative, 1.6).getpixel((89, 64))[2], 180)
        self.assertLess(max(self.image_at(relative, 2).getpixel((89, 64)))-min(self.image_at(relative, 2).getpixel((89, 64))), 8)
        global_output = self.render(self.timed(layer, start=1, end=2.8, enter="cut", exit="cut", clock="global"), "global")
        self.assertIsNone(self.red_bounds(self.image_at(global_output, 1.2)))
        gif = self.root / "animated.gif"
        self.ffmpeg(["-y", "-i", str(source), str(gif)])
        gif_layer = self.layer("animated", path=str(gif), source_file=str(gif), media_loop=True)
        output = self.render(self.timed(gif_layer, start=1, end=2.8, enter="cut", exit="cut"), "gif")
        self.assertIsNotNone(self.red_bounds(self.image_at(output, 1.2)))
        self.assertGreater(self.image_at(output, 1.6).getpixel((89, 64))[2], 180)
        self.assertIsNotNone(self.red_bounds(self.image_at(output, 2)))

    def test_plain_and_scrolling_text_reuse_same_backend_and_visibility(self):
        self.media()
        for animation in ("none", "up", "down"):
            with self.subTest(animation=animation):
                layer = self.layer("text", color="#ff0000", outline_width=0, text_animation=animation,
                    text="Božia láska\npre nás všetkých", height=60, font_min=65, font_max=65)
                op = self.timed(layer, start=.7, end=2.5, enter="cut", exit="cut")
                output = self.render(op, "text_"+animation)
                self.assertIsNone(self.red_bounds(self.image_at(output, .3)))
                self.assertIsNotNone(self.red_bounds(self.image_at(output, 1.6)))
                self.assertIsNone(self.red_bounds(self.image_at(output, 2.8)))

    def test_basic_full_and_catalog_captions_keep_or_shift_cue_times(self):
        self.media()
        srt = self.root / "字幕.srt"
        srt.write_text("1\n00:00:00,100 --> 00:00:00,450\nBožia\n\n2\n00:00:01,500 --> 00:00:01,850\nláska\n", encoding="utf-8")
        asset = ResourceRef.capture(srt, "subtitles")
        original = srt.read_bytes()
        # Word-level SRT now respects multiword grouping; give this horizontal
        # timing fixture room for the group instead of relying on one page/word.
        layer = new_caption(); layer.update(caption_timing="task", font_min=65, font_max=65, color="#ff0000", outline_width=0,
                                             caption_per_line=2,height=60)
        from app_plugins.builtin.batch_text_video.caption_styles import apply_style
        catalog = apply_style(layer, "videokit:打字机模式")
        catalog.update(caption_timing="task", color="#ff0000", caption_effects={"color_text": "#ff0000"})
        for kind, caption in (("basic", None), ("pop", layer), ("catalog", catalog)):
            params = {"caption": caption, "color": "#ff0000", "size": 40, "outline": 0}
            operation = build_caption({"subtitles": asset}, params, self.c)["operations"][0]
            for clock in ("auto", "restart"):
                with self.subTest(kind=kind, clock=clock):
                    op = self.timed(operation, start=1, end=2.8, enter="cut", exit="cut", clock=clock)
                    output = self.render(op, kind+"_"+clock)
                    self.assertIsNone(self.red_bounds(self.image_at(output, .3)))
                    expected_time = 1.3 if clock == "restart" else 1.6
                    self.assertIsNotNone(self.red_bounds(self.image_at(output, expected_time)))
                    self.assertIsNone(self.red_bounds(self.image_at(output, 2.9)))
        self.assertEqual(srt.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

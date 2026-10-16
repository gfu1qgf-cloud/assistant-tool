import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.components import new_text_component, resolve_sources, commit_sequences
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, add_pool_paths
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.captions import new_caption, static_planes
from app_plugins.builtin.batch_text_video.caption_gallery import sample, CaptionGallery
from app_plugins.builtin.batch_text_video.caption_styles import preset_choices
from app_plugins.builtin.batch_text_video.engine import resolve_tools, render_static_text, probe
from app_plugins.builtin.batch_text_video.playback import preview_key
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.visual_media import normalize_visual, keyed_image

class UnifiedSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_source_picker_lists_compatible_pools_without_another_dialog(self):
        state = fresh_state()
        pools = [create_pool(state, kind, kind) for kind in ("visual", "video", "image", "voice", "text", "subtitle")]
        panel = LayerPanel(state["settings"]["layers"], ".", settings=state["settings"])
        panel.set_pool_state(state)
        panel.add_text()
        combo = panel.content_editor.source
        self.assertGreaterEqual(combo.findData("pool:" + pools[4]["id"]), 0)
        self.assertEqual(combo.findData("pool:" + pools[3]["id"]), -1)
        combo.setCurrentIndex(combo.findData("pool:" + pools[4]["id"]))
        self.assertEqual(panel.current()["pool_id"], pools[4]["id"])
        self.assertEqual(panel.editor.sections['font'].toggle.text(), "字体")
        self.assertEqual(panel.editor.sections['source'].toggle.text(), "内容与来源")
        actions = [action.text() for action in panel.add_button.menu().actions()]
        self.assertFalse(any("轮换" in text for text in actions))
        panel.add_caption()
        self.assertGreaterEqual(panel.content_editor.source.findData("pool:" + pools[5]["id"]), 0)
        panel.deleteLater(); self.app.processEvents()

    def test_fixed_text_file_and_named_text_pool_are_non_destructive(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"copy.txt"; path.write_text("标题\n正文", "utf-8-sig")
            original = path.read_bytes()
            layer = new_text_component(); layer.update(source="file", source_file=str(path))
            resolved, plan = resolve_sources([layer])
            self.assertEqual(resolved[0]["text"], "标题\n正文")
            self.assertEqual(plan, [])
            state = fresh_state(); pool = create_pool(state, "文字", "text")
            add_pool_paths(pool, [path])
            layer.update(source="pool", pool_id=pool["id"], pool_field="body")
            bound = bind_pool_layers([layer], state)
            self.assertEqual(resolve_sources(bound)[0][0]["text"], "正文")
            self.assertEqual(path.read_bytes(), original)

    def test_subtitle_pool_and_file_use_existing_timestamps(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"words.srt"; path.write_text("1\n00:00:00,000 --> 00:00:00,400\nAmen\n", "utf-8")
            layer = new_caption(); layer.update(source="file", source_file=str(path))
            resolved, plan = resolve_sources([layer])
            self.assertEqual(resolved[0]["caption_file"], str(path))
            self.assertEqual(resolved[0]["caption_timing"], "file")
            self.assertEqual(plan, [])
            state = fresh_state(); pool = create_pool(state, "已有字幕", "subtitle")
            add_pool_paths(pool, [path])
            layer.update(source="pool", pool_id=pool["id"])
            bound = bind_pool_layers([layer], state)
            self.assertEqual(resolve_sources(bound)[0][0]["caption_file"], str(path))
            self.assertEqual(commit_sequences(bound, resolve_sources(bound)[1])[0]["sequence_cursor"], 0)

    def test_corner_watermark_has_file_pool_source_and_separate_ledger(self):
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark
        state = fresh_state(); obj = new_watermark()
        obj["enabled"] = True
        child = obj["corners"]["top_left"]
        child.update(source="pool", enabled=True, sequence_items=[{"id":"a", "text":"first"}, {"id":"b", "text":"next"}])
        layers = normalize_layers([obj])
        resolved, steps = resolve_sources(layers)
        self.assertEqual(resolved[0]["corners"]["top_left"]["text"], "first")
        committed = commit_sequences(layers, steps)
        self.assertEqual(committed[0]["corners"]["top_left"]["sequence_cursor"], 1)
        self.assertEqual(layers[0]["corners"]["top_left"]["sequence_cursor"], 0)

    def test_visual_options_validate_and_key_preserves_other_color_alpha(self):
        image = QtGui.QImage(2, 1, QtGui.QImage.Format.Format_RGBA8888)
        image.setPixelColor(0, 0, QtGui.QColor("green")); image.setPixelColor(0, 0, QtGui.QColor(0,255,0))
        image.setPixelColor(1, 0, QtGui.QColor(255,0,0,127))
        layer = {"key_mode":"green", "key_similarity":.15, "key_softness":.08}
        normalize_visual(layer)
        output = keyed_image(image, layer)
        self.assertEqual(output.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(output.pixelColor(1, 0).alpha(), 127)
        for change in ({"blend_mode":"unknown"}, {"key_similarity":float("nan")}, {"media_loop":1}):
            with self.subTest(change=change), self.assertRaises(ValueError): normalize_visual({**layer, **change})

    def test_pool_plus_button_and_audio_mixing_folded_by_default(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            self.assertIs(dialog.resources.cornerWidget(QtCore.Qt.Corner.TopRightCorner), dialog.new_pool_button)
            self.assertFalse(dialog.mix_toggle.isChecked())
            labels = [section.toggle.text() for section in dialog.project_sections.sections.values()]
            self.assertIn("音频设置", labels); self.assertNotIn("混音细节", labels)
            with patch("app_plugins.builtin.batch_text_video.custom_pool_ui.NewPoolDialog.exec",
                       return_value=QtWidgets.QDialog.DialogCode.Rejected):
                hit = []; dialog.new_pool_button.clicked.connect(lambda: hit.append(True))
                dialog.new_pool_button.click(); self.assertTrue(hit)
            dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_fixed_audio_file_sets_one_file_source_and_persists(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, "request_background"):
            path = Path(root)/"voice.wav"; path.touch()
            dialog = BatchTextVideoDialog(store=Store(Path(root)/"store"))
            with patch("app_plugins.builtin.batch_text_video.audio_ui.QtWidgets.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                dialog.audio_cards["voice"].choose_file()
            self.assertEqual(dialog.controls["voice_pool_mode"].currentData(), "single")
            self.assertTrue(dialog.controls["use_voice"].isChecked())
            self.assertTrue(dialog.persist())
            saved = dialog.store.load()
            self.assertEqual(saved["resource_pools"][0]["items"][0]["path"], str(path))
            self.assertEqual(saved["settings"]["voice_pool_id"], saved["resource_pools"][0]["id"])
            dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_all_subtitle_presets_have_actual_renderer_thumbnails(self):
        for _name, key in [("pop", "pop"), ("appear", "appear")] + preset_choices():
            with self.subTest(key=key):
                image = sample(key)
                self.assertFalse(image.isNull())
                self.assertEqual(image.size(), QtCore.QSize(440,248))

    def test_pool_visual_and_fixed_text_file_changes_invalidate_preview_cache(self):
        with tempfile.TemporaryDirectory() as root:
            image_path = Path(root)/"element.png"
            image = QtGui.QImage(20,20,QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor("red")); image.save(str(image_path))
            text_path = Path(root)/"copy.txt"; text_path.write_text("first", "utf8")
            state = fresh_state(); pool = create_pool(state, "视觉", "visual")
            add_pool_paths(pool, [image_path])
            text = new_text_component(); text.update(source="file", source_file=str(text_path))
            state["settings"]["layers"] = normalize_layers([text, {"id":"element", "kind":"image", "source":"pool", "pool_id":pool["id"]}])
            first = preview_key({},state)
            text_path.write_text("second text", "utf8")
            second = preview_key({},state); self.assertNotEqual(first,second)
            image = QtGui.QImage(21,21,QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor("blue")); image.save(str(image_path))
            self.assertNotEqual(second,preview_key({},state))

    def test_subtitle_source_ui_preserves_uniform_and_can_return_to_task(self):
        state = fresh_state(); pool = create_pool(state,"字幕","subtitle")
        panel = LayerPanel(state["settings"]["layers"],".",settings=state["settings"])
        panel.set_pool_state(state); panel.add_caption()
        panel.caption_properties.controls["caption_timing"].setCurrentIndex(
            panel.caption_properties.controls["caption_timing"].findData("uniform"))
        editor = panel.content_editor
        editor.source.setCurrentIndex(editor.source.findData("pool:"+pool["id"]))
        self.assertEqual(panel.current()["caption_timing"],"uniform")
        timing = panel.caption_properties.controls["caption_timing"]
        # Explicitly choosing alignment switches back to task voice rather
        # than silently letting an old SRT/pool override the selected model.
        self.assertTrue(timing.model().item(timing.findData("model")).isEnabled())
        editor.source.setCurrentIndex(editor.source.findData("voice"))
        self.assertEqual(panel.current()["source"],"voice")
        self.assertEqual(panel.current()["caption_file"],"")
        self.assertTrue(timing.model().item(timing.findData("model")).isEnabled())
        panel.deleteLater(); self.app.processEvents()

    def test_failed_fixed_audio_selection_does_not_leave_an_empty_pool(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root))
            before = copy.deepcopy(dialog.state)
            with patch("app_plugins.builtin.batch_text_video.audio_ui.QtWidgets.QFileDialog.getOpenFileName",return_value=(str(Path(root)/"missing.wav"),"")):
                dialog.audio_cards["voice"].choose_file()
            self.assertEqual(dialog.state,before)
            dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_deleting_pool_retains_watermark_corner_contents_as_legacy_sequence(self):
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            store = Store(root); state = fresh_state()
            path = Path(root)/"corner.txt"; path.write_text("brand", "utf8")
            pool = create_pool(state,"水印文字","text"); add_pool_paths(pool,[path])
            obj = new_watermark(); obj["enabled"] = True
            obj["corners"]["top_left"].update(enabled=True,source="pool",pool_id=pool["id"])
            state["settings"]["layers"] = normalize_layers([obj]); store.save(state,0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.tabs.setCurrentWidget(dialog.resources)
            dialog.resources.setCurrentWidget(dialog.custom_pages[pool["id"]])
            with patch("app_plugins.builtin.batch_text_video.custom_pool_ui.QtWidgets.QMessageBox.question",return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.delete_resource_pool()
            watermark = next(layer for layer in dialog.state["settings"]["layers"] if layer["kind"] == "watermark")
            self.assertEqual(watermark["corners"]["top_left"]["source"],"sequence")
            self.assertEqual(watermark["corners"]["top_left"]["sequence_items"][0]["text"],"brand")
            self.assertEqual(path.read_text("utf8"),"brand")
            dialog.close(); dialog.deleteLater(); self.app.processEvents()

class VisualRenderTests(UnifiedSourcesTests):
    def make_media(self, root):
        ffmpeg, ffprobe = resolve_tools()
        bg, element = Path(root)/"background.mp4", Path(root)/"element.mp4"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for path, source, extra in ((bg, "color=c=0x203060:s=160x90:r=10:d=0.6", []),
            (element, "color=c=0x00ff00:s=64x64:r=10:d=0.2", ["-vf", "drawbox=x=26:y=26:w=12:h=12:color=red:t=fill"])):
            subprocess.run([ffmpeg,"-y","-v","error","-f","lavfi","-i",source,*extra,"-c:v","libx264","-pix_fmt","yuv420p",str(path)],
                check=True, capture_output=True, creationflags=flags)
        return ffmpeg, ffprobe, bg, element
    def pixel(self, path, time, x=80, y=45):
        ffmpeg, _ = resolve_tools()
        result = subprocess.run([ffmpeg,"-v","error","-ss",str(time),"-i",str(path),"-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"],
            capture_output=True, check=True, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        image = QtGui.QImage.fromData(result.stdout)
        return image.pixelColor(x,y)
    def state(self, root, bg, element, **properties):
        state = fresh_state(); state["settings"].update(aspect="source", darkness=0, music_enabled=False, overwrite=True)
        layer = {"id":"visual", "kind":"image", "path":str(element), "source":"fixed", "enabled":True,
                 "opacity":100, "x":50,"y":50,"width":50,"height":80,"key_mode":"green", **properties}
        from app_plugins.builtin.batch_text_video.layers import componentize_layers
        state["settings"]["layers"] = componentize_layers(normalize_layers([layer]), state["settings"])
        job = {"id":"job","path":str(bg),"title":"","body":"","name":"rendered","task_dir":str(root)}
        state["jobs"] = [job]
        return state, job
    def test_real_video_key_loop_and_no_loop_match_playback_renderer(self):
        with tempfile.TemporaryDirectory() as root:
            ffmpeg, ffprobe, bg, element = self.make_media(root)
            state, job = self.state(root, bg, element)
            source_hash = element.read_bytes()
            output, _ = render_static_text(job,state,root,preview=True)
            center, outside = self.pixel(output,.4), self.pixel(output,.4,100,45)
            self.assertGreater(center.red(),150); self.assertLess(center.green(),100)
            self.assertGreater(outside.blue(), outside.green())
            self.assertLess(outside.green(),90)
            self.assertFalse(probe(output,ffprobe)["audio"])
            old_key = preview_key(job,state)
            state["settings"]["layers"][0]["media_loop"] = False
            self.assertNotEqual(old_key,preview_key(job,state))
            output, _ = render_static_text(job,state,root,preview=True)
            after_end = self.pixel(output,.4)
            self.assertLess(after_end.red(),100); self.assertGreater(after_end.blue(),after_end.red())
            self.assertEqual(element.read_bytes(),source_hash)
    def test_real_add_screen_with_alpha_and_clipping(self):
        with tempfile.TemporaryDirectory() as root:
            _ffmpeg, _ffprobe, bg, element = self.make_media(root)
            for mode in ("normal","add","screen"):
                with self.subTest(mode=mode):
                    state, job = self.state(root,bg,element,blend_mode=mode,opacity=50)
                    output, _ = render_static_text(job,state,root,preview=True)
                    color = self.pixel(output,.4)
                    self.assertGreater(color.red(),100)
                    outside = self.pixel(output,.4,4,4)
                    self.assertLess(outside.red(),60); self.assertGreater(outside.blue(),outside.green())

    def test_real_gif_and_blue_screen_are_supported(self):
        with tempfile.TemporaryDirectory() as root:
            ffmpeg, _ffprobe, bg, element = self.make_media(root)
            gif = Path(root)/"element.gif"
            subprocess.run([ffmpeg,"-y","-v","error","-i",str(element),"-vf","fps=10","-loop","0",str(gif)],
                check=True,capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            state, job = self.state(root,bg,gif)
            output,_ = render_static_text(job,state,root,preview=True)
            self.assertGreater(self.pixel(output,.4).red(),150)
            blue = Path(root)/"blue.png"
            image = QtGui.QImage(64,64,QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor(0,0,255)); image.setPixelColor(32,32,QtGui.QColor("red")); image.save(str(blue))
            state,job = self.state(root,bg,blue,key_mode="blue",blend_mode="screen")
            output,_ = render_static_text(job,state,root,preview=True)
            self.assertLess(self.pixel(output,.4,100,45).red(),60)

    def test_fixed_overlay_source_cannot_be_overwritten_by_output(self):
        with tempfile.TemporaryDirectory() as root:
            _ffmpeg,_ffprobe,bg,element = self.make_media(root)
            state,job = self.state(root,bg,element)
            job["name"] = element.stem
            before = element.read_bytes()
            with self.assertRaisesRegex(ValueError,"覆盖源文件"):
                render_static_text(job,state,root)
            self.assertEqual(element.read_bytes(),before)

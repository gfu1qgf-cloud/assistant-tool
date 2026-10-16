import copy
import gc
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.captions import (new_caption, caption_text,
    caption_events, load_word_timings, write_caption_ass, static_planes, filter_path, validate_words,
    caption_subtitle_path, read_task_subtitle, task_word_timings)
from app_plugins.builtin.batch_text_video.components import resolve_sources, commit_sequences
from app_plugins.builtin.batch_text_video.layers import normalize_layers, componentize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, TextDoesNotFit
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.engine import Cancelled, render_static_text, probe

WORDS = [{"text":"Chlieb", "start":.25, "end":.65},
         {"text":"náš", "start":.9, "end":1.25},
         {"text":"každodenný.", "start":1.6, "end":2.0}]
TEXT = "Chlieb náš každodenný."
PREFIX = "app_plugins.builtin.batch_text_video."


def component(**values):
    layer = new_caption()
    layer.update(values)
    return normalize_layers([layer])[0]


class DynamicCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        # Release signal-retained editors while the shared QApplication is
        # alive, rather than leaving Qt native widgets to interpreter teardown.
        gc.collect()
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_caption_is_an_independent_component_and_never_consumes_a_pool(self):
        layer = component()
        resolved, plan = resolve_sources(normalize_layers([layer]), "title", TEXT)
        self.assertEqual(plan, [])
        self.assertEqual(resolved[0]["kind"], "caption")
        self.assertEqual(commit_sequences(resolved, plan), resolved)

    def test_old_static_defaults_are_not_migrated_to_dynamic(self):
        state = fresh_state()
        original = copy.deepcopy(state)
        normalize_layers(state["settings"]["layers"])
        self.assertEqual(state, original)
        self.assertFalse(any(layer["kind"] == "caption" for layer in state["settings"]["layers"]))

    def test_component_validation_and_limits(self):
        for changes in ({"caption_words":0}, {"caption_words":True},
                        {"caption_pop_ms":900}, {"caption_preset":"unknown"},
                        {"caption_language":"not a language"}, {"source":"unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                component(**changes)
        with self.assertRaisesRegex(ValueError,"4"):
            normalize_layers([new_caption() for _ in range(5)])

    def test_source_modes_are_explicit(self):
        layer = component()
        self.assertEqual(caption_text(layer,"TITLE",TEXT), TEXT)
        layer["caption_source"] = "full"
        self.assertEqual(caption_text(layer,"TITLE",TEXT), "TITLE\n"+TEXT)
        layer["caption_source"] = "asr"
        self.assertEqual(caption_text(layer,"TITLE",TEXT), "")

    def test_design_preview_never_loads_or_runs_a_model(self):
        settings = componentize_layers([component()],fresh_state()["settings"])
        for layer in settings:
            if layer["kind"] in {"title","body"}:
                layer["enabled"] = False
        with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("GUI inference")):
            image, info = render_overlay("",TEXT,360,640,{**fresh_state()["settings"],"layers":settings})
        self.assertFalse(image.isNull())
        self.assertIn(settings[0]["id"],info["component_sizes"])

    def test_each_word_starts_at_its_actual_time_and_groups_clear_together(self):
        events = caption_events(WORDS,component(),1080,1920,2.4)
        self.assertEqual([item["start"] for item in events],[.25,.9,1.6])
        self.assertEqual(events[0]["end"],events[1]["end"])
        self.assertLessEqual(events[0]["end"],1.6)
        self.assertEqual([item["text"] for item in events],[item["text"] for item in WORDS])
        self.assertLess(events[0]["y"],events[1]["y"])

    def test_random_sizes_are_repeatable_but_seed_can_be_changed(self):
        layer = component(caption_random_size=True)
        first = caption_events(WORDS,layer,1080,1920,2.4)
        self.assertEqual(first,caption_events(WORDS,layer,1080,1920,2.4))
        layer["caption_seed"] += 1
        self.assertNotEqual(first,caption_events(WORDS,layer,1080,1920,2.4))
        layer.update(font_min=60,font_max=60)
        fixed = caption_events(WORDS,layer,1080,1920,2.4)
        layer["caption_seed"] += 1
        self.assertEqual(fixed,caption_events(WORDS,layer,1080,1920,2.4))

    def test_out_of_bounds_words_fail_instead_of_silent_text_clipping(self):
        with self.assertRaises(TextDoesNotFit):
            caption_events(WORDS,component(width=2,height=2,font_min=180,font_max=180),1080,1920,2.4)
        for words in ([],[{"text":"zero","start":1,"end":1}],
                      [{"text":"late","start":3,"end":4}]):
            with self.assertRaises(ValueError):
                validate_words(words,2.4)

    def test_glyph_vectors_prevent_ass_markup_injection_and_support_unicode(self):
        words = [{"text":r"{\pos(0,0)}náš", "start":.2,"end":1.2}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"caption.ass"
            events = write_caption_ass(path,words,component(),1080,1920,2.4)
            data = path.read_text("utf8")
            self.assertNotIn(words[0]["text"],data)
            self.assertIn(r"\p1",data)
            self.assertIn(r"\fscx72",data)
            self.assertEqual(events[0]["start"],.2)

    def test_appear_preset_has_no_scale_animation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"caption.ass"
            write_caption_ass(path,WORDS,component(caption_preset="appear"),360,640,2.4)
            self.assertNotIn(r"\t(",path.read_text("utf8"))

    def test_cache_reuses_audio_content_after_rename_but_not_changed_text_or_audio(self):
        from globalValue import globalValue
        result = Mock()
        result.all_words.return_value = [types.SimpleNamespace(word=w["text"],start=w["start"],end=w["end"]) for w in WORDS]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(globalValue,"whisper_model_name",return_value="large-v3"), \
             patch.object(globalValue,"get_whisper_model",return_value=object()) as get_model, \
             patch("stable_whisper.alignment.align",return_value=result) as align:
            voice = Path(folder)/"task.wav"
            voice.write_bytes(b"audio content")
            first = load_word_timings(voice,TEXT,2.4,folder)
            renamed = Path(folder)/"renamed.wav"
            voice.rename(renamed)
            self.assertEqual(first,load_word_timings(renamed,TEXT,2.4,folder))
            self.assertEqual(get_model.call_count,1)
            self.assertEqual(align.call_args.kwargs["language"],"sk")
            renamed.write_bytes(b"changed content")
            load_word_timings(renamed,TEXT,2.4,folder)
            self.assertEqual(align.call_count,2)
            with self.assertRaisesRegex(ValueError,"缺少或改变"):
                load_word_timings(renamed,TEXT+" MORE",2.4,folder)
            self.assertEqual(align.call_count,4)  # Failed coverage now receives one automatic retry.
            self.assertEqual(len(list((Path(folder)/"caption-cache").glob("*.json"))),2)

    def test_cancellation_never_publishes_a_timing_cache(self):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(Cancelled):
            load_word_timings(Path(folder)/"unused.wav",TEXT,2.4,folder,cancel)
        self.assertTrue(cancel.is_set())

    def test_layer_order_splits_static_images_on_both_sides_of_caption(self):
        state = fresh_state()
        layers = componentize_layers(state["settings"]["layers"],state["settings"])
        caption = component()
        layers.insert(1,caption)
        settings = {**state["settings"],"layers":layers}
        planes = static_planes("Title",TEXT,360,640,settings,None)
        self.assertEqual([kind for kind,_value in planes],["image","caption","image"])
        self.assertEqual(planes[1][1]["id"],caption["id"])

    def test_component_ui_add_edit_disable_and_remove(self):
        with tempfile.TemporaryDirectory() as folder:
            state = fresh_state()
            panel = LayerPanel(state["settings"]["layers"],folder,settings=state["settings"])
            panel.add_caption()
            self.assertEqual(panel.current()["kind"],"caption")
            self.assertTrue(panel.editor.sections['caption'].toggle.isChecked())
            self.assertTrue(('source' in panel.editor.sections))
            self.assertTrue(panel.text_properties.isEnabled())
            panel.caption_properties.controls["caption_words"].setValue(3)
            self.assertEqual(panel.current()["caption_words"],3)
            timing = panel.caption_properties.controls["caption_timing"]
            timing.setCurrentIndex(timing.findData("uniform"))
            self.assertEqual(timing.currentText(),"匀速动画")
            self.assertEqual(panel.current()["caption_timing"],"uniform")
            panel.set_geometry(panel.current()["id"],dict(x=30,y=40,width=50,height=60))
            self.assertEqual(panel.current()["x"],30)
            panel.remove_image()
            self.assertFalse(any(layer["kind"] == "caption" for layer in panel.values()))
            panel.close()
            panel.deleteLater()

    def test_saved_profiles_keep_caption_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            state = fresh_state()
            state["settings"]["layers"].append(component(caption_seed=132,caption_timing="uniform"))
            store = Store(folder)
            store.save(state,0)
            loaded = store.load()
            self.assertEqual(next(layer for layer in loaded["settings"]["layers"] if layer["kind"] == "caption")["caption_seed"],132)
            self.assertEqual(next(layer for layer in loaded["settings"]["layers"] if layer["kind"] == "caption")["caption_timing"],"uniform")

    def test_missing_voice_has_a_clear_error_before_encoding(self):
        with tempfile.TemporaryDirectory() as folder, patch(PREFIX+"engine.resolve_tools",return_value=("ffmpeg","ffprobe")), \
             patch(PREFIX+"engine.probe",return_value={"duration":2.4,"video":{"width":360,"height":640},"audio":False}), \
             patch(PREFIX+"engine._run") as encode:
            source = Path(folder)/"source.mp4"
            source.touch()
            state = fresh_state()
            state["settings"].update(layers=[component()])
            job = {"path":str(source),"title":"","body":TEXT,"name":"result"}
            with self.assertRaisesRegex(ValueError,"请启用人声"):
                render_static_text(job,state,folder)
            encode.assert_not_called()

    def test_existing_task_srt_is_used_without_any_model_or_file_change(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = Path(folder)/"task_audio.wav"
            subtitle = voice.with_suffix(".srt")
            data = "1\n00:00:00,250 --> 00:00:01,400\nChlieb náš\n\n2\n00:00:01,600 --> 00:00:02,000\nkaždodenný.\n"
            subtitle.write_text(data,"utf8")
            layer = component()
            with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("must not load model")):
                words = task_word_timings({},str(voice),layer,"",TEXT,2.4,folder)
            self.assertEqual([word["start"] for word in words],[.25,.25,1.6])
            events = caption_events(words,layer,360,640,2.4)
            self.assertEqual([event["end"] for event in events],[1.4,1.4,2.0])
            self.assertEqual(subtitle.read_text("utf8"),data)

    def test_word_level_srt_preserves_exact_word_start_and_end(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"words.srt"
            path.write_text("1\n00:00:00,250 --> 00:00:00,650\nChlieb\n\n2\n00:00:00,900 --> 00:00:01,250\nnáš\n","utf8")
            events = caption_events(read_task_subtitle(path,2.4),component(),360,640,2.4)
            self.assertEqual([(item["start"],item["end"]) for item in events],[(.25,.65),(.9,1.25)])

    def test_uniform_srt_divides_each_cue_and_preserves_gaps_without_model(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = Path(folder)/"task_audio.wav"
            subtitle = voice.with_suffix(".srt")
            data = "1\n00:00:00,200 --> 00:00:01,200\nChlieb náš\n\n2\n00:00:01,600 --> 00:00:02,400\nkaždodenný 2026.\n"
            subtitle.write_text(data,"utf8")
            with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("uniform must not load model")):
                words = task_word_timings({},str(voice),component(caption_timing="uniform"),"","",2.4,folder)
            self.assertEqual([word["text"] for word in words],["Chlieb","náš","každodenný","2026."])
            for word,expected in zip(words,[(.2,.7),(.7,1.2),(1.6,2.0),(2.0,2.4)]):
                self.assertAlmostEqual(word["start"],expected[0])
                self.assertAlmostEqual(word["end"],expected[1])
                self.assertNotIn("cue",word)
            self.assertEqual(subtitle.read_text("utf8"),data)

    def test_uniform_without_srt_uses_whole_words_and_full_voice_duration(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("uniform must not load model")):
            words = task_word_timings({},str(Path(folder)/"voice.wav"),component(caption_timing="uniform"),
                "ignored title","Chlieb  náš\n2026 každodenný.",8,folder)
        self.assertEqual(words,[{"text":text,"start":start,"end":start+2} for text,start in
            [("Chlieb",0),("náš",2),("2026",4),("každodenný.",6)]])

    def test_uniform_full_text_includes_title_and_no_asr_fallback(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("uniform must not load model")):
            words = task_word_timings({},"",component(caption_timing="uniform",caption_source="full"),
                "Title","Body",2,folder)
            self.assertEqual([word["text"] for word in words],["Title","Body"])
            with self.assertRaisesRegex(ValueError,"匀速动画需要已有 SRT 或任务文案"):
                task_word_timings({},"",component(caption_timing="uniform",caption_source="asr"),
                    "Title","Body",2,folder)

    def test_uniform_manual_srt_is_respected_and_invalid_path_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"explicit.srt"
            path.write_text("1\n00:00:00,500 --> 00:00:01,500\nManual words\n","utf8")
            layer = component(caption_timing="uniform",caption_file=str(path))
            self.assertEqual(caption_subtitle_path({},"",layer),path)
            with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("uniform must not load model")):
                words = task_word_timings({},"",layer,"","unrelated",2,folder)
            self.assertEqual([word["start"] for word in words],[.5,1.0])
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                task_word_timings({},"",layer,"","Body",2,folder)

    def test_choosing_srt_keeps_the_selected_uniform_mode(self):
        from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
        panel = CaptionProperties()
        panel.set_layer(component(caption_timing="uniform"))
        with patch.object(QtWidgets.QFileDialog,"getOpenFileName",return_value=("words.srt","")):
            panel.choose_subtitle()
        self.assertEqual(panel.layer["caption_timing"],"uniform")
        self.assertEqual(panel.layer["caption_file"],"words.srt")
        panel.close()
        panel.deleteLater()

    def test_playback_cache_tracks_model_only_when_inference_is_used(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from globalValue import globalValue
        state = fresh_state()
        layer = component(caption_timing="model")
        state["settings"]["layers"] = [layer]
        with patch.object(globalValue,"whisper_model_name",return_value="large-v3"):
            first = preview_key({},state)
        with patch.object(globalValue,"whisper_model_name",return_value="medium"):
            self.assertNotEqual(first,preview_key({},state))
        layer["caption_timing"] = "uniform"
        with patch.object(globalValue,"whisper_model_name",return_value="large-v3"):
            first = preview_key({},state)
        with patch.object(globalValue,"whisper_model_name",side_effect=AssertionError("uniform must not use model")):
            self.assertEqual(first,preview_key({},state))

    def test_subtitle_source_interface_and_missing_file_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/"custom.srt"
            path.touch()
            self.assertEqual(caption_subtitle_path({"subtitle_path":str(path)},"",component()),path)
            self.assertIsNone(caption_subtitle_path({"subtitle_path":str(path)},"",component(caption_timing="model")))
            self.assertEqual(caption_subtitle_path({},"",component(caption_timing="file",caption_file=str(path))),path)
            with self.assertRaisesRegex(FileNotFoundError,"task_audio.srt"):
                caption_subtitle_path({"task_dir":folder},"",component(caption_timing="task"))
            with self.assertRaises(FileNotFoundError):
                caption_subtitle_path({},"",component(caption_timing="file"))

    def test_srt_decoder_accepts_utf16_and_formatting_but_rejects_broken_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"words.srt"
            path.write_bytes("1\r\n00:00:00,250 --> 00:00:01,250\r\n<b>Chlieb</b>\r\nnáš\r\n".encode("utf16"))
            self.assertEqual([word["text"] for word in read_task_subtitle(path,2.4)],["Chlieb","náš"])
            path.write_text("1\nBAD TIMESTAMP\nword\n","utf8")
            with self.assertRaisesRegex(ValueError,"第1"):
                read_task_subtitle(path,2.4)

    def test_srt_change_invalidates_playback_cache_but_model_mode_ignores_srt(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root/"task_audio.wav"
            voice.touch()
            subtitle = voice.with_suffix(".srt")
            subtitle.write_text("original","utf8")
            state = fresh_state()
            layer = component()
            state["settings"].update(use_voice=True,layers=[layer])
            job = {"voice_path":str(voice),"voice_override":True}
            first = preview_key(job,state)
            subtitle.write_text("changed subtitles","utf8")
            self.assertNotEqual(first,preview_key(job,state))
            layer["caption_timing"] = "model"
            model_key = preview_key(job,state)
            subtitle.write_text("change again","utf8")
            self.assertEqual(model_key,preview_key(job,state))

    def test_preview_freezes_task_subtitle_before_replacing_task_directory(self):
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task = root/"task"
            task.mkdir()
            subtitle = task/"task_audio.srt"
            subtitle.touch()
            voice = root/"voice.wav"
            voice.touch()
            state = fresh_state()
            layer = component(caption_timing="task")
            state["settings"].update(use_voice=True,layers=[layer])
            job = {"path":str(task/"background.mp4"),"voice_path":str(voice),"voice_override":True,"task_dir":str(task)}
            worker = PlaybackWorker(job,state,root,"a"*64)
            with patch(PREFIX+"playback.resolve_tools",return_value=("ffmpeg","ffprobe")), \
                 patch(PREFIX+"playback.probe",return_value={"duration":2.4}), \
                 patch(PREFIX+"playback.render_static_text",return_value=(root/"result.mp4",None)) as render:
                worker.run()
            self.assertEqual(render.call_args.args[0]["caption_subtitles"][layer["id"]],str(subtitle))


class NativeDynamicCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()
        cls.ffmpeg = Path(r"C:\Users\RTV\pythonPro\辅助小工具\runtime\ffmpeg\ffmpeg.exe")
        cls.ffprobe = cls.ffmpeg.with_name("ffprobe.exe")

    def run_ffmpeg(self,args):
        result = subprocess.run([str(self.ffmpeg),"-hide_banner","-loglevel","error",*args],
            capture_output=True,timeout=60,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,0,result.stderr.decode("utf8","replace"))
        return result.stdout

    def test_real_ffmpeg_export_preview_and_word_visibility(self):
        if not self.ffmpeg.is_file():
            self.skipTest("Bundled FFmpeg not present on this host")
        with tempfile.TemporaryDirectory(prefix="字幕测试 '") as folder:
            root = Path(folder)
            source,voice = root/"background.mp4",root/"task_audio.wav"
            self.run_ffmpeg(["-y","-f","lavfi","-i","color=c=black:s=360x640:r=20:d=2.4",
                             "-c:v","libx264","-pix_fmt","yuv420p",str(source)])
            self.run_ffmpeg(["-y","-f","lavfi","-i","sine=frequency=330:duration=2.4",str(voice)])
            state = fresh_state()
            layer = component()
            state["settings"].update(aspect="source",use_voice=True,music_enabled=False,
                overwrite=True,ffmpeg_path=str(self.ffmpeg),layers=[layer]+[
                    dict(item,enabled=False) for item in state["settings"]["layers"]])
            job = {"id":"one","path":str(source),"voice_path":str(voice),"voice_override":True,
                   "title":"","body":TEXT,"name":"成品","task_dir":str(root)}
            state["jobs"] = [job]
            original = copy.deepcopy(state)
            with patch(PREFIX+"captions.load_word_timings",return_value=WORDS):
                output,_music = render_static_text(job,state,root)
                preview,_music = render_static_text(dict(job,name="预览"),state,root,preview=True)
            self.assertEqual(state,original)
            self.assertAlmostEqual(probe(output,str(self.ffprobe))["duration"],2.4,places=1)
            self.assertTrue(probe(preview,str(self.ffprobe))["audio"])
            images = [QtGui.QImage.fromData(self.run_ffmpeg(["-ss",str(t),"-i",str(output),"-frames:v","1",
                "-f","image2pipe","-vcodec","png","-"])) for t in (.1,.55,1.15)]
            def lit(image,rect=None):
                if rect:
                    image = image.copy(rect)
                return sum(image.pixelColor(x,y).lightness()>100 for y in range(image.height()) for x in range(image.width()))
            self.assertEqual(lit(images[0]),0,"No caption may appear before spoken word")
            self.assertGreater(lit(images[1]),20,"First spoken word must be visible")
            events = caption_events(WORDS,layer,360,640,2.4)
            second = events[1]
            rect = QtCore.QRect(int(second["x"])-2,int(second["y"])-2,
                                int(second["width"])+4,int(second["height"])+4)
            self.assertEqual(lit(images[1],rect),0,"Future second word must remain invisible")
            self.assertGreater(lit(images[2],rect),5,"Second word must appear after its timestamp")
            # Real SRT rendering, not just a parser test: block-level captions
            # appear together at the authoritative cue start, with no inference.
            voice.with_suffix(".srt").write_text("1\n00:00:00,250 --> 00:00:01,400\nChlieb náš\n\n2\n00:00:01,600 --> 00:00:02,000\nkaždodenný.\n","utf8")
            with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("SRT must bypass model")):
                srt_output,_ = render_static_text(dict(job,name="任务字幕成品"),state,root)
            self.assertTrue(probe(srt_output,str(self.ffprobe))["audio"])
            layer["caption_timing"] = "uniform"
            with patch(PREFIX+"captions.load_word_timings",side_effect=AssertionError("uniform must bypass model")):
                uniform_output,_ = render_static_text(dict(job,name="匀速动画成品"),state,root)
            uniform_words = task_word_timings(job,str(voice),layer,"",TEXT,2.4,root)
            self.assertAlmostEqual(uniform_words[1]["start"],.825)
            uniform_images = [QtGui.QImage.fromData(self.run_ffmpeg(["-ss",str(t),"-i",str(uniform_output),
                "-frames:v","1","-f","image2pipe","-vcodec","png","-"])) for t in (.55,1.15)]
            second = caption_events(uniform_words,layer,360,640,2.4)[1]
            rect = QtCore.QRect(int(second["x"])-2,int(second["y"])-2,int(second["width"])+4,int(second["height"])+4)
            self.assertEqual(lit(uniform_images[0],rect),0,"Uniform second word must wait for its divided start")
            self.assertGreater(lit(uniform_images[1],rect),5,"Uniform second word must appear after divided start")
            qa = os.environ.get("LZX_CAPTION_QA_DIR")
            if qa:
                dest = Path(qa)
                dest.mkdir(parents=True,exist_ok=True)
                for index,image in enumerate(images):
                    image.save(str(dest/f"frame-{index}.png"),"PNG")
                shutil.copy2(output,dest/"逐词跃动测试.mp4")
                shutil.copy2(srt_output,dest/"任务字幕测试.mp4")
                shutil.copy2(uniform_output,dest/"匀速动画测试.mp4")


if __name__ == "__main__":
    unittest.main()

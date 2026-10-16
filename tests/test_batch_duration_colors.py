"""Fixed silent-video duration, stable color families and Slovak uppercase."""
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.adaptation import target_duration, normalize_duration
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers, componentize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.text_colors import family_color, resolve_colors, normalize_color, job_color_scope
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_preview_overlay
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.playback import preview_key, PlaybackWorker

FFMPEG = os.environ.get("NODE_FFMPEG", "")
SLOVAK = "á ä č ď é í ĺ ľ ň ó ô ŕ š ť ú ý ž"
UPPER = "Á Ä Č Ď É Í Ĺ Ľ Ň Ó Ô Ŕ Š Ť Ú Ý Ž"


class DurationColorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self, **changes):
        value = new_text_component()
        value.update(id="sample", text="Božia láska", color="#ddb56a", font="Open Sans", font_min=22,
                     font_max=22, width=95, height=75, outline_width=0, color_mode="family")
        value.update(changes)
        return normalize_layers([value])[0]

    def test_fixed_15_and_voice_priority_preserve_old_behavior(self):
        settings = copy.deepcopy(fresh_state()["settings"])
        self.assertEqual(target_duration(3, settings), 3)
        settings.update(unvoiced_duration_mode="fixed", unvoiced_duration_seconds=15)
        for length in (2,8,15,27): self.assertEqual(target_duration(length,settings),15)
        self.assertEqual(target_duration(2,settings,23.5),23.5)
        for change in ({"unvoiced_duration_mode":"bad"}, {"unvoiced_duration_seconds":True},
                       {"unvoiced_duration_seconds":float("nan")},{"unvoiced_duration_seconds":0},
                       {"unvoiced_duration_seconds":86401}):
            with self.assertRaises(ValueError): normalize_duration({**settings,**change})

    def test_color_ranges_are_bounded_and_deterministic(self):
        layer = self.layer(); before = copy.deepcopy(layer)
        h,s,v,a = QtGui.QColor(layer["color"]).getHsvF()
        values = set()
        for index in range(100):
            color = family_color(layer,"job-"+str(index)); values.add(color)
            self.assertEqual(color,family_color(layer,"job-"+str(index)))
            oh,osat,oval,oa = QtGui.QColor(color).getHsvF()
            delta = min(abs(oh-h), 1-abs(oh-h))*360
            self.assertLessEqual(delta,layer["color_hue_variation"]+.4)
            self.assertLessEqual(abs(osat-s)*100,layer["color_saturation_variation"]+.7)
            self.assertLessEqual(abs(oval-v)*100,layer["color_value_variation"]+.4)
            self.assertAlmostEqual(a,oa,delta=.01)
        self.assertGreater(len(values),80)
        self.assertEqual(layer,before)
        resolved = resolve_colors([layer],"same")
        self.assertEqual(resolved,resolve_colors(resolved,"different"))
        self.assertEqual(layer["color_mode"],"family")

    def test_gray_preserves_family_and_zero_variation_is_base(self):
        layer = self.layer(color="#ffffff")
        for scope in ("a","b","c"):
            color = QtGui.QColor(family_color(layer,scope))
            self.assertEqual(color.red(),color.green()); self.assertEqual(color.green(),color.blue())
        for color in ("#ddb56a","#f54e4e","#80332211"):
            layer = self.layer(color=color,color_hue_variation=0,color_saturation_variation=0,color_value_variation=0)
            self.assertEqual(QtGui.QColor(family_color(layer,"x")),QtGui.QColor(color))

    def test_invalid_color_parameters_fail_before_painting(self):
        for change in ({"color_mode":"all"},{"color_hue_variation":31},{"color_value_variation":float("inf")},
                       {"color_seed":1.5},{"color_seed":True}):
            with self.subTest(change=change),self.assertRaises(ValueError): self.layer(**change)

    def test_scope_survives_pairing_quick_export_and_preview_cache_renaming(self):
        from app_plugins.builtin.batch_text_video.pools import apply_pairs
        from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"background.mp4"; path.touch()
            source = {"id":"copy", "title":"Title", "body":"Body", "task_dir":directory, "name":"copy"}
            state = fresh_state(); state["copy_pool"] = [source]
            preview = {**source,"path":str(path)}
            scope = job_color_scope(preview)
            apply_pairs(state,[{"path":str(path),"copy":source}])
            self.assertEqual(scope,job_color_scope(state["jobs"][0]))
            quick, key = prepare_current_export(state,preview,copy_id="copy")
            job = next(item for item in quick["jobs"] if item["id"] == key)
            self.assertEqual(scope,job_color_scope(job))
            frozen = {**job,"_color_scope":scope,"name":"cache","task_dir":"temporary"}
            self.assertEqual(job_color_scope(frozen),scope)

    def test_color_and_duration_persist_independently_per_profile(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); state = fresh_state()
            state["settings"].update(unvoiced_duration_mode="fixed", unvoiced_duration_seconds=15,
                layers=normalize_layers([self.layer()]))
            saved = store.save(state,0)
            other = store.create_profile(saved,"another")
            self.assertEqual(other["settings"]["unvoiced_duration_mode"],"source")
            restored = store.select_profile(other,"default")
            self.assertEqual(restored["settings"]["unvoiced_duration_seconds"],15)
            self.assertEqual(restored["settings"]["layers"][0]["color_mode"],"family")

    def test_preview_color_matches_export_plane_and_cache_changes(self):
        from app_plugins.builtin.batch_text_video.captions import static_planes
        from app_plugins.builtin.batch_text_video.layers import ImageCache
        state = fresh_state(); layer = self.layer()
        state["settings"].update(darkness=0,layers=[layer]+[dict(item,enabled=False) for item in DEFAULT_LAYERS])
        job = {"path":"", "title":"", "body":"", "name":"a"}
        settings = {**state["settings"],"_color_scope":job_color_scope(job)}
        a,_ = render_preview_overlay("","",320,180,settings)
        b = static_planes("","",320,180,settings,ImageCache())[0][1]
        self.assertEqual(a,b)
        key = preview_key(job,state)
        state["settings"]["layers"][0]["color_seed"] += 1
        self.assertNotEqual(key,preview_key(job,state))
        state["settings"].update(unvoiced_duration_mode="fixed")
        self.assertNotEqual(key,preview_key(job,state))

    def test_ui_exposes_time_and_color_controls_and_existing_uppercase(self):
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            dialog = BatchTextVideoDialog(store=Store(root)); self.addCleanup(dialog.deleteLater)
            dialog.controls["unvoiced_duration_mode"].setCurrentIndex(
                dialog.controls["unvoiced_duration_mode"].findData("fixed"))
            self.assertTrue(dialog.controls["unvoiced_duration_seconds"].isEnabled())
            dialog.controls["use_voice"].setChecked(True)
            self.assertFalse(dialog.controls["unvoiced_duration_seconds"].isEnabled())
            dialog.controls["use_voice"].setChecked(False)
            dialog.layer_panel.add_text()
            controls = dialog.layer_panel.text_properties.controls
            controls["color_mode"].setCurrentIndex(controls["color_mode"].findData("family"))
            self.assertEqual(dialog.layer_panel.current()["color_mode"],"family")
            dialog.layer_panel.text_properties.new_color_group.click()
            self.assertEqual(dialog.layer_panel.current()["color_seed"],1)
            controls["capitalization"].setCurrentIndex(controls["capitalization"].findData("uppercase"))
            self.assertEqual(dialog.layer_panel.current()["capitalization"],"uppercase")
            dialog.edit_timer.stop(); dialog.close(); self.app.processEvents()

    def test_slovak_uppercase_matches_explicit_upper_text_and_keeps_original(self):
        self.assertEqual(SLOVAK.upper(),UPPER)
        for writing in ("horizontal","vertical_rl"):
            layer = self.layer(text=SLOVAK, color_mode="fixed", font_min=18,font_max=18,
                               capitalization="uppercase",writing_mode=writing,height=95)
            before = copy.deepcopy(layer)
            actual,_ = render_text_box(layer,640,360)
            expected,_ = render_text_box({**layer,"text":UPPER,"capitalization":"normal"},640,360)
            self.assertEqual(actual,expected)
            self.assertEqual(layer,before)

    def test_node_random_colors_reuse_helper_and_follow_current_iteration(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import layer_node,build_layer
        from model.node_workflow.global_data import WorkflowGlobals
        from test_node_batch_layers import context
        params = {item.key:copy.deepcopy(item.default) for item in layer_node("text").parameters}
        params["layer"] = self.layer()
        before = copy.deepcopy(params)
        c = context(); c.task = None
        colors = []
        for iteration in (1,2,1):
            c.workflow_globals = WorkflowGlobals.create("line",3).at_iteration(iteration)
            operation = build_layer({},params,c)["operations"][0]
            layer = json.loads(dict(operation.options)["layer"])
            colors.append(layer["color"])
            self.assertEqual(layer["color_mode"],"fixed")
            self.assertEqual(layer["color"],family_color(dict(params["layer"]),f"node:::"+str(iteration)))
        self.assertEqual(colors[0],colors[2]); self.assertNotEqual(colors[0],colors[1])
        self.assertEqual(params,before)

    def test_native_capture_when_requested(self):
        directory = os.environ.get("BATCH_COLOR_QA")
        if not directory: return
        from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog,"request_background"):
            panel = TextBoxProperties(); panel.resize(410,650); panel.set_layer(self.layer())
            panel.style_sections.reveal("font"); panel.show(); self.app.processEvents()
            directory = Path(directory); directory.mkdir(parents=True,exist_ok=True)
            self.assertTrue(panel.grab().save(str(directory/"同色系随机.png")))
            panel.close(); panel.deleteLater()
            dialog = BatchTextVideoDialog(store=Store(root)); dialog.resize(1180,760)
            dialog.controls["unvoiced_duration_mode"].setCurrentIndex(dialog.controls["unvoiced_duration_mode"].findData("fixed"))
            dialog.right_tabs.setCurrentIndex(1); dialog.project_sections.reveal("duration"); dialog.show(); self.app.processEvents()
            self.assertTrue(dialog.project_sections.grab().save(str(directory/"固定时长.png")))
            dialog.close(); dialog.deleteLater(); self.app.processEvents()


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),"需要实际FFmpeg")
class DurationColorsRenderTests(DurationColorsTests):
    def ffmpeg(self,args):
        run = subprocess.run([FFMPEG,"-nostdin","-v","error",*args],capture_output=True,timeout=60,
                             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(run.returncode,0,run.stderr.decode("utf8","replace"))
        return run.stdout

    def make_source(self,root,seconds,audio=False):
        path = Path(root)/f"source-{seconds}-{audio}.mp4"
        args = ["-y","-f","lavfi","-i",f"color=c=0x304050:s=160x90:r=10:d={seconds}"]
        if audio: args += ["-f","lavfi","-i",f"sine=frequency=330:duration={seconds}","-c:a","aac"]
        self.ffmpeg([*args,"-c:v","libx264","-pix_fmt","yuv420p",str(path)])
        return path

    def render(self,root,path,seconds=15,**settings):
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe
        state = fresh_state()
        state["settings"].update(ffmpeg_path=FFMPEG,aspect="source",music_enabled=False,darkness=0,
            unvoiced_duration_mode="fixed",unvoiced_duration_seconds=seconds,loop_transition="none")
        state["settings"].update(settings)
        state["settings"]["layers"] = normalize_layers([self.layer(font_min=16,font_max=16,height=95)]+[
            dict(item,enabled=False) for item in DEFAULT_LAYERS])
        state["settings"]["layers"] = componentize_layers(state["settings"]["layers"],state["settings"])
        job = {"id":"j","path":str(path),"title":"","body":"","task_dir":str(root),"name":"output"}
        state["jobs"] = [job]
        output,_ = render_static_text(job,state,root)
        info = probe(output,str(Path(FFMPEG).with_name("ffprobe.exe")))
        self.assertAlmostEqual(info["duration"],seconds,delta=.12)
        return state,job,output,info

    def test_actual_short_and_long_sources_both_export_15_seconds(self):
        for source_length in (2,20):
            with tempfile.TemporaryDirectory() as root:
                source = self.make_source(root,source_length)
                before = source.read_bytes()
                self.render(root,source)
                self.assertEqual(source.read_bytes(),before)

    def test_fixed_duration_reuses_fade_and_slow_modes_with_background_sound(self):
        for strategy,transition in (("loop","fade"),("slow","none"),("hybrid","none")):
            with tempfile.TemporaryDirectory() as root:
                source = self.make_source(root,1,audio=True)
                _,_,_,info = self.render(root,source,seconds=3,duration_strategy=strategy,
                    loop_transition=transition,keep_audio=True)
                self.assertTrue(info["audio"])

    def test_playback_freezes_same_random_color_as_final_export(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root,1)
            state,job,output,_ = self.render(root,source,seconds=2)
            results = []
            worker = PlaybackWorker(job,state,root,preview_key(job,state))
            worker.ready.connect(results.append)
            worker.run()
            self.assertNotIn("error",results[0],results[0])
            self.assertEqual(worker.job["_color_scope"],job_color_scope(job))
            def pixel_image(path):
                return QtGui.QImage.fromData(self.ffmpeg(["-ss","0.5","-i",str(path),"-frames:v","1",
                    "-f","image2pipe","-vcodec","png","-"]))
            a,b = pixel_image(output),pixel_image(results[0]["path"])
            self.assertEqual(a.size(),b.size())
            # Different CRF in preview; verify the stable colored text region.
            differences = []
            for x in range(a.width()):
                for y in range(a.height()):
                    ca,cb = a.pixelColor(x,y),b.pixelColor(x,y)
                    differences.append(max(abs(ca.red()-cb.red()),abs(ca.green()-cb.green()),abs(ca.blue()-cb.blue())))
            self.assertLess(sum(differences)/len(differences),3)

    def test_voice_duration_is_not_cut_to_fixed_target(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe
        with tempfile.TemporaryDirectory() as root:
            source = self.make_source(root,1)
            voice = Path(root)/"voice.wav"
            self.ffmpeg(["-y","-f","lavfi","-i","sine=frequency=220:duration=2.5",str(voice)])
            state = fresh_state()
            state["settings"].update(ffmpeg_path=FFMPEG,aspect="source",music_enabled=False,
                use_voice=True,unvoiced_duration_mode="fixed",unvoiced_duration_seconds=1,loop_transition="none")
            state["settings"]["layers"] = [self.layer(font_min=16,font_max=16)]+[dict(item,enabled=False) for item in DEFAULT_LAYERS]
            state["settings"]["layers"] = componentize_layers(state["settings"]["layers"],state["settings"])
            job = {"id":"j","path":str(source),"voice_path":str(voice),"voice_override":True,
                   "title":"A","body":"B","name":"voiced","task_dir":root}
            state["jobs"] = [job]
            output,_ = render_static_text(job,state,root)
            info = probe(output,str(Path(FFMPEG).with_name("ffprobe.exe")))
            self.assertAlmostEqual(info["duration"],2.5,delta=.12)


if __name__ == "__main__": unittest.main()

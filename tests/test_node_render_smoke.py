"""Opt-in real FFmpeg smoke: synthetic media only, no user videos or models."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.graph import NodeRegistry, register_core, example_graph, snapshot
from model.node_workflow.render import run_workflow, probe
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node

FFMPEG = os.environ.get("NODE_FFMPEG", "")


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),"设置 NODE_FFMPEG 可运行真实合成媒体测试")
class NodeRenderSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/"原视频 '中' 文.mp4"
        self.subtitle = self.root/"字幕,含特殊符号.srt"
        self.subtitle.write_text("1\n00:00:00,100 --> 00:00:01,500\nAhoj svet. 测试字幕\n",encoding="utf-8")
        self.execute([FFMPEG,"-y","-v","error","-f","lavfi","-i","color=c=gray:s=360x640:r=25:d=2",
                      "-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=2",
                      "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(self.source)])
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.registry.register("batch_text_video",caption_node())
        self.graph = example_graph(self.registry)
        self.graph["nodes"][0]["params"].update(video=str(self.source),subtitles=str(self.subtitle))
        self.graph["nodes"][-1]["params"]["output"] = str(self.root/"成品.mp4")
        self.cancel = threading.Event()
        self.progress = []
        self.context = TaskRunContext(self.cancel,lambda message,percent:self.progress.append(percent),lambda *_:None)

    def tearDown(self): self.temp.cleanup()

    def execute(self,args):
        result = subprocess.run(args,capture_output=True,timeout=30,
                                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,0,result.stderr.decode("utf-8","replace"))
        return result.stdout

    def test_real_single_pass_render_preserves_audio_duration_and_geometry(self):
        result = run_workflow({"graph":snapshot(self.graph,self.registry),"ffmpeg":FFMPEG},self.context,registry=self.registry)
        output = Path(result["video"])
        info = probe(output,str(Path(FFMPEG).with_name("ffprobe.exe" if os.name=="nt" else "ffprobe")))
        self.assertEqual((info["video"]["width"],info["video"]["height"]),(360,640))
        self.assertAlmostEqual(info["duration"],2,places=2)
        self.assertTrue(info["audio"])
        self.assertEqual(result["video_encodings"],1)
        self.assertEqual(result["intermediate_videos"],0)
        self.assertEqual(self.progress[-1],100)
        self.assertFalse(any(p.name.startswith(".node-render-") for p in self.root.iterdir()))
        def audio_hash(path):
            return self.execute([FFMPEG,"-v","error","-i",str(path),"-map","0:a:0","-c:a","copy","-f","hash","-"])
        self.assertEqual(audio_hash(self.source),audio_hash(output))
        pixels = self.execute([FFMPEG,"-v","error","-ss","1","-i",str(output),"-frames:v","1",
                               "-vf","crop=360:160:0:480","-f","rawvideo","-pix_fmt","rgb24","-"])
        self.assertGreater(max(pixels)-min(pixels),80,"字幕应实际出现在成品中")
        print("REAL_NODE_RENDER:",result)

    def test_real_cancellation_keeps_sources_and_no_partial_final(self):
        self.cancel.set()
        with self.assertRaises(BackgroundTaskCanceled):
            run_workflow({"graph":snapshot(self.graph,self.registry),"ffmpeg":FFMPEG},self.context,registry=self.registry)
        self.assertFalse((self.root/"成品.mp4").exists())
        self.assertTrue(self.source.exists())

    def test_multiple_caption_operations_preview_and_export_from_one_settler(self):
        from model.node_workflow.graph import new_node, empty_graph, build_plan
        from model.node_workflow.preview import frame_preview
        imported = self.graph["nodes"][0]
        captions = [new_node(caption_node()) for _ in range(2)]
        captions[1]["params"]["position"] = "top"
        settling = new_node(self.registry.definitions["core.settle"])
        exporting = new_node(self.registry.definitions["core.export"])
        exporting["params"].update(destination="file", output=str(self.root / "multi.mp4"))
        graph = empty_graph()
        graph.update(nodes=[imported, *captions, settling, exporting], output_node=exporting["id"])
        graph["connections"] = [[imported["id"], "video", settling["id"], "video"],
                                [settling["id"], "plan", exporting["id"], "plan"]]
        for caption in captions:
            graph["connections"] += [[imported["id"], "subtitles", caption["id"], "subtitles"],
                                     [caption["id"], "operations", settling["id"], "operations"]]
        self.context.ffmpeg = FFMPEG
        frozen = snapshot(graph, self.registry)
        plan = build_plan(frozen, self.registry, self.context)
        preview = self.root / "preview"
        preview.mkdir()
        encoded, at = frame_preview(plan, .8, self.context, preview)
        self.assertTrue(encoded)
        self.assertAlmostEqual(at, .8)
        self.assertFalse(Path(plan.output).exists())
        result = run_workflow({"graph": frozen, "ffmpeg": FFMPEG}, self.context, registry=self.registry)
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        self.assertAlmostEqual(result["duration"], 2, places=2)
        for y in (0, 480):
            pixels = self.execute([FFMPEG, "-v", "error", "-ss", "0.8", "-i", result["video"],
                "-frames:v", "1", "-vf", f"crop=360:160:0:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            self.assertGreater(max(pixels) - min(pixels), 80, "上下两个字幕操作应同时生效")

    def test_cancellation_during_ffmpeg_removes_temporary_result(self):
        original = subprocess.Popen
        timers = []
        def start(*args,**kwargs):
            process = original(*args,**kwargs)
            if "-progress" in args[0]:
                timer = threading.Timer(.02,self.cancel.set)
                timer.start()
                timers.append(timer)
            return process
        try:
            with mock.patch("model.node_workflow.render.subprocess.Popen",side_effect=start):
                with self.assertRaises(BackgroundTaskCanceled):
                    run_workflow({"graph":snapshot(self.graph,self.registry),"ffmpeg":FFMPEG},self.context,registry=self.registry)
        finally:
            for timer in timers: timer.join(timeout=1)
        self.assertFalse((self.root/"成品.mp4").exists())
        self.assertFalse(any(p.name.startswith(".node-render-") for p in self.root.iterdir()))
        self.assertTrue(self.source.exists())

    def test_begin_end_reuses_caption_plugin_and_only_encodes_final_video(self):
        from model.node_workflow.graph import new_node, empty_graph
        from model.node_workflow.loops import add_loop_pair
        imported = self.graph["nodes"][0]
        graph = empty_graph()
        graph["nodes"].append(imported)
        begin, end = add_loop_pair(graph, self.registry)
        end["params"]["count"] = 2
        caption = new_node(caption_node())
        merge = new_node(self.registry.definitions["core.loop_operations"])
        settle = new_node(self.registry.definitions["core.settle"])
        export = new_node(self.registry.definitions["core.export"])
        export["params"].update(destination="file", output=str(self.root / "loop-caption.mp4"))
        graph["nodes"].extend((caption, merge, settle, export))
        graph["connections"] += [[imported["id"], "subtitles", begin["id"], "initial"],
            [begin["id"], "value", caption["id"], "subtitles"],
            [caption["id"], "operations", end["id"], "result"],
            [end["id"], "results", merge["id"], "results"],
            [merge["id"], "operations", settle["id"], "operations"],
            [imported["id"], "video", settle["id"], "video"],
            [settle["id"], "plan", export["id"], "plan"]]
        graph["output_node"] = export["id"]
        from dataclasses import replace
        definition = self.registry.definitions["batch_text_video.caption"]
        calls = []
        def counted(inputs, params, context):
            calls.append(1)
            return definition.build(inputs, params, context)
        self.registry.definitions[definition.kind] = replace(definition, build=counted)
        result = run_workflow({"graph": snapshot(graph, self.registry), "ffmpeg": FFMPEG}, self.context, registry=self.registry)
        self.assertEqual(len(calls), 2)
        self.assertTrue(Path(result["video"]).is_file())
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        self.assertAlmostEqual(result["duration"], 2, places=2)


if __name__ == "__main__": unittest.main()

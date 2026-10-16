"""Real synthetic video, real analysis/cache/alignment, stub ASR (no downloads)."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import (ResourceRef, VideoTimeline, VideoSection, SubtitleTrack, SubtitleCue,
                                           FilterOperation, RenderPlan)
from model.node_workflow.graph import NodeRegistry, register_core, task_example_graph
from model.node_workflow.tasks import capture_task, prepare_job
from model.node_workflow.render import render_plan, run_task_workflow, run_workflow, probe
from app_plugins.builtin.smart_video_editor.workflow_nodes import edit_node
from app_plugins.builtin.smart_video_editor import engine
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node

FFMPEG = os.environ.get("NODE_FFMPEG", "")


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), "设置 NODE_FFMPEG 运行真实媒体测试")
class TaskWorkflowSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.red, self.blue = self.root / "01.mp4", self.root / "02.mp4"
        self.create(self.red, "red", True)
        self.create(self.blue, "blue", False)
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel, lambda *_: None, lambda *_: None)
        self.context.ffmpeg = FFMPEG
        self.context.notes = []
        self.ffprobe = str(Path(FFMPEG).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, args):
        result = subprocess.run(args, capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def create(self, path, color, audio):
        args = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=240x320:r=25:d=2"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2"]
        args += ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-shortest"]
        self.execute(args + [str(path)])

    def test_deferred_multi_cut_concat_caption_preserves_order_audio_and_duration(self):
        red, blue = ResourceRef.capture(self.red, "video"), ResourceRef.capture(self.blue, "video")
        video = VideoTimeline((VideoSection(red, .2, 1.2), VideoSection(blue, .4, 1.4), VideoSection(red, 1.2, 1.8)),
                              240, 320, "25", "test")
        captions = SubtitleTrack((SubtitleCue(.1, 2.5, "Ahoj svet."),), "test", video.duration)
        operation = FilterOperation("字幕", video, "subtitles", (("force_style", "Fontsize=24,Alignment=2"),),
                                    (("filename", captions),))
        result = render_plan(RenderPlan(video, (operation,), str(self.root / "final.mp4")), self.context, FFMPEG)
        info = probe(result["video"], self.ffprobe)
        self.assertAlmostEqual(info["duration"], 2.6, places=2)
        self.assertTrue(info["audio"])
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (240, 320))
        self.assertEqual(result["video_encodings"], 1)
        self.assertEqual(result["intermediate_videos"], 0)
        def pixel(second):
            return self.execute([FFMPEG, "-v", "error", "-ss", str(second), "-i", result["video"],
                "-frames:v", "1", "-vf", "crop=2:2:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        first, second, third = pixel(.5), pixel(1.5), pixel(2.3)
        self.assertGreater(first[0], first[2] + 100)
        self.assertGreater(second[2], second[0] + 100)
        self.assertGreater(third[0], third[2] + 100)
        self.assertFalse(any(path.name.startswith(".node-render-") for path in self.root.iterdir()))

    def task_setup(self):
        text = "Dnes je krásny slnečný deň.\nBoh nás vedie a chráni."
        task = capture_task({"task": {"task_id": "239", "task_type": "口播", "task_audio_text": text,
                                      "raw": {"扩展列": "retained"}}, "target_dir": self.root, "label": "239"}, language="sk")
        settings = engine.normalize_smart_video_editor_settings({"whisper_model_size": "large-v3",
            "voice_detection_enabled": False, "silence_detection_enabled": False, "ffmpeg_path": FFMPEG})
        services = SimpleNamespace(_effective_settings=lambda: settings)
        registry = NodeRegistry()
        register_core(registry)
        registry.register("smart_video_editor", edit_node(services))
        registry.register("batch_text_video", caption_node())
        graph = task_example_graph(registry)
        class Model:
            def transcribe(self, path, **kwargs):
                words = ("Dnes je krásny slnečný deň." if Path(path).name == "01.mp4" else "Boh nás vedie a chráni.").split()
                items = [SimpleNamespace(word=word, start=.2 + index * .23, end=.4 + index * .23, probability=.99)
                         for index, word in enumerate(words)]
                return iter([SimpleNamespace(text=" ".join(words), words=items)]), SimpleNamespace(duration=2, language="sk")
        return task, registry, graph, Model()

    def test_full_task_pipeline_uses_real_analysis_and_cache_no_manual_review(self):
        task, registry, graph, model = self.task_setup()
        payload, _ = prepare_job(graph, registry, task, None)
        payload["ffmpeg"] = FFMPEG
        with mock.patch("globalValue.globalValue.get_whisper_model", return_value=model) as load, \
             mock.patch("qt_compat.QtWidgets.QMessageBox.exec", side_effect=AssertionError("不能弹窗")):
            result = run_task_workflow(payload, self.context, registry=registry)
        self.assertTrue(Path(result["video"]).is_file())
        self.assertTrue(Path(result["subtitles"]).is_file())
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["source_count"], 2)
        self.assertEqual(report["summary"]["model"], "large-v3")
        self.assertEqual(report["task"]["key"], task.key)
        self.assertIn("Dnes", Path(result["subtitles"]).read_text(encoding="utf-8"))
        self.assertEqual(result["video_encodings"], 1)
        self.assertIn(result["status"], {"completed", "completed_with_notes"})
        self.assertTrue(load.called)
        # Style-only change reuses the complete smart-editor analysis cache.
        graph["nodes"][3]["params"]["size"] = 32
        second, _ = prepare_job(graph, registry, task, None)
        second["ffmpeg"] = FFMPEG
        with mock.patch("globalValue.globalValue.get_whisper_model", side_effect=AssertionError("缓存命中不应加载模型")):
            repeated = run_task_workflow(second, self.context, registry=registry)
        self.assertNotEqual(repeated["video"], result["video"])
        self.assertEqual(json.loads(Path(repeated["report"]).read_text(encoding="utf-8"))["summary"]["analysis_from_cache"], True)
        self.assertEqual(len(list((self.root / "节点工作流结果").glob("*.mp4"))), 2)

    def test_changed_file_after_listdir_fails_without_output(self):
        task, registry, graph, _ = self.task_setup()
        payload, _ = prepare_job(graph, registry, task, None)
        payload["ffmpeg"] = FFMPEG
        from model.node_workflow.filesystem import build_listdir
        from dataclasses import replace
        def list_then_change(*args):
            result = build_listdir(*args)
            with self.red.open("ab") as stream:
                stream.write(b"changed")
            return result
        registry.definitions["core.listdir"] = replace(registry.definitions["core.listdir"], build=list_then_change)
        from model.BackgroundTasks import BackgroundTaskResultError
        with self.assertRaises(BackgroundTaskResultError) as caught:
            run_task_workflow(payload, self.context, registry=registry)
        self.assertIn("发生变化", caught.exception.result["error"])
        self.assertEqual(caught.exception.result["failed_node"]["title"], "智能剪辑 · 自动处理")
        self.assertFalse((self.root / "节点工作流结果").exists())

    def test_rotated_fractional_fps_source_keeps_display_shape_and_mapped_duration(self):
        fixture = self.root / ".node-fixture"
        fixture.mkdir()
        converted = fixture / "fractional.mp4"
        self.execute([FFMPEG, "-y", "-v", "error", "-i", str(self.red), "-r", "30000/1001",
                      "-c:v", "libx264", "-threads", "2", "-c:a", "copy", str(converted)])
        self.execute([FFMPEG, "-y", "-v", "error", "-display_rotation", "90", "-i", str(converted),
                      "-c", "copy", str(self.red)])
        source = probe(self.red, self.ffprobe)["video"]
        self.assertEqual((source["display_width"], source["display_height"]), (320, 240))
        task, registry, graph, model = self.task_setup()
        payload, _ = prepare_job(graph, registry, task, None)
        payload["ffmpeg"] = FFMPEG
        with mock.patch("globalValue.globalValue.get_whisper_model", return_value=model):
            result = run_task_workflow(payload, self.context, registry=registry)
        info = probe(result["video"], self.ffprobe)
        self.assertEqual((info["video"]["width"], info["video"]["height"]), (320, 240))
        self.assertEqual(info["video"]["avg_frame_rate"], "30000/1001")
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertAlmostEqual(info["duration"], report["summary"]["duration"], delta=.04)

    def test_standalone_list_and_text_can_edit_without_any_task_snapshot(self):
        from model.node_workflow.graph import new_node
        _task, registry, graph, model = self.task_setup()
        directory = new_node(registry.definitions["core.directory"])
        directory["params"]["path"] = str(self.root)
        text = new_node(registry.definitions["core.text"])
        text["params"]["text"] = "Dnes je krásny slnečný deň.\nBoh nás vedie a chráni."
        listing, edit, caption = graph["nodes"][1:4]
        output = new_node(registry.definitions["core.render"])
        output["params"]["output"] = str(self.root / "standalone.mp4")
        graph["nodes"] = [directory, text, listing, edit, caption, output]
        graph["execution_mode"] = "detail"
        graph["output_node"] = output["id"]
        graph["connections"] = [[directory["id"], "directory", listing["id"], "directory"],
            [listing["id"], "files", edit["id"], "files"], [text["id"], "text", edit["id"], "script"],
            [edit["id"], "subtitles", caption["id"], "subtitles"],
            [edit["id"], "video", output["id"], "video"], [caption["id"], "operations", output["id"], "operations"]]
        edit["params"]["language"] = "sk"
        payload, _ = prepare_job(graph, registry, None, None)
        payload["ffmpeg"] = FFMPEG
        with mock.patch("globalValue.globalValue.get_whisper_model", return_value=model), \
             mock.patch.object(engine, "discover_task_videos", side_effect=AssertionError("不能搜隐式任务文件")):
            result = run_workflow(payload, self.context, registry=registry)
        self.assertIsNone(self.context.task)
        self.assertTrue(Path(result["video"]).is_file())
        self.assertEqual(self.context.processing_summary["input_files"], [str(self.red), str(self.blue)])

    def test_cancel_during_timeline_render_has_no_partial_output(self):
        video = VideoTimeline((VideoSection(ResourceRef.capture(self.red, "video"), 0, 2),), 240, 320, "25", "test")
        original = subprocess.Popen
        timers = []
        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            if "-progress" in args[0]:
                timer = threading.Timer(.02, self.cancel.set)
                timer.start()
                timers.append(timer)
            return process
        try:
            with mock.patch("model.node_workflow.render.subprocess.Popen", side_effect=start):
                with self.assertRaises(BackgroundTaskCanceled):
                    render_plan(RenderPlan(video, (), str(self.root / "final.mp4")), self.context, FFMPEG)
        finally:
            for timer in timers:
                timer.join(timeout=1)
        self.assertFalse((self.root / "final.mp4").exists())
        self.assertFalse(any(path.name.startswith(".node-render-") for path in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()

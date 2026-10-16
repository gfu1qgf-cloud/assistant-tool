"""Real compressed-memory/pipe tests; all input media is synthetic and disposable."""
import copy
import hashlib
import io
import json
from pathlib import Path
import threading
import time
from unittest import mock

import test_node_render_smoke as baseline
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import ResourceRef
from model.node_workflow.graph import new_node, snapshot
from model.node_workflow.media import MediaSession, PROCESS_BUDGET, MemoryBudget, PipeReader
from model.node_workflow.render import run_workflow, probe
from model.node_workflow.render import render_plan
from model.node_workflow.contracts import RenderPlan
from app_plugins.builtin.batch_text_video.workflow_nodes import build_caption


class NodeMediaSmokeTests(baseline.NodeRenderSmokeTests):
    def transport_graph(self, modes):
        graph = copy.deepcopy(self.graph)
        original = graph["nodes"][0]["id"]
        previous = original
        for mode in modes:
            node = new_node(self.registry.definitions["core.transport"])
            node["params"]["mode"] = mode
            graph["nodes"].append(node)
            graph["connections"].append([previous, "video", node["id"], "video"])
            previous = node["id"]
        for edge in graph["connections"]:
            if edge[0] == original and edge[1] == "video" and edge[2] in {
                graph["nodes"][1]["id"], graph["nodes"][3]["id"]
            }:
                edge[0] = previous
        graph["nodes"][3]["params"]["output"] = str(self.root / ("-".join(modes) + ".mp4"))
        return graph

    def setUp(self):
        super().setUp()
        self.graph = self.transport_graph(["memory"])

    def test_cancellation_during_ffmpeg_removes_temporary_result(self):
        super().test_cancellation_during_ffmpeg_removes_temporary_result()
        self.assertFalse(Path(self.graph["nodes"][3]["params"]["output"]).exists())
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_file_memory_pipe_have_same_audio_and_video_timing(self):
        source = ResourceRef.capture(self.source, "video")
        records = []
        for mode in ("file", "memory", "stream"):
            graph = copy.deepcopy(self.graph)
            if mode == "file":
                transport = graph["nodes"][-1]["id"]
                for edge in graph["connections"]:
                    if edge[0] == transport:
                        edge[0] = graph["nodes"][0]["id"]
            else:
                graph["nodes"][-1]["params"]["mode"] = mode
            graph["nodes"][3]["params"]["output"] = str(self.root / (mode + "-compare.mp4"))
            started = time.perf_counter()
            result = run_workflow({"graph": snapshot(graph, self.registry), "ffmpeg": baseline.FFMPEG},
                                  self.context, registry=self.registry)
            elapsed = time.perf_counter() - started
            output = Path(result["video"])
            ffprobe = str(Path(baseline.FFMPEG).with_name("ffprobe.exe"))
            original, final = probe(self.source, ffprobe), probe(output, ffprobe)
            self.assertAlmostEqual(original["duration"], final["duration"], places=3)
            self.assertEqual(original["video"]["nb_frames"], final["video"]["nb_frames"])
            def pcm_hash(path):
                return hashlib.sha256(self.execute([baseline.FFMPEG, "-v", "error", "-i", str(path),
                    "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"])).hexdigest()
            self.assertEqual(pcm_hash(self.source), pcm_hash(output))
            self.assertEqual(result["video_encodings"], 1)
            self.assertEqual(result["intermediate_videos"], 0)
            self.assertEqual(result["input_storage"], mode)
            self.assertEqual(PROCESS_BUDGET.used, 0)
            source.verify()
            records.append(dict(result, total_seconds=round(elapsed, 3)))
        self.assertGreater(records[1]["compressed_memory_peak"], 0)
        self.assertEqual(records[2]["compressed_memory_peak"], 0)
        print("TRANSPORT_BENCHMARK:", json.dumps(records, ensure_ascii=False))

    def test_memory_reader_supports_real_random_access_decode(self):
        import av
        with MediaSession(self.context, baseline.FFMPEG) as session:
            ref = session.convert(ResourceRef.capture(self.source, "video"), "memory")
            first, second = session.open_reader(ref), session.open_reader(ref)
            self.assertIs(first.blocks, second.blocks)
            with av.open(first, mode="r") as video:
                self.assertEqual(sum(1 for _ in video.decode(video=0)), 50)
                video.seek(1_000_000)
                frame = next(video.decode(video=0))
                self.assertEqual((frame.width, frame.height), (360, 640))
            self.assertEqual(second.tell(), 0)
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_generated_mp4_stream_has_no_intermediate_video_file(self):
        output = self.root / "generated-final.mp4"
        metadata = {"duration": 2, "video": {"width": 360, "height": 640}, "audio": True}
        command = [baseline.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=360x640:r=25:d=2",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags",
                   "frag_keyframe+delay_moov+default_base_moof", "-f", "mp4", "pipe:1"]
        with MediaSession(self.context, baseline.FFMPEG) as session:
            self.context.media = session
            stream = session.register_stream(lambda context: PipeReader(command, context), metadata)
            memory = session.convert(stream, "memory")
            self.assertEqual(memory.path, "")
            self.context.ffmpeg = baseline.FFMPEG
            operations = build_caption({"subtitles": ResourceRef.capture(self.subtitle, "subtitles")},
                                       {}, self.context)["operations"]
            before = set(self.root.iterdir())
            result = render_plan(RenderPlan(memory, operations, str(output)), self.context, baseline.FFMPEG)
            self.assertEqual(set(self.root.iterdir()) - before, {output})
            self.assertEqual(result["duration"], 2)
        del self.context.media
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_ntsc_motion_and_bframes_keep_audio_and_duration(self):
        source = self.root / "ntsc.mp4"
        self.execute([baseline.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                      "testsrc2=size=720x1280:rate=30000/1001:duration=6", "-f", "lavfi", "-i",
                      "sine=frequency=880:sample_rate=44100:duration=6", "-c:v", "libx264", "-preset", "fast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
        self.source = source
        self.graph["nodes"][0]["params"]["video"] = str(source)
        self.test_file_memory_pipe_have_same_audio_and_video_timing()

    def test_real_stream_to_memory_to_stream_chain(self):
        # Add two more transfers behind the existing memory node.
        graph = copy.deepcopy(self.graph)
        previous = graph["nodes"][-1]["id"]
        for mode in ("stream", "memory"):
            node = new_node(self.registry.definitions["core.transport"])
            node["params"]["mode"] = mode
            for edge in graph["connections"]:
                if edge[0] == previous and edge[2] in {graph["nodes"][1]["id"], graph["nodes"][3]["id"]}:
                    edge[0] = node["id"]
            graph["connections"].append([previous, "video", node["id"], "video"])
            graph["nodes"].append(node)
            previous = node["id"]
        graph["nodes"][3]["params"]["output"] = str(self.root / "mixed.mp4")
        result = run_workflow({"graph": snapshot(graph, self.registry), "ffmpeg": baseline.FFMPEG},
                              self.context, registry=self.registry)
        self.assertEqual(result["duration"], 2)
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_real_stream_cancel_and_error_release_producer(self):
        graph = copy.deepcopy(self.graph)
        graph["nodes"][-1]["params"]["mode"] = "stream"
        output = Path(graph["nodes"][3]["params"]["output"])
        with mock.patch("model.node_workflow.render.compile_filters", return_value="no_such_filter"):
            with self.assertRaisesRegex(RuntimeError, "渲染失败"):
                run_workflow({"graph": snapshot(graph, self.registry), "ffmpeg": baseline.FFMPEG},
                             self.context, registry=self.registry)
        self.assertFalse(output.exists())
        self.assertEqual(PROCESS_BUDGET.used, 0)
        self.graph = graph
        self.test_cancellation_during_ffmpeg_removes_temporary_result()
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_real_cache_cancel_and_limit_release_buffers(self):
        tiny = MemoryBudget(128)
        with MediaSession(self.context, baseline.FFMPEG, budget=tiny) as session:
            with self.assertRaises(MemoryError):
                session.convert(ResourceRef.capture(self.source, "video"), "memory")
        self.assertEqual(tiny.used, 0)
        original = PipeReader.read
        def cancel_after_first(reader, size):
            result = original(reader, size)
            self.cancel.set()
            return result
        with MediaSession(self.context, baseline.FFMPEG) as session:
            with mock.patch.object(PipeReader, "read", cancel_after_first):
                with self.assertRaises(BackgroundTaskCanceled):
                    session.convert(ResourceRef.capture(self.source, "video"), "memory")
        self.assertEqual(PROCESS_BUDGET.used, 0)

    def test_real_parallel_memory_and_stream_use_separate_sessions(self):
        errors, results = [], []
        def job(mode):
            graph = copy.deepcopy(self.graph)
            graph["nodes"][-1]["params"]["mode"] = mode
            graph["nodes"][3]["params"]["output"] = str(self.root / ("parallel-" + mode + ".mp4"))
            context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
            try:
                results.append(run_workflow({"graph": snapshot(graph, self.registry), "ffmpeg": baseline.FFMPEG},
                                            context, registry=self.registry))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=job, args=(mode,)) for mode in ("memory", "stream")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(PROCESS_BUDGET.used, 0)

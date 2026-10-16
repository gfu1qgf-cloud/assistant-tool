import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef
from model.node_workflow.graph import NodeRegistry, register_core, example_graph, new_node, build_plan, GraphError, validate
from model.node_workflow.media import MediaSession, MemoryBudget, MemoryReader


class NodeMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "source.mp4"
        self.path.write_bytes(b"original")
        self.ref = ResourceRef.capture(self.path, "video")
        self.cancel = threading.Event()
        self.context = TaskRunContext(self.cancel, lambda *_: None, lambda *_: None)
        self.budget = MemoryBudget(128)
        self.session = MediaSession(self.context, budget=self.budget)
        self.context.media = self.session

    def tearDown(self):
        self.session.close()
        self.assertEqual(self.budget.used, 0)
        self.temp.cleanup()

    def cache(self, source=None, data=b"compressed-mp4", limit=128):
        with mock.patch.object(self.session, "info", return_value={"duration": 2}), \
             mock.patch.object(self.session, "_mp4_reader", return_value=io.BytesIO(data)):
            return self.session.convert(source or self.ref, "memory", limit)

    def test_file_memory_stream_share_one_reference_contract(self):
        memory = self.cache()
        stream = self.session.convert(memory, "stream")
        for ref in (self.ref, memory, stream):
            self.assertIs(type(ref), ResourceRef)
            self.assertEqual(ref.data_type, "video")
        self.assertIn("seek", memory.capabilities)
        self.assertNotIn("seek", stream.capabilities)
        self.assertIn("replay", stream.capabilities)

    def test_memory_reader_has_independent_positions_and_no_full_copy(self):
        memory = self.cache()
        first, second = self.session.open_reader(memory), self.session.open_reader(memory)
        self.assertIs(first.blocks, second.blocks)
        self.assertEqual(first.read(4), b"comp")
        self.assertEqual(second.tell(), 0)
        first.seek(-3, io.SEEK_END)
        self.assertEqual(first.read(), b"mp4")
        self.assertEqual(second.read(), b"compressed-mp4")

    def test_memory_block_boundaries_readinto_seek(self):
        with MemoryReader((b"abc", b"defg", b"hi")) as reader:
            self.assertEqual(reader.read(5), b"abcde")
            reader.seek(-2, io.SEEK_CUR)
            target = bytearray(4)
            self.assertEqual(reader.readinto(target), 4)
            self.assertEqual(target, b"defg")
            reader.seek(99)
            self.assertEqual(reader.read(), b"")
            with self.assertRaises(ValueError): reader.seek(-1)

    def test_stream_from_memory_is_reopenable_but_not_seekable(self):
        stream = self.session.convert(self.cache(), "stream")
        for _ in range(2):
            with self.session.open_reader(stream) as reader:
                self.assertEqual(reader.read(4), b"comp")
                with self.assertRaises(io.UnsupportedOperation): reader.seek(0)

    def test_memory_to_stream_to_memory_works_without_file_materialization(self):
        original = self.cache()
        stream = self.session.convert(original, "stream")
        final = self.session.convert(stream, "memory")
        self.assertEqual(final, original)
        self.assertEqual(self.budget.used, len(b"compressed-mp4"))
        with self.session.open_reader(final) as reader:
            self.assertEqual(reader.read(), b"compressed-mp4")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_generated_stream_and_memory_need_no_intermediate_path(self):
        metadata = {"duration": 2, "video": {"width": 360, "height": 640}, "audio": True}
        stream = self.session.register_stream(lambda context: io.BytesIO(b"generated-mp4"), metadata)
        with self.session.open_reader(stream) as reader:
            self.assertFalse(reader.seekable())
            with self.assertRaises(io.UnsupportedOperation): reader.seek(0)
        memory = self.session.convert(stream, "memory")
        self.assertEqual(stream.path, "")
        self.assertEqual(memory.path, "")
        self.assertEqual(self.session.info(memory), metadata)
        with self.session.open_reader(memory) as reader:
            self.assertEqual(reader.read(), b"generated-mp4")

    def test_generated_factory_requires_metadata_and_reopenable_callback(self):
        with self.assertRaises(TypeError): self.session.register_stream(io.BytesIO(b"x"), {})
        with self.assertRaises(ValueError): self.session.register_stream(lambda *_: io.BytesIO(b"x"), {})

    def test_generated_memory_retains_source_snapshot_checks(self):
        metadata = {"duration": 2, "video": {"width": 360, "height": 640}, "audio": True}
        stream = self.session.register_stream(lambda context: io.BytesIO(b"generated-mp4"), metadata, (self.ref,))
        memory = self.session.convert(stream, "memory")
        self.path.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.session.open_reader(memory)

    def test_same_cache_is_shared_and_budget_not_double_reserved(self):
        first = self.cache()
        self.assertEqual(self.session.convert(self.ref, "memory"), first)
        self.assertEqual(self.budget.used, len(b"compressed-mp4"))

    def test_global_budget_exhaustion_releases_partial_cache(self):
        with self.assertRaises(MemoryError): self.cache(data=b"x" * 129)
        self.assertEqual(self.budget.used, 0)
        self.assertFalse(self.session.entries)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_local_cap_releases_partial_cache(self):
        self.budget.limit = 20 * 1024 * 1024
        with self.assertRaises(MemoryError): self.cache(data=b"x" * (16 * 1024 * 1024 + 1), limit=16)
        self.assertEqual(self.budget.used, 0)

    def test_cancellation_does_not_create_a_resource(self):
        self.cancel.set()
        with self.assertRaises(BackgroundTaskCanceled): self.cache()
        self.assertFalse(self.session.entries)

    def test_source_changes_invalidate_transports(self):
        memory = self.cache()
        self.path.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.session.open_reader(memory)

    def test_cross_session_reference_is_rejected(self):
        memory = self.cache()
        with MediaSession(self.context, budget=self.budget) as other:
            with self.assertRaisesRegex(ValueError, "不属于本次"):
                other.open_reader(memory)

    def test_session_close_invalidates_readers_and_references(self):
        memory = self.cache()
        reader = self.session.open_reader(memory)
        self.session.close()
        self.assertTrue(reader.closed)
        with self.assertRaisesRegex(ValueError, "会话已释放"):
            self.session.open_reader(memory)
        self.assertEqual(self.budget.used, 0)

    def test_port_requiring_seek_rejects_stream_before_its_builder(self):
        stream = self.session.convert(self.cache(), "stream")
        registry = NodeRegistry()
        register_core(registry)
        registry.register("test", NodeDefinition("test.source", "source",
            outputs=(NodePort("video", "video", "video"),), build=lambda *_: {"video": stream}))
        called = mock.Mock(side_effect=AssertionError("should not execute"))
        registry.register("test", NodeDefinition("test.seek", "seek",
            inputs=(NodePort("video", "video", "video", capabilities=("seek",)),),
            outputs=(NodePort("operations", "operations", "operations"),), build=called))
        graph = {"schema": 1, "nodes": [new_node(registry.definitions[kind])
                 for kind in ("test.source", "test.seek", "core.render")], "connections": [], "output_node": ""}
        source, seek, render = graph["nodes"]
        render["params"]["output"] = str(self.path.with_name("out.mp4"))
        graph["connections"] = [[source["id"], "video", seek["id"], "video"],
                                 [source["id"], "video", render["id"], "video"],
                                 [seek["id"], "operations", render["id"], "operations"]]
        graph["output_node"] = render["id"]
        with self.assertRaisesRegex(GraphError, "不能直接连接顺序流"):
            build_plan(graph, registry, self.context)
        called.assert_not_called()

    def test_raw_buffer_cannot_masquerade_as_video_port_output(self):
        registry = NodeRegistry()
        register_core(registry)
        registry.register("test", NodeDefinition("test.bad", "bad",
            outputs=(NodePort("video", "video", "video"),), build=lambda *_: {"video": io.BytesIO(b"bad")}))
        graph = example_graph(registry)
        source = new_node(registry.definitions["test.bad"])
        graph["nodes"].append(source)
        render = next(n for n in graph["nodes"] if n["kind"] == "core.render")
        render["params"]["output"] = str(self.path.with_name("out.mp4"))
        merge = next(n for n in graph["nodes"] if n["kind"] == "core.merge")
        # Fill the required operations input through a valid empty source; the bad source runs first.
        registry.register("test", NodeDefinition("test.empty", "empty",
            outputs=(NodePort("operations", "operations", "operations"),), build=lambda *_: {"operations": ()}))
        empty = new_node(registry.definitions["test.empty"])
        graph["nodes"].append(empty)
        graph["connections"] = [[source["id"], "video", render["id"], "video"],
                                 [empty["id"], "operations", merge["id"], "operations"],
                                 [merge["id"], "operations", render["id"], "operations"]]
        with self.assertRaisesRegex(GraphError, "数据类型不符合"):
            build_plan(graph, registry, self.context)

    def test_known_pipe_capabilities_are_checked_before_execution(self):
        from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
        registry = NodeRegistry()
        register_core(registry)
        definition = caption_node()
        from dataclasses import replace
        definition = replace(definition, inputs=(NodePort("video", "视频", "video", capabilities=("seek",)),
                                                 definition.inputs[0]))
        registry.register("batch_text_video", definition)
        graph = example_graph(registry, "stream")
        caption = next(node for node in graph["nodes"] if node["kind"] == definition.kind)
        transport = next(node for node in graph["nodes"] if node["kind"] == "core.transport")
        graph["connections"].append([transport["id"], "video", caption["id"], "video"])
        self.assertTrue(any("不能直接连接顺序流" in text for _, text in validate(graph, registry)[0]))
        graph["nodes"][-1]["params"]["mode"] = "memory"
        self.assertFalse(any("不能直接连接顺序流" in text for _, text in validate(graph, registry)[0]))


if __name__ == "__main__": unittest.main()

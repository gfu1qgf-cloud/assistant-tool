import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.node_workflow.contracts import DirectoryRef, FileList, ResourceRef, VideoTimeline, VideoSection, SubtitleTrack, SubtitleCue
from model.node_workflow.filesystem import build_listdir
from model.node_workflow.graph import NodeRegistry, register_core, task_example_graph, migrate_task_ports, validate
from model.node_workflow.tasks import capture_task, prepare_job
from app_plugins.builtin.smart_video_editor.workflow_nodes import edit_node, build_edit
from app_plugins.builtin.smart_video_editor import engine
from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node


class FileNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.event = threading.Event()
        self.context = TaskRunContext(self.event, lambda *_: None, lambda *_: None)
        self.directory = DirectoryRef(str(self.root), "owner")
        for name in ("10.mp4", "2.mp4", "1.MP4", "客户-口播.mp4", "客户-ReeLS.mp4", "notes.txt", ".hidden.mp4"):
            (self.root / name).write_bytes(b"synthetic")
        nested = self.root / "sub"
        nested.mkdir()
        (nested / "3.mp4").write_bytes(b"nested")
        output = self.root / "节点工作流结果"
        output.mkdir()
        (output / "result.mp4").write_bytes(b"rendered")

    def tearDown(self):
        self.temp.cleanup()

    def listing(self, **params):
        return build_listdir({"directory": self.directory}, params, self.context)["files"]

    def names(self, result):
        return [Path(item.path).name for item in result.files]

    def test_default_ignores_subdirs_outputs_keywords_and_sorts_naturally(self):
        result = self.listing()
        self.assertEqual(self.names(result), ["1.MP4", "2.mp4", "10.mp4"])
        self.assertEqual(result.directory.task_key, "owner")
        self.assertTrue(all(item.data_type == "video" for item in result.files))

    def test_recursive_and_directory_exclusions_are_explicit(self):
        self.assertEqual(self.names(self.listing(recursive="yes")), ["1.MP4", "2.mp4", "10.mp4", "3.mp4"])
        all_dirs = self.names(self.listing(recursive="yes", exclude_dirs=""))
        self.assertIn("result.mp4", all_dirs)

    def test_user_can_clear_keyword_exclusions(self):
        names = self.names(self.listing(exclude_keywords=""))
        self.assertIn("客户-口播.mp4", names)
        self.assertIn("客户-ReeLS.mp4", names)

    def test_include_exclude_patterns_and_unicode_separators(self):
        self.assertEqual(self.names(self.listing(include="*.txt；2.mp4", exclude="2.*")), ["notes.txt"])

    def test_modification_order(self):
        import os
        os.utime(self.root / "1.MP4", ns=(1000000000, 1000000000))
        os.utime(self.root / "2.mp4", ns=(2000000000, 2000000000))
        os.utime(self.root / "10.mp4", ns=(3000000000, 3000000000))
        self.assertEqual(self.names(self.listing(sort="modified")), ["10.mp4", "2.mp4", "1.MP4"])

    def test_hidden_names_are_configurable(self):
        self.assertIn(".hidden.mp4", self.names(self.listing(hidden="yes")))

    def test_empty_list_is_not_replaced_by_other_files(self):
        result = self.listing(include="*.missing")
        self.assertEqual(result.files, ())
        self.assertIsInstance(result, FileList)

    def test_limit_raises_instead_of_silently_truncating(self):
        with self.assertRaisesRegex(ValueError, "未截断"):
            self.listing(limit=1)

    def test_cancel_is_cooperative(self):
        self.event.set()
        with self.assertRaises(BackgroundTaskCanceled):
            self.listing()

    def test_file_signature_is_fixed_at_enumeration(self):
        result = self.listing()
        (self.root / "2.mp4").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            result.verify()

    def test_symlink_escape_is_not_followed(self):
        outside = self.root.parent / (self.root.name + "-external")
        outside.mkdir()
        try:
            (outside / "escape.mp4").write_bytes(b"external")
            try:
                (self.root / "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("系统未授权创建符号链接")
            self.assertNotIn("escape.mp4", self.names(self.listing(recursive="yes")))
        finally:
            import shutil
            shutil.rmtree(outside)

    def registry(self):
        registry = NodeRegistry()
        register_core(registry)
        services = SimpleNamespace(_effective_settings=lambda: engine.normalize_smart_video_editor_settings({}))
        registry.register("smart_video_editor", edit_node(services))
        registry.register("batch_text_video", caption_node())
        return registry

    def test_old_bundled_task_graph_migrates_to_explicit_wires_without_mutating_original(self):
        registry = self.registry()
        graph = task_example_graph(registry)
        task, listing, edit, caption, render = graph["nodes"]
        graph["nodes"].remove(listing)
        graph["connections"] = [edge for edge in graph["connections"]
                                if edge[0] not in {task["id"], listing["id"]} and edge[2] != listing["id"]]
        graph["connections"].append([task["id"], "task", edit["id"], "task"])
        task["version"] = edit["version"] = 1
        # Previous templates had no spare column between the task and editor.
        for index, node in enumerate(graph["nodes"]):
            node["position"] = [index * 300, 40]
        original = copy.deepcopy(graph)
        upgraded, changed = migrate_task_ports(graph, registry)
        self.assertTrue(changed)
        self.assertEqual(graph, original)
        self.assertFalse(validate(upgraded, registry)[0])
        self.assertFalse(any(edge[1] == "task" or edge[3] == "task" for edge in upgraded["connections"]))
        self.assertEqual(sum(node["kind"] == "core.listdir" for node in upgraded["nodes"]), 1)
        migrated = {node["id"]: node for node in upgraded["nodes"]}
        new_listing = next(node for node in upgraded["nodes"] if node["kind"] == "core.listdir")
        self.assertEqual(new_listing["position"], original["nodes"][1]["position"])
        self.assertEqual(migrated[edit["id"]]["position"][0], new_listing["position"][0] + 300)
        self.assertEqual(migrated[task["id"]]["position"], original["nodes"][0]["position"])
        self.assertEqual(migrated[caption["id"]]["position"][0], original["nodes"][2]["position"][0] + 300)

    def test_migration_preserves_an_explicit_script_override(self):
        registry = self.registry()
        graph = task_example_graph(registry)
        task, listing, edit, caption, render = graph["nodes"]
        task["version"] = edit["version"] = 1
        graph["connections"] = [edge for edge in graph["connections"] if edge[2:] != [edit["id"], "files"]]
        graph["connections"].append([task["id"], "task", edit["id"], "task"])
        upgraded, _ = migrate_task_ports(graph, registry)
        self.assertEqual(sum(edge[2:] == [edit["id"], "script"] for edge in upgraded["connections"]), 1)

    def test_migration_does_not_duplicate_partly_upgraded_task_fields(self):
        registry = self.registry()
        graph = task_example_graph(registry)
        task, listing, edit, caption, render = graph["nodes"]
        task["version"] = edit["version"] = 1
        graph["connections"].append([task["id"], "task", edit["id"], "task"])
        graph["connections"] = [edge for edge in graph["connections"]
                                if edge[2:] != [edit["id"], "task_id"]]
        before = copy.deepcopy(graph)
        upgraded, changed = migrate_task_ports(graph, registry)
        self.assertTrue(changed)
        self.assertEqual(graph, before)
        for field in ("script", "language", "task_id", "task_name"):
            self.assertEqual(sum(edge[2:] == [edit["id"], field] for edge in upgraded["connections"]), 1)
        self.assertFalse(validate(upgraded, registry)[0])
        again, changed = migrate_task_ports(upgraded, registry)
        self.assertFalse(changed)
        self.assertEqual(again, upgraded)

    def test_preparing_manual_directory_works_without_a_bound_task(self):
        registry = self.registry()
        graph = task_example_graph(registry)
        from model.node_workflow.graph import new_node
        directory = new_node(registry.definitions["core.directory"])
        directory["params"]["path"] = str(self.root)
        text = new_node(registry.definitions["core.text"])
        text["params"]["text"] = "Ahoj svet."
        old_task, listing, edit, caption, output = graph["nodes"]
        graph["nodes"] = [directory, text, listing, edit, caption,
                          new_node(registry.definitions["core.render"])]
        render = graph["nodes"][-1]
        render["params"]["output"] = str(self.root / "final.mp4")
        graph["output_node"] = render["id"]
        graph["execution_mode"] = "detail"
        graph["connections"] = [[directory["id"], "directory", listing["id"], "directory"],
            [listing["id"], "files", edit["id"], "files"], [text["id"], "text", edit["id"], "script"],
            [edit["id"], "subtitles", caption["id"], "subtitles"],
            [edit["id"], "video", render["id"], "video"], [caption["id"], "operations", render["id"], "operations"]]
        payload, resources = prepare_job(graph, registry, None, None)
        self.assertIsNone(payload["task"])
        self.assertTrue(resources)

    def test_smart_edit_uses_only_supplied_list_and_text_without_task_access(self):
        # Stop at the engine boundary: no model, media processing, or fake success.
        files = self.listing(include="2.mp4")
        self.context.node_prepared = {"settings": engine.normalize_smart_video_editor_settings({})}
        self.context.ffmpeg = "ffmpeg"
        def received(jobs, *_args):
            self.assertEqual(jobs[0]["sources"], [str(self.root / "2.mp4")])
            self.assertEqual(jobs[0]["script"], "Explicit text.")
            raise RuntimeError("boundary verified")
        with mock.patch.object(engine, "discover_task_videos", side_effect=AssertionError("不能隐式搜文件")), \
             mock.patch.object(engine, "smart_video_jobs_require_model", return_value=False), \
             mock.patch.object(engine, "analyze_smart_video_jobs", side_effect=received):
            with self.assertRaisesRegex(RuntimeError, "boundary verified"):
                build_edit({"files": files, "script": "Explicit text."}, {}, self.context)


if __name__ == "__main__":
    unittest.main()

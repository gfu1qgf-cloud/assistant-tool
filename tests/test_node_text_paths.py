"""Literal text, lexical path nodes, and typed conditional compression."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import DirectoryRef, ResourceRef, FilterOperation
from model.node_workflow.graph import NodeRegistry, register_core, new_node, empty_graph, evaluate_graph, validate, GraphError
from model.node_workflow.video_stream import stream, EncodingSettings


class TextPathNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeRegistry()
        register_core(self.registry)
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def build(self, kind, inputs, **updates):
        definition = self.registry.definitions["core." + kind]
        params = {param.key: param.default for param in definition.parameters}
        params.update(updates)
        return definition.build(inputs, params, self.context)

    def test_literal_text_judgments_and_case_selection(self):
        for operation, needle, expected in (
                ("contains", "余夏", True), ("contains", "慕雪", False),
                ("not_contains", "慕雪", True), ("starts_with", "余夏", True),
                ("ends_with", ".MP4", True), ("equals", "余夏LZX.MP4", True),
                ("not_equals", "余夏LZX.mp4", True)):
            with self.subTest(operation=operation):
                self.assertIs(self.build("text_test", {"text": "余夏LZX.MP4"},
                                         operation=operation, needle=needle)["boolean"], expected)
        self.assertFalse(self.build("text_test", {"text": "LZX.MP4"}, needle="lzx")["boolean"])
        self.assertTrue(self.build("text_test", {"text": "LZX.MP4"}, needle="lzx", case="ignore")["boolean"])

    def test_connected_needle_overrides_inspector_and_unicode_is_normalized(self):
        self.assertTrue(self.build("text_test", {"text": "Ma\u0301ria", "needle": "Mária"},
                                   operation="equals", needle="unused")["boolean"])
        self.assertTrue(self.build("text_test", {"text": "Straße", "needle": "STRASSE"},
                                   operation="equals", case="ignore")["boolean"])

    def test_empty_keywords_cannot_accidentally_enable_all_compression(self):
        for operation in ("contains", "not_contains", "starts_with", "ends_with"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "非空"):
                self.build("text_test", {"text": "anything"}, operation=operation)
        self.assertTrue(self.build("text_test", {"text": ""}, operation="equals")["boolean"])
        with self.assertRaisesRegex(ValueError, "匹配文字"):
            self.build("path_match", {"path": "anything.mp4"})

    def test_text_operations_do_not_interpret_code_or_regex(self):
        code = "__import__('os').system('not-executed')"
        self.assertTrue(self.build("text_test", {"text": code}, needle=code)["boolean"])
        self.assertFalse(self.build("text_test", {"text": "a123b"}, needle="a.*b")["boolean"])

    def test_text_cleanup_and_character_length(self):
        self.assertEqual(self.build("text_transform", {"text": "  a\nb  "})["text"], "a\nb")
        self.assertEqual(self.build("text_transform", {"text": " a\n\t b "}, operation="normalize_spaces")["text"], "a b")
        self.assertEqual(self.build("text_transform", {"text": "abc"}, operation="upper")["text"], "ABC")
        self.assertEqual(self.build("text_transform", {"text": "ABC"}, operation="lower")["text"], "abc")
        self.assertEqual(self.build("text_length", {"text": "中文🙂"})["number"], 3)

    def test_text_slice_static_dynamic_and_out_of_range(self):
        self.assertEqual(self.build("text_slice", {"text": "abcdef"}, start=1, length=3)["text"], "bcd")
        self.assertEqual(self.build("text_slice", {"text": "abcdef", "start": -2, "length": -1})["text"], "ef")
        self.assertEqual(self.build("text_slice", {"text": "abcdef"}, start=99)["text"], "")
        self.assertEqual(self.build("text_slice", {"text": "abcdef"}, start=-99, length=2)["text"], "ab")
        for start, length in ((True, 1), (.5, 2), (0, -2), (0, float("inf"))):
            with self.subTest(start=start, length=length), self.assertRaises(ValueError):
                self.build("text_slice", {"text": "abc", "start": start, "length": length})

    def test_path_name_and_parts_handle_windows_posix_and_unc_without_io(self):
        cases = (
            (r"C:\任务\余夏-01.final.mp4", "余夏-01.final.mp4", "余夏-01.final", ".mp4", "C:"),
            ("C:/任务/余夏-01.mp4", "余夏-01.mp4", "余夏-01", ".mp4", "C:"),
            (r"\\server\share\任务\video.mov", "video.mov", "video", ".mov", r"\\server\share"),
            ("//server/share/任务/video.mov", "video.mov", "video", ".mov", r"\\server\share"),
            ("/tmp/任务/video.tar.gz", "video.tar.gz", "video.tar", ".gz", ""),
            (".gitignore", ".gitignore", ".gitignore", "", ""))
        with patch("pathlib.Path.stat", side_effect=AssertionError("Lexical nodes must not read the disk")):
            for path, name, stem, extension, drive in cases:
                with self.subTest(path=path):
                    parts = self.build("path_parts", {"path": path})
                    self.assertEqual((parts["name"], parts["stem"], parts["extension"], parts["drive"]),
                                     (name, stem, extension, drive))
                    self.assertEqual(self.build("path_name", {"path": path})["name"], name)
                    self.assertEqual(self.build("path_name", {"path": path}, part="stem")["name"], stem)

    def test_original_path_name_parameters_and_port_are_still_usable(self):
        node = self.registry.definitions["core.path_name"]
        self.assertEqual(node.version, 1)
        self.assertEqual(node.outputs[0].key, "name")
        self.assertEqual(node.build({"path": r"C:\任务\01.mp4"}, {"part": "stem"}, self.context), {"name": "01"})

    def test_parent_directory_and_root(self):
        self.assertEqual(self.build("path_parent", {"path": r"C:\任务\1011\01.mp4"})["path"], r"C:\任务\1011")
        self.assertEqual(self.build("path_parent", {"path": "/tmp/task/01.mp4"}, levels=2)["path"], "/tmp")
        self.assertEqual(self.build("path_parent", {"path": "/tmp/task/01.mp4"}, levels=99)["path"], "/")

    def test_join_and_absolute_component_rules(self):
        self.assertEqual(self.build("path_join", {"parts": ("C:/任务", "1011", "01.mp4")})["path"], r"C:\任务\1011\01.mp4")
        self.assertEqual(self.build("path_join", {"parts": ("/tmp", "task", "01.mp4")})["path"], "/tmp/task/01.mp4")
        self.assertEqual(self.build("path_join", {"parts": ("C:/任务", "D:/else/01.mp4")})["path"], r"D:\else\01.mp4")
        with self.assertRaises(ValueError):
            self.build("path_join", {"parts": ()})

    def test_replace_or_remove_only_last_extension_without_renaming(self):
        self.assertEqual(self.build("path_suffix", {"path": "archive.tar.gz"}, extension="mp4")["path"], "archive.tar.mp4")
        self.assertEqual(self.build("path_suffix", {"path": "video.mov", "extension": ""})["path"], "video")
        for invalid in ("../../bad", "mp4 ", ".", "bad\nmp4"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.build("path_suffix", {"path": "video.mov"}, extension=invalid)

    def test_relative_path_normalization_and_outside_rejection(self):
        self.assertEqual(self.build("path_relative", {"path": "C:/任务/1011/01.mp4"}, base="C:/任务")["path"], r"1011\01.mp4")
        self.assertEqual(self.build("path_relative", {"path": "/tmp/library/a/../01.mp4", "base": "/tmp/library"})["path"], "01.mp4")
        for value, base in (("C:/任务-other/01.mp4", "C:/任务"), ("D:/任务/a.mp4", "C:/任务"),
                            ("/tmp/library/../private/a.mp4", "/tmp/library"), ("../outside/a.mp4", ".")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "基准目录"):
                self.build("path_relative", {"path": value}, base=base)

    def test_normpath_supports_format_override(self):
        self.assertEqual(self.build("path_normalize", {"path": "C:/任务/a/../01.mp4"})["path"], r"C:\任务\01.mp4")
        self.assertEqual(self.build("path_normalize", {"path": "a//b/../c"}, style="posix")["path"], "a/c")

    def test_path_match_can_check_full_path_filename_or_parent(self):
        value = r"C:\库存\余夏\另一个人-01.MP4"
        self.assertTrue(self.build("path_match", {"path": value}, pattern="余夏")["boolean"])
        self.assertFalse(self.build("path_match", {"path": value}, pattern="余夏", subject="name")["boolean"])
        self.assertTrue(self.build("path_match", {"path": value}, pattern="余夏", subject="parent")["boolean"])
        self.assertTrue(self.build("path_match", {"path": value}, pattern="*-01.mp4", subject="name", operation="glob")["boolean"])
        self.assertFalse(self.build("path_match", {"path": value}, pattern="*-01.mp4", subject="name", operation="glob", case="sensitive")["boolean"])
        self.assertTrue(self.build("path_match", {"path": value, "pattern": "C:/库存/余夏"},
                                  operation="starts_with", pattern="unused")["boolean"])

    def test_path_text_accepts_resources_and_rejects_other_values(self):
        file = self.root / "01.mp4"
        file.write_bytes(b"synthetic")
        ref = ResourceRef.capture(file, "video")
        for value in (str(file), ref, stream(ref), DirectoryRef(str(self.root))):
            self.assertEqual(self.build("path_text", {"value": value})["path"],
                             value if isinstance(value, str) else value.path)
        for value in (123, True, ("path",), None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.build("path_text", {"value": value})
        for value in ("", " ", "bad\x00path"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build("path_name", {"path": value})

    def test_exists_and_text_to_directory_are_read_only(self):
        file = self.root / "01.mp4"
        file.write_bytes(b"synthetic")
        self.assertEqual(self.build("path_exists", {"path": str(file)}), {"exists": True, "is_file": True, "is_directory": False})
        self.assertEqual(self.build("path_exists", {"path": str(self.root)}), {"exists": True, "is_file": False, "is_directory": True})
        missing = self.root / "missing"
        self.assertEqual(self.build("path_exists", {"path": str(missing)}), {"exists": False, "is_file": False, "is_directory": False})
        directory = self.build("path_directory", {"path": str(self.root)})["directory"]
        self.assertIsInstance(directory, DirectoryRef)
        with self.assertRaises(FileNotFoundError):
            self.build("path_directory", {"path": str(missing)})
        self.assertFalse(missing.exists())
        self.assertEqual(file.read_bytes(), b"synthetic")

    def test_compression_false_preserves_operations_and_existing_encoding(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"synthetic")
        value = replace(stream(ResourceRef.capture(source, "video")),
                        operations=(FilterOperation("existing", None, "null"),),
                        encoding=EncodingSettings(crf=19))
        disabled = self.build("compress", {"video": value, "enabled": False}, crf=40)["video"]
        self.assertIs(disabled, value)
        enabled = self.build("compress", {"video": value, "enabled": True}, crf=40)["video"]
        self.assertEqual(enabled.encoding.crf, 40)
        self.assertEqual(enabled.operations, value.operations)
        self.assertEqual(self.build("compress", {"video": value}, crf=30)["video"].encoding.crf, 30)
        with self.assertRaises(TypeError):
            self.build("compress", {"video": value, "enabled": "false"})

    def test_typed_graph_directory_to_text_match_to_conditional_compression(self):
        from model.node_workflow.contracts import NodeDefinition, NodePort
        source = self.root / "source.mp4"
        source.write_bytes(b"synthetic")
        ref = ResourceRef.capture(source, "video")
        self.registry.register("test", NodeDefinition("test.source", "test", outputs=(
            NodePort("directory", "directory", "directory"), NodePort("video", "video", "video")),
            build=lambda *_: {"directory": DirectoryRef(str(self.root / "余夏")), "video": ref}))
        nodes = [new_node(self.registry.definitions[kind]) for kind in (
            "test.source", "core.path_text", "core.text_test", "core.compress", "core.preview")]
        a, b, c, d, e = [n["id"] for n in nodes]
        nodes[2]["params"]["needle"] = "余夏"
        graph = empty_graph()
        graph.update(nodes=nodes, connections=[[a, "directory", b, "value"], [b, "path", c, "text"],
            [c, "boolean", d, "enabled"], [a, "video", d, "video"], [d, "video", e, "value"]])
        self.assertFalse(validate(graph, self.registry, root_id=e)[0])
        result = evaluate_graph(graph, self.registry, self.context, root_id=e)["value"]
        self.assertEqual(result.encoding.crf, 23)
        nodes[2]["params"]["needle"] = "其他人"
        result = evaluate_graph(graph, self.registry, self.context, root_id=e)["value"]
        self.assertIsNone(result.encoding)
        # A number cannot accidentally be used as the condition.
        wrong = new_node(self.registry.definitions["core.number"])
        graph["nodes"].append(wrong)
        graph["connections"][2] = [wrong["id"], "number", d, "enabled"]
        with self.assertRaises(GraphError):
            evaluate_graph(graph, self.registry, self.context, root_id=e)


if __name__ == "__main__":
    unittest.main()

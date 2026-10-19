"""Pinned vendor integrity and reproducible bridge assembly, with no browser."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest


class FlowUpstreamBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ.get('LZX_TEST_SOURCE_ROOT', Path(__file__).resolve().parents[1]))
        spec = importlib.util.spec_from_file_location('audited_flow_builder', cls.root / 'tools/build_flow_bridge.py')
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)

    def test_pinned_upstream_and_all_critical_original_hashes(self):
        self.assertEqual(self.builder.UPSTREAM_VERSION, '1.1.4')
        self.assertEqual(self.builder.PIN, 'e4e8f34ea6a0e0d7d9e0e87bbf7218e66d315ec2')
        for name, expected in self.builder.HASHES.items():
            self.assertEqual(hashlib.sha256((self.root / 'third_party/flowcue/extension' / name).read_bytes()).hexdigest(), expected)

    def test_assembly_is_reproducible_and_does_not_modify_vendor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(self.root / 'third_party/flowcue', root / 'third_party/flowcue')
            shutil.copytree(self.root / 'browser_extensions/flow_adapter', root / 'browser_extensions/flow_adapter')
            before = {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / 'third_party').rglob('*') if p.is_file()}
            output = self.builder.build(root)
            first = {p.relative_to(output): hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}
            self.builder.build(root)
            second = {p.relative_to(output): hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(first, second)
            self.assertEqual(before, {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / 'third_party').rglob('*') if p.is_file()})
            self.assertEqual(json.loads((output/'SOURCE.json').read_text('utf-8'))['upstream_version'], '1.1.4')

    def test_changed_original_is_rejected_before_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(self.root / 'third_party/flowcue', root / 'third_party/flowcue')
            (root/'third_party/flowcue/extension/background.js').write_text('changed source', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '上游源码校验不通过'):
                self.builder.build(root)
            self.assertFalse((root/'browser_extensions/flow_bridge').exists())


if __name__ == '__main__': unittest.main()

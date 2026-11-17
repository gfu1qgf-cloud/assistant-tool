import hashlib
from pathlib import Path
import runpy
import unittest

ROOT=Path(__file__).resolve().parents[1]
security=runpy.run_path(str(ROOT/'tools/flow_security_patches.py'))
builder=runpy.run_path(str(ROOT/'tools/build_flow_bridge.py'))


class FlowCodeQLSecurityTests(unittest.TestCase):
    def test_upstream_inputs_remain_hash_identical(self):
        for name, expected in builder['HASHES'].items():
            self.assertEqual(hashlib.sha256((ROOT/'third_party/flowcue/extension'/name).read_bytes()).hexdigest(),expected)

    def test_ui_patch_fails_closed_if_upstream_seams_change(self):
        source=(ROOT/'third_party/flowcue/extension/injects/ui.js').read_text('utf-8')
        result=security['patch_ui'](source)
        self.assertIn('host.endsWith(".flow.google.com")',result)
        self.assertNotIn('host.endsWith("flow.google.com")',result)
        self.assertIn('String(value).replace(/\\n|\\r|\\u2028|\\u2029/g',result)
        with self.assertRaisesRegex(ValueError,'re-audit'):
            security['patch_ui'](source.replace('host.endsWith("flow.google.com")','changed'))

    def test_console_format_patch_keeps_url_as_separate_data(self):
        source=(ROOT/'third_party/flowcue/extension/content-isolated.js').read_text('utf-8')
        result=security['patch_isolated'](source)
        self.assertIn('URL=%s",',result)
        self.assertNotIn('URL=" + location.href',result)
        with self.assertRaises(ValueError):
            security['patch_isolated'](result)

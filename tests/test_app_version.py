import re
from pathlib import Path
import unittest
from model.AppVersion import APP_VERSION


class AppVersionTests(unittest.TestCase):
    def test_version_has_one_matching_changelog_heading(self):
        self.assertRegex(APP_VERSION, r'^\d+\.\d+\.\d+$')
        root=Path(__file__).resolve().parents[1]
        text=(root/'CHANGELOG.md').read_text(encoding='utf-8-sig')
        self.assertEqual(len(re.findall(r'^## v'+re.escape(APP_VERSION)+r'(?:\s|$)',text,re.MULTILINE)),1)

    def test_both_source_launch_modes_use_the_new_isolated_environment(self):
        root=Path(__file__).resolve().parents[1]
        normal=(root/'启动程序.cmd').read_text(encoding='utf-8-sig')
        protected=(root/'启动VC加密模式.cmd').read_text(encoding='utf-8-sig')
        self.assertIn('.venv-py314\\Scripts\\python.exe',normal)
        self.assertNotIn('.venv\\Scripts\\python.exe',normal)
        self.assertIn('启动程序.cmd',protected)

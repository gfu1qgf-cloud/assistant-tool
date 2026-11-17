import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
module = runpy.run_path(str(ROOT/'packaging/build_environment.py'))


class ReleaseNativeEnvironmentTests(unittest.TestCase):
    def test_only_interpreter_and_windows_directories_are_used(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = module['windows_build_path'](root/'venv/Scripts/python.exe',
                                                 root/'python', root/'Windows')
            self.assertEqual(value.split(os.pathsep), [str(root/'venv/Scripts'),
                str(root/'python'), str(root/'python/DLLs'), str(root/'Windows/System32'),
                str(root/'Windows'), str(root/'Windows/System32/Wbem')])
        with self.assertRaises(ValueError):
            module['windows_build_path']('relative/python.exe', 'python', 'Windows')

    def test_caller_environment_is_not_used_as_native_search_path(self):
        with tempfile.TemporaryDirectory() as folder:
            environment = {'SystemRoot': folder, 'PATH': 'unrelated-poppler;untrusted-dir',
                'QT_PLUGIN_PATH': 'other-qt', 'QT_QPA_PLATFORM_PLUGIN_PATH': 'other-platform',
                'PYTHONPATH': 'other-python', 'UNCHANGED': 'keep'}
            original = dict(os.environ)
            module['isolate_windows_build_environment'](environment)
            self.assertNotIn('unrelated-poppler', environment['PATH'])
            self.assertNotIn('untrusted-dir', environment['PATH'])
            for key in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONPATH'):
                self.assertNotIn(key, environment)
            self.assertEqual(environment['UNCHANGED'], 'keep')
            self.assertEqual(dict(os.environ), original)

    def test_spec_isolates_before_native_hook_collection(self):
        spec = (ROOT/'packaging/AssistantTool.spec').read_text('utf-8')
        self.assertLess(spec.index('isolate_windows_build_environment'),
                        spec.index('from PyInstaller.utils.hooks'))

    def test_ci_uses_the_tested_cpu_wheels_and_checks_real_executable(self):
        workflow = (ROOT/'.github/workflows/release.yml').read_text('utf-8')
        self.assertIn('--index-url https://download.pytorch.org/whl/cpu', workflow)
        self.assertIn('torch==2.13.0+cpu', workflow)
        self.assertIn('torchaudio==2.11.0+cpu', workflow)
        self.assertLess(workflow.index('packaging/smoke_frozen.py'),
                        workflow.index('- name: Create GitHub Release'))

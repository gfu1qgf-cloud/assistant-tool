import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
runner = runpy.run_path(str(ROOT/'packaging/run_tests_isolated.py'))


class ReleaseTestRunnerTests(unittest.TestCase):
    def test_ffmpeg_is_exposed_to_subprocess_and_unit_fixture_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ffmpeg = root/'runtime/ffmpeg/ffmpeg.exe'
            ffmpeg.parent.mkdir(parents=True); ffmpeg.touch()
            with patch.dict(os.environ, {'PATH':'previous'}, clear=True):
                env = runner['test_environment'](root)
            self.assertEqual(env['NODE_FFMPEG'], str(ffmpeg.resolve()))
            self.assertEqual(env['PATH'].split(os.pathsep)[0], str(ffmpeg.parent.resolve()))
            self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        worker = (ROOT/'packaging/test_worker.py').read_text('utf8')
        self.assertIn("str(root / 'tests')", worker)
        self.assertIn('TemporaryDirectory', worker)
        self.assertIn('not callbacks', worker)
        self.assertIn('socket.socket.connect = no_network', worker)

    def test_nonzero_exit_is_preserved_not_relabelled_as_pass(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 7)):
                result = runner['execute_module'](root, 'test_synthetic', root, {})
            self.assertEqual(result['exit_code'], 7)
            self.assertFalse(result['timed_out'])

    def test_timeout_is_failure_with_a_persistent_log(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired([], 1)):
                result=runner['execute_module'](root,'test_synthetic',root,{},timeout=1)
            self.assertEqual(result['exit_code'],124)
            self.assertTrue(result['timed_out'])
            self.assertIn('RELEASE_TEST_TIMEOUT',Path(result['log']).read_text('utf8'))

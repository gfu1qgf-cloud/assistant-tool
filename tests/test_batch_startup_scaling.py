"""Large pool preflight must scale with sources plus jobs, and keep guards."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_plugins.builtin.batch_text_video import engine, store
from app_plugins.builtin.batch_text_video.background import validate_batch_outputs


class BatchStartupScalingTests(unittest.TestCase):
    def state(self, root, count=782):
        state = store.fresh_state()
        state['settings'].update(background_pool_id='none', output_dir=str(root/'outputs'), layers=[])
        state['jobs'] = [dict(id=str(i), path='', name=f'result-{i}', voice_path=str(root/f'voice-{i%63}.wav'))
                         for i in range(count)]
        state['image_pool'] = [dict(id=str(i), path=str(root/f'image-{i}.png')) for i in range(872)]
        state['backgrounds'] = [str(root/f'video-{i}.mp4') for i in range(5)]
        return state

    def test_782_jobs_and_2639_references_do_not_repeat_every_source_for_every_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self.state(root)
            # Add repeated pool references, just like imported task bindings.
            state['copy_pool'] = [dict(path=str(root/f'image-{i%872}.png')) for i in range(980)]
            before = copy.deepcopy(state)
            calls = []
            def identity(path):
                calls.append(str(path))
                return os.path.normcase(os.path.abspath(path))
            with patch.object(store, 'file_identity', side_effect=identity), \
                    patch.object(engine, 'file_identity', side_effect=identity):
                validate_batch_outputs(state, [job['id'] for job in state['jobs']])
            self.assertLessEqual(len(calls), 940+2*782)
            self.assertEqual(state, before)

    def test_preflight_keeps_unpaired_source_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self.state(root, 1)
            victim = root/'outputs'/'pool-source.mp4'
            victim.parent.mkdir()
            victim.write_bytes(b'KEEP SOURCE')
            state['backgrounds'].append(str(victim))
            state['jobs'][0]['name'] = 'pool-source'
            with self.assertRaisesRegex(ValueError, '覆盖源文件'):
                validate_batch_outputs(state, ['0'])
            self.assertEqual(victim.read_bytes(), b'KEEP SOURCE')

    def test_colliding_sanitized_output_names_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.state(Path(tmp), 2)
            state['jobs'][0]['name'] = 'same:name'
            state['jobs'][1]['name'] = 'same?name'
            with self.assertRaisesRegex(ValueError, '同一成品'):
                validate_batch_outputs(state, ['0', '1'])

    def test_safe_output_refreshes_sources_each_time_without_a_batch_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.mp4'
            output = root/'result.mp4'
            changed = False
            def identity(path):
                if Path(path) == source:
                    return str(output if changed else source)
                return str(Path(path))
            with patch.object(engine, 'file_identity', side_effect=identity):
                self.assertEqual(engine.safe_output(root, 'result', [source, source]), output)
                changed = True
                with self.assertRaisesRegex(ValueError, '覆盖源文件'):
                    engine.safe_output(root, 'result', [source, source])

    def test_source_snapshot_is_not_reused_across_preflight_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self.state(root, 1)
            validate_batch_outputs(state, ['0'])
            state['backgrounds'].append(str(root/'outputs'/'result-0.mp4'))
            with self.assertRaisesRegex(ValueError, '覆盖源文件'):
                validate_batch_outputs(state, ['0'])

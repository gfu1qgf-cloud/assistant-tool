"""Protected default TEMP must not receive disposable audio audition outputs."""
import io
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from app_plugins.builtin.batch_text_video import preview_cache
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.voice_effects_preview import render_audition
from app_plugins.builtin.batch_text_video.engine import _run, Cancelled
from model.AppDataStorage import ApplicationStorage, data_io

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class PreviewCacheTests(unittest.TestCase):
    def test_root_never_falls_back_to_private_temp_or_follows_a_private_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root/'private'; private.mkdir()
            guard = SimpleNamespace(volume=SimpleNamespace(root=private), check=lambda *a, **k: None)
            storage = ApplicationStorage(private, guard=guard)
            with patch('model.AppDataStorage.application_storage', return_value=storage), patch('model.MediaStorage.application_storage', return_value=storage), patch.dict(os.environ, {'LOCALAPPDATA': str(root/'local')}):
                self.assertFalse(preview_cache.cache_root('audition').is_relative_to(private))
                with preview_cache.audition_temporary() as folder:
                    self.assertFalse(Path(folder).is_relative_to(private))
                self.assertFalse(Path(folder).exists())
                # Redirect lookup is compiled at construction. Build a real
                # redirected storage rather than mutating its diagnostic list.
                redirected=ApplicationStorage(private,guard=guard,redirects=[(root/'local',private)])
                with patch('model.AppDataStorage.application_storage',return_value=redirected), \
                     patch('model.MediaStorage.application_storage',return_value=redirected):
                    with self.assertRaisesRegex(ValueError, '加密卷'): preview_cache.cache_root('thumbnails')

    @unittest.skipUnless(FFMPEG, 'Real FFmpeg required')
    def test_real_audio_audition_survives_private_default_temp_and_cleans_both_ab_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root/'private'; private.mkdir()
            (private/'Temp').mkdir(); source = private/'voice.wav'
            with wave.open(str(source), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(48000)
                audio.writeframes(b'\x10\x00'*24000)
            original = source.read_bytes()
            guard = SimpleNamespace(volume=SimpleNamespace(root=private), check=lambda *a, **k: None)
            storage = ApplicationStorage(private, guard=guard)
            settings = fresh_state()['settings']; settings.update(voice_fx_preset='church', use_voice=True)
            outputs = []
            def checked_run(args, *a, **kw):
                outputs.append(Path(args[-1])); self.assertFalse(Path(args[-1]).is_relative_to(private))
                return _run(args, *a, **kw)
            with patch('model.AppDataStorage.application_storage', return_value=storage), \
                 patch('model.MediaStorage.application_storage', return_value=storage), \
                 patch.object(tempfile, 'tempdir', str(private/'Temp')), \
                 patch.dict(os.environ, {'LOCALAPPDATA': str(root/'local')}), \
                 patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=checked_run):
                for processed in (False, True):
                    result = render_audition(source, settings, processed=processed, ffmpeg=FFMPEG)
                    with wave.open(io.BytesIO(result['audio'])) as stream:
                        self.assertGreater(stream.getnframes(), 24000)
                with patch('app_plugins.builtin.batch_text_video.engine._run', side_effect=Cancelled('cancel')):
                    with self.assertRaises(Cancelled): render_audition(source, settings, ffmpeg=FFMPEG)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(outputs); self.assertTrue(all(not p.parent.exists() for p in outputs))
            self.assertFalse(list(private.rglob('audition.wav')))
            self.assertFalse(list((root/'local').rglob('voice-space-audition-*')))


if __name__ == '__main__': unittest.main()

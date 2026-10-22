"""Real audio tests: timing, decay, dry voice, stereo, ducking and source safety."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.voice_effects import DEFAULTS as FX_DEFAULTS, PRESETS, normalized, active, resolved
from app_plugins.builtin.batch_text_video.voice_reverb import impulse_response, write_ir
from app_plugins.builtin.batch_text_video.mixing import audio_graph
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.engine import Cancelled
from app_plugins.builtin.batch_text_video.voice_effects_preview import render_audition

FFMPEG = os.environ.get('NODE_FFMPEG', '')
RATE = 48000


def write_wave(path, data):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(data.shape[1]); stream.setsampwidth(2); stream.setframerate(RATE)
        stream.writeframes(np.rint(np.clip(data, -1, 1)*32767).astype('<i2').tobytes())


class VoiceEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.voice = self.root/'voice.wav'; signal = np.zeros((RATE, 2)); signal[4800] = .4; write_wave(self.voice, signal)

    def settings(self, preset='church', **kwargs): return {**DEFAULTS, 'voice_fx_preset': preset, **kwargs}

    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args], capture_output=True, timeout=45)
        if result.returncode: self.fail(result.stderr.decode('utf-8', 'replace')[-4000:])
        return result.stdout

    def native(self, settings, duration=3., *, voice=True, music=None, filter_threads=None):
        args = ['-i', str(self.voice)] if voice else []
        voice_index = 0 if voice else None; music_index = None
        if music:
            music_index = len(args)//2; args += ['-i', str(music)]
        ir_index = None
        if voice and active(settings):
            target = write_ir(self.root/'response.wav', settings)
            ir_index = len(args)//2; args += ['-i', str(target)]
        graph, _ = audio_graph(settings, duration, voice_index, music_index, {'start': 0}, voice_ir_index=ir_index)
        threading_args = ['-filter_complex_threads', str(filter_threads)] if filter_threads is not None else []
        data = self.ffmpeg([*threading_args, *args, '-filter_complex', ';'.join(graph), '-map', '[a]', '-t', str(duration), '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'])
        return np.frombuffer(data, '<f4').reshape(-1, 2)

    def test_old_defaults_off_invalid_values_and_zero_strength_bypass(self):
        self.assertFalse(active({})); self.assertFalse(active(self.settings(voice_fx_amount=0)))
        self.assertIsNone(impulse_response({}))
        for values in ({'voice_fx_preset': 'unknown'}, {'voice_fx_amount': float('nan')}, {'voice_fx_decay': 9},
                       {'voice_fx_predelay': 1.5}, {'voice_fx_width': True}, {'voice_fx_amount': -1}):
            with self.subTest(values=values), self.assertRaises(ValueError): normalized(values)
        self.assertEqual(resolved(self.settings(voice_fx_decay=4, voice_fx_predelay=100))['rt'], 4)

    def test_all_presets_are_deterministic_stereo_finite_and_delayed(self):
        hashes = set()
        for key in PRESETS:
            if key == 'none': continue
            p = self.settings(key); response = impulse_response(p)
            self.assertEqual(response.shape[1], 2); self.assertTrue(np.isfinite(response).all())
            delay = round(resolved(p)['delay']*RATE/1000)
            self.assertFalse(response[:delay].any()); self.assertGreater(np.max(np.abs(response[delay:])), .001)
            self.assertFalse(np.array_equal(response[:, 0], response[:, 1]))
            hashes.add(hashlib.sha256(response.tobytes()).hexdigest())
        self.assertEqual(len(hashes), len(PRESETS)-1)
        np.testing.assert_array_equal(impulse_response(self.settings()), impulse_response(self.settings()))

    def test_decay_and_width_controls_have_physical_effect_and_cancel_is_safe(self):
        narrow = impulse_response(self.settings(voice_fx_width=0)); np.testing.assert_array_equal(narrow[:, 0], narrow[:, 1])
        short, long = (impulse_response(self.settings(voice_fx_decay=v)) for v in (1, 5))
        self.assertGreater(len(long), len(short)*4)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled): write_ir(self.root/'cancelled.wav', self.settings(), cancel)
        self.assertFalse((self.root/'cancelled.wav').exists())

    def test_24_bit_ir_output_and_off_does_not_create_file(self):
        target = self.root/'space.wav'; write_ir(target, self.settings())
        with wave.open(str(target)) as stream:
            self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (2, 3, RATE))
        missing = self.root/'unused.wav'; self.assertIsNone(write_ir(missing, {})); self.assertFalse(missing.exists())

    def test_effect_never_routes_music_to_reverb_and_duck_detector_stays_dry(self):
        graph, _ = audio_graph(self.settings(), 4, 1, 2, {'start': 0}, voice_ir_index=3)
        joined = ';'.join(graph)
        self.assertIn('[voice_fx_clock]asplit=3[voice_fx_dry][voice_fx_send][voice_side]', joined)
        self.assertIn('[music_raw][voice_side]sidechaincompress', joined)
        self.assertNotIn('afir', next(value for value in graph if value.startswith('[2:a]')))
        self.assertNotIn('atempo', joined)
        no_voice = audio_graph(self.settings(), 4, music_index=0, music={'start': 0})[0]
        self.assertNotIn('afir', ';'.join(no_voice))
        with self.assertRaisesRegex(ValueError, '缺少空间响应'): audio_graph(self.settings(), 4, voice_index=0)

    def test_project_save_switch_and_old_settings_preserve_pool_progress(self):
        store = Store(self.root/'data'); state = fresh_state(); state['settings'].update(self.settings('cathedral', voice_fx_amount=72, voice_fx_decay=4.4))
        state['music'] = [dict(id='m', path=str(self.voice), cursor=9.25)]
        state = store.save(state, 0); state = store.create_profile(state, '其他项目')
        self.assertEqual(state['settings']['voice_fx_preset'], 'none')
        state = store.select_profile(state, 'default')
        self.assertEqual(state['settings']['voice_fx_preset'], 'cathedral'); self.assertEqual(state['settings']['voice_fx_decay'], 4.4)
        self.assertEqual(state['music'][0]['cursor'], 9.25)
        for key in FX_DEFAULTS: state['settings'].pop(key)
        store.path.write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(store.load()['settings']['voice_fx_preset'], 'none')

    def test_panel_presets_custom_values_and_loading_do_not_trigger_resets(self):
        from app_plugins.builtin.batch_text_video.voice_effects_ui import VoiceEffectPanel
        panel = VoiceEffectPanel(self.settings('cathedral', voice_fx_decay=6.1)); self.addCleanup(panel.deleteLater)
        preset = panel.controls['voice_fx_preset']
        preset.setCurrentIndex(preset.findData('chapel'))
        self.assertEqual(panel.controls['voice_fx_decay'].value(), 6.1)
        preset.activated.emit(preset.currentIndex())
        self.assertEqual(panel.controls['voice_fx_decay'].value(), 0)
        self.assertIn('温暖', panel.note.text())
        preset.setCurrentIndex(0); self.assertFalse(panel.controls['voice_fx_amount'].isEnabled())

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_all_presets_keep_dry_impulse_time_and_make_distinct_tails(self):
        dry = self.native(self.settings('none'), 8); hashes = set()
        for key in PRESETS:
            if key == 'none': continue
            with self.subTest(preset=key):
                wet = self.native(self.settings(key), 8)
                self.assertEqual(len(wet), RATE*8)
                self.assertEqual(np.argmax(np.abs(wet[:, 0])), 4800)
                delay = round(resolved(self.settings(key))['delay']*RATE/1000)
                np.testing.assert_allclose(wet[:4800+delay], dry[:4800+delay], atol=2e-6)
                self.assertGreater(np.sum(wet[4800+delay:]**2), .0005)
                self.assertLessEqual(np.max(np.abs(wet)), .951)
                hashes.add(hashlib.sha256(wet.tobytes()).hexdigest())
        self.assertEqual(len(hashes), len(PRESETS)-1)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_zero_strength_and_muted_voice_are_exactly_dry_and_silent(self):
        np.testing.assert_array_equal(self.native(self.settings('none')), self.native(self.settings(voice_fx_amount=0)))
        self.assertFalse(self.native(self.settings(voice_volume=0)).any())

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_sidechain_final_partial_block_stays_intact_across_thread_counts(self):
        music = self.root/'music.wav'; t = np.arange(RATE*2)/RATE
        write_wave(music, np.repeat((np.sin(2*np.pi*220*t)*.08)[:, None], 2, axis=1))
        settings = self.settings(fade_seconds=0)
        one = self.native(settings, 1., music=music, filter_threads=1)
        many = self.native(settings, 1., music=music, filter_threads=4)
        np.testing.assert_allclose(one, many, atol=1e-7)
        self.assertGreater(float(np.sqrt(np.mean(one[-2048:]**2))), .008)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_audition_ab_has_same_duration_tail_and_preserves_input(self):
        before = self.voice.read_bytes()
        dry = render_audition(self.voice, self.settings(), processed=False, ffmpeg=FFMPEG)
        wet = render_audition(self.voice, self.settings(), processed=True, ffmpeg=FFMPEG)
        self.assertEqual(dry['duration'], wet['duration']); self.assertNotEqual(dry['audio'], wet['audio'])
        with wave.open(io.BytesIO(wet['audio'])) as stream: self.assertAlmostEqual(stream.getnframes()/RATE, wet['duration'], delta=.001)
        self.assertEqual(self.voice.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, '超过'): render_audition(self.voice, self.settings(), start=3, ffmpeg=FFMPEG)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled): render_audition(self.voice, self.settings(), ffmpeg=FFMPEG, cancel=cancel)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_export_and_playback_with_music_preserve_duration_and_sources(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text, probe, resolve_tools
        background = self.root/'background.mp4'; music = self.root/'music.wav'
        overlay = self.root/'overlay.png'
        image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGBA8888); image.fill(QtGui.QColor('red')); image.save(str(overlay))
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        layers = normalize_layers([dict(id='image', kind='image', path=str(overlay))])
        self.ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=240x160:r=10:d=0.4', '-c:v', 'libx264', str(background)])
        t = np.arange(RATE*2)/RATE; write_wave(music, np.repeat((np.sin(2*np.pi*220*t)*.08)[:, None], 2, axis=1))
        original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.voice, background, music)}
        outputs = []
        for mode in ('export', 'preview', 'dry'):
            state = fresh_state(); state['settings'].update(self.settings('none' if mode == 'dry' else 'church'),
                aspect='source', use_voice=True, ffmpeg_path=FFMPEG, output_dir=str(self.root/'out'), loop_transition='none', layers=layers)
            state['music'] = [dict(id='m', path=str(music), cursor=0., last_used='', audio_key='')]
            job = dict(id='job', path=str(background), voice_path=str(self.voice), name=mode, title='', body='', task_dir='')
            state['jobs'] = [job]; output, _ = render_static_text(job, state, self.root/'private', preview=mode == 'preview')
            _, ffprobe = resolve_tools(FFMPEG)
            self.assertAlmostEqual(probe(output, ffprobe)['duration'], 1., delta=.05)
            raw = self.ffmpeg(['-i', str(output), '-vn', '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'])
            outputs.append(np.frombuffer(raw, '<f4'))
        # AAC can choose different quantisation with different video/filter
        # worker counts. Compare decoded level and waveform, not codec bits.
        self.assertEqual(outputs[0].shape, outputs[1].shape)
        self.assertGreater(float(np.corrcoef(outputs[0], outputs[1])[0, 1]), .995)
        self.assertLess(float(np.mean(abs(outputs[0]-outputs[1]))), .0003)
        self.assertGreater(float(np.mean(abs(outputs[0]-outputs[2]))), .00005)
        self.assertEqual(original, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in original})


class VoiceEffectHostTests(unittest.TestCase):
    from test_batch_review_regressions import BatchReviewRegressions as Fixture
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    wait = Fixture.wait
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_controls_roundtrip_and_undo_preserve_voice_effect(self):
        panel = self.dialog.voice_fx_panel
        preset = panel.controls['voice_fx_preset']; preset.setCurrentIndex(preset.findData('church'))
        panel.controls['voice_fx_amount'].setValue(65)
        self.dialog.flush_editor_history()
        self.assertTrue(self.dialog.persist())
        self.assertEqual(self.dialog.read_settings()['voice_fx_preset'], 'church')
        self.assertEqual(self.dialog.store.load()['settings']['voice_fx_amount'], 65)
        self.dialog.undo_edit(); self.assertEqual(self.dialog.read_settings()['voice_fx_preset'], 'none')
        self.dialog.redo_edit(); self.assertEqual(self.dialog.read_settings()['voice_fx_amount'], 65)

    def test_preview_cancellation_queued_result_and_shutdown_guard(self):
        from app_plugins.builtin.batch_text_video.voice_effects_preview import VoiceEffectPreviewWorker
        request = dict(serial=1, source='unused.wav', settings=DEFAULTS, processed=True, start=0., ffmpeg=FFMPEG)
        worker = VoiceEffectPreviewWorker(request, self.dialog)
        self.dialog.voice_fx_worker = worker
        self.assertFalse(self.plugin.can_close()[0]); self.assertTrue(worker.cancel.is_set())
        self.dialog.voice_fx_worker = None; worker.deleteLater()
        self.dialog._voice_fx_serial = 10
        with patch.object(self.dialog, '_play_voice_effect_audio') as play:
            self.dialog._voice_effect_preview_ready(dict(serial=9, processed=True, audio=b'unused', start=0, clip=1))
            play.assert_not_called()

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_current_voice_audition_latest_request_wins_without_rotations(self):
        source = Path(self.temp.name)/'voice.wav'; signal = np.zeros((RATE, 2)); signal[4800] = .2; write_wave(source, signal)
        self.dialog.controls['use_voice'].setChecked(True)
        preset = self.dialog.controls['voice_fx_preset']; preset.setCurrentIndex(preset.findData('church'))
        self.dialog.ffmpeg.setText(FFMPEG); self.dialog.edit_timer.stop()
        before = copy.deepcopy(self.dialog.state['music']); stored = source.read_bytes()
        job = dict(id='fixture', voice_path=str(source), title='', body='', name='fixture')
        with patch.object(self.dialog, 'active_entry', return_value=job), patch.object(self.dialog, '_play_voice_effect_audio') as play:
            self.dialog.audition_voice_effect(True)
            self.dialog.audition_voice_effect(False)
            self.wait(lambda: self.dialog.voice_fx_worker is None and self.dialog._voice_fx_pending is None)
            self.assertEqual(play.call_count, 1)
            self.assertIn('原声对比', self.dialog.voice_fx_panel.status.text())
            self.dialog.audition_voice_effect(True)
            self.assertFalse(self.plugin.can_close()[0])
            self.wait(lambda: self.dialog.voice_fx_worker is None)
        self.assertEqual(before, self.dialog.state['music']); self.assertEqual(source.read_bytes(), stored)

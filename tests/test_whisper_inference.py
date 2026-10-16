import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from model.WhisperInference import whisper_inference
from model.SubtitleHelper import generate_srt_whisper_only
from app_plugins.builtin.smart_video_editor.engine import (
    normalize_smart_video_editor_settings, transcribe_source,
)


class AlignmentResult:
    def merge_all_segments(self):
        return self

    def split_by_length(self, **kwargs):
        return self

    def to_srt_vtt(self, path, **kwargs):
        Path(path).write_text('1\n00:00:00,000 --> 00:00:01,000\nAmen.\n', encoding='utf-8')


class WhisperInferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'clip.mp4'
        self.source.write_bytes(b'synthetic audio for mocked recognition')
        self.settings = normalize_smart_video_editor_settings({})
        self.threads, self.releases, self.errors = [], [], []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'inference waiter failed to stop')
        self.temp.cleanup()

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def launch(self, action):
        def run():
            try:
                action()
            except Exception as error:
                self.errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def test_lazy_recognition_and_forced_alignment_do_not_overlap_or_reload_model(self):
        inside, waiting, aligned, closed = (threading.Event() for _ in range(4))
        release = self.event()
        def transcribe(*args, **kwargs):
            def segments():
                inside.set()
                try:
                    release.wait(3)
                    yield SimpleNamespace(text='Amen.', words=[])
                finally:
                    closed.set()
            return segments(), SimpleNamespace(language='sk', duration=1)
        model = SimpleNamespace(transcribe=transcribe)
        def align(*args, **kwargs):
            self.assertTrue(closed.is_set())
            self.assertIs(args[0], model)
            aligned.set()
            return AlignmentResult()
        with patch('stable_whisper.alignment.align', side_effect=align), \
             patch('stable_whisper.load_faster_whisper') as load:
            first = self.launch(lambda: transcribe_source(model, self.source, 'sk', self.root, self.settings))
            self.assertTrue(inside.wait(2))
            second = self.launch(lambda: generate_srt_whisper_only('final.mp4', 'Amen.', str(self.root / 'final.srt'),
                model=model, progress=lambda text: waiting.set() if text.startswith('等待') else None))
            self.assertTrue(waiting.wait(2))
            self.assertFalse(aligned.is_set())
            release.set()
            first.join(3)
            second.join(3)
            self.assertTrue(aligned.is_set())
            load.assert_not_called()
        self.assertEqual(self.errors, [])
        self.assertTrue((self.root / 'final.srt').exists())

    def test_waiting_for_model_is_cancelable_without_running_native_work(self):
        inside, waiting, native, canceled, done = (threading.Event() for _ in range(5))
        release = self.event()
        def owner():
            with whisper_inference():
                inside.set()
                release.wait(3)
        self.launch(owner)
        self.assertTrue(inside.wait(2))
        def waiter():
            try:
                with whisper_inference(cancelled=canceled.is_set,
                    progress=lambda text: waiting.set() if text.startswith('等待') else None):
                    native.set()
            except InterruptedError:
                done.set()
        self.launch(waiter)
        self.assertTrue(waiting.wait(2))
        canceled.set()
        self.assertTrue(done.wait(1))
        self.assertFalse(native.is_set())
        self.assertFalse(release.is_set())

    def test_cache_hit_does_not_wait_for_model(self):
        model = SimpleNamespace(transcribe=Mock(return_value=([], SimpleNamespace(language='sk', duration=1))))
        transcribe_source(model, self.source, 'sk', self.root, self.settings)
        inside, read = threading.Event(), threading.Event()
        release = self.event()
        def owner():
            with whisper_inference():
                inside.set()
                release.wait(3)
        self.launch(owner)
        self.assertTrue(inside.wait(2))
        values = []
        def cached():
            values.append(transcribe_source(None, self.source, 'sk', self.root, self.settings))
            read.set()
        self.launch(cached)
        self.assertTrue(read.wait(1))
        self.assertTrue(values[0]['from_cache'])
        self.assertEqual(model.transcribe.call_count, 1)
        self.assertEqual(self.errors, [])

    def test_iterator_error_closes_and_releases_guard(self):
        closed = threading.Event()
        def transcribe(*args, **kwargs):
            def segments():
                try:
                    yield SimpleNamespace(text='Amen.', words=[])
                    raise ValueError('synthetic lazy recognition error')
                finally:
                    closed.set()
            return segments(), SimpleNamespace(language='sk', duration=1)
        with self.assertRaisesRegex(ValueError, 'lazy recognition'):
            transcribe_source(SimpleNamespace(transcribe=transcribe), self.source, 'sk', self.root, self.settings)
        self.assertTrue(closed.is_set())
        acquired = threading.Event()
        def next_operation():
            with whisper_inference():
                acquired.set()
        self.launch(next_operation)
        self.assertTrue(acquired.wait(1))

    def test_srt_formatting_does_not_hold_model_and_original_alignment_options_remain(self):
        formatting, acquired = threading.Event(), threading.Event()
        release = self.event()
        class Result(AlignmentResult):
            def merge_all_segments(self):
                formatting.set()
                release.wait(3)
                return self
        model = object()
        with patch('stable_whisper.alignment.align', return_value=Result()) as align:
            first = self.launch(lambda: generate_srt_whisper_only('final.mp4', 'Amen.', str(self.root / 'out.srt'),
                model=model, language='sk', max_words_per_block=3, block_gap_ms=0))
            self.assertTrue(formatting.wait(2))
            def next_operation():
                with whisper_inference():
                    acquired.set()
            self.launch(next_operation)
            self.assertTrue(acquired.wait(1))
            self.assertFalse(release.is_set())
            release.set()
            first.join(3)
            self.assertIs(align.call_args.args[0], model)
            self.assertEqual(align.call_args.kwargs['language'], 'sk')
        self.assertEqual(self.errors, [])
        self.assertTrue((self.root / 'out.srt').exists())

    def test_native_completion_after_cancel_does_not_commit_alignment_file(self):
        canceled = threading.Event()
        def align(*args, **kwargs):
            canceled.set()
            return AlignmentResult()
        with patch('stable_whisper.alignment.align', side_effect=align):
            with self.assertRaises(InterruptedError):
                generate_srt_whisper_only('final.mp4', 'Amen.', str(self.root / 'canceled.srt'),
                    model=object(), cancelled=canceled.is_set)
        self.assertFalse((self.root / 'canceled.srt').exists())

    def test_lazy_model_loader_waits_inside_guard_not_during_encoding(self):
        inside, waiting, loaded, aligned = (threading.Event() for _ in range(4))
        release = self.event()
        def owner():
            with whisper_inference():
                inside.set(); release.wait(3)
        self.launch(owner); self.assertTrue(inside.wait(2))
        model = object()
        def loader():
            loaded.set(); return model
        def align(native, *args, **kwargs):
            self.assertIs(native,model); aligned.set(); return AlignmentResult()
        with patch('stable_whisper.alignment.align',side_effect=align):
            worker = self.launch(lambda: generate_srt_whisper_only('final.mp4','Amen.',str(self.root/'lazy.srt'),
                model_loader=loader,progress=lambda text: waiting.set() if text.startswith('等待') else None))
            self.assertTrue(waiting.wait(2)); self.assertFalse(loaded.is_set()); self.assertFalse(aligned.is_set())
            release.set(); worker.join(3)
        self.assertEqual(self.errors,[]); self.assertTrue(aligned.is_set())

    def test_cancel_during_model_load_does_not_begin_alignment(self):
        canceled = threading.Event()
        def loader():
            canceled.set(); return object()
        with patch('stable_whisper.alignment.align') as align:
            with self.assertRaises(InterruptedError):
                generate_srt_whisper_only('final.mp4','Amen.',str(self.root/'canceled.srt'),
                    model_loader=loader,cancelled=canceled.is_set)
            align.assert_not_called()
        self.assertFalse((self.root/'canceled.srt').exists())

    def test_model_loader_error_releases_native_guard(self):
        with self.assertRaisesRegex(ValueError,'model-load-test'):
            generate_srt_whisper_only('final.mp4','Amen.',str(self.root/'error.srt'),
                model_loader=Mock(side_effect=ValueError('model-load-test')))
        acquired = threading.Event()
        def next_operation():
            with whisper_inference(): acquired.set()
        self.launch(next_operation); self.assertTrue(acquired.wait(1))

    def test_waiters_get_fifo_turns_instead_of_whole_batch_reacquiring(self):
        inside, second_waiting, third_waiting = (threading.Event() for _ in range(3))
        release = self.event(); entered = []
        def batch():
            with whisper_inference():
                inside.set(); release.wait(3)
            with whisper_inference(): entered.append('next clip in batch')
        first = self.launch(batch); self.assertTrue(inside.wait(2))
        def waiter(label,waiting):
            with whisper_inference(progress=lambda text: waiting.set() if text.startswith('等待') else None):
                entered.append(label)
        second = self.launch(lambda: waiter('second',second_waiting)); self.assertTrue(second_waiting.wait(2))
        third = self.launch(lambda: waiter('third',third_waiting)); self.assertTrue(third_waiting.wait(2))
        release.set()
        for worker in (first,second,third): worker.join(3)
        self.assertEqual(entered,['second','third','next clip in batch'])
        self.assertEqual(self.errors,[])

    def test_canceled_ticket_does_not_block_next_waiter_and_reentrancy_remains_safe(self):
        inside, waiting, canceled, removed, next_waiting, nested = (threading.Event() for _ in range(6))
        release = self.event(); enter_nested = self.event(); acquired = threading.Event()
        def owner():
            with whisper_inference():
                inside.set(); enter_nested.wait(3)
                with whisper_inference(): nested.set()
                release.wait(3)
        first = self.launch(owner); self.assertTrue(inside.wait(2))
        def cancel_waiter():
            try:
                with whisper_inference(cancelled=canceled.is_set,
                    progress=lambda text: waiting.set() if text.startswith('等待') else None):
                    self.fail('canceled ticket ran native work')
            except InterruptedError: removed.set()
        second = self.launch(cancel_waiter); self.assertTrue(waiting.wait(2))
        def next_operation():
            with whisper_inference(progress=lambda text: next_waiting.set() if text.startswith('等待') else None):
                acquired.set()
        third = self.launch(next_operation); self.assertTrue(next_waiting.wait(2))
        canceled.set(); self.assertTrue(removed.wait(1))
        enter_nested.set(); self.assertTrue(nested.wait(1)); self.assertFalse(acquired.is_set())
        release.set()
        for worker in (first,second,third): worker.join(3)
        self.assertTrue(acquired.is_set()); self.assertEqual(self.errors,[])

    def test_failed_waiting_progress_callback_removes_ticket(self):
        inside, removed, acquired, waiting = (threading.Event() for _ in range(4))
        release = self.event()
        def owner():
            with whisper_inference(): inside.set(); release.wait(3)
        first = self.launch(owner); self.assertTrue(inside.wait(2))
        def bad_waiter():
            def report(_text): raise ValueError('synthetic progress callback failure')
            try:
                with whisper_inference(progress=report): self.fail('bad ticket ran')
            except ValueError: removed.set()
        self.launch(bad_waiter); self.assertTrue(removed.wait(1))
        def next_operation():
            with whisper_inference(progress=lambda text: waiting.set() if text.startswith('等待') else None):
                acquired.set()
        second = self.launch(next_operation); self.assertTrue(waiting.wait(2))
        release.set(); first.join(3); second.join(3)
        self.assertTrue(acquired.is_set()); self.assertEqual(self.errors,[])


if __name__ == '__main__':
    unittest.main()

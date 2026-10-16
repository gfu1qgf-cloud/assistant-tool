"""No-op means no encoder, no duplicate, and no lost delivery identity."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ResourceRef, FilterOperation, VideoTimeline, VideoSection
from model.node_workflow.media import MediaSession
from model.node_workflow.video_stream import VideoStream, EncodingSettings, unchanged_source
from app_plugins.builtin.task_delivery_workflow import process_file, processor
from model.TaskResultOrganizer import file_identity, compress_routed_batches
from model.TaskDeliveryBackground import version_of


class PassthroughTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'result' / 'sub' / 'original.mov'
        self.original.parent.mkdir(parents=True); self.original.write_bytes(b'unchanged-source')
        self.source = self.root / 'snapshot' / '0' / 'sub' / self.original.name
        self.source.parent.mkdir(parents=True); self.source.write_bytes(self.original.read_bytes())
        self.ref = ResourceRef.capture(self.source, 'video')
        self.context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)

    def test_only_proven_noop_is_skipped(self):
        with MediaSession(self.context) as session:
            value = VideoStream(self.ref)
            self.assertTrue(unchanged_source(value, self.ref, session))
            self.assertTrue(unchanged_source(replace(value, operations=(
                FilterOperation('disabled', self.ref, 'null'),)), self.ref, session))
            self.assertFalse(unchanged_source(replace(value, encoding=EncodingSettings()), self.ref, session))
            self.assertFalse(unchanged_source(replace(value, operations=(
                FilterOperation('resize', self.ref, 'scale', (('w', '1280'),)),)), self.ref, session))
            other = ResourceRef.capture(self.original, 'video')
            self.assertFalse(unchanged_source(VideoStream(other), self.ref, session))
            timeline = VideoTimeline((VideoSection(self.ref, 0, 1),), 240, 160, '20', 'test', True)
            self.assertFalse(unchanged_source(VideoStream(timeline), self.ref, session))

    def test_process_does_not_invoke_encoder_or_create_output(self):
        with patch('model.node_workflow.graph.evaluate_graph', return_value={'video': VideoStream(self.ref)}), \
             patch('model.node_workflow.render.resolve_tools', return_value=('fake-ffmpeg', 'fake-ffprobe')), \
             patch('model.node_workflow.render.render_plan') as render:
            result = process_file(self.source, self.original, self.root/'processed', {'graph': {}}, None, self.context)
        render.assert_not_called()
        self.assertEqual(result, self.source)
        self.assertFalse((self.root/'processed').exists())

    def test_processor_keeps_original_name_routing_and_no_redundant_local_copy(self):
        root = self.root/'snapshot'/'0'
        info = {'original': str(self.original), 'root': str(self.root/'result'), 'version': version_of(self.original)}
        plan = {'files': {file_identity(self.source): info}}
        with patch('app_plugins.builtin.task_delivery_workflow.process_file', return_value=self.source):
            for local in (False, True):
                callback = processor(plan, {}, None, self.context, local=local)
                result = callback(self.source, root, Path('review/admin'))
                self.assertEqual(result, self.original if local else self.source)
            callback = processor(plan, {}, None, self.context)
            batches = compress_routed_batches([(root, [self.source], Path('review/admin'))], {}, postprocessor=callback)
        self.assertEqual(batches, [(root, [self.source], Path('review/admin'))])
        self.assertFalse((self.root/'后处理结果').exists())
        self.assertFalse(getattr(self.context, 'postprocessed_paths', []))

    def test_local_noop_does_not_return_changed_original(self):
        plan = {'files': {file_identity(self.source): {'original': str(self.original),
            'root': str(self.root/'result'), 'version': version_of(self.original)}}}
        self.original.write_bytes(b'new render was completed')
        with patch('app_plugins.builtin.task_delivery_workflow.process_file', return_value=self.source), \
             self.assertRaisesRegex(ValueError, '排队后发生变化'):
            processor(plan, {}, None, self.context, local=True)(self.source, self.root/'snapshot'/'0', '')

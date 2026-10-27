import copy
import json
import os
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from model.BackgroundTasks import BackgroundTaskCanceled
from model.node_workflow.contracts import NodeDefinition, NodePort
from model.node_workflow.evaluation import GraphEvaluator
from model.node_workflow.function_library import node_function, function_definition
from model.node_workflow.graph import NodeRegistry, new_node, empty_graph, GraphError
from model.node_workflow.mutexes import named_mutex, node_mutex


class Context:
    def __init__(self, waiting=None):
        self.cancel = threading.Event()
        self.messages = []
        self.waiting = waiting

    def check_canceled(self):
        if self.cancel.is_set():
            raise BackgroundTaskCanceled('test cancellation')

    def report(self, message, percent=None):
        self.messages.append(message)
        if self.waiting and message.startswith('等待互斥锁：'):
            self.waiting.set()


class NodeMutexTests(unittest.TestCase):
    def setUp(self):
        self.name = 'synthetic.' + uuid4().hex
        self.threads, self.events, self.errors = [], [], []
        self.addCleanup(self.stop_threads)

    def event(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def stop_threads(self):
        for event in self.events:
            event.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'test worker did not stop')
        self.assertEqual(self.errors, [])

    def launch(self, action):
        def run():
            try:
                action()
            except BaseException as error:
                self.errors.append(repr(error))
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def evaluator(self, action, *, mutex=None, context=None, kind='synthetic.operation'):
        definition = NodeDefinition(kind, '测试操作', outputs=(NodePort('result', '结果', 'text'),),
            build=lambda inputs, params, ctx: {'result': action()}, execution='compute',
            mutex=self.name if mutex is None else mutex)
        registry = NodeRegistry()
        registry.register('synthetic', definition)
        record = new_node(definition)
        graph = dict(empty_graph(), nodes=[record], output_node=record['id'])
        return GraphEvaluator(graph, registry, context or Context(), {}, {}), record['id']

    def run_node(self, action, **kwargs):
        evaluator, key = self.evaluator(action, **kwargs)
        return evaluator.evaluate(key)['result']

    def test_same_name_has_one_lock_even_during_concurrent_creation(self):
        barrier = threading.Barrier(12)
        locks = []
        def lookup():
            barrier.wait(3)
            locks.append(named_mutex(self.name))
        for _ in range(12):
            self.launch(lookup)
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(len(locks), 12)
        self.assertEqual(len({id(lock) for lock in locks}), 1)

    def test_two_different_registries_with_same_name_are_mutually_exclusive(self):
        entered, release, waiting, second = [self.event() for _ in range(4)]
        def first():
            entered.set()
            self.assertTrue(release.wait(3))
            return 'one'
        self.launch(lambda: self.run_node(first))
        self.assertTrue(entered.wait(2))
        self.launch(lambda: self.run_node(lambda: second.set() or 'two', context=Context(waiting)))
        self.assertTrue(waiting.wait(2))
        self.assertFalse(second.is_set())
        release.set()
        self.assertTrue(second.wait(2))

    def test_different_names_and_unmarked_nodes_still_execute_in_parallel(self):
        entered, release, other, unmarked = [self.event() for _ in range(4)]
        def first():
            entered.set()
            release.wait(3)
            return 'one'
        self.launch(lambda: self.run_node(first))
        self.assertTrue(entered.wait(2))
        self.launch(lambda: self.run_node(lambda: other.set() or 'other', mutex=self.name + '.other'))
        self.launch(lambda: self.run_node(lambda: unmarked.set() or 'free', mutex=''))
        self.assertTrue(other.wait(2))
        self.assertTrue(unmarked.wait(2))
        self.assertFalse(release.is_set())

    def test_same_thread_nested_nodes_can_reenter_same_name(self):
        self.assertEqual(self.run_node(lambda: self.run_node(lambda: 'nested')), 'nested')

    def test_nested_release_keeps_outer_lock_owned(self):
        waiting, second = self.event(), self.event()
        with node_mutex(self.name, Context()):
            with node_mutex(self.name, Context()):
                pass
            self.launch(lambda: self.run_node(lambda: second.set() or 'two', context=Context(waiting)))
            self.assertTrue(waiting.wait(2))
            self.assertFalse(second.is_set())
        self.assertTrue(second.wait(2))

    def test_canceled_waiter_does_not_execute_or_release_another_owner(self):
        entered, release, waiting, canceled, ran = [self.event() for _ in range(5)]
        context = Context(waiting)
        def owner():
            with node_mutex(self.name, Context()):
                entered.set()
                release.wait(3)
        def waiter():
            try:
                self.run_node(lambda: ran.set() or 'wrong', context=context)
            except BackgroundTaskCanceled:
                canceled.set()
        self.launch(owner)
        self.assertTrue(entered.wait(2))
        self.launch(waiter)
        self.assertTrue(waiting.wait(2))
        context.cancel.set()
        self.assertTrue(canceled.wait(2))
        self.assertFalse(ran.is_set())
        self.assertFalse(named_mutex(self.name).acquire(blocking=False))
        release.set()

    def test_cancel_before_start_does_not_enter_build(self):
        context = Context()
        context.cancel.set()
        with self.assertRaises(BackgroundTaskCanceled):
            self.run_node(lambda: self.fail('canceled node ran'), context=context)

    def test_builder_exception_and_invalid_output_release_lock(self):
        for action in (lambda: (_ for _ in ()).throw(ValueError('synthetic failure')), lambda: 42):
            with self.assertRaises(GraphError):
                self.run_node(action)
            done = self.event()
            self.launch(lambda: self.run_node(lambda: done.set() or 'ok'))
            self.assertTrue(done.wait(2))

    def test_progress_failure_after_acquisition_releases_lock(self):
        entered, release, waiting, failed = [self.event() for _ in range(4)]
        class BrokenProgress(Context):
            def report(self, message, percent=None):
                super().report(message, percent)
                if message.startswith('已获得互斥锁：'):
                    raise ValueError('synthetic UI report error')
        def owner():
            with node_mutex(self.name, Context()):
                entered.set()
                release.wait(3)
        def waiter():
            try:
                with node_mutex(self.name, BrokenProgress(waiting)):
                    self.fail('broken progress entered body')
            except ValueError:
                failed.set()
        self.launch(owner)
        self.assertTrue(entered.wait(2))
        self.launch(waiter)
        self.assertTrue(waiting.wait(2))
        release.set()
        self.assertTrue(failed.wait(2))
        self.assertTrue(named_mutex(self.name).acquire(blocking=False))
        named_mutex(self.name).release()

    def test_keyboard_interrupt_also_releases_mutex(self):
        with self.assertRaises(KeyboardInterrupt):
            with node_mutex(self.name, Context()):
                raise KeyboardInterrupt()
        done = self.event()
        self.launch(lambda: self.run_node(lambda: done.set() or 'ok'))
        self.assertTrue(done.wait(2))

    def test_cache_hit_does_not_acquire_mutex_again(self):
        evaluator, key = self.evaluator(lambda: 'cached')
        self.assertEqual(evaluator.evaluate(key), {'result': 'cached'})
        entered, release = self.event(), self.event()
        def owner():
            with node_mutex(self.name, Context()):
                entered.set()
                release.wait(3)
        self.launch(owner)
        self.assertTrue(entered.wait(2))
        self.assertEqual(evaluator.evaluate(key), {'result': 'cached'})
        self.assertFalse(release.is_set())

    def test_upstream_computation_happens_before_downstream_lock_acquisition(self):
        upstream_ran, waiting, done = self.event(), self.event(), self.event()
        registry = NodeRegistry()
        first = NodeDefinition('synthetic.source', '上游', outputs=(NodePort('text', '文字', 'text'),),
            build=lambda i, p, c: {'text': upstream_ran.set() or 'value'})
        last = NodeDefinition('synthetic.target', '下游', inputs=(NodePort('text', '文字', 'text'),),
            outputs=(NodePort('text', '文字', 'text'),), build=lambda i, p, c: dict(i), mutex=self.name)
        registry.register('synthetic', first)
        registry.register('synthetic', last)
        a, b = new_node(first), new_node(last)
        edge = [a['id'], 'text', b['id'], 'text']
        graph = dict(empty_graph(), nodes=[a, b], connections=[edge], output_node=b['id'])
        evaluator = GraphEvaluator(graph, registry, Context(waiting), {(b['id'], 'text'): [edge]}, {})
        with node_mutex(self.name, Context()):
            self.launch(lambda: (evaluator.evaluate(b['id']), done.set()))
            self.assertTrue(upstream_ran.wait(2))
            self.assertTrue(waiting.wait(2))
            self.assertFalse(done.is_set())
        self.assertTrue(done.wait(2))

    def test_mutex_is_trusted_registration_metadata_not_saved_graph_state(self):
        evaluator, key = self.evaluator(lambda: 'value')
        record = evaluator.nodes[key]
        self.assertNotIn('mutex', record)
        self.assertNotIn(self.name, json.dumps(record))
        self.assertNotIn('mutex', record['params'])
        subset = evaluator.registry.subset(lambda owner: True)
        self.assertEqual(subset.definitions[record['kind']].mutex, self.name)

    def test_registration_rejects_non_string_or_ambiguous_names(self):
        for name in (None, 1, False, ' ', ' x', 'x ', 'a\nb', 'a\x7fb', 'x' * 121):
            with self.subTest(name=name), self.assertRaises(ValueError):
                NodeRegistry().register('synthetic', NodeDefinition('synthetic.bad', '无效', build=lambda *a: {}, mutex=name))
        self.assertIsNot(named_mutex(self.name), named_mutex(self.name.upper()))

    def test_function_node_decorator_propagates_mutex(self):
        @node_function(kind='synthetic.function', title='函数', outputs=(NodePort('text', '文字', 'text'),), mutex=self.name)
        def function():
            return 'ok'
        definition = function_definition(function)
        self.assertEqual(definition.mutex, self.name)
        registry = NodeRegistry()
        registry.register_function('synthetic', function)

    def test_actual_shared_model_node_marked_but_deferred_caption_is_not(self):
        from app_plugins.builtin.smart_video_editor.workflow_nodes import edit_node
        from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
        definition = edit_node(SimpleNamespace())
        self.assertEqual(definition.mutex, 'whisper')
        self.assertEqual(caption_node().mutex, '')


class NodeMutexCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def scene(self, orientation='horizontal'):
        from PYUI.node_graph_canvas import NodeScene
        registry = NodeRegistry()
        for kind, title, mutex in (('locked', '共享模型任务 · 有执行互斥', 'whisper'), ('free', '普通处理 · 可独立并行', '')):
            registry.register('synthetic', NodeDefinition('synthetic.' + kind, title,
                outputs=(NodePort('text', '结果', 'text'),), build=lambda *a: {'text': 'ok'}, mutex=mutex))
        nodes = [new_node(definition, (index * 300, 0)) for index, definition in enumerate(registry.definitions.values())]
        graph = dict(empty_graph(), nodes=nodes, view={'orientation': orientation})
        scene = NodeScene(graph, registry)
        self.addCleanup(scene.deleteLater)
        return scene, nodes

    def test_badge_shows_fixed_name_without_disabling_or_rewriting_node(self):
        scene, nodes = self.scene()
        before = copy.deepcopy(scene.graph)
        locked, free = [scene.nodes[node['id']] for node in nodes]
        self.assertIsNotNone(locked.mutex_badge)
        self.assertIsNone(free.mutex_badge)
        self.assertIn('whisper', locked.mutex_badge.toolTip())
        self.assertIn('不锁定参数', locked.mutex_badge.toolTip())
        self.assertTrue(locked.isEnabled())
        self.assertTrue(locked.boundingRect().contains(locked.mutex_badge.mapRectToParent(locked.mutex_badge.boundingRect())))
        scene.rebuild()
        self.assertEqual(scene.graph, before)

    def test_vertical_layout_keeps_lock_inside_title(self):
        scene, nodes = self.scene('vertical')
        node = scene.nodes[nodes[0]['id']]
        self.assertTrue(node.boundingRect().contains(node.mutex_badge.mapRectToParent(node.mutex_badge.boundingRect())))

    def test_optional_real_canvas_screenshot(self):
        target = os.environ.get('LZX_NODE_MUTEX_SCREENSHOT')
        if not target:
            return
        from qt_compat import QtCore, QtGui
        scene, nodes = self.scene()
        bounds = scene.itemsBoundingRect().adjusted(-15, -15, 15, 15)
        image = QtGui.QImage(int(bounds.width() * 1.6), int(bounds.height() * 1.6), QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor('#182230'))
        painter = QtGui.QPainter(image)
        scene.render(painter, QtCore.QRectF(0, 0, image.width(), image.height()), bounds)
        painter.end()
        self.assertTrue(image.save(target))

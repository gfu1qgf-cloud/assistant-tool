"""A running registry may contain pre-mutex definitions from older modules."""
import copy
from dataclasses import fields, make_dataclass
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.node_workflow.contracts import NodeDefinition, NodePort, ResourceRef
from model.node_workflow.evaluation import GraphEvaluator
from model.node_workflow.graph import NodeRegistry, new_node, empty_graph


# Real old shape: not a modern instance with mutex='', and no inherited default.
OldDefinition = make_dataclass('NodeDefinition',
    [(field.name, field.type, copy.copy(field)) for field in fields(NodeDefinition) if field.name != 'mutex'],
    frozen=True, namespace={key: NodeDefinition.__dict__[key]
        for key in ('input_ports', 'output_ports', 'result_ports', 'result_fields')})


def old_definition(definition):
    return OldDefinition(**{field.name: getattr(definition, field.name)
                            for field in fields(OldDefinition)})


def context(**values):
    return SimpleNamespace(check_canceled=lambda: None, report=lambda *args: None, **values)


class LegacyMutexCompatibilityTests(unittest.TestCase):
    def test_real_delivery_input_in_already_running_legacy_registry(self):
        from app_plugins.builtin.task_delivery_workflow import register
        definitions = []
        register(SimpleNamespace(register_node=definitions.append, register_workflow_type=lambda _: None))
        definition = old_definition(next(d for d in definitions if d.kind == 'task_delivery.video_input'))
        self.assertFalse(hasattr(definition, 'mutex'))
        registry = NodeRegistry()
        # The registry predates the new executor, exactly as in a live process.
        registry.definitions[definition.kind] = definition
        registry.owners[definition.kind] = 'task_delivery'
        record = new_node(definition)
        graph = dict(empty_graph(), nodes=[record], output_node=record['id'])
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'synthetic.mp4'
            source.write_bytes(b'synthetic source, no decoding')
            ctx = context(workflow_input={'video': ResourceRef.capture(source, 'video'),
                                        'path': str(source), 'task': None})
            outputs = GraphEvaluator(graph, registry, ctx, {}, {}).evaluate(record['id'])
            self.assertEqual(outputs['path'], str(source))
            self.assertFalse(outputs['has_task'])
            self.assertEqual(outputs['video'].source.path, str(source.resolve()))

    def test_legacy_registration_and_subset_do_not_require_new_optional_field(self):
        definition = old_definition(NodeDefinition('synthetic.legacy', '旧节点',
            outputs=(NodePort('text', '文字', 'text'),), build=lambda *args: {'text': 'ok'}))
        self.assertFalse(hasattr(definition, 'mutex'))
        # Simulate graph.py importing the already cached old contracts class.
        with patch('model.node_workflow.graph.NodeDefinition', OldDefinition):
            registry = NodeRegistry()
            registry.register('synthetic', definition)
            subset = registry.subset(lambda _: True)
        record = new_node(definition)
        graph = dict(empty_graph(), nodes=[record], output_node=record['id'])
        result = GraphEvaluator(graph, subset, context(), {}, {}).evaluate(record['id'])
        self.assertEqual(result, {'text': 'ok'})

    def test_legacy_canvas_definition_has_no_lock_badge_and_remains_editable(self):
        from qt_compat import QtCore, QtWidgets
        from PYUI.node_graph_canvas import NodeScene
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        definition = old_definition(NodeDefinition('synthetic.legacy', '旧节点',
            outputs=(NodePort('text', '文字', 'text'),), build=lambda *args: {'text': 'ok'}))
        registry = NodeRegistry()
        registry.definitions[definition.kind] = definition
        registry.owners[definition.kind] = 'synthetic'
        record = new_node(definition)
        graph = dict(empty_graph(), nodes=[record], output_node=record['id'])
        scene = NodeScene(graph, registry)
        self.assertIsNone(scene.nodes[record['id']].mutex_badge)
        self.assertTrue(scene.nodes[record['id']].isEnabled())
        scene.deleteLater()
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()

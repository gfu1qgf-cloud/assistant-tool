import copy
import json
from pathlib import Path
import tempfile
import unittest

from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, validate
from app_plugins.builtin.task_delivery_workflow import register, example, TYPE_ID
from types import SimpleNamespace


class NamedSavingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.library = WorkflowLibrary(Path(self.temp.name) / 'NodeWorkflows')
        self.registry = NodeRegistry(); register_core(self.registry)

    def test_incomplete_draft_can_save_without_turning_execution_validation_off(self):
        graph = empty_graph(); node = new_node(self.registry.definitions['core.import'])
        graph['nodes'] = [node]
        self.assertTrue(validate(graph, self.registry)[0])
        saved = self.library.save(graph, '我的草稿', self.registry)
        self.assertEqual(self.library.load(saved['workflow_id']), saved)
        self.assertEqual(graph, dict(empty_graph(), nodes=[node]))
        self.assertTrue(validate(saved, self.registry)[0])
        self.assertEqual(self.library.list()[0]['name'], '我的草稿')

    def test_name_not_used_as_path_and_updating_id_does_not_create_duplicate(self):
        graph = empty_graph()
        saved = self.library.save(graph, '../../标题', self.registry)
        self.assertEqual(self.library._path(saved['workflow_id']).parent, self.library.root)
        saved['view'] = {'connection_style': 'straight'}
        newer = self.library.save(saved, '改名', self.registry)
        self.assertEqual(newer['workflow_id'], saved['workflow_id'])
        self.assertEqual(len(self.library.list()), 1)
        self.assertEqual(self.library.load(saved['workflow_id'])['view']['connection_style'], 'straight')

    def test_save_copy_has_independent_identity(self):
        saved = self.library.save(empty_graph(), '原工作流', self.registry)
        other = copy.deepcopy(saved); other.pop('workflow_id')
        other = self.library.save(other, '副本', self.registry)
        self.assertNotEqual(other['workflow_id'], saved['workflow_id'])
        self.assertEqual(len(self.library.list()), 2)

    def test_legacy_delivery_filename_contract_upgrades_only_in_memory(self):
        register(SimpleNamespace(register_node=lambda n: self.registry.register('task_delivery', n),
            register_workflow_type=lambda t: self.registry.register_workflow_type('task_delivery', t)))
        graph = example(self.registry)
        graph['workflow_type_version'] = 1
        graph['nodes'][-1]['version'] = 1
        name = new_node(self.registry.definitions['core.path_name'])
        graph['nodes'].append(name)
        graph['connections'] += [[graph['nodes'][0]['id'], 'path', name['id'], 'path'],
                                  [name['id'], 'name', graph['output_node'], 'filename']]
        saved = self.library.save(graph, '旧后处理', self.registry)
        path = self.library._path(saved['workflow_id']); before = path.read_bytes()
        upgraded = self.library.load(saved['workflow_id'], self.registry)
        self.assertEqual(upgraded['workflow_type_version'], 2)
        self.assertFalse(any(edge[3] == 'filename' for edge in upgraded['connections']))
        self.assertFalse(validate(upgraded, self.registry)[0])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.library.list(TYPE_ID)), 1)
        output = self.registry.definitions['task_delivery.video_output']
        self.assertEqual(output.outputs, ())
        self.assertEqual([port.key for port in output.inputs], ['video'])
        self.assertEqual(output.terminal_result.data_type, 'video')

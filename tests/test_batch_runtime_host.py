"""Host/provider boundary and real node-inspector runtime injection."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from app_plugins.host import PluginHost
from app_plugins.builtin.batch_text_video.plugin import BatchTextVideoPlugin
from app_plugins.builtin.batch_text_video.store import fresh_state
from app_plugins.builtin.batch_text_video.workflow_pool_ui import PoolNodeEditor


class RuntimeHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.main=QtWidgets.QWidget()
        self.main.load_config=lambda:{}
        self.main.appendLog=lambda *_args,**_kwargs:None
        self.host=PluginHost(self.main)

    def tearDown(self):
        self.main.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_provider_is_main_thread_only_and_returns_independent_plain_data(self):
        guard=Mock()
        self.host._background_tasks=SimpleNamespace(_assert_main_thread=guard)
        value={'items':[{'path':'synthetic.mp4'}]}
        plugin=SimpleNamespace(plugin_id='runtime_demo',register=lambda context:None,
            workflow_runtime_context=lambda:value)
        self.host.install(plugin)
        prepared=self.host._contexts['runtime_demo'].workflow_runtime_context('runtime_demo')
        guard.assert_called_once()
        prepared['items'][0]['path']='changed'
        self.assertEqual(value['items'][0]['path'],'synthetic.mp4')
        self.host._enabled_states['runtime_demo']=False
        with self.assertRaisesRegex(ValueError,'未提供'):
            self.host.workflow_runtime_context('runtime_demo')
        self.host._enabled_states['runtime_demo']=True
        plugin.workflow_runtime_context=lambda:{'live_object':self.main}
        with self.assertRaisesRegex(TypeError,'无法提交'):
            self.host.workflow_runtime_context('runtime_demo')
        guard.side_effect=RuntimeError('not main thread')
        with self.assertRaisesRegex(RuntimeError,'not main thread'):
            self.host.workflow_runtime_context('runtime_demo')

    def test_workspace_supplies_live_project_to_pool_inspector_without_graph_edits(self):
        from PYUI.node_workspace import NodeWorkspace
        plugin=self.host.install(BatchTextVideoPlugin())
        state=fresh_state()
        state['profiles'][0]['name']='项目 A'
        state['copy_pool']=[{'id':'one','title':'t','body':'b'}]
        plugin.dialog=SimpleNamespace(state=state)
        panel=NodeWorkspace(self.host,self.main)
        panel.add_node('batch_text_video.load_pool')
        for _ in range(4):self.app.processEvents()
        node=panel.graph['nodes'][-1]
        editor=panel.inspector.widget().findChild(PoolNodeEditor)
        self.assertIsNotNone(editor)
        self.assertIn('项目 A',editor.origin.text())
        self.assertEqual(node['params'],{'pool_id':'builtin:copy'})
        state['profiles'][0]['name']='项目 B'
        editor.refresh()
        self.assertIn('项目 B',editor.origin.text())
        self.assertEqual(node['params'],{'pool_id':'builtin:copy'})
        panel.scene.clearSelection()
        panel.scene.nodes[node['id']].setSelected(True)
        for _ in range(4):self.app.processEvents()
        editor=panel.inspector.widget().findChild(PoolNodeEditor)
        self.assertIn('项目 B',editor.origin.text())
        self.assertFalse(hasattr(editor,'path'))

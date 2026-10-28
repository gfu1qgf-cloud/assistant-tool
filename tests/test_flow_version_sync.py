"""Every current-version stamp agrees; older reviewed bridges remain usable."""
import ast
import json
import os
from pathlib import Path
import re
import unittest

from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION, compatible_extension, project_block


class FlowVersionSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ.get('LZX_TEST_SOURCE_ROOT',Path(__file__).resolve().parents[1]))

    def read(self,path):return (self.root/path).read_text('utf-8')

    def test_main_builder_manifest_and_source_metadata_have_one_current_version(self):
        builder=ast.parse(self.read('tools/build_flow_bridge.py'))
        version=next(ast.literal_eval(node.value) for node in builder.body if isinstance(node,ast.Assign) and
            any(isinstance(target,ast.Name) and target.id=='BRIDGE_VERSION' for target in node.targets))
        self.assertEqual(EXTENSION_VERSION,'1.1.59');self.assertEqual(version,EXTENSION_VERSION)
        self.assertEqual(json.loads(self.read('browser_extensions/flow_bridge/manifest.json'))['version'],version)
        self.assertEqual(json.loads(self.read('browser_extensions/flow_bridge/SOURCE.json'))['bridge_version'],version)

    def test_connection_page_and_native_project_reload_expect_the_current_build(self):
        expected=re.escape(EXTENSION_VERSION)
        options=self.read('browser_extensions/flow_adapter/options.js')
        self.assertRegex(options,r"const expectedVersion = '"+expected+r"';")
        background=self.read('browser_extensions/flow_adapter/background.js')
        self.assertRegex(background,r"getManifest\(\)\.version !== '"+expected+r"'")
        self.assertRegex(background,r"profile: reply\.profile, version: '"+expected+r"'")
        self.assertEqual(self.read('browser_extensions/flow_adapter/options.js'),self.read('browser_extensions/flow_bridge/lzx-options.js'))
        self.assertEqual(self.read('browser_extensions/flow_adapter/visual.js')+'\n'+background,
            self.read('browser_extensions/flow_bridge/lzx-background.js'))

    def test_old_reviewed_extension_is_not_rejected_when_new_build_is_available(self):
        for version in ('1.1.8','1.1.22','1.1.38','1.1.39','1.1.40','1.1.41','1.1.42','1.1.43','1.1.44','1.1.45','1.1.46','1.1.47','1.1.48','1.1.49','1.1.50','1.1.51','1.1.58','1.1.59'):
            self.assertTrue(compatible_extension(version),version)
        for version in ('1.1.7','1.1.60','2.0.0'):
            self.assertFalse(compatible_extension(version),version)
        url='https://flow.google.com/project/version-fixture'
        for version in ('1.1.39','1.1.40'):
            self.assertEqual(project_block({'extension_version':version,'profile':'Profile 1',
                'saved_project':url,'project':url},{'profile_projects':{'Profile 1':url}}),'')


if __name__=='__main__':unittest.main()

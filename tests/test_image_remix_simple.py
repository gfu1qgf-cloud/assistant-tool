from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from qt_compat import QtWidgets, QtCore
from model.AppDataStorage import data_directory
from app_plugins.builtin.image_remix.planning import Planner, request_body, checked_variants
from app_plugins.builtin.image_remix.prompts import validate_spec
from app_plugins.builtin.image_remix.store import Store
from app_plugins.builtin.image_remix.client import BatchClient
from test_image_remix import spec
from test_image_remix_planning import response


class SimplePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private')
        self.job = self.store.create(spec(self.root, mode='batch', innovation=100, rewrite_enabled=True), 'identity')
        self.client = Mock(); self.planner = Planner(self.store, self.client)
    def tearDown(self): self.temp.cleanup()

    def test_new_schema_only_requests_number_and_prompt(self):
        body = request_body(self.job, self.job['planning']['chunks'][0])
        schema = body['text']['format']['schema']
        self.assertEqual(schema['required'], ['variants'])
        variant = schema['properties']['variants']['items']
        self.assertEqual(variant['required'], ['index', 'prompt'])
        self.assertNotIn('changes', variant['properties'])

    def test_minimal_prompts_accepted_at_100_without_theme_or_dimensions(self):
        rows = [{'index': i + 1, 'prompt': '新画面' + str(i)} for i in range(3)]
        self.assertEqual(len(checked_variants(self.job, self.job['planning']['chunks'][0], {'variants': rows})), 3)

    def test_saved_previously_blocked_response_restored_without_network(self):
        chunk = self.job['planning']['chunks'][0]; chunk.update(state='failed', token='saved', error='旧维度限制')
        self.job['planning']['state'] = 'failed'; self.job['status'] = 'planning_failed'
        body = response(chunk['indices'])
        value = json.loads(body['output'][0]['content'][0]['text'])
        for variant in value['variants']: variant['changes'] = [{'dimension': 'lighting', 'description': '新的光线'}]
        body['output'][0]['content'][0]['text'] = json.dumps(value)
        self.planner.receipt(self.job, 'saved', chunk).write_json(body)
        self.store.save(self.job); self.planner.retry(self.job)
        self.assertEqual(self.job['planning']['state'], 'done')
        self.assertEqual(self.job['status'], 'ready')
        self.assertTrue(all(i['prompt'] for i in self.job['items']))
        self.assertFalse(self.job['attempts']); self.assertEqual(self.client.mock_calls, [])

    def test_model_list_uses_get_only_and_separates_image_and_text(self):
        session = Mock(); session.headers = {}; reply = Mock(status_code=200)
        reply.json.return_value = {'data': [{'id':'gpt-5-mini'}, {'id':'gpt-image-2'}, {'id':'text-embedding-3-small'}, {'id':'o3'}]}
        session.request.return_value = reply
        result = BatchClient('SYNTHETIC', session=session).list_models()
        self.assertEqual(result, {'text':['gpt-5-mini','o3'], 'image':['gpt-image-2']})
        self.assertEqual(session.request.call_args.args[0], 'GET')

    def test_explicit_image_model_id_is_preserved_without_fallback(self):
        value = validate_spec(spec(self.root, model='gpt-image-2'))
        self.assertEqual(value['model'], 'gpt-image-2')


class ModelPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_refresh_preserves_typed_models_and_populates_choices(self):
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        with tempfile.TemporaryDirectory() as root:
            parent = QtWidgets.QWidget(); plugin = ImageRemixPlugin()
            plugin.context = SimpleNamespace(storage=data_directory(Path(root) / 'private'))
            dialog = RemixDialog(plugin, parent)
            dialog.rewrite_model.setCurrentText('my-text-model')
            dialog.model.setCurrentText('gpt-image-2')
            dialog.set_models({'text':['gpt-5-mini','o3'], 'image':['gpt-image-2.5-flare','gpt-image-2.5-sunburst']})
            self.assertEqual(dialog.spec()['rewrite_model'], 'my-text-model')
            self.assertEqual(dialog.spec()['model'], 'gpt-image-2')
            self.assertGreater(dialog.rewrite_model.count(), 1); self.assertGreater(dialog.model.count(), 1)
            self.assertTrue(dialog.model.isEditable())
            dialog.close(); parent.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__': unittest.main()

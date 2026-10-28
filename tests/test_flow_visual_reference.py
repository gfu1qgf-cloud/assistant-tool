"""Visual fallback stays local, follows its setting and uses frozen image bytes."""
import base64
from io import BytesIO
import tempfile
from pathlib import Path
import unittest

from app_plugins.builtin.flow_assistant.store import fingerprint
from app_plugins.builtin.flow_assistant.visual_reference import template
from app_plugins.builtin.flow_assistant.browser_options import snapshot, normalize


class VisualReferenceTests(unittest.TestCase):
    def test_template_is_small_and_bound_to_exact_frozen_bytes(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('LZXREF'+'a'*32+'.png')
            Image.new('RGB',(1000,500),(100,150,200)).save(path)
            reference={'path':str(path),'upload_id':'a'*32,'sha256':fingerprint(path)['sha256']}
            result=template(reference)
            self.assertEqual(result['upload_id'],reference['upload_id'])
            self.assertEqual(result['sha256'],reference['sha256'])
            self.assertNotIn('path',result)
            with Image.open(BytesIO(base64.b64decode(result['thumbnail'].split(',')[1]))) as image:
                self.assertEqual(image.size,(256,128))
            path.write_bytes(b'replaced')
            with self.assertRaisesRegex(ValueError,'内容已经变化'):template(reference)

    def test_switch_preserves_existing_focus_option_and_rejects_invalid_values(self):
        self.assertTrue(snapshot({})['visual_upload_fallback'])
        self.assertFalse(normalize({'focus_before_action':False})['focus_before_action'])
        self.assertFalse(normalize({'visual_upload_fallback':False})['visual_upload_fallback'])
        with self.assertRaises(ValueError):normalize({'visual_upload_fallback':'true'})

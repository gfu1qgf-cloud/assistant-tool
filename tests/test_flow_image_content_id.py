"""Permanent byte IDs coexist with unique attempts and old page scripts."""
import json
from pathlib import Path
import unittest

from qt_compat import QtWidgets
import test_flow_reference_image as fixtures
import test_flow_setup as setup
from flow_reference_fixtures import upload_receipt
from app_plugins.builtin.flow_assistant.reference_image import freeze_reference, verified_receipt, supports_image_id
from app_plugins.builtin.flow_assistant.store import fingerprint, QueueStore


class ImageContentIdTests(unittest.TestCase):
    setUpClass=fixtures.ReferenceImageTests.__dict__['setUpClass']
    setUp=fixtures.ReferenceImageTests.setUp
    service=fixtures.ReferenceImageTests.service
    jobs=fixtures.ReferenceImageTests.jobs
    client=fixtures.ReferenceImageTests.client
    upload=fixtures.ReferenceImageTests.upload
    request=fixtures.ReferenceImageTests.request
    actions=fixtures.ReferenceImageTests.actions
    ui=setup.SetupTests.ui

    def test_id_survives_rename_restart_and_attempt_change_without_sharing_upload_tokens(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0]
        row['attempt']='a'*32
        first=freeze_reference(row,service.downloads_root,include_image_id=True)
        renamed=self.target/'renamed.png';self.image.rename(renamed)
        restored=QueueStore(self.namespace).get(row['id'])
        self.assertEqual(restored['payload']['image_identity']['sha256'],fingerprint(renamed)['sha256'])
        row['payload']['image']=str(renamed);row['attempt']='b'*32
        next_ref=freeze_reference(row,service.downloads_root,include_image_id=True)
        self.assertEqual(first['sha256'],next_ref['sha256'])
        self.assertIn(first['sha256'],first['filename']);self.assertIn(first['sha256'],next_ref['filename'])
        self.assertNotEqual(first['upload_id'],next_ref['upload_id']);self.assertNotEqual(first['filename'],next_ref['filename'])
        self.assertEqual(Path(first['path']).read_bytes(),Path(next_ref['path']).read_bytes())

    def test_same_name_changed_bytes_changes_id_and_old_snapshot_cannot_upload_them(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0];row['attempt']='a'*32
        first=freeze_reference(row,service.downloads_root,include_image_id=True)
        self.image.write_bytes(b'other image with same filename')
        self.assertNotEqual(fingerprint(self.image)['sha256'],first['sha256'])
        row['attempt']='b'*32
        with self.assertRaisesRegex(ValueError,'变化'):freeze_reference(row,service.downloads_root,include_image_id=True)

    def test_new_receipt_binds_hash_filename_and_attempt_together(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0];row['attempt']='a'*32
        reference=freeze_reference(row,service.downloads_root,include_image_id=True)
        receipt={**reference,'media_id':'exact-media','verified':True}
        self.assertTrue(verified_receipt(reference,receipt))
        for changed in ('sha256','upload_id'):
            wrong={**reference,changed:'b'*(64 if changed=='sha256' else 32)}
            self.assertFalse(verified_receipt(wrong,{**wrong,'media_id':'wrong-media','verified':True}))
        self.assertFalse(verified_receipt(reference,{**receipt,'upload_id':'b'*32,'reused':True}))

    def test_old_pages_use_legacy_name_even_if_extension_version_is_current(self):
        service=self.service();row,client=self.upload(service)
        self.assertTrue(row['remote']['upload_reference']['filename'].startswith('LZXREF'))
        self.assertFalse(supports_image_id(client))
        service.pending.clear();client['reference_sha256_names']=True
        self.assertTrue(service.start_upload(service.store.get(row['id'])))
        request=[v for v in self.actions(client) if v['action']=='upload_images'][-1]
        ref=request['params']['reference'];self.assertEqual(ref,row['remote']['upload_reference'])
        self.assertEqual(ref['upload_id'],row['attempt'])
        service.pending.clear();service.store.update(row['id'],'uploading',attempt='c'*32)
        self.assertTrue(service.start_upload(service.store.get(row['id'])))
        request=[v for v in self.actions(client) if v['action']=='upload_images'][-1]
        ref=request['params']['reference'];self.assertIn(row['payload']['image_identity']['sha256'],ref['filename'])
        self.assertEqual(ref['upload_id'],'c'*32)
        request_id=self.request(service,'upload_images')
        reply={'request_id':request_id,'success':True,'data':upload_receipt(service,row['id'])}
        service._response(client,reply);service._response(client,reply)
        runs=[item for item in self.actions(client) if item['action']=='run_tasks']
        self.assertEqual(len(runs),1)
        self.assertEqual(runs[0]['params']['tasks'][0]['reference']['filename'],ref['filename'])

    def test_document_receipt_can_enable_then_remove_optional_name_support(self):
        service=self.service();client=self.client(service)
        for incoming,expected in ((True,True),(None,False),('true',False),(True,True)):
            service._snapshot(client,{'url':client['saved_project'],'page_instance':'page','reference_sha256_names':incoming})
            self.assertEqual(supports_image_id(client),expected)
        service._snapshot(client,{'url':setup.SECOND,'page_instance':'foreign','reference_sha256_names':True})
        self.assertFalse(supports_image_id(client))
        service._message(client['socket'],json.dumps({'type':'page_status','url':client['saved_project'],
            'page_instance':'right-page','reference_sha256_names':True}))
        self.assertTrue(supports_image_id(client))
        service._message(client['socket'],json.dumps({'type':'page_status','url':client['saved_project'],'page_instance':'older-script'}))
        self.assertFalse(supports_image_id(client))

    def test_invalid_hash_is_rejected_before_creating_a_reference(self):
        service=self.service();service.store.enqueue(self.jobs());row=service.store.rows()[0];row['attempt']='a'*32
        row['payload']['image_identity']['sha256']='../not-a-hash'
        with self.assertRaisesRegex(ValueError,'编号无效'):freeze_reference(row,service.downloads_root,include_image_id=True)

    def test_copy_id_and_tooltip_use_existing_saved_hash_and_deduplicate_clips(self):
        dialog,_,_=self.ui();service=self.service();service.store.enqueue(self.jobs(count=2));rows=service.store.rows()
        dialog.update_state({'jobs':rows,'clients':[],'paused':True,'preferences':{},'spaces':[]})
        for index in range(dialog.queue.rowCount()):
            item=dialog.queue.item(index,1)
            if item:self.assertIn(rows[0]['payload']['image_identity']['sha256'],item.toolTip())
        dialog.selected_ids=lambda:[row['id'] for row in rows]
        self.assertIn('复制图片固定编号（SHA-256）',[a.text() for a in dialog.queue_menu_button.menu().actions()])
        dialog.queue_tools.copy_image_id()
        self.assertEqual(QtWidgets.QApplication.clipboard().text(),rows[0]['payload']['image_identity']['sha256'])
        QtWidgets.QApplication.clipboard().clear()


if __name__=='__main__':unittest.main()

"""Task-scoped flagged moves with real filesystem operations and fake receipts."""
import copy
from concurrent.futures import Future
from pathlib import Path
import unittest
from unittest.mock import patch

import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.media_identity import owns_download
from app_plugins.builtin.flow_assistant.suspicious_download import preserve, recover, output_path
from app_plugins.builtin.flow_assistant.credit_packs import active_count

INFO={'duration':10.,'video':{'width':720,'height':1280},'audio':True}


class ManualPool:
    def __init__(self):self.work=[]
    def submit(self,fn,*args,**kwargs):
        future=Future();self.work.append((future,fn,args,kwargs));return future
    def run(self):
        future,fn,args,kwargs=self.work.pop(0)
        try:future.set_result(fn(*args,**kwargs))
        except Exception as error:future.set_exception(error)
    def shutdown(self,**_):pass


class SuspiciousDownloadTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    bound_row=fixtures.TransferTests.bound_row
    actions=fixtures.TransferTests.actions
    ui=fixtures.TransferTests.ui

    def ready(self):
        service=self.service();row,client=self.bound_row(service,status='download_parked')
        service.pool.shutdown(wait=True);service.pool=ManualPool()
        source=Path(service.downloads_root)/'FlowBridge'/row['attempt']/'result.mp4'
        source.parent.mkdir(parents=True);source.write_bytes(b'\x00\x00\x00\x20ftypisom'+b'\x00'*120)
        event={'batch_id':row['attempt'],'event_id':'local-download','data':{'state':'complete','downloadId':9,
            'filename':str(source),'download_path':service.download_name(row),'taskId':'task','submissionId':'submission','mediaId':'unverified-media'}}
        self.addCleanup(patch.stopall)
        patch('model.node_workflow.render.probe',return_value=INFO).start()
        patch('model.node_workflow.render.resolve_tools',return_value=('test-only','test-only')).start()
        return service,row,client,source,event

    def finish(self,service):
        while service.pool.work:service.pool.run()

    def test_completed_unverified_video_is_moved_with_exact_prefix_and_not_completed(self):
        service,row,client,source,event=self.ready();original=copy.deepcopy(row)
        service._download(client,event)
        self.assertEqual(active_count(service.store.rows()),0)
        self.assertIn(row['id'],service.archiving);self.assertTrue(source.exists())
        service.pool.run()
        copied=service.store.get(row['id']);destination=Path(output_path(copied))
        self.assertEqual(destination.name,'【存疑】'+Path(row['payload']['output']).name)
        self.assertEqual(destination.parent,Path(row['payload']['directory']))
        self.assertTrue(destination.exists());self.assertTrue(source.exists())
        self.assertEqual(copied['remote']['suspicious_delivery']['state'],'copied')
        self.assertEqual(copied['status'],'needs_review')
        service.pool.run();saved=service.store.get(row['id'])
        self.assertFalse(source.exists());self.assertEqual(saved['remote']['suspicious_delivery']['state'],'moved')
        self.assertEqual(saved['remote']['bindings'],original['remote']['bindings'])
        self.assertEqual(saved['payload'],original['payload']);self.assertEqual(saved['attempt'],original['attempt'])
        self.assertFalse(owns_download(saved,event['data']));self.assertEqual(saved['status'],'needs_review')
        self.assertFalse(service.archiving);self.assertFalse(self.actions(client)[-1].get('action')=='run_tasks')
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],0)

    def test_replayed_completion_does_not_move_again_or_request_download(self):
        service,row,client,source,event=self.ready();service._download(client,event);self.finish(service)
        before=service.store.get(row['id']);service._download(client,event);recover(service)
        self.assertEqual(service.store.get(row['id']),before);self.assertFalse(service.pool.work)
        self.assertEqual(len(list(self.target.glob('【存疑】*.mp4'))),1)
        self.assertNotIn('retry_download',[a.get('action') for a in self.actions(client)])

    def test_existing_normal_and_flagged_files_are_not_overwritten(self):
        service,row,client,source,event=self.ready()
        normal=Path(row['payload']['output']);normal.write_bytes(b'normal must survive')
        flagged=normal.with_name('【存疑】'+normal.name);flagged.write_bytes(b'other flagged must survive')
        service._download(client,event);self.finish(service)
        self.assertEqual(normal.read_bytes(),b'normal must survive');self.assertEqual(flagged.read_bytes(),b'other flagged must survive')
        saved=Path(output_path(service.store.get(row['id'])))
        self.assertNotEqual(saved,flagged);self.assertTrue(saved.name.startswith('【存疑】'));self.assertFalse(source.exists())

    def test_html_and_failed_video_probe_never_remove_original(self):
        for invalid in ('html','probe'):
            with self.subTest(invalid=invalid):
                self.setUp();service,row,client,source,event=self.ready()
                if invalid=='html':source.write_bytes(b'<html>failed response</html>'*20)
                else:patch('model.node_workflow.render.probe',side_effect=ValueError('invalid video')).start()
                service._download(client,event);self.finish(service)
                current=service.store.get(row['id'])
                self.assertEqual(current['status'],'download_parked');self.assertTrue(source.exists())
                self.assertEqual(current['remote']['suspicious_delivery']['state'],'failed')
                self.assertIn('保存失败',current['error']);self.assertFalse(list(self.target.glob('【存疑】*.mp4')))
                self.assertEqual(active_count(service.store.rows()),0)
                patch.stopall()

    def test_wrong_attempt_other_browser_path_or_partial_file_is_not_moved(self):
        for mode in ('attempt','browser','path','partial'):
            with self.subTest(mode=mode):
                self.setUp();service,row,client,source,event=self.ready()
                if mode=='attempt':event['batch_id']='b'*32
                if mode=='browser':client={**client,'id':'another-browser'}
                if mode=='path':event['data']['filename']=str(self.image)
                if mode=='partial':event['data']['state']='in_progress'
                service._download(client,event)
                self.assertFalse(service.pool.work);self.assertTrue(source.exists())

    def test_legacy_parked_download_is_recovered_locally_without_browser_connection(self):
        service,row,client,source,event=self.ready();service.clients.clear()
        service.store.update(row['id'],'download_parked',remote={**row['remote'],'unverified_download':event['data']})
        recover(service);self.finish(service)
        saved=service.store.get(row['id'])
        self.assertEqual(saved['status'],'needs_review');self.assertFalse(source.exists());self.assertTrue(Path(output_path(saved)).exists())
        self.assertFalse(service.pending)

    def test_restart_after_copy_can_finish_move_without_recopying(self):
        service,row,client,source,event=self.ready();service._download(client,event);service.pool.run()
        saved=service.store.get(row['id']);destination=Path(output_path(saved));before=destination.stat().st_mtime_ns
        service.pool.work.clear();service.archiving.clear();recover(service);self.finish(service)
        self.assertFalse(source.exists());self.assertEqual(destination.stat().st_mtime_ns,before)
        self.assertEqual(service.store.get(row['id'])['remote']['suspicious_delivery']['state'],'moved')

    def test_changed_original_after_durable_copy_is_preserved(self):
        service,row,client,source,event=self.ready();service._download(client,event);service.pool.run()
        source.write_bytes(b'new unrelated bytes');service.pool.run()
        saved=service.store.get(row['id']);self.assertTrue(source.exists())
        self.assertTrue(Path(output_path(saved)).is_file());self.assertIn('清理未完成',saved['error'])
        self.assertFalse(service.archiving)

    def test_deleted_or_reset_row_does_not_adopt_old_callback_or_remove_original(self):
        service,row,client,source,event=self.ready();service._download(client,event)
        service.store.update(row['id'],'queued',attempt='b'*32,remote={})
        service.pool.run()
        self.assertEqual(service.store.get(row['id'])['status'],'queued');self.assertTrue(source.exists())
        self.assertFalse(service.pool.work)

    def test_copy_failure_can_be_retried_without_redownloading_or_generating(self):
        service,row,client,source,event=self.ready()
        with patch('app_plugins.builtin.flow_assistant.suspicious_download.archive_download',side_effect=OSError('disk unavailable')):
            service._download(client,event);self.finish(service)
        self.assertTrue(source.exists())
        service.execute({'action':'retry_download_selected','ids':[row['id']]});self.finish(service)
        self.assertFalse(source.exists());self.assertEqual(service.store.get(row['id'])['status'],'needs_review')
        self.assertNotIn('retry_download',[a.get('action') for a in self.actions(client)])
        self.assertNotIn('run_tasks',[a.get('action') for a in self.actions(client)])

    def test_queue_and_recent_output_use_flagged_path(self):
        service,row,client,source,event=self.ready();service._download(client,event);self.finish(service)
        saved=service.store.get(row['id']);dialog,launcher,commands=self.ui();dialog.update_state({'jobs':[saved],'paused':True,'clients':[]})
        output=Path(output_path(saved))
        items=[dialog.queue.item(i,4) for i in range(dialog.queue.rowCount()) if dialog.queue.item(i,4)]
        self.assertTrue(any(output.name in item.text() and str(output)==item.toolTip() for item in items))
        dialog.manual.recent.update_rows([saved]);dialog.manual.recent.update_outputs()
        self.assertEqual(dialog.manual.recent.outputs.currentText(),output.name)
        with patch.object(dialog,'selected_job',return_value=saved),patch.object(dialog.manual.recent,'open_row') as play:
            dialog.open_output();play.assert_called_once_with(saved)


if __name__=='__main__':unittest.main()

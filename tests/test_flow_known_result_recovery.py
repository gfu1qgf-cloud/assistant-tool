"""Obtain a URL for an owned media ID without waiting for a manual download."""
import copy
import json
import unittest
from unittest.mock import patch
import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.result_download import unique_result


class KnownResultRecoveryTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    bound_row=fixtures.TransferTests.bound_row
    request=fixtures.TransferTests.request
    actions=fixtures.TransferTests.actions

    def waiting(self):
        service=self.service();row,client=self.bound_row(service,finished=False)
        remote=copy.deepcopy(row['remote']);binding=remote['bindings'][0]
        binding.update(readyIds=[],urls={})
        service.store.update(row['id'],'download_failed',remote=remote)
        return service,service.store.get(row['id']),client

    def event(self,service,row,download=41,**updates):
        return {'batch_id':row['attempt'],'event_id':'complete-'+str(download),'data':{
            'state':'complete','downloadId':download,'taskId':'task','submissionId':'submission','mediaId':'media',
            'download_path':service.download_name(row),**updates}}

    def test_known_id_actively_refreshes_url_instead_of_consuming_snapshot_probes(self):
        service,row,client=self.waiting();service.retry_download(row,automatic=True)
        request=next(a for a in self.actions(client) if a.get('action')=='retry_download')
        self.assertEqual(request['params']['restore']['media_id'],'media')
        self.assertTrue(request['params']['restore']['refresh_url'])
        self.assertFalse(any(a.get('action')=='run_tasks' for a in self.actions(client)))
        self.assertFalse(service.store.get(row['id'])['remote']['bindings'][0]['readyIds'])

    def test_completed_chrome_receipt_confirms_readiness_before_archiving(self):
        service,row,client=self.waiting();service.retry_download(row)
        key=self.request(service,'retry_download')
        service._response(client,{'request_id':key,'success':True,'data':{'downloadId':41}})
        with patch.object(service,'archive') as archive:service._download(client,self.event(service,row))
        archive.assert_called_once_with(row['id'])
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'][0]['readyIds'],['media'])

    def test_completion_before_start_ack_is_replayed_after_exact_download_id_returns(self):
        service,row,client=self.waiting();service.retry_download(row);key=self.request(service,'retry_download')
        with patch.object(service,'archive') as archive:
            service._download(client,self.event(service,row));archive.assert_not_called()
            service._response(client,{'request_id':key,'success':True,'data':{'downloadId':41}})
            archive.assert_called_once_with(row['id'])

    def test_foreign_or_stale_download_cannot_be_promoted(self):
        for changes in ({'downloadId':42},{'mediaId':'foreign'},{'submissionId':'old'},{'taskId':'other'}):
            with self.subTest(changes=changes):
                self.setUp();service,row,client=self.waiting();service.retry_download(row)
                service._response(client,{'request_id':self.request(service,'retry_download'),'success':True,'data':{'downloadId':41}})
                with patch.object(service,'archive') as archive:service._download(client,self.event(service,row,**changes))
                archive.assert_not_called()
                self.assertFalse(service.store.get(row['id'])['remote']['bindings'][0]['readyIds'])

    def test_ambiguous_or_non_generation_bindings_keep_the_existing_probe_path(self):
        service,row,client=self.waiting();remote=copy.deepcopy(row['remote']);remote['bindings'][0]['mediaIds']=['one','two']
        service.store.update(row['id'],row['status'],remote=remote)
        self.assertIsNone(unique_result(remote));service.retry_download(service.store.get(row['id']))
        self.assertFalse(any(a.get('action')=='retry_download' for a in self.actions(client)))
        self.assertTrue(any(a.get('action')=='snapshot' for a in self.actions(client)))


if __name__=='__main__':unittest.main()

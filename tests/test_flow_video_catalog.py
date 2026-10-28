"""Explicit video corrections, asynchronous refresh and download-only effects."""
import copy
import json
import time
from unittest.mock import patch
import unittest

import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.media_identity import generated_bindings, owns_download
from app_plugins.builtin.flow_assistant.video_catalog import manual_binding
from app_plugins.builtin.flow_assistant.video_catalog_ui import VideoCatalogDialog
from qt_compat import QtWidgets

PROJECT='https://flow.google.com/project/manual-catalog'


class VideoCatalogTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    bound_row=fixtures.TransferTests.bound_row
    ui=fixtures.TransferTests.ui
    actions=fixtures.TransferTests.actions

    def ready(self):
        service=self.service();row,client=self.bound_row(service, status='download_parked')
        client.update(project=PROJECT,saved_project=PROJECT)
        remote=copy.deepcopy(row['remote']);remote['page']=PROJECT
        service.store.update(row['id'],row['status'],remote=remote)
        self.events=[];service.video_catalog_result.connect(self.events.append)
        return service,service.store.get(row['id']),client

    def load(self,service,row,client,request='window',videos=None):
        service.execute({'action':'list_project_videos','job':row['id'],'request_id':request})
        pending=next(k for k,p in service.pending.items() if p.get('catalog',{}).get('request_id')==request)
        service._response(client,{'request_id':pending,'success':True,'data':{'project':PROJECT,'partial':True,
            'videos':videos if videos is not None else [{'media_id':'chosen-video','prompt':'Example spoken dialogue','duration':'10秒'}]}})
        return self.events[-1]

    def bind(self,service,row,listing,**changes):
        service.execute({'action':'bind_download_video','job':row['id'],'request_id':'binding',
            'catalog':listing['catalog'],'key':listing['videos'][0]['key'],'confirmed':True,**changes})

    def test_listing_is_readonly_and_does_not_acquire_a_slot_or_change_receipts(self):
        service,row,client=self.ready();before=copy.deepcopy(row)
        result=self.load(service,row,client)
        self.assertTrue(result['success']);self.assertEqual(result['videos'][0]['media_id'],'chosen-video')
        self.assertEqual(service.store.get(row['id']),before)
        self.assertFalse(service.pending)
        self.assertEqual([v['action'] for v in self.actions(client)],['list_project_videos'])
        self.assertNotIn('foreground_token',self.actions(client)[0])

    def test_explicit_different_video_preserves_original_receipt_and_never_generates(self):
        service,row,client=self.ready();listing=self.load(service,row,client)
        before=copy.deepcopy(row);self.bind(service,row,listing)
        current=service.store.get(row['id'])
        self.assertEqual(current['remote']['bindings'],before['remote']['bindings'])
        self.assertEqual(generated_bindings(current['remote']),generated_bindings(before['remote']))
        self.assertEqual(current['attempt'],before['attempt']);self.assertEqual(current['browser'],before['browser'])
        self.assertEqual(current['payload'],before['payload'])
        request=self.actions(client)[-1]
        self.assertEqual(request['action'],'retry_download')
        self.assertEqual(request['params']['restore']['media_id'],'chosen-video')
        self.assertEqual(request['params']['restore']['bindings'][0]['source'],'manual_selection')
        self.assertNotIn('video_page',request['params']['restore'])
        self.assertNotIn('run_tasks',[a['action'] for a in self.actions(client)])
        self.assertEqual(current['status'],'downloading')
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],0)

    def test_missing_or_unverified_original_receipt_can_be_manually_corrected(self):
        for mode in ('missing','unverified'):
            with self.subTest(mode=mode):
                self.setUp();service,row,client=self.ready();remote=copy.deepcopy(row['remote'])
                if mode=='missing':remote.pop('bindings');remote.pop('upstream_task')
                else:remote['bindings'][0]['source']='dom_discovery'
                service.store.update(row['id'],row['status'],remote=remote)
                listing=self.load(service,row,client);self.bind(service,row,listing)
                self.assertEqual(service.store.get(row['id'])['status'],'downloading')
                self.assertEqual(self.actions(client)[-1]['action'],'retry_download')

    def test_automatic_download_without_ownership_still_rejected(self):
        service,row,client=self.ready();remote=copy.deepcopy(row['remote']);remote['bindings'][0]['source']='dom_discovery'
        service.store.update(row['id'],'download_failed',remote=remote)
        service.retry_download(service.store.get(row['id']))
        self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        self.assertFalse(self.actions(client))

    def test_manual_download_only_accepts_selected_media_and_matching_attempt(self):
        service,row,client=self.ready();listing=self.load(service,row,client);self.bind(service,row,listing)
        current=service.store.get(row['id']);binding=manual_binding(current)
        data={'mediaId':'chosen-video','taskId':binding['taskId'],'submissionId':binding['submissionId']}
        self.assertTrue(owns_download(current,data))
        self.assertFalse(owns_download(current,{**data,'mediaId':'media'}))
        self.assertFalse(owns_download(current,{**data,'submissionId':'submission'}))
        self.assertFalse(owns_download({**current,'attempt':'b'*32},data))

    def test_refresh_invalidates_previous_selection_and_ignores_older_responses(self):
        service,row,client=self.ready();old=self.load(service,row,client,'first')
        service.execute({'action':'list_project_videos','job':row['id'],'request_id':'second'})
        self.bind(service,row,old)
        self.assertFalse(self.events[-1]['success']);self.assertIn('刷新',self.events[-1]['error'])
        self.assertEqual(service.store.get(row['id'])['status'],'download_parked')
        new_pending=next(k for k,p in service.pending.items() if p['action']=='list_project_videos')
        service._response(client,{'request_id':new_pending,'success':True,'data':{'project':PROJECT,'videos':[{'media_id':'new'}]}})
        self.assertEqual(self.events[-1]['videos'][0]['media_id'],'new')
        count=len(self.events)
        from app_plugins.builtin.flow_assistant.video_catalog import response
        response(service,client,{'catalog':{'request_id':'first','job':row['id']}},{'success':True})
        self.assertEqual(len(self.events),count)

    def test_invalid_selection_expiry_account_switch_reset_and_no_confirmation_do_not_bind(self):
        for mode in ('key','expired','account','attempt','unconfirmed'):
            with self.subTest(mode=mode):
                self.setUp();service,row,client=self.ready();listing=self.load(service,row,client);changes={}
                if mode=='key':changes['key']='forged-key'
                if mode=='expired':service.video_catalogs[listing['catalog']]['expires_at']=0
                if mode=='account':client['account']='another@example.test'
                if mode=='attempt':service.store.update(row['id'],'download_parked',attempt='b'*32)
                if mode=='unconfirmed':changes['confirmed']=False
                self.bind(service,row,listing,**changes)
                self.assertFalse(self.events[-1]['success'])
                self.assertNotIn('manual_download_selection',service.store.get(row['id'])['remote'])
                self.assertNotIn('retry_download',[a['action'] for a in self.actions(client)])

    def test_new_ui_action_fails_clearly_on_old_extension_or_disconnection(self):
        for mode in ('old','offline','project'):
            with self.subTest(mode=mode):
                self.setUp();service,row,client=self.ready()
                if mode=='old':client['extension_version']='1.1.52'
                if mode=='offline':service.clients.clear()
                if mode=='project':client['saved_project']='https://flow.google.com/project/other'
                service.execute({'action':'list_project_videos','job':row['id'],'request_id':'window'})
                self.assertFalse(self.events[-1]['success']);self.assertFalse(self.actions(client))
                self.assertEqual(service.store.get(row['id']),row)

    def test_completion_and_late_original_events_cannot_overwrite_manual_choice(self):
        service,row,client=self.ready();listing=self.load(service,row,client);self.bind(service,row,listing)
        chosen=service.store.get(row['id']);binding=manual_binding(chosen)
        service._snapshot(client,{'batch_id':row['attempt'],'running':False,'tasks':[{'taskId':'task',
            'download_path':service.download_name(row),'status':['failed'],'mediaBindings':row['remote']['bindings']}]})
        self.assertEqual(service.store.get(row['id']),chosen)
        service._download(client,{'batch_id':row['attempt'],'event_id':'old','data':{
            'download_path':service.download_name(row),'mediaId':'media','taskId':'task','submissionId':'submission','state':'complete','downloadId':2}})
        self.assertEqual(service.store.get(row['id']),chosen)
        with patch.object(service,'archive') as archive:
            service._download(client,{'batch_id':row['attempt'],'event_id':'selected','data':{
                'download_path':service.download_name(row),'mediaId':'chosen-video','taskId':binding['taskId'],
                'submissionId':binding['submissionId'],'state':'complete','downloadId':9,'filename':'safe.mp4'}})
            archive.assert_called_once_with(row['id'])

    def test_new_video_list_can_be_empty_and_filters_malformed_or_duplicate_entries(self):
        service,row,client=self.ready()
        result=self.load(service,row,client,videos=[{'media_id':'valid','thumbnail':'https://evil.test/x'},
            {'media_id':'valid'}, {'media_id':'../bad'},{'media_id':False}])
        self.assertEqual(len(result['videos']),1);self.assertEqual(result['videos'][0]['thumbnail'],'')
        result=self.load(service,row,client,'empty',videos=[])
        self.assertEqual(result['videos'],[])

    def test_list_order_matches_flow_even_when_ids_or_prompts_would_sort_differently(self):
        service,row,client=self.ready()
        result=self.load(service,row,client,videos=[{'media_id':'z-newest','prompt':'Z'},
            {'media_id':'m-middle','prompt':'A'},{'media_id':'a-oldest','prompt':'M'}])
        self.assertEqual([v['media_id'] for v in result['videos']],['z-newest','m-middle','a-oldest'])

    def test_manual_binding_survives_restart_and_only_download_retries_without_old_receipts(self):
        service,row,client=self.ready();remote=copy.deepcopy(row['remote'])
        remote.pop('bindings');remote.pop('upstream_task')
        service.store.update(row['id'],row['status'],remote=remote)
        listing=self.load(service,row,client);self.bind(service,row,listing)
        selected=copy.deepcopy(service.store.get(row['id'])['remote']['manual_download_selection'])
        service.pending.clear();service.store.recover()
        recovered=service.store.get(row['id'])
        self.assertEqual(recovered['status'],'download_failed')
        self.assertEqual(recovered['remote']['manual_download_selection'],selected)
        service.retry_download(recovered)
        self.assertEqual(self.actions(client)[-1]['params']['restore']['manual_selection'],selected)
        self.assertNotIn('run_tasks',[a['action'] for a in self.actions(client)])

    def test_original_page_status_does_not_stop_a_manual_download_retry(self):
        service,row,client=self.ready();listing=self.load(service,row,client);self.bind(service,row,listing)
        service.pending.clear();service.store.update(row['id'],'download_failed')
        before=copy.deepcopy(service.store.get(row['id']))
        service._status(client,{'batch_id':row['attempt'],'status':'failed','message':'Original page failed'})
        self.assertEqual(service.store.get(row['id']),before)

    def test_original_page_can_be_saved_as_video_detail_url(self):
        service,row,client=self.ready();remote=copy.deepcopy(row['remote']);remote['page']=PROJECT+'/edit/original'
        service.store.update(row['id'],row['status'],remote=remote)
        result=self.load(service,row,client)
        self.assertTrue(result['success'])

    def test_picker_refresh_search_and_explicit_confirmation_send_only_opaque_keys(self):
        service,row,client=self.ready();dialog,launcher,commands=self.ui()
        picker=VideoCatalogDialog(dialog,row);self.addCleanup(picker.close)
        picker.refresh();request=commands[-1]
        self.assertEqual(request['action'],'list_project_videos');self.assertFalse(picker.use_button.isEnabled())
        picker.receive({'request_id':request['request_id'],'job':row['id'],'success':True,'catalog':'token',
            'videos':[{'key':'key1','media_id':'one','prompt':'First text'},{'key':'key2','media_id':'two','prompt':'Second text'}]})
        self.assertFalse(picker.use_button.isEnabled())
        picker.table.selectRow(1);self.assertTrue(picker.use_button.isEnabled())
        self.assertIn('Second text',picker.detail.toPlainText())
        picker.search.setText('First');self.assertFalse(picker.use_button.isEnabled())
        picker.search.clear();picker.table.selectRow(1);picker.bind()
        self.assertEqual(commands[-1]['action'],'bind_download_video');self.assertEqual(commands[-1]['key'],'key2')
        self.assertTrue(commands[-1]['confirmed']);self.assertNotIn('media_id',commands[-1])
        picker.timeout();self.assertTrue(picker.refresh_button.isEnabled())
        picker.refresh();self.assertEqual(picker.table.rowCount(),0);self.assertFalse(picker.use_button.isEnabled())

    def test_inventory_timeout_does_not_fail_task_or_keep_pending_request(self):
        service,row,client=self.ready()
        service.execute({'action':'list_project_videos','job':row['id'],'request_id':'slow'})
        key=next(iter(service.pending));service.pending[key]['at']=time.time()-21
        with patch('model.OfflineMode.is_offline',return_value=False):service._tick()
        self.assertNotIn(key,service.pending);self.assertEqual(service.store.get(row['id']),row)
        self.assertFalse(self.events[-1]['success']);self.assertIn('超时',self.events[-1]['error'])

    def test_queue_entry_service_reply_and_confirm_button_are_connected(self):
        service,row,client=self.ready();dialog,launcher,commands=self.ui()
        dialog.plugin.command.connect(service.execute)
        service.video_catalog_result.connect(dialog.plugin._video_catalog_result)
        with patch.object(dialog,'selected_job',return_value=row):dialog.queue_tools.choose_download_video()
        picker=dialog.queue_tools.video_dialog
        self.assertIsNotNone(picker)
        request=next(k for k,p in service.pending.items() if p['action']=='list_project_videos')
        service._response(client,{'request_id':request,'success':True,'data':{'project':PROJECT,
            'videos':[{'media_id':'selected-in-window','prompt':'Example'}]}})
        self.assertEqual(picker.table.rowCount(),1);self.assertFalse(picker.use_button.isEnabled())
        picker.table.selectRow(0);picker.use_button.click()
        self.assertIsNone(dialog.queue_tools.video_dialog)
        self.assertEqual(self.actions(client)[-1]['params']['restore']['media_id'],'selected-in-window')
        self.assertEqual(service.store.get(row['id'])['status'],'downloading')
        self.assertNotIn('run_tasks',[a['action'] for a in self.actions(client)])


if __name__=='__main__':unittest.main()

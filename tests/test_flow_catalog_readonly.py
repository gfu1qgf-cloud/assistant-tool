"""Reproduce lifecycle changes between opening a picker and its refresh."""
import copy
import unittest
from unittest.mock import patch

import test_flow_video_catalog as fixtures
from app_plugins.builtin.flow_assistant.suspicious_download import output_path


class CatalogReadonlyTests(fixtures.VideoCatalogTests):
    # Inherit fixture helpers but not the old suite a second time.
    def test_readonly_inventory_does_not_require_a_download_failure_status(self):
        for state in ('uncertain','generating','downloading','needs_review','completed','preparing','cancelled'):
            with self.subTest(state=state):
                self.setUp();service,row,client=self.ready();service.store.update(row['id'],state)
                before=copy.deepcopy(service.store.get(row['id']))
                result=self.load(service,row,client)
                self.assertTrue(result['success']);self.assertEqual(len(result['videos']),1)
                self.assertEqual(service.store.get(row['id']),before)
                self.assertNotIn('retry_download',[a['action'] for a in self.actions(client)])
                self.assertNotIn('run_tasks',[a['action'] for a in self.actions(client)])

    def test_status_changes_after_open_and_during_list_read_keep_the_videos(self):
        service,row,client=self.ready()
        service.execute({'action':'list_project_videos','job':row['id'],'request_id':'race'})
        key=next(k for k,p in service.pending.items() if p['action']=='list_project_videos')
        service.store.update(row['id'],'downloading')
        service._response(client,{'request_id':key,'success':True,'data':{'project':fixtures.PROJECT,
            'videos':[{'media_id':'visible-video','prompt':'Visible on Flow'}]}})
        result=self.events[-1]
        self.assertTrue(result['success']);self.assertEqual(result['videos'][0]['media_id'],'visible-video')
        self.assertFalse(result['can_bind']);self.assertIn('下载',result['binding_reason'])
        self.assertEqual(service.store.get(row['id'])['status'],'downloading')

    def test_viewing_during_archive_and_while_account_unknown_does_not_start_credit_probes(self):
        service,row,client=self.ready();service.archiving.add(row['id']);client['account']=''
        result=self.load(service,row,client)
        self.assertTrue(result['success']);self.assertTrue(result['videos']);self.assertFalse(result['can_bind'])
        self.assertEqual([a['action'] for a in self.actions(client)],['list_project_videos'])
        service.archiving.clear();result=self.load(service,row,client,'unknown-account')
        self.assertTrue(result['videos']);self.assertFalse(result['can_bind']);self.assertIn('账号',result['binding_reason'])

    def test_old_current_page_readiness_does_not_override_saved_project_for_reading(self):
        service,row,client=self.ready();client['project']='https://flow.google.com/'
        result=self.load(service,row,client)
        self.assertTrue(result['success']);self.assertTrue(result['videos'])

    def test_pending_download_does_not_prevent_inventory_but_still_prevents_rebinding(self):
        service,row,client=self.ready();service.request(client,'retry_download',{},row['id'])
        result=self.load(service,row,client);self.assertTrue(result['videos']);self.assertFalse(result['can_bind'])
        before=copy.deepcopy(service.store.get(row['id']));self.bind(service,row,result)
        self.assertFalse(self.events[-1]['success']);self.assertIn('下载',self.events[-1]['error'])
        self.assertEqual(service.store.get(row['id']),before)

    def test_uncertain_generated_and_failed_rows_accept_explicit_download_only_correction(self):
        for state in ('uncertain','generating','failed'):
            with self.subTest(state=state):
                self.setUp();service,row,client=self.ready();service.store.update(row['id'],state)
                listing=self.load(service,row,client);self.assertTrue(listing['can_bind'])
                self.bind(service,row,listing)
                self.assertEqual(service.store.get(row['id'])['status'],'downloading')
                self.assertEqual(self.actions(client)[-1]['action'],'retry_download')
                self.assertNotIn('run_tasks',[a['action'] for a in self.actions(client)])

    def test_live_submission_or_completed_output_cannot_be_overwritten(self):
        for state in ('submitting','completed'):
            with self.subTest(state=state):
                self.setUp();service,row,client=self.ready();service.store.update(row['id'],state)
                listing=self.load(service,row,client);before=copy.deepcopy(service.store.get(row['id']))
                self.assertTrue(listing['videos']);self.assertFalse(listing['can_bind']);self.bind(service,row,listing)
                self.assertFalse(self.events[-1]['success']);self.assertEqual(service.store.get(row['id']),before)

    def test_pending_submit_ack_cannot_be_rebound_even_if_ui_already_says_generating(self):
        service,row,client=self.ready();service.store.update(row['id'],'generating')
        service.request(client,'run_tasks',{},row['id'])
        listing=self.load(service,row,client);self.assertTrue(listing['videos']);self.assertFalse(listing['can_bind'])
        before=copy.deepcopy(service.store.get(row['id']));self.bind(service,row,listing)
        self.assertFalse(self.events[-1]['success']);self.assertEqual(service.store.get(row['id']),before)

    def test_flagged_copy_can_be_manually_corrected_without_hiding_the_new_result(self):
        service,row,client=self.ready();remote=copy.deepcopy(row['remote'])
        remote['suspicious_delivery']={'state':'moved','output':str(self.target/'【存疑】old.mp4')}
        service.store.update(row['id'],'needs_review',remote=remote)
        listing=self.load(service,row,client);self.assertTrue(listing['can_bind']);self.bind(service,row,listing)
        current=service.store.get(row['id']);self.assertEqual(current['status'],'downloading')
        self.assertEqual(current['remote']['suspicious_delivery']['state'],'superseded')
        self.assertEqual(output_path(current),current['payload']['output'])
        self.assertEqual(current['remote']['bindings'],row['remote']['bindings'])

    def test_missing_attempt_can_still_list_without_claiming_a_new_generation_attempt(self):
        service,row,client=self.ready();service.store.update(row['id'],'download_parked',attempt='')
        listing=self.load(service,row,client);self.assertTrue(listing['videos']);self.assertFalse(listing['can_bind'])
        self.assertIn('批次',listing['binding_reason']);self.assertEqual(service.store.get(row['id'])['attempt'],'')

    def test_changed_attempt_keeps_results_visible_but_requires_a_fresh_inventory(self):
        service,row,client=self.ready();service.execute({'action':'list_project_videos','job':row['id'],'request_id':'attempt-race'})
        key=next(k for k,p in service.pending.items() if p['action']=='list_project_videos')
        service.store.update(row['id'],'download_parked',attempt='b'*32)
        service._response(client,{'request_id':key,'success':True,'data':{'project':fixtures.PROJECT,'videos':[{'media_id':'still-visible'}]}})
        listing=self.events[-1];self.assertTrue(listing['videos']);self.assertFalse(listing['can_bind'])
        self.bind(service,row,listing);self.assertFalse(self.events[-1]['success']);self.assertIn('刷新',self.events[-1]['error'])

    def test_window_shows_returned_videos_even_when_binding_is_temporarily_disabled(self):
        service,row,client=self.ready();dialog,launcher,commands=self.ui()
        from app_plugins.builtin.flow_assistant.video_catalog_ui import VideoCatalogDialog
        picker=VideoCatalogDialog(dialog,row);self.addCleanup(picker.close);picker.refresh()
        picker.receive({'success':True,'request_id':commands[-1]['request_id'],'job':row['id'],'catalog':'read-only',
            'videos':[{'key':'one','media_id':'shown','prompt':'Expected video'}], 'can_bind':False,
            'binding_reason':'正在下载；列表可查看。'})
        self.assertEqual(picker.table.rowCount(),1);picker.table.selectRow(0)
        self.assertFalse(picker.use_button.isEnabled());self.assertTrue(picker.refresh_button.isEnabled())
        self.assertIn('正在下载',picker.note.text());self.assertIn('Expected video',picker.detail.toPlainText())


# Reuse only the fixture methods; inherited production regression tests are
# already run under VideoCatalogTests, so keep this module's test count focused.
for name in list(fixtures.VideoCatalogTests.__dict__):
    if name.startswith('test_') and name not in CatalogReadonlyTests.__dict__:
        setattr(CatalogReadonlyTests,name,None)

if __name__=='__main__':unittest.main()

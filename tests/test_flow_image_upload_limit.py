"""Image-wide terminal upload failures, counted separately from verification."""
import json
import time
import unittest
from unittest.mock import patch

import test_flow_upload_stall_exit as fixtures
from app_plugins.builtin.flow_assistant import image_upload_limit as limits
from app_plugins.builtin.flow_assistant import scheduler_recovery,ui_mutex
from app_plugins.builtin.flow_assistant.store import QueueStore,choose_client
from app_plugins.builtin.flow_assistant.ui_permission_recovery import recover as permit_retry

ACTUAL_ERROR='本条参考图上传未确认：图片上传持续无进展，尚未确认本次图片的素材编号'
VERIFY_ERROR='此图此前已发起上传，本次仅核对原素材，未重复拖放：图片上传达到最长等待时间，尚未确认本次图片的素材编号'


class ImageUploadLimitTests(unittest.TestCase):
    setUpClass=fixtures.UploadStallExitTests.__dict__['setUpClass']
    setUp=fixtures.UploadStallExitTests.setUp
    service=fixtures.UploadStallExitTests.service
    browser=fixtures.UploadStallExitTests.browser
    client=fixtures.UploadStallExitTests.client
    jobs=fixtures.UploadStallExitTests.jobs
    ready=fixtures.UploadStallExitTests.ready
    messages=fixtures.UploadStallExitTests.messages
    uploads=fixtures.UploadStallExitTests.uploads
    uploading=fixtures.UploadStallExitTests.uploading

    def progress(self,service,client,row,phase):
        request=next(k for k,p in service.pending.items() if p.get('job')==row['id'] and p['action']=='upload_images' and not p.get('retired'))
        service._message(client['socket'],json.dumps({'type':'reference_progress','request_id':request,
            'upload_id':row['attempt'],'url':client['saved_project'],'phase':phase,'page_instance':'synthetic'}))
        return request

    def fail(self,service,client,row,at,number,error=ACTUAL_ERROR,phase='uploading'):
        with patch('time.time',return_value=at+number*181):
            if not any(p.get('job')==row['id'] and p['action']=='upload_images' for p in service.pending.values()):
                service.start_upload(service.store.get(row['id']),retry=False)
            row=service.store.get(row['id']);request=self.progress(service,client,row,phase)
            service._response(client,{'request_id':request,'success':False,'code':'FLOW_REFERENCE_NOT_CONFIRMED','error':error})
        return service.store.get(row['id'])

    def test_fifth_failed_upload_abandons_all_same_image_clips_and_preserves_paid_work(self):
        service,client,row,at=self.uploading()
        service.store.enqueue(self.jobs(count=5,duration=8,budget=0))
        paid=service.store.rows()[-1];paid_attempt='f'*32
        service.store.reserve(paid['id'],paid_attempt,12,'fixed_rule')
        service.store.update(paid['id'],'generating',attempt=paid_attempt,browser='paid-browser',remote={
            'account':'paid@example.test','profile':'Paid','generation_response':{'taskId':'original-paid'}})
        paid_before=service.store.get(paid['id'])
        other=self.target/'other.png';other.write_bytes(b'another image')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0));other_row=service.store.rows()[-1]
        for number in range(4):
            current=self.fail(service,client,row,at,number)
            self.assertEqual(len(service.preferences[limits.SETTING][limits.image_key(row)]['failed']),number+1)
            self.assertNotIn('image_upload_abandoned',current['remote'])
        final=self.fail(service,client,row,at,4)
        same=[r for r in service.store.rows() if limits.image_key(r)==limits.image_key(row) and r['id']!=paid['id']]
        self.assertTrue(all(r['status']=='failed' for r in same))
        self.assertTrue(all(r['remote']['image_upload_abandoned']['failures']==5 for r in same))
        self.assertIn('同图未提交任务已跳过',final['error'])
        self.assertEqual(service.store.get(paid['id']),paid_before)
        self.assertTrue(service.store.attempt_charged(paid_attempt))
        self.assertEqual(service.store.get(other_row['id'])['status'],'queued')
        self.assertFalse(client.get('blocked'));self.assertFalse(service.preferences.get('browser_blocks'))
        selected=choose_client([client],[r for r in service.store.rows() if r['id']!=paid['id']],4,other_row['payload'],service.preferences,12)
        self.assertIs(selected,client,'the rejected image must not disable its healthy account')
        snapshots=[];service.changed.connect(snapshots.append);service.publish()
        visible=next(r for r in snapshots[-1]['jobs'] if r['id']==row['id'])
        self.assertEqual(visible['status_label'],'图片已放弃 · 同图任务已跳过')
        self.assertEqual(visible['wait_reason'],limits.NOTE)

    def test_verification_only_retries_never_increment(self):
        service,client,row,at=self.uploading()
        for number in range(8):
            self.fail(service,client,row,at,number,error=VERIFY_ERROR,phase='verifying_upload')
        self.assertFalse(service.preferences.get(limits.SETTING))
        self.assertNotIn('image_upload_abandoned',service.store.get(row['id'])['remote'])

    def test_one_failed_upload_cannot_be_counted_twice_by_rechecks_or_deadline(self):
        service,client,row,at=self.uploading();row=self.fail(service,client,row,at,0)
        for _ in range(20):
            limits.remember_failure(service,row,VERIFY_ERROR)
            limits.remember_failure(service,row,ACTUAL_ERROR)
            limits.remember_failure(service,row,expired=True)
        self.assertEqual(len(service.preferences[limits.SETTING][limits.image_key(row)]['failed']),1)

    def test_known_no_drag_and_ui_denial_do_not_count_as_image_failures(self):
        service,client,row,at=self.uploading()
        row=self.fail(service,client,row,at,0,error='本条参考图上传未确认：拖放失败：No current window')
        self.assertEqual(limits.remember_failure(service,row,expired=True),0)
        self.assertFalse(service.preferences.get(limits.SETTING))
        with patch('time.time',return_value=at+181):
            service.start_upload(row,retry=False);row=service.store.get(row['id']);request=self.progress(service,client,row,'uploading')
            service.pending.pop(request)
            permit_retry(service,client,row,'upload_images',{'request_id':request,
                'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'前台 UI 操作互斥申请未完成：rejected'})
        queued=service.store.get(row['id'])
        self.assertFalse(queued['remote']['upload_cycle'].get('upload_attempt'))
        self.assertEqual(limits.remember_failure(service,queued,expired=True),0)

    def test_three_minute_unconfirmed_upload_counts_once_before_next_real_round(self):
        service,client,row,at=self.uploading()
        with patch('time.time',return_value=at):self.progress(service,client,row,'uploading')
        service.pending.clear();row=service.store.get(row['id'])
        with patch('time.time',return_value=at+180):service.redrag_due_upload(row)
        current=service.store.get(row['id'])
        self.assertEqual(len(service.preferences[limits.SETTING][limits.image_key(row)]['failed']),1)
        self.assertFalse(current['remote']['upload_cycle'].get('upload_attempt'))
        self.assertNotIn('image_upload_abandoned',current['remote'])

    def test_limit_survives_restart_and_blocks_new_same_image_records_without_upload(self):
        service,client,row,at=self.uploading()
        for number in range(5):self.fail(service,client,row,at,number)
        persisted=QueueStore(self.namespace).settings()
        self.assertEqual(len(persisted[limits.SETTING][limits.image_key(row)]['failed']),5)
        service.store.enqueue(self.jobs(count=3,duration=8,budget=0));new=service.store.rows()[-3:]
        before=len(self.uploads(client));self.assertTrue(limits.watch(service))
        self.assertTrue(all(service.store.get(r['id'])['status']=='failed' for r in new))
        self.assertEqual(len(self.uploads(client)),before)
        with patch('time.time',return_value=at+9999):
            service.watch_transfer_retries();scheduler_recovery.recover(service)
        self.assertTrue(all(r['status']=='failed' for r in service.store.rows()))
        self.assertEqual(len(self.uploads(client)),before)

    def test_explicit_task_reset_allows_a_new_image_trial_and_success_clears_old_limit(self):
        service,client,row,at=self.uploading()
        for number in range(5):self.fail(service,client,row,at,number)
        old=service.store.get(row['id'])
        versions={row['id']:{k:old[k] for k in ('attempt','browser','status')}}
        self.assertTrue(service.reset_tasks({'ids':[row['id']],'versions':versions}))
        reset=service.store.get(row['id']);self.assertFalse(limits.blocked(service,reset))
        service.store.update(row['id'],'uploading',attempt='e'*32,browser=client['id'],remote={
            'account':client['account'],'profile':client['profile'],'cost':12})
        service.pending.clear();service.start_upload(service.store.get(row['id']))
        current=service.store.get(row['id']);self.progress(service,client,current,'uploading')
        self.progress(service,client,service.store.get(row['id']),'mounting')
        self.assertNotIn(limits.image_key(row),service.preferences.get(limits.SETTING,{}))

    def test_cancel_same_image_prepare_only_and_keep_other_windows_lock(self):
        service,client,row,at=self.uploading()
        for number in range(4):self.fail(service,client,row,at,number)
        service.preferences['browser_options']={'ui_mutex':True,'focus_before_action':False,'visual_upload_fallback':False}
        client['capabilities'].append('ui_mutex_v1')
        bad=self.ready(service,'Bad image other browser');bad['capabilities'].append('ui_mutex_v1')
        good=self.ready(service,'Good image browser');good['capabilities'].append('ui_mutex_v1')
        service.store.enqueue(self.jobs(duration=8,budget=0));bad_row=service.store.rows()[-1]
        service.store.update(bad_row['id'],'preparing',attempt='b'*32,browser=bad['id'],remote={'account':bad['account'],'profile':bad['profile']})
        service.request(bad,'prepare',job=bad_row['id'])
        other=self.target/'good.png';other.write_bytes(b'good reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0));good_row=service.store.rows()[-1]
        service.store.update(good_row['id'],'preparing',attempt='c'*32,browser=good['id'],remote={'account':good['account'],'profile':good['profile']})
        service.request(good,'prepare',job=good_row['id'])
        ui_mutex.receive(service,good,{'type':'ui_mutex_request','token':'d'*32,'operation':'prepare','url':good['saved_project']})
        self.fail(service,client,row,at,4)
        self.assertEqual(service.store.get(bad_row['id'])['status'],'failed')
        self.assertEqual(service.store.get(good_row['id'])['status'],'preparing')
        self.assertEqual(ui_mutex.state(service)['owner']['client'],good['id'])
        self.assertTrue(any(p.get('job')==bad_row['id'] and p.get('retired') for p in service.pending.values()))

    def test_same_image_failures_accumulate_across_browsers(self):
        service,one,row,at=self.uploading();two=self.ready(service,'Second browser')
        service.store.enqueue(self.jobs(duration=8,budget=0));second=service.store.rows()[-1]
        service.store.update(second['id'],'uploading',attempt='b'*32,browser=two['id'],remote={
            'account':two['account'],'profile':two['profile'],'cost':12})
        for number in range(5):
            self.fail(service,two if number%2 else one,second if number%2 else row,at,number)
        self.assertTrue(all(r['status']=='failed' for r in service.store.rows()))
        self.assertEqual(len(service.preferences[limits.SETTING][limits.image_key(row)]['failed']),5)

    def test_confirmed_image_resets_the_failed_upload_streak(self):
        service,client,row,at=self.uploading()
        for number in range(4):self.fail(service,client,row,at,number)
        with patch('time.time',return_value=at+4*181):
            service.start_upload(service.store.get(row['id']),retry=False)
            self.progress(service,client,service.store.get(row['id']),'uploading')
            self.progress(service,client,service.store.get(row['id']),'mounting')
        self.assertNotIn(limits.image_key(row),service.preferences.get(limits.SETTING,{}))
        self.assertNotIn('image_upload_abandoned',service.store.get(row['id'])['remote'])

    def test_pending_round_survives_json_roundtrip_without_resetting_its_clock(self):
        service,client,row,at=self.uploading()
        with patch('time.time',return_value=at):self.progress(service,client,row,'uploading')
        saved=service.store.get(row['id']);original=saved['remote']['upload_cycle']
        service.upload_cycles={}
        with patch('time.time',return_value=at+120):restored=service.upload_cycle(saved,client)
        self.assertEqual(restored['at'],at)
        self.assertEqual(restored['upload_attempt'],original['upload_attempt'])

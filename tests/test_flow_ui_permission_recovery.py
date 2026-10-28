"""Pre-permit contention keeps retrying; real faults and paid ambiguity do not."""
import copy
import time
import unittest
from unittest.mock import patch
import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant.ui_permission_recovery import PREFIX,recover,recover_legacy
from app_plugins.builtin.flow_assistant.credit_policy import debit_balance


class PermissionRecoveryTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    bound_row=fixtures.TransferTests.bound_row

    def row(self):
        service=self.service();service.store.enqueue(self.jobs(duration=6,budget=0));client=self.client(service)
        row=service.store.rows()[0]
        service.store.update(row['id'],'preparing',attempt='a'*32,browser=client['id'],remote={'account':client['account'],'profile':client['profile']})
        return service,service.store.get(row['id']),client

    def test_prepare_rejection_automatically_requeues_without_quarantining_account(self):
        service,row,client=self.row();service.request(client,'prepare',job=row['id'])
        request=next(iter(service.pending))
        service._response(client,{'request_id':request,'success':False,'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'前台 UI 操作互斥申请未完成：rejected'})
        current=service.store.get(row['id'])
        self.assertEqual(current['status'],'queued');self.assertEqual(current['remote']['ui_permission_retry']['count'],1)
        self.assertFalse(service.preferences.get('browser_blocks'));self.assertFalse(client.get('blocked'))

    def test_preclick_submit_denial_returns_only_unused_reservation_and_is_idempotent(self):
        service,row,client=self.row();service.store.reserve(row['id'],row['attempt'],10,'rule')
        service.store.update(row['id'],'submitting');row=service.store.get(row['id'])
        debit_balance(client,service.clients.values(),10);service.credit_debits[client['account']]=10
        message={'code':'FLOW_UI_MUTEX_UNAVAILABLE'}
        self.assertTrue(recover(service,client,row,'run_tasks',message))
        self.assertEqual(client['credits'],50);self.assertEqual(service.credit_debits[client['account']],0)
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'],0)
        self.assertFalse(recover(service,client,row,'run_tasks',message));self.assertEqual(client['credits'],50)

    def test_rejection_keeps_retrying_past_the_old_limit_without_consuming_fault_budget(self):
        service,row,client=self.row()
        for count in range(1,16):
            current=service.store.get(row['id'])
            service.store.update(row['id'],'preparing',attempt=f'{count:032x}',browser=client['id'])
            self.assertTrue(recover(service,client,service.store.get(row['id']),'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE'}))
            updated=service.store.get(row['id'])
            self.assertEqual(updated['status'],'queued');self.assertEqual(updated['browser'],'');self.assertEqual(updated['attempt'],'')
            self.assertEqual(updated['remote']['ui_permission_retry']['wait_count'],count)
            self.assertEqual(updated['remote']['ui_permission_retry']['fault_count'],0)
            self.assertEqual(updated['remote']['prepare_retry']['count'],0)
            self.assertGreaterEqual(updated['remote']['ui_permission_retry']['next_at']-updated['remote']['ui_permission_retry']['at'],1)
        self.assertFalse(service.safety_paused)

    def test_real_foreground_errors_and_active_phase_expiry_still_have_a_limit(self):
        for message in ({'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'前台 UI 操作互斥申请未完成：无法确认原项目文档'},
                        {'code':'FLOW_UI_MUTEX_EXPIRED','error':'操作中权限已经失效'}):
            with self.subTest(message=message):
                self.setUp();service,row,client=self.row()
                for count in range(1,5):
                    service.store.update(row['id'],'preparing',attempt=f'{count:032x}',browser=client['id'])
                    self.assertTrue(recover(service,client,service.store.get(row['id']),'prepare',message))
                current=service.store.get(row['id']);self.assertEqual(current['status'],'failed')
                self.assertEqual(current['remote']['ui_permission_retry']['fault_count'],4)
                self.assertIn(message['error'],current['error']);self.assertFalse(service.safety_paused)

    def test_waits_do_not_use_a_later_real_window_faults_retry_budget(self):
        service,row,client=self.row()
        for count in range(1,10):
            service.store.update(row['id'],'preparing',attempt=f'{count:032x}',browser=client['id'])
            recover(service,client,service.store.get(row['id']),'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'expired'})
        service.store.update(row['id'],'preparing',attempt='f'*32,browser=client['id'])
        recover(service,client,service.store.get(row['id']),'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'窗口激活失败'})
        current=service.store.get(row['id']);self.assertEqual(current['status'],'queued')
        self.assertEqual(current['remote']['ui_permission_retry']['fault_count'],1)

    def test_wait_recovery_removes_only_stale_waits_and_keeps_other_windows_owner(self):
        from app_plugins.builtin.flow_assistant import ui_mutex
        service,row,client=self.row();other=self.client(service,'Profile 9')
        service.preferences['browser_options']={'ui_mutex':True,'focus_before_action':False}
        for c in (client,other):c['capabilities']=['ui_mutex_v1']
        service.request(other,'prepare')
        ui_mutex.receive(service,other,{'type':'ui_mutex_request','token':'b'*32,'operation':'prepare','url':other['saved_project']})
        service.request(client,'prepare',job=row['id'])
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':'c'*32,'operation':'prepare','url':client['saved_project']})
        request=next(k for k,p in service.pending.items() if p['client']==client['id'])
        service._response(client,{'request_id':request,'success':False,'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':PREFIX+'：rejected'})
        self.assertEqual(ui_mutex.state(service)['owner']['client'],other['id'])
        self.assertFalse(any(e['client']==client['id'] for e in ui_mutex.state(service)['queue']))

    def test_wait_reason_is_visible_for_a_pack_and_cancellation_is_not_revived(self):
        from app_plugins.builtin.flow_assistant.policy import waiting_reason
        service,row,client=self.row();remote={**row['remote'],'credit_pack':{'id':'pack'}}
        service.store.update(row['id'],'preparing',remote=remote)
        recover(service,client,service.store.get(row['id']),'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'rejected'})
        current=service.store.get(row['id'])
        self.assertEqual(waiting_reason(current,[],[current],{}, {},{}),current['error'])
        self.assertEqual(current['remote']['credit_pack'],{'id':'pack'})
        service.execute({'action':'cancel','ids':[row['id']]});before=service.store.get(row['id'])
        self.assertFalse(recover(service,client,current,'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE'}))
        recover_legacy(service);self.assertEqual(service.store.get(row['id']),before)

    def test_pause_and_record_deletion_exit_the_repeated_wait_normally(self):
        service,row,client=self.row()
        recover(service,client,row,'prepare',{'code':'FLOW_UI_MUTEX_UNAVAILABLE','error':'rejected'})
        service.execute({'action':'pause'})
        with patch('time.time',return_value=time.time()+20):service._tick()
        self.assertEqual(service.store.get(row['id'])['status'],'queued')
        self.assertFalse(any(p['action'] in {'prepare','upload_images','run_tasks'} for p in service.pending.values()))
        service.execute({'action':'delete_records','ids':[row['id']]})
        self.assertIsNone(service.store.get(row['id']))
        with patch('time.time',return_value=time.time()+20):service._tick()
        self.assertFalse(any(p['action'] in {'prepare','upload_images','run_tasks'} for p in service.pending.values()))

    def test_old_three_limit_stop_recovers_once_but_new_real_fault_stop_and_paid_evidence_stay(self):
        service,row,client=self.row()
        remote={**row['remote'],'ui_permission_retry':{'count':4,'limit':3},'prepare_retry':{'browser':client['id']}}
        service.store.update(row['id'],'failed',attempt='',browser='',remote=remote,
            error='前台UI锁申请连续失败，已停止本条自动重试并释放账号／并行名额（重试上限3）')
        recover_legacy(service);current=service.store.get(row['id'])
        self.assertEqual(current['status'],'queued');self.assertEqual(current['remote']['ui_permission_retry']['kind'],'waiting')
        recover_legacy(service);self.assertEqual(service.store.get(row['id']),current)
        remote={**current['remote'],'ui_permission_retry':{'count':4,'kind':'fault','fault_count':4}}
        service.store.update(row['id'],'failed',remote=remote,error='前台UI锁申请连续失败，真实窗口错误')
        before=service.store.get(row['id']);recover_legacy(service);self.assertEqual(service.store.get(row['id']),before)
        remote={**remote,'ui_permission_retry':{'count':4},'generation_response':{'observed':True}}
        service.store.update(row['id'],'failed',remote=remote,error='前台UI锁申请连续失败，旧记录')
        before=service.store.get(row['id']);recover_legacy(service);self.assertEqual(service.store.get(row['id']),before)

    def test_existing_generation_evidence_or_postclick_expiry_cannot_refund_or_regenerate(self):
        service=self.service();row,client=self.bound_row(service,status='generating',finished=False)
        before=copy.deepcopy(row)
        self.assertFalse(recover(service,client,row,'run_tasks',{'code':'FLOW_UI_MUTEX_UNAVAILABLE'}))
        self.assertFalse(recover(service,client,row,'run_tasks',{'code':'FLOW_UI_MUTEX_EXPIRED'}))
        self.assertEqual(service.store.get(row['id']),before)

    def test_legacy_ui_error_recovers_but_login_and_manual_limits_are_kept(self):
        service,row,client=self.row();prefix='前台 UI 操作互斥申请未完成：rejected'
        service.store.update(row['id'],'failed',error=prefix)
        service.preferences['browser_blocks']={'profile:'+client['profile']:prefix,'profile:Other':'需要登录'}
        recover_legacy(service)
        self.assertEqual(service.store.get(row['id'])['status'],'queued')
        self.assertEqual(service.preferences['browser_blocks'],{'profile:Other':'需要登录'})


if __name__=='__main__':unittest.main()

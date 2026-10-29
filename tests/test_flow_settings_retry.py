"""Retry exact unpaid settings waits without reopening unsafe browser work."""
import copy,unittest
from unittest.mock import patch
import test_flow_retry_now as fixtures
from app_plugins.builtin.flow_assistant.retry_now import retry_kind
from app_plugins.builtin.flow_assistant.problem_details import temporary_code
from app_plugins.builtin.flow_assistant.browser_lifecycle import fault_reason

ERROR='设置面板正在重建，自动等待后重新核对；未提交生成。'


class SettingsRetryTests(unittest.TestCase):
    setUpClass=fixtures.RetryNowTests.__dict__['setUpClass']
    setUp=fixtures.RetryNowTests.setUp
    service=fixtures.RetryNowTests.service
    jobs=fixtures.RetryNowTests.jobs
    client=fixtures.RetryNowTests.client
    ui=fixtures.RetryNowTests.ui
    command=fixtures.RetryNowTests.command

    def legacy(self):
        service=self.service();service.timer.stop();service.store.enqueue(self.jobs(duration=10,budget=0))
        client=self.client(service);row=service.store.rows()[0]
        service.store.update(row['id'],'preparing',attempt='a'*32,browser=client['id'],remote={'account':client['account']})
        service.quarantine(client,service.store.get(row['id']),ERROR)
        return service,service.store.get(row['id']),client

    def test_legacy_queued_error_has_an_effective_immediate_retry_route(self):
        service,row,client=self.legacy();self.assertEqual(retry_kind(row),'environment')
        with patch.object(service,'_tick'):
            service.execute(self.command(row))
        plan=service.preferences['environment_recoveries']['profile:'+client['profile']]
        self.assertEqual(plan['state'],'waiting');self.assertEqual(plan.get('count',0),0)
        self.assertEqual(plan['error'],ERROR);self.assertIn(row['id'],service.released)
        self.assertEqual(service.store.get(row['id'])['status'],'queued')
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_missing_error_code_uses_bounded_prepare_retry_instead_of_quarantine(self):
        service,row,client=self.legacy()
        service.preferences['browser_blocks']={}
        service.store.update(row['id'],'preparing',attempt='b'*32,browser=client['id'],remote={'account':client['account']})
        service.request(client,'prepare',job=row['id'])
        request=next(k for k,p in service.pending.items() if p['action']=='prepare')
        service._response(client,{'request_id':request,'success':False,'error':ERROR})
        updated=service.store.get(row['id'])
        self.assertEqual(updated['status'],'queued');self.assertEqual(updated['remote']['prepare_retry']['count'],1)
        self.assertFalse(service.preferences.get('browser_blocks'))
        self.assertEqual(retry_kind(updated),'prepare')

    def test_toolbar_routes_legacy_settings_record_to_retry_now(self):
        service,row,client=self.legacy();dialog,_,commands=self.ui()
        dialog.state={'jobs':[row]};dialog.selected_ids=lambda:[row['id']]
        dialog.retry_now_button.click();self.assertEqual(commands[-1],self.command(row))

    def test_retry_preserves_manual_block_and_paid_evidence(self):
        service,row,client=self.legacy();key='profile:'+client['profile']
        service.preferences['browser_blocks'][key]='用户手动标记账号不可用'
        before=copy.deepcopy(service.preferences)
        with patch.object(service,'_tick'):service.execute(self.command(row))
        self.assertEqual(service.preferences,before)
        paid={**row,'attempt':'paid-attempt'};self.assertFalse(retry_kind(paid))
        for text in ('需要登录','验证码','设置面板有问题','此模型不支持10秒'):
            self.assertFalse(temporary_code(text))

    def test_browser_restart_is_after_page_recovery_not_before(self):
        service,row,client=self.legacy();key='profile:'+client['profile']
        self.assertTrue(temporary_code(ERROR))
        service.preferences['environment_recoveries']={key:{'state':'waiting','error':ERROR}}
        self.assertFalse(fault_reason(client,service.preferences))
        service.preferences['environment_recoveries'][key]['state']='refreshing'
        self.assertFalse(fault_reason(client,service.preferences))
        service.preferences['environment_recoveries'][key]['state']='stopped'
        self.assertEqual(fault_reason(client,service.preferences),ERROR)
        service.preferences['browser_blocks'][key]='用户手动暂停浏览器，保留窗口'
        self.assertFalse(fault_reason(client,service.preferences))

    def test_direct_submit_error_releases_unused_reserve_then_manual_retry_is_effective(self):
        from app_plugins.builtin.flow_assistant.failure_release import watch
        service,row,client=self.legacy()
        service.store.update(row['id'],'submitting',attempt='c'*32,browser=client['id'],remote={'account':client['account']})
        service.store.reserve(row['id'],'c'*32,15,'duration_rule')
        service.request(client,'run_tasks',job=row['id'])
        request=next(k for k,p in service.pending.items() if p['action']=='run_tasks')
        service._response(client,{'request_id':request,'success':False,'error':ERROR,'code':'FLOW_SETTINGS_NOT_READY'})
        self.assertTrue(watch(service))
        released=service.store.get(row['id']);self.assertEqual(released['status'],'failed')
        self.assertTrue(released['remote']['confirmed_not_submitted'])
        service.store.retry([row['id']])
        self.assertEqual(service.store.get(row['id'])['status'],'queued')

    def test_paid_evidence_is_preserved_even_if_settings_text_is_present(self):
        from app_plugins.builtin.flow_assistant.failure_release import watch
        service,row,client=self.legacy()
        service.store.update(row['id'],'uncertain',attempt='d'*32,browser=client['id'],
            remote={'upstream_task':'original-paid-task'},error=ERROR)
        self.assertFalse(watch(service));service.store.retry([row['id']])
        self.assertEqual(service.store.get(row['id'])['status'],'uncertain')


if __name__=='__main__':unittest.main()

"""Production-shaped old blocks cannot silently disable ordinary accounts."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_assistant as fixtures
import test_flow_preparation_recovery as preparation
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.policy import block_for
from app_plugins.builtin.flow_assistant.browser_slots import recover_legacy_probe_failures
from app_plugins.builtin.flow_assistant.environment_recovery import watch, schedule
from app_plugins.builtin.flow_assistant.store import QueueStore

TIMEOUT = preparation.TIMEOUT
WINDOW = 'No current window'
DURATION = '找不到视频时长选项，无法确认 10 秒；请检查当前模型的设置面板'


class TransientRecoveryTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    browser = preparation.PreparationRecoveryTests.browser
    page = preparation.PreparationRecoveryTests.page

    def test_offline_temporary_errors_launch_in_order_without_asking_for_high_account(self):
        service = self.service()
        profiles = ['Profile 7','Profile 12','Profile 13']
        projects = {p:'https://flow.google.com/project/'+p.replace(' ','-') for p in profiles}
        service.preferences.update(concurrency=4, profiles=profiles, profile_projects=projects,
            browser_blocks=dict(zip(['profile:'+p for p in profiles],[DURATION,TIMEOUT,WINDOW])))
        rich = self.browser(service,'Rich'); rich.update(credits=417)
        for p in profiles:
            status = browser_status({'profile':p},service.preferences)
            self.assertEqual(status['state'],'等待自动重试')
            self.assertIn('上次问题',status['reason'])
            self.assertIn('无需手动解除旧错误',status['reason'])
            self.assertFalse(block_for({'profile':p},service.preferences))
        service.store.enqueue(self.jobs(count=4,duration=8,budget=0))
        launches=[]; service.launch_requested.connect(launches.append)
        service.execute({'action':'start'}); service.fill_dispatch_slots()
        self.assertEqual(launches,profiles)
        self.assertIsNone(service.high_credit_pending)
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))
        self.assertEqual(sum(b['reserved'] for b in service.store.batches().values()),0)

    def test_reconnected_temporary_error_requires_fresh_snapshot_then_full_preflight(self):
        for error in (TIMEOUT,WINDOW,DURATION):
            with self.subTest(error=error):
                self.setUp(); service=self.service(); browser=self.browser(service)
                key='profile:'+browser['profile']; service.preferences['browser_blocks']={key:error}
                service.store.enqueue(self.jobs(duration=10,budget=0))
                self.page(service,browser)
                self.assertNotIn(key,service.preferences['browser_blocks'])
                service.paused=False; service._tick()
                self.assertEqual(service.store.rows()[0]['status'],'preparing')
                self.assertEqual({p['action'] for p in service.pending.values()},{'prepare'})

    def test_offline_manual_security_and_real_model_faults_are_explained_and_preserved(self):
        for error in ('用户手动暂停','需要登录','captcha：请完成验证','当前模型不支持10秒',TIMEOUT+' captcha'):
            with self.subTest(error=error):
                prefs={'browser_blocks':{'profile:P':error}}
                self.assertEqual(block_for({'profile':'P'},prefs),error)
                status=browser_status({'profile':'P'},prefs)
                self.assertEqual(status['state'],'需处理')
                self.assertIn(error,status['reason'])
                self.assertIn('需要手动处理',status['reason'])
        prefs={'browser_blocks':{'profile:P':'需要登录'}}
        self.assertIn('登录正确的 Flow 账号',browser_status({'profile':'P'},prefs)['reason'])

    def test_stopped_recovery_does_not_reopen_or_reset_on_application_restart(self):
        prefs={'browser_blocks':{'profile:P':WINDOW},'environment_recoveries':{
            'profile:P':{'code':'FLOW_PAGE_NOT_READY','error':WINDOW,'state':'stopped','count':3,
                'stop_reason':'原项目刷新后仍未恢复就绪'}}}
        self.assertEqual(block_for({'profile':'P'},prefs),WINDOW)
        status=browser_status({'profile':'P'},prefs)
        self.assertEqual(status['state'],'需处理')
        self.assertIn('已跳过此账号',status['reason'])
        self.assertIn('需要手动处理',status['reason'])

    def test_orphan_timeout_on_unready_original_page_is_adopted_and_refreshed(self):
        service=self.service(); client=self.browser(service)
        client.update(page_ready=False,batch_running=False,capabilities=['runtime_controls_v1'])
        key='profile:'+client['profile']; service.preferences['browser_blocks']={key:TIMEOUT}
        service.store.enqueue(self.jobs(duration=8,budget=0)); service.paused=False
        self.assertTrue(watch(service))
        entry=service.preferences['environment_recoveries'][key]
        self.assertEqual(entry['count'] if 'count' in entry else 0,0)
        with patch('time.time',return_value=entry['next_at']+.1): self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['count'],1)
        request=next(p for p in service.pending.values() if p['action']=='repair_project')
        self.assertIsNone(request['job'])
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def fault(self):
        service=self.service(); client=self.browser(service)
        client.update(capabilities=['runtime_controls_v1'],batch_running=False)
        service.store.enqueue(self.jobs(duration=8,budget=0)); service.paused=False
        key='profile:'+client['profile']; service.preferences['browser_blocks']={key:TIMEOUT}
        service.preferences=schedule(service.preferences,client,TIMEOUT,'FLOW_PAGE_NOT_READY')
        service.preferences['environment_recoveries'][key]['next_at']=0
        return service,client,key

    def test_missing_runtime_support_reports_exact_manual_fix_and_stops_waiting(self):
        service,client,key=self.fault(); client['capabilities']=[]
        self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'stopped')
        reason=browser_status(client,service.preferences)['reason']
        self.assertIn('尚未加载运行时',reason)
        self.assertIn('重新加载 Flow 桥接扩展',reason)
        self.assertFalse(service.pending)
        client['capabilities']=['runtime_controls_v1']
        self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['count'],1)
        self.assertEqual({p['action'] for p in service.pending.values()},{'repair_project'})

    def test_precondition_timeout_recovers_when_native_flag_clears_without_manual_resume(self):
        service,client,key=self.fault(); client['native_busy']=True
        self.assertTrue(watch(service))
        since=service.preferences['environment_recoveries'][key]['wait_started_at']
        with patch('time.time',return_value=since+1000): self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'stopped')
        client['native_busy']=False
        self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['count'],1)
        self.assertEqual({p['action'] for p in service.pending.values()},{'repair_project'})

    def test_stale_native_flag_has_finite_durable_wait_without_refreshing_busy_page(self):
        service,client,key=self.fault(); client['native_busy']=True
        self.assertTrue(watch(service))
        entry=service.preferences['environment_recoveries'][key]
        self.assertIn('原生操作',entry['wait_reason'])
        with patch('time.time',return_value=entry['wait_started_at']+1000): self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'stopped')
        self.assertIn('未刷新在途操作',browser_status(client,service.preferences)['reason'])
        self.assertEqual(QueueStore(self.namespace).settings()['environment_recoveries'][key]['state'],'stopped')
        self.assertFalse(service.pending)

    def test_pending_request_wait_resumes_automatically_when_response_arrives(self):
        service,client,key=self.fault(); service.request(client,'snapshot')
        self.assertTrue(watch(service)); self.assertIn('等待当前浏览器请求',browser_status(client,service.preferences)['reason'])
        service.pending.clear(); self.assertTrue(watch(service))
        self.assertEqual(service.preferences['environment_recoveries'][key]['state'],'refreshing')
        self.assertEqual({p['action'] for p in service.pending.values()},{'repair_project'})

    def test_real_paid_or_unknown_work_is_not_refreshed_or_timed_out_as_temporary_flag(self):
        for status in ('generating','uncertain','download_failed'):
            with self.subTest(status=status):
                self.setUp(); service,client,key=self.fault()
                row=service.store.rows()[0]
                service.store.update(row['id'],status,attempt='a'*32,browser=client['id'],
                    remote={'profile':client['profile'],'account':client['account']})
                service.store.enqueue(self.jobs(duration=10,budget=0))
                before=copy.deepcopy(service.preferences['environment_recoveries'][key])
                with patch('time.time',return_value=time.time()+1000): self.assertFalse(watch(service))
                self.assertEqual(service.preferences['environment_recoveries'][key],before)
                self.assertFalse(service.pending)

    def test_later_fresh_idle_page_recovers_stopped_account_without_more_refreshes(self):
        service,client,key=self.fault()
        entry=service.preferences['environment_recoveries'][key]
        entry.update(state='stopped',count=3,page_instance='before')
        client.update(page_instance='before',page_ready=True,batch_running=False)
        self.assertFalse(watch(service)); self.assertIn(key,service.preferences['browser_blocks'])
        client['page_instance']='after'
        self.assertTrue(watch(service)); self.assertNotIn(key,service.preferences['browser_blocks'])
        self.assertEqual(service.preferences['environment_recoveries'][key]['count'],3)
        self.assertFalse(service.pending)
        service._tick()
        self.assertEqual({p['action'] for p in service.pending.values()},{'prepare'})

    def test_old_success_worded_probe_failures_are_rechecked_once_and_real_faults_preserved(self):
        message='扩展安装检查未通过；已核对本配置安装路径及扩展身份；启动后等待扩展在线确认。'
        bad={'kind':'extension_probe_v3','retryable':False,'message':message}
        genuine={'kind':'extension_probe_v3','retryable':False,'message':'扩展被停用'}
        prefs={'extension_probe_revision':3,'browser_launch_failures':{'P':bad,'Q':genuine,'R':{**bad,'probe':{'version':'1.1.99'}}}}
        result=recover_legacy_probe_failures(prefs)
        self.assertNotIn('P',result['browser_launch_failures'])
        self.assertEqual(result['browser_launch_failures']['Q'],genuine)
        self.assertIn('R',result['browser_launch_failures'])
        self.assertEqual(recover_legacy_probe_failures(result),result)
        self.assertIn('P',prefs['browser_launch_failures'])

    def test_connected_ten_and_eleven_points_do_not_claim_ready_for_fifteen_point_head(self):
        service=self.service(); service.store.enqueue(self.jobs(duration=10,budget=0))
        for points in (10,11):
            browser=self.browser(service,'Balance'+str(points)); browser.update(credits=points,batch_running=False)
            status=browser_status(browser,service.preferences,service.store.rows())
            self.assertEqual(status['state'],'积分不足当前任务')
            self.assertIn('需要 15 分',status['reason'])
            self.assertIn(f'剩余 {points} 分',status['reason'])


if __name__=='__main__': unittest.main()

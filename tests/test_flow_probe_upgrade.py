"""A supported bridge upgrade must not leave old version rejections permanent."""
import copy
import unittest

import test_flow_credit_cooldown as fixtures
import test_flow_image_capacity as capacity_fixtures
from app_plugins.builtin.flow_assistant.browser_slots import recover_legacy_probe_failures
from app_plugins.builtin.flow_assistant.policy import waiting_reason


def version_failure(version='1.1.37', ceiling='1.1.36'):
    return {'at': 1, 'kind': 'extension_probe_v3', 'retryable': False,
        'message': '扩展安装检查未通过，请在连接向导修正后恢复；尚未打开此配置：Spare；'
            f'检测到扩展 {version}，当前程序支持 1.1.8～{ceiling}；请使程序与桥接扩展版本匹配；'
            '需要手动处理：在此配置的扩展管理页检查启用状态、安装路径和版本，再点“修正后恢复选中浏览器”',
        'probe': {'status': 'unknown', 'version': version, 'can_launch': False,
            'reason': '已核对本配置安装路径及扩展身份；启动后等待扩展在线确认。'}}


class ProbeUpgradeTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser

    def test_revision_four_rechecks_now_supported_version_without_changing_other_preferences(self):
        prefs={'extension_probe_revision':4, 'browser_launch_failures':{'Spare':version_failure()},
            'browser_probe_retries':{'Spare':{'count':3,'next_at':1}, 'Other':{'count':2,'next_at':2}},
            'browser_blocks':{'profile:Paused':'用户已手动关闭此配置'},
            'account_blocks':{'blocked@example.test':{'kind':'disabled'}},
            'account_images':{'original@example.test':{'image':'original-image'}},
            'high_credit_accounts':{'high@example.test':{'decision':'skipped'}},
            'credit_balances':{'small@example.test':{'credits':50}}, 'concurrency':8}
        original=copy.deepcopy(prefs);result=recover_legacy_probe_failures(prefs)
        self.assertNotIn('Spare',result['browser_launch_failures'])
        self.assertNotIn('Spare',result['browser_probe_retries'])
        self.assertEqual(result['browser_probe_retries']['Other'],prefs['browser_probe_retries']['Other'])
        for key in prefs.keys()-{'browser_launch_failures','browser_probe_retries'}:
            self.assertEqual(result[key],prefs[key])
        self.assertEqual(prefs,original)
        self.assertEqual(recover_legacy_probe_failures(result),result)

    def test_disabled_missing_conflicting_and_foreign_path_errors_are_preserved(self):
        for status,reason in [('disabled','扩展被停用'),('missing','扩展不存在'),
                ('unknown','检测到相互矛盾的安装记录'),('unknown','安装路径和身份无法确认')]:
            with self.subTest(status=status,reason=reason):
                entry=version_failure();entry['probe'].update(status=status,reason=reason)
                prefs={'extension_probe_revision':4,'browser_launch_failures':{'Spare':entry}}
                self.assertEqual(recover_legacy_probe_failures(prefs),prefs)

    def test_future_version_and_new_preflight_failure_are_not_retried_on_every_restart(self):
        for entry in (version_failure('1.1.99'),version_failure(ceiling='1.1.37'),
                {**version_failure(),'kind':'launch'},
                {**version_failure(),'message':'扩展安装路径无法确认'},
                {**version_failure(),'probe':{}}):
            with self.subTest(entry=entry):
                prefs={'extension_probe_revision':4,'browser_launch_failures':{'Spare':entry}}
                self.assertEqual(recover_legacy_probe_failures(prefs),prefs)

    def test_previous_supported_versions_can_reenter_but_transient_retry_limits_remain(self):
        transient={'kind':'extension_probe_v3','retryable':True,'message':'临时读取失败',
            'probe':{'status':'unknown','version':'1.1.37','reason':'暂未落盘'}}
        prefs={'extension_probe_revision':4,'browser_launch_failures':{
            'Old':version_failure('1.1.35','1.1.34'),'Transient':transient},
            'browser_probe_retries':{'Transient':{'count':3,'next_at':1}}}
        result=recover_legacy_probe_failures(prefs)
        self.assertNotIn('Old',result['browser_launch_failures'])
        self.assertEqual(result['browser_launch_failures']['Transient'],transient)
        self.assertEqual(result['browser_probe_retries'],prefs['browser_probe_retries'])

    def test_worker_restart_rechecks_backup_records_but_recovers_seven_existing_idle_accounts_first(self):
        service,owners,spares,_=capacity_fixtures.ImageCapacityTests.queue(self)
        service.store.enqueue(self.jobs(images=[self.target/'new-image.png'],count=4,duration=8,budget=0))
        spares+=['New spare '+str(i) for i in range(5,29)]
        profiles=[c['profile'] for c in service.clients.values()]+spares
        service.commit_preferences({**service.preferences,'profiles':profiles,
            'profile_projects':{p:fixtures.PROJECT for p in profiles},'extension_probe_revision':4,
            'browser_launch_failures':{p:version_failure() for p in spares},
            'browser_probe_retries':{spares[0]:{'count':3,'next_at':1}}})
        claims=copy.deepcopy(service.preferences['account_images'])
        service.execute({'action':'stop'});restarted=self.service();restarted.timer.stop()
        rows=restarted.store.rows()
        for row in rows[:3]:restarted.store.update(row['id'],'completed')
        restarted.store.update(rows[3]['id'],'generating')
        for owner in owners:
            client=self.browser(restarted,owner['profile'],points=50)
            client['page_ready']=False
        launches=[];restarted.launch_requested.connect(launches.append);restarted.paused=False
        restarted.fill_dispatch_slots()
        self.assertFalse(launches)
        self.assertEqual(len([p for p in restarted.pending.values() if p['action']=='repair_project']),7)
        self.assertEqual(restarted.preferences['account_images'],claims)
        self.assertFalse(restarted.preferences['browser_launch_failures'])
        self.assertFalse(restarted.preferences['browser_probe_retries'])
        self.assertTrue(all(row['status']=='queued' for row in restarted.store.rows()[4:]))
        restarted.fill_dispatch_slots();self.assertFalse(launches)
        self.assertEqual(len([p for p in restarted.pending.values() if p['action']=='repair_project']),7)
        self.assertFalse(any(p['action']=='run_tasks' for p in restarted.pending.values()))

    def test_idle_history_does_not_misreport_other_image_exclusion_or_unneeded_backup_failures(self):
        service,owners,spares,_=capacity_fixtures.ImageCapacityTests.queue(self,cold=False)
        service.preferences['browser_launch_failures']={p:version_failure('1.1.99') for p in spares}
        rows=service.store.rows()
        reason=waiting_reason(rows[4],list(service.clients.values()),rows,service.preferences,{},service.store.batches())
        self.assertNotIn('其他参考图独占',reason)
        self.assertNotIn('备用配置启动检查失败',reason,'usable existing accounts do not need these backups')
        self.assertTrue(reason)
        self.assertFalse(any(row['status']=='failed' for row in rows))

    def test_online_and_unmatched_profiles_do_not_count_as_backup_launch_failures(self):
        service,owners,spares,_=capacity_fixtures.ImageCapacityTests.queue(self,cold=False)
        service.preferences['browser_launch_failures']={owners[0]['profile']:version_failure(),
            'Not configured':version_failure()}
        rows=service.store.rows()
        reason=waiting_reason(rows[4],list(service.clients.values()),rows,service.preferences,{},service.store.batches())
        self.assertNotIn('备用配置启动检查失败',reason)


if __name__=='__main__':unittest.main()

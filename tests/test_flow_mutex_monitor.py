"""Actual lease snapshots and offscreen Qt monitoring; no browser operations."""
import json
import time
import unittest
from unittest.mock import MagicMock,patch

from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.flow_assistant import ui_mutex,mutex_monitor
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
import test_flow_ui_mutex as mutex_cases
import test_flow_setup as setup


class MutexMonitorTests(unittest.TestCase):
    setUpClass=mutex_cases.UiMutexTests.__dict__['setUpClass']
    setUp=mutex_cases.UiMutexTests.setUp
    service=mutex_cases.UiMutexTests.service
    browser=mutex_cases.UiMutexTests.browser
    client=mutex_cases.UiMutexTests.client
    jobs=mutex_cases.UiMutexTests.jobs
    configured=mutex_cases.UiMutexTests.configured
    request=mutex_cases.UiMutexTests.request

    def test_exact_owner_queue_and_countdown_are_serializable_without_sockets_or_tokens(self):
        service,clients=self.configured();now=time.time()
        with patch('time.time',return_value=now):
            self.request(service,clients[0],'1'*32,'upload_images')
            self.request(service,clients[1],'2'*32,'prepare')
            ui_mutex.host_receive(service,{'action':'ui_native_request','token':'3'*32,'profile':'Launch profile'})
        data=mutex_monitor.snapshot(service,now+4)
        self.assertEqual(data['owner']['profile'],clients[0]['profile'])
        self.assertEqual(data['owner']['operation'],'上传／选择参考图')
        self.assertEqual([r['profile'] for r in data['queue']],[clients[1]['profile'],'Launch profile'])
        self.assertEqual(data['owner']['expires_at'],now+ui_mutex.phase_limit(service,service.ui_mutex['owner'])-1)
        self.assertIn('已持有 4 秒',mutex_monitor.summary(data,now+4))
        encoded=json.dumps(data)
        for secret in ('socket','token','1'*32,'2'*32,'3'*32,clients[0]['account']):self.assertNotIn(secret,encoded)

    def test_handoff_and_release_follow_the_actual_mutex(self):
        service,clients=self.configured()
        self.request(service,clients[0],'1'*32);self.request(service,clients[1],'2'*32)
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':'1'*32})
        self.assertEqual(mutex_monitor.snapshot(service)['owner']['profile'],clients[1]['profile'])
        ui_mutex.receive(service,clients[1],{'type':'ui_mutex_release','token':'2'*32})
        data=mutex_monitor.snapshot(service)
        self.assertIsNone(data['owner']);self.assertEqual(data['queue'],[])
        self.assertIn('UI锁：空闲',mutex_monitor.summary(data))

    def test_native_launch_owner_and_pending_launch_are_visible_without_duplicate_rows(self):
        service,clients=self.configured();now=time.time()
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'1'*32,'profile':'Opening'})
        ui_mutex.host_receive(service,{'action':'ui_launch_wait','token':'1'*32,'profile':'Opening','waiting':True})
        ui_mutex.host_receive(service,{'action':'ui_launch_wait','token':'2'*32,'profile':'Later','waiting':True})
        data=mutex_monitor.snapshot(service)
        self.assertTrue(data['owner']['host']);self.assertEqual(data['owner']['operation'],'启动浏览器')
        self.assertEqual(data['owner']['profile'],'Opening')
        self.assertEqual([r['profile'] for r in data['launches']],['Later'])

    def test_cancel_grace_is_shown_and_elapsed_deadline_never_claims_release(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        now=time.time();service.ui_mutex['owner']['cancel_deadline']=now+3
        data=mutex_monitor.snapshot(service,now)
        self.assertEqual(data['owner']['expires_at'],now+3)
        self.assertIn('待确认释放',mutex_monitor.summary(data,now+4))
        self.assertIsNotNone(service.ui_mutex['owner'])
        self.assertIn('状态 6 秒未更新',mutex_monitor.summary(data,now+6))

    def test_pack_priority_is_visible_without_granting_or_mutating_a_lease(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        self.request(service,clients[1],'2'*32);self.request(service,clients[2],'3'*32)
        now=time.time();current=service.ui_mutex
        current['burst']={'id':'pack','client':clients[2]['id'],'socket':clients[2]['socket'],
            'elapsed':3,'clock_at':now-2,'started_at':now-5,'last_phase_at':now}
        before_owner=dict(current['owner']);before_queue=list(current['queue']);before_burst=dict(current['burst'])
        with patch.object(service.store,'rows',side_effect=AssertionError('monitor must not query tasks')):
            data=mutex_monitor.snapshot(service,now)
        self.assertEqual(data['queue'][0]['profile'],clients[2]['profile'])
        self.assertEqual(data['priority']['profile'],clients[2]['profile'])
        self.assertEqual(current['owner'],before_owner);self.assertEqual(current['queue'],before_queue)
        self.assertEqual(current['burst'],before_burst)
        current.pop('burst')

    def test_disabled_and_not_started_monitor_do_not_create_a_lock_state(self):
        service=self.service();self.assertFalse(hasattr(service,'ui_mutex') and service.ui_mutex.get('owner'))
        before=getattr(service,'ui_mutex',None)
        data=mutex_monitor.snapshot(service)
        self.assertFalse(data['enabled']);self.assertEqual(mutex_monitor.summary(data),'UI锁：已关闭')
        self.assertIs(getattr(service,'ui_mutex',None),before)

    def test_lightweight_heartbeat_does_not_publish_tasks_or_send_browser_commands(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        heartbeats=[];full=[];service.ui_lock_changed.connect(heartbeats.append);service.changed.connect(full.append)
        sent=clients[0]['socket'].sendTextMessage.call_count
        service.publish_ui_lock()
        self.assertEqual(len(heartbeats),1);self.assertEqual(full,[])
        self.assertEqual(clients[0]['socket'].sendTextMessage.call_count,sent)
        self.assertEqual(service.ui_lock_timer.interval(),1000)
        service.execute({'action':'stop'});self.assertFalse(service.ui_lock_timer.isActive())


class MutexMonitorUiTests(unittest.TestCase):
    setUpClass=setup.SetupTests.__dict__['setUpClass']
    setUp=setup.SetupTests.setUp
    ui=setup.SetupTests.ui

    def sample(self):
        now=time.time()
        return {'enabled':True,'observed_at':now,'owner':{'profile':'Profile 13','host':False,
            'operation':'上传／选择参考图','since':now-8,'expires_at':now+20},
            'queue':[{'profile':'Profile 25','host':False,'operation':'准备参数','since':now-3}],
            'priority':None,'launches':[]}

    def test_live_line_and_modeless_details_refresh_without_rebuilding_task_table(self):
        dialog,launcher,commands=self.ui();plugin=dialog.plugin;data=self.sample()
        original=dict(plugin.snapshot)
        with patch.object(dialog,'update_state',side_effect=AssertionError('do not rebuild task list')):
            plugin._ui_lock_changed(data)
        self.assertEqual(plugin.snapshot,original)
        view=dialog.ui_lock_monitor
        self.assertIn('Profile 13',view.label.text());self.assertIn('已持有 8 秒',view.label.text())
        self.assertTrue(view.clock.isActive());self.assertEqual(view.clock.interval(),1000)
        view.open_details();self.assertFalse(view.details.isModal());self.assertTrue(view.details.isVisible())
        self.assertEqual(view.table.rowCount(),2)
        self.assertEqual(view.table.item(0,1).text(),'Profile 13');self.assertEqual(view.table.item(1,1).text(),'Profile 25')
        with patch('time.time',return_value=data['observed_at']+2):view.refresh()
        self.assertIn('已持有 10 秒',view.label.text());self.assertEqual(view.table.item(1,3).text(),'5 秒')
        dialog.close();self.assertFalse(view.details.isVisible())
        self.assertEqual(commands,[])

    def test_older_snapshot_cannot_overwrite_new_owner_and_disabled_is_explicit(self):
        dialog,launcher,commands=self.ui();view=dialog.ui_lock_monitor;data=self.sample();view.apply(data)
        older={**data,'observed_at':data['observed_at']-1,'owner':None};view.apply(older)
        self.assertIn('Profile 13',view.label.text())
        view.apply({**data,'observed_at':data['observed_at']+1,'enabled':False,'owner':None,'queue':[]})
        self.assertEqual(view.label.text(),'UI锁：已关闭')

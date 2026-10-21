import json
import time
import unittest
import os
from pathlib import Path
from qt_compat import QtWidgets
import test_flow_credit_cooldown as fixtures
import test_flow_browser_table as tables
from app_plugins.builtin.flow_assistant.runtime_controls import refresh
from app_plugins.builtin.flow_assistant.browser_table import Column as C


class OrderedRuntimeTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs

    def test_four_heads_follow_insertion_order_despite_later_priority_and_different_image(self):
        service=self.service();service.preferences.update(concurrency=4,profiles=['P3','P1','P4','P2'])
        clients={p:self.browser(service,p,points=n) for p,n in zip(('P1','P2','P3','P4'),(26,38,75,50))}
        service.store.enqueue(self.jobs(duration=8,count=4,budget=0))
        other=self.target/'other.png';other.write_bytes(b'other reference')
        service.store.enqueue(self.jobs(images=[other],duration=8,budget=0))
        rows=service.store.rows();service.store.set_payload(rows[-1]['id'],{**rows[-1]['payload'],'priority':999})
        service.paused=False;launches=[];service.launch_requested.connect(launches.append)
        for _ in range(4):service._tick()
        self.assertTrue(all(service.store.get(r['id'])['status']=='preparing' for r in rows[:4]))
        self.assertEqual(service.store.get(rows[-1]['id'])['status'],'queued')
        self.assertEqual([service.store.get(r['id'])['browser'] for r in rows[:4]],
            [clients[p]['id'] for p in ('P3','P1','P4','P2')])
        self.assertFalse(launches)

    def test_unavailable_head_duration_does_not_let_a_later_job_jump(self):
        service=self.service();client=self.browser(service,'P1',points=50)
        service.preferences.update(concurrency=4,profile_lanes={'P1':6},profile_lane_modes={'P1':'fixed'})
        service.store.enqueue(self.jobs(duration=10,budget=0));service.store.enqueue(self.jobs(duration=6,budget=0))
        service.paused=False;service._tick()
        self.assertEqual([r['status'] for r in service.store.rows()],['queued','queued'])

    def test_snapshot_keeps_other_window_of_same_busy_account_excluded(self):
        service=self.service();one=self.browser(service,'P1',points=50);alias=self.browser(service,'P2',points=50)
        other=self.browser(service,'P3',points=50);alias['account']=one['account'];one['native_busy']=True
        service.preferences.update(concurrency=4,profiles=['P1','P2','P3'])
        service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False;service._tick()
        self.assertEqual(service.store.rows()[0]['browser'],other['id'])

    def test_delayed_retry_at_head_blocks_later_submission(self):
        service=self.service();self.browser(service,'P1',points=50)
        service.store.enqueue(self.jobs(duration=8,count=2,budget=0));head=service.store.rows()[0]
        service.store.update(head['id'],'queued',remote={'prepare_retry':{'next_at':time.time()+300}})
        service.paused=False;service._tick()
        self.assertEqual([r['status'] for r in service.store.rows()],['queued','queued'])

    def test_runtime_focus_is_explicit_even_with_automatic_focus_disabled(self):
        service=self.service();client=self.browser(service,'P1',points=50)
        client['capabilities']=['native_foreground_v1','runtime_controls_v1'];service.preferences['browser_options']={'focus_before_action':False}
        service.execute({'action':'browser_runtime','operation':'focus','keys':['profile:P1']})
        request=next(p for p in service.pending.values() if p['action']=='focus_window')
        self.assertTrue(request.get('foreground_token'))

    def test_f5_refuses_paid_work_and_never_changes_its_binding(self):
        service=self.service();client=self.browser(service,'P1',points=50)
        client['capabilities']=['runtime_controls_v1']
        service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[0]
        for status in ('submitting','generating','downloading','uncertain','download_failed'):
            service.store.update(row['id'],status,browser=client['id'],attempt='a'*32,remote={'account':client['account'],'profile':'P1'})
            before=service.store.get(row['id']);self.assertFalse(refresh(service,client))
            self.assertEqual(service.store.get(row['id']),before)
        self.assertFalse(service.pending)

    def test_idle_f5_sends_one_exact_page_repair_and_waits(self):
        service=self.service();client=self.browser(service,'P1',points=50)
        client['capabilities']=['runtime_controls_v1']
        self.assertTrue(refresh(service,client));self.assertFalse(refresh(service,client))
        commands=[json.loads(c.args[0]) for c in client['socket'].sendTextMessage.call_args_list]
        repairs=[c for c in commands if c.get('action')=='repair_project']
        self.assertEqual(len(repairs),1);self.assertTrue(repairs[0]['params']['force_reload'])
        self.assertEqual(repairs[0]['params']['url'],fixtures.PROJECT)

    def test_refreshing_existing_window_reserves_lane_before_backup_launch(self):
        service=self.service();client=self.browser(service,'P1',points=50)
        client.update(capabilities=['runtime_controls_v1'],connected=time.time()-3600)
        service.preferences.update(concurrency=1,profiles=['P1','Backup'])
        service.preferences['profile_projects']['Backup']=fixtures.PROJECT
        self.assertTrue(refresh(service,client))
        service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False
        launches=[];service.launch_requested.connect(launches.append);service._tick()
        self.assertFalse(launches)
        self.assertEqual(service.store.rows()[0]['status'],'queued')


class RuntimeTableTests(unittest.TestCase):
    setUpClass=tables.BrowserTableTests.__dict__['setUpClass']
    setUp=tables.BrowserTableTests.setUp
    ui=tables.BrowserTableTests.ui
    table=tables.BrowserTableTests.table
    profiles=tables.BrowserTableTests.profiles
    row=tables.BrowserTableTests.row
    def test_runtime_menu_requires_all_selected_browsers_connected(self):
        dialog,_,commands=self.table()
        for client in dialog.state['clients']:client.update(capabilities=['runtime_controls_v1','native_foreground_v1'],extension_version='1.1.33')
        row=self.row(dialog,'Profile 2');dialog.browsers.selectRow(row)
        menu=QtWidgets.QMenu();runtime=dialog.runtime_menu(menu);self.assertTrue(runtime.isEnabled())
        runtime.actions()[0].trigger();self.assertEqual(commands[-1]['action'],'browser_runtime')
        dialog.state['clients']=[];dialog.update_browsers()
        menu=QtWidgets.QMenu();runtime=dialog.runtime_menu(menu)
        self.assertFalse(runtime.isEnabled());self.assertFalse(runtime.menuAction().isEnabled())

    def test_whole_row_and_lane_editor_have_requested_state_colors(self):
        dialog,_,_=self.table()
        expected={'Profile 10':'#d9eaff','Profile 2':'#d8f0d8','Profile 4':'#fff0a6'}
        for profile,color in expected.items():
            row=self.row(dialog,profile)
            self.assertTrue(all(dialog.browsers.item(row,c).background().color().name()==color for c in C))
            self.assertIn(color,dialog.lane_edits[('profile',profile)].styleSheet())
        dialog.state['clients'][0]['blocked']='explicit runtime failure';dialog.update_browsers()
        row=self.row(dialog,'Profile 10');self.assertEqual(dialog.browsers.item(row,C.PROFILE).background().color().name(),'#ffd6d6')

    def test_optional_runtime_color_preview(self):
        target=os.environ.get('LZX_FLOW_RUNTIME_SCREENSHOT')
        if not target:self.skipTest('optional runtime/color visual QA')
        dialog,_,_=self.table()
        # Add a separate exception so all four requested states remain visible.
        dialog.state['clients'].append({**dialog.state['clients'][1],'id':'failure','profile':'Profile 31','blocked':'页面上传多次失败，等待安全 F5 恢复'})
        for client in dialog.state['clients']:client.update(capabilities=['runtime_controls_v1','native_foreground_v1'],extension_version='1.1.33')
        dialog.update_browsers();dialog.tabs.setCurrentWidget(dialog.browser_page);dialog.resize(1480,740)
        dialog.show();self.app.processEvents();dialog.grab().save(target)
        dialog.browsers.selectRow(self.row(dialog,'Profile 2'))
        menu=QtWidgets.QMenu(dialog);runtime=dialog.runtime_menu(menu);runtime.popup(dialog.browsers.mapToGlobal(dialog.browsers.rect().center()))
        self.app.processEvents();runtime.grab().save(str(Path(target).with_name('runtime-menu.png')))
        runtime.close()


if __name__=='__main__':unittest.main()

"""One manual sweep closes only verified exhausted, idle connected profiles."""
import time
import unittest

import test_flow_runtime_close as fixtures
from app_plugins.builtin.flow_assistant.runtime_close import batch_summary, low_balance
from app_plugins.builtin.flow_assistant.credit_cooldown import remember_balance


class BulkCloseTests(unittest.TestCase):
    setUpClass = fixtures.RuntimeCloseTests.__dict__['setUpClass']
    setUp = fixtures.RuntimeCloseTests.setUp
    service = fixtures.RuntimeCloseTests.service
    browser = fixtures.RuntimeCloseTests.browser
    client = fixtures.RuntimeCloseTests.client
    jobs = fixtures.RuntimeCloseTests.jobs
    ready = fixtures.RuntimeCloseTests.ready

    def sweep(self, service): service.execute({'action':'close_low_credit_browsers'})

    def test_sweep_only_targets_zero_to_nine_and_never_unknown_expired_or_funded(self):
        service = self.service()
        clients = [self.ready(service,str(i),points=p) for i,p in enumerate((0,5,9,10,50,1038,None,5,5))]
        clients[-2]['credits_at'] -= 21601
        clients[-1]['authorized'] = False
        self.sweep(service)
        self.assertEqual({p['client'] for p in service.pending.values()}, {c['id'] for c in clients[:3]})
        self.assertTrue(all(p['action']=='close_browser' for p in service.pending.values()))
        self.assertEqual(batch_summary(service)['waiting'],3)
        self.assertEqual(batch_summary(service)['closed'],0)

    def test_valid_six_hour_low_credit_hold_remains_sweepable_after_balance_cache_expires(self):
        service = self.service(); client = self.ready(service,points=5)
        remember_balance(service,client)
        client.update(credits_at=time.time()-21601,credits=None)
        self.assertTrue(low_balance(client,service.preferences))
        self.sweep(service)
        self.assertEqual(len(service.pending),1)

    def test_busy_and_unresolved_profiles_are_skipped_without_cancelling_or_binding_other_work(self):
        service = self.service()
        busy = [self.ready(service,'Busy '+str(i),points=5) for i in range(3)]
        idle = self.ready(service,'Idle',points=0)
        service.store.enqueue(self.jobs(duration=8,count=3,budget=0))
        for row,client,status in zip(service.store.rows(),busy,('generating','downloading','uncertain')):
            service.store.update(row['id'],status,browser=client['id'],attempt=row['id'],
                remote={'profile':client['profile'],'account':client['account']})
        before = service.store.rows(); self.sweep(service)
        self.assertEqual(service.store.rows(),before)
        self.assertEqual([p['client'] for p in service.pending.values()],[idle['id']])
        report = batch_summary(service)
        self.assertEqual(report['skipped'],3); self.assertEqual(report['waiting'],1)
        self.assertIn('待核对',report['detail'])

    def test_old_extension_skip_is_visible_and_cannot_abort_other_closes(self):
        service = self.service(); old = self.ready(service,'Old',points=5); old['capabilities'] = []
        idle = self.ready(service,'New',points=5)
        self.sweep(service)
        self.assertEqual([p['client'] for p in service.pending.values()],[idle['id']])
        report = batch_summary(service)
        self.assertEqual(report['skipped'],1); self.assertIn('1.1.34',report['detail'])

    def test_progress_counts_actual_acknowledgements_and_failures_and_deduplicates_clicks(self):
        service = self.service(); one = self.ready(service,'One',points=5); two = self.ready(service,'Two',points=0)
        self.sweep(service); first = batch_summary(service)['id']; pending = dict(service.pending)
        self.sweep(service); self.assertEqual(batch_summary(service)['id'],first)
        self.assertEqual(service.pending,pending)
        for key,p in pending.items():
            client = one if p['client']==one['id'] else two
            service._response(client,{'request_id':key,'success':client is one,
                'data':{'closed':True} if client is one else {},'error':'测试窗口关闭失败'})
        report = batch_summary(service)
        self.assertEqual((report['closed'],report['waiting'],report['failed']),(1,0,1))
        self.assertIn('测试窗口关闭失败',report['detail'])

    def test_disconnection_during_sweep_is_reported_unconfirmed_without_a_false_success(self):
        service = self.service(); client = self.ready(service,points=5)
        self.sweep(service); service._disconnected(client['socket'])
        report = batch_summary(service)
        self.assertEqual(report['closed'],0); self.assertEqual(report['failed'],1)
        self.assertEqual(report['waiting'],0)

    def test_no_matching_profiles_reports_no_action_and_never_starts_a_browser_or_credit_read(self):
        service = self.service(); self.ready(service,points=50); launches = []
        service.launch_requested.connect(launches.append); self.sweep(service)
        self.assertFalse(service.pending); self.assertFalse(launches)
        self.assertIn('没有发现',batch_summary(service)['message'])


class BulkCloseButtonTests(unittest.TestCase):
    setUpClass = fixtures.RuntimeCloseMenuTests.__dict__['setUpClass']
    setUp = fixtures.RuntimeCloseMenuTests.setUp
    ui = fixtures.RuntimeCloseMenuTests.ui
    table = fixtures.RuntimeCloseMenuTests.table
    profiles = fixtures.RuntimeCloseMenuTests.profiles
    row = fixtures.RuntimeCloseMenuTests.row

    def test_button_closes_all_without_requiring_row_selection_and_disables_when_no_connected_low_account(self):
        dialog,_,commands = self.table(); dialog.browsers.clearSelection()
        self.assertTrue(dialog.close_low_credit_button.isEnabled())
        dialog.close_low_credit_button.click()
        self.assertEqual(commands[-1],{'action':'close_low_credit_browsers'})
        dialog.state['clients'] = []; dialog.update_browsers()
        self.assertFalse(dialog.close_low_credit_button.isEnabled())

    def test_batch_summary_updates_bottom_note_only_when_close_progress_changes(self):
        dialog,_,_ = self.table()
        report = {'id':'bulk-test','message':'批量关闭：已关闭 1；跳过 2。','detail':'Profile 4：仍有下载。'}
        state = {**dialog.state,'jobs':[],'paused':True,'batches':{},'spaces':[],
                 'runtime_close_batch':report}
        dialog.update_state(state)
        self.assertEqual(dialog.note.text(),report['message'])
        self.assertEqual(dialog.note.toolTip(),report['detail'])
        dialog.note.setText('后续用户操作提示'); dialog.update_state(state)
        self.assertEqual(dialog.note.text(),'后续用户操作提示')


if __name__=='__main__': unittest.main()

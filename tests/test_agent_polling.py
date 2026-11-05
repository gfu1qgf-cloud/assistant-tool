"""Slow user-controlled polling, immediate manual checks and bounded continuation."""
from unittest.mock import patch
import unittest

from app_plugins.builtin.ai_agent_assistant.receipt_monitor import poll_minutes
import test_agent_delivery as fixture


class PollIntervalTests(unittest.TestCase):
    def test_default_invalid_and_bounded_minutes(self):
        self.assertEqual(poll_minutes({}),30)
        self.assertEqual(poll_minutes(None),30)
        for value in (None,False,'broken',float('nan'),float('inf')):
            self.assertEqual(poll_minutes({'poll_minutes':value}),30)
        self.assertEqual(poll_minutes({'poll_minutes':'60'}),60)
        self.assertEqual(poll_minutes({'poll_minutes':0}),1)
        self.assertEqual(poll_minutes({'poll_minutes':999999}),1440)


class PollMonitorTests(unittest.TestCase):
    setUpClass=fixture.ReceiptMonitorTests.__dict__['setUpClass']
    setUp=fixture.ReceiptMonitorTests.setUp
    tearDown=fixture.ReceiptMonitorTests.tearDown
    stage=fixture.ReceiptMonitorTests.stage
    def check(self,**kwargs):
        with patch.object(self.plugin.receipts,'download_authorization',return_value=None):
            self.plugin.receipts.check(**kwargs)
    def test_start_waits_full_interval_not_immediate_network_check(self):
        self.stage(); monitor=self.plugin.receipts; monitor.start()
        self.assertTrue(monitor.timer.isSingleShot()); self.assertTrue(monitor.timer.isActive())
        self.assertEqual(monitor.timer.interval(),30*60000)
        self.context.submit_background_task.assert_not_called(); monitor.stop()
    def test_manual_check_stops_timer_and_rearms_from_completion(self):
        self.stage(); monitor=self.plugin.receipts; monitor.start(); self.check(manual=True)
        self.assertFalse(monitor.timer.isActive()); self.assertTrue(monitor.manual_notice)
        monitor.completed({'message':'已完成','downloaded':0}); self.handle.finished.emit()
        self.assertTrue(monitor.timer.isActive()); self.assertEqual(monitor.timer.interval(),30*60000)
        self.context.submit_background_task.assert_called_once(); monitor.stop()
    def test_manual_works_with_automatic_disabled_and_does_not_enable_polling(self):
        self.stage(); self.plugin.store.save_settings({'auto_download':False})
        monitor=self.plugin.receipts; monitor.start(); self.assertFalse(monitor.timer.isActive())
        self.check(manual=True); self.context.submit_background_task.assert_called_once()
        self.handle.finished.emit(); self.assertFalse(monitor.timer.isActive()); monitor.stop()
    def test_interval_change_while_busy_waits_until_worker_finishes(self):
        self.stage(); monitor=self.plugin.receipts; monitor.start(); self.check(manual=True)
        self.plugin.store.save_settings({'poll_minutes':90}); monitor.configure()
        self.assertFalse(monitor.timer.isActive()); self.assertFalse(self.handle.canceled)
        self.handle.finished.emit(); self.assertEqual(monitor.timer.interval(),90*60000); monitor.stop()
    def test_empty_or_offline_check_still_rearms_not_a_dead_single_shot(self):
        monitor=self.plugin.receipts; monitor.start()
        with patch('app_plugins.builtin.ai_agent_assistant.receipt_monitor.is_offline',return_value=True): monitor.tick()
        self.assertTrue(monitor.timer.isActive()); self.assertEqual(monitor.timer.interval(),30*60000)
        self.context.submit_background_task.assert_not_called(); monitor.stop()
    def test_stop_before_late_completion_never_restarts_timer(self):
        self.stage(); monitor=self.plugin.receipts; monitor.start(); self.check()
        self.assertFalse(monitor.stop())
        monitor.completed({'message':'收尾','continue_ids':['later-job'],'downloaded':1})
        self.handle.finished.emit(); self.assertFalse(monitor.timer.isActive()); self.assertEqual(monitor._continue_ids,[])
    def test_budget_continuation_is_scoped_not_a_new_full_poll(self):
        self.stage(); monitor=self.plugin.receipts; monitor.start(); self.check(manual=True)
        monitor.completed({'message':'本轮完成','continue_ids':['only_remaining_job'],'downloaded':8})
        self.handle.finished.emit(); self.assertEqual(monitor.timer.interval(),2000)
        with patch.object(monitor,'download_authorization',return_value=None): monitor.tick()
        payload=self.context.submit_background_task.call_args.kwargs['payload']
        self.assertEqual(payload['ids'],['only_remaining_job']); self.assertFalse(payload['force'])
        monitor.completed({'message':'全部完成','downloaded':1,'continue_ids':[]}); self.handle.finished.emit()
        self.assertEqual(monitor.timer.interval(),30*60000); monitor.stop()
    def test_manual_continuation_survives_auto_off_without_enabling_auto(self):
        self.stage(); self.plugin.store.save_settings({'auto_download':False})
        monitor=self.plugin.receipts; monitor.start(); self.check(manual=True)
        monitor.completed({'message':'继续','continue_ids':['remaining'],'downloaded':8}); self.handle.finished.emit()
        self.assertTrue(monitor.timer.isActive()); self.assertEqual(monitor.timer.interval(),2000)
        with patch.object(monitor,'download_authorization',return_value=None): monitor.tick()
        monitor.completed({'message':'完成','downloaded':1}); self.handle.finished.emit()
        self.assertFalse(monitor.timer.isActive()); self.assertFalse(self.plugin.store.settings()['auto_download'])
        monitor.stop()
    def test_interval_setting_can_save_without_valid_sheet_url(self):
        self.stage(); self.context.task_language=lambda _: 'sk'; dialog=self.plugin.open_dialog()
        dialog.url.setText('unfinished URL'); dialog.poll_interval.setValue(75)
        self.assertEqual(self.plugin.store.settings()['poll_minutes'],75)
        self.assertEqual(self.plugin.store.settings()['sheet'],'口播表格')
    def test_disabling_auto_does_not_cancel_explicit_manual_transfer(self):
        self.stage(); self.context.task_language=lambda _: 'sk'; dialog=self.plugin.open_dialog()
        self.check(manual=True); dialog.auto_download.setChecked(False)
        self.assertFalse(self.handle.canceled); self.handle.finished.emit()


class PollContinuationDataTests(unittest.TestCase):
    setUp=fixture.AgentReceiptTests.setUp
    stage=fixture.AgentReceiptTests.stage
    cloud=fixture.AgentReceiptTests.cloud
    download=fixture.AgentReceiptTests.download
    receipt=fixture.AgentReceiptTests.receipt
    def test_budget_exhausted_records_explicit_continuation(self):
        self.items=[{'id':'video_'+str(index),'url':fixture.LINK} for index in range(10)]
        result=self.receipt()
        self.assertEqual(result['downloaded'],8); self.assertEqual(result['continue_ids'],[self.job['id']])
        next_result=self.receipt(ids=result['continue_ids'])
        self.assertEqual(next_result['downloaded'],2); self.assertEqual(next_result['continue_ids'],[])
    def test_failed_or_review_waits_do_not_cause_fast_background_loop(self):
        self.fetch.side_effect=TimeoutError('temporary failure')
        result=self.receipt(); self.assertTrue(result['pending']); self.assertEqual(result['continue_ids'],[])

    def test_archive_budget_continues_without_restarting_download(self):
        for index in range(2,11):
            job=self.stage(f'{index:03}'); self.api.rows.append(self.cloud(job))
        first=self.receipt(); self.receipt(ids=first['continue_ids'])
        for row in self.api.rows: row[fixture.COLS['archive']]='未下载'
        self.fetch.reset_mock(); first=self.receipt()
        self.assertEqual(len(first['continue_ids']),2); self.fetch.assert_not_called()
        second=self.receipt(ids=first['continue_ids'])
        self.assertEqual(second['continue_ids'],[]); self.fetch.assert_not_called()

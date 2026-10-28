"""Immediate retry shortens waiting, never resets submission/credit safeguards."""
import copy
import time
import unittest

import test_flow_transfer_recovery as transfer
import test_flow_generation_recovery as generation
from app_plugins.builtin.flow_assistant.generation_recovery import confirmed_failure
from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info


class RetryNowTests(unittest.TestCase):
    setUpClass = transfer.TransferTests.__dict__['setUpClass']
    setUp = transfer.TransferTests.setUp
    service = transfer.TransferTests.service
    jobs = transfer.TransferTests.jobs
    client = transfer.TransferTests.client
    upload = transfer.TransferTests.upload
    bound_row = transfer.TransferTests.bound_row
    request = transfer.TransferTests.request
    actions = transfer.TransferTests.actions
    ui = transfer.TransferTests.ui

    def command(self, row):
        return {'action': 'retry_now', 'ids': [row['id']], 'versions': {
            row['id']: {key: row[key] for key in ('attempt', 'browser', 'status')}}}

    def waiting_upload(self):
        service = self.service(); row, client = self.upload(service)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        return service, service.store.get(row['id']), client

    def test_upload_runs_now_once_without_resetting_counter(self):
        service, row, client = self.waiting_upload()
        client['capabilities']=['runtime_controls_v1']
        info = {**retry_info(row, 'upload'), 'count': 2, 'next_at': time.time()+999}
        service._save_transfer(row, 'upload', info); row = service.store.get(row['id'])
        command = self.command(row); service.execute(command); service.execute(command)
        current = service.store.get(row['id'])
        self.assertEqual(retry_info(current, 'upload')['count'], 2,'F5 recovery does not consume an upload retry')
        self.assertEqual(sum(p['action'] == 'repair_project' for p in service.pending.values()), 1)
        self.assertFalse(any(p['action']=='upload_images' for p in service.pending.values()))
        self.assertEqual(current['attempt'], row['attempt'])
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_download_keeps_exact_media_and_skips_backoff_only(self):
        service = self.service(); row, client = self.bound_row(service)
        service.transfer_failed(row, 'download', 'network'); row = service.store.get(row['id'])
        service._save_transfer(row, 'download', {**retry_info(row, 'download'), 'count': 2, 'next_at': time.time()+999})
        service.execute(self.command(service.store.get(row['id'])))
        current = service.store.get(row['id']); self.assertEqual(current['status'], 'download_parked')
        self.assertTrue(retry_info(current,'download')['active'])
        self.assertTrue(current['remote']['download_backlog'])
        self.assertEqual(current['attempt'],row['attempt'])
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))
        self.assertEqual(retry_info(current, 'download')['count'], 3)
        action = self.actions(client)[-1]; self.assertEqual(action['action'], 'retry_download')
        self.assertEqual(action['params']['restore']['bindings'], row['remote']['bindings'])

    def test_prepare_wakes_original_snapshot_without_pretending_ready(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10,budget=0))
        client = self.client(service); service.paused = False; service._tick()
        row = service.store.rows()[0]; service.pending.clear()
        defer_prepare(service, client, row, 'wait', 'FLOW_PAGE_NOT_READY'); service.pending.clear()
        row = service.store.get(row['id']); count = row['remote']['prepare_retry']['count']
        service.execute(self.command(row))
        self.assertLessEqual(client['prepare_wait']['next_at'], time.time())
        self.assertFalse(client['page_ready'])
        self.assertEqual(client['prepare_wait']['count'], count)
        self.assertEqual(sum(p['action'] == 'snapshot' for p in service.pending.values()), 1)
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_active_operation_and_stale_click_do_not_change_current_attempt(self):
        service = self.service(); row, client = self.upload(service); before=copy.deepcopy(service.store.get(row['id']))
        service.execute(self.command(row)); self.assertEqual(service.store.get(row['id']), before)
        service, row, client = self.waiting_upload(); command=self.command(row)
        service.store.update(row['id'], 'uploading', attempt='c'*32)
        service.execute(command); self.assertFalse(service.pending)

    def test_selected_only_wake_keeps_other_jobs_paused(self):
        service, row, client = self.waiting_upload(); service.paused = True
        service.store.enqueue(self.jobs(duration=6,budget=0)); other=service.store.rows()[-1]
        service.execute(self.command(row))
        self.assertTrue(service.paused); self.assertEqual(service.released,{row['id']})
        self.assertEqual(service.store.get(other['id'])['status'],'queued')
        self.assertEqual(sum(p['action']=='upload_images' for p in service.pending.values()),1)

    def test_safety_pause_and_account_block_do_not_get_cleared(self):
        service, row, client = self.waiting_upload(); service.safety_paused = True
        before=service.store.get(row['id']); service.execute(self.command(row))
        self.assertEqual(service.store.get(row['id']),before); self.assertFalse(service.pending)
        service.safety_paused=False; client['blocked']='disabled account'
        service.execute(self.command(row)); self.assertEqual(client['blocked'],'disabled account')
        self.assertFalse(service.pending)

    def test_uncertain_paid_result_is_never_regenerated(self):
        service = self.service(); row,client=self.bound_row(service)
        service.store.update(row['id'],'uncertain'); before=service.store.get(row['id'])
        service.execute(self.command(before)); self.assertEqual(service.store.get(row['id']),before)
        self.assertFalse(service.pending)

    def test_missing_download_binding_keeps_probe_counter_and_never_generates(self):
        service=self.service(); row,client=self.bound_row(service)
        from app_plugins.builtin.flow_assistant.timing import value
        limit=value(service.preferences,'download_probe_attempts')
        remote={**row['remote'],'bindings':[]}; service.store.update(row['id'],'download_failed',remote=remote)
        service._save_transfer(service.store.get(row['id']),'download',
            {'count':2,'probes':limit,'next_at':time.time()+999,'probe_at':time.time(),'active':False,'stopped':False})
        service.execute(self.command(service.store.get(row['id'])))
        current=service.store.get(row['id']); info=retry_info(current,'download')
        self.assertEqual(info['count'],2); self.assertEqual(info['probes'],limit)
        self.assertTrue(info['stopped']); self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_low_credit_hold_is_not_released_by_immediate_upload_retry(self):
        service,row,client=self.waiting_upload()
        import test_flow_credit_cooldown as credit
        credit.CreditCooldownTests.observe(self,service,client,5)
        from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
        hold=copy.deepcopy(credit_hold(client,service.preferences)); self.assertTrue(hold)
        service.execute(self.command(service.store.get(row['id'])))
        self.assertEqual(credit_hold(client,service.preferences),hold)
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_toolbar_routes_waiting_rows_with_attempt_versions(self):
        dialog, plugin, commands = self.ui(); plugin.retry_now_api=1
        service,row,client=self.waiting_upload(); dialog.state={'jobs':[row]}
        dialog.selected_ids=lambda:[row['id']]
        dialog.retry_now_button.click()
        self.assertEqual(commands[-1],self.command(row))


class GenerationRetryNowTests(unittest.TestCase):
    setUpClass = generation.GenerationRecoveryTests.__dict__['setUpClass']
    setUp = generation.GenerationRecoveryTests.setUp
    service = generation.GenerationRecoveryTests.service
    client = generation.GenerationRecoveryTests.client
    browser = generation.GenerationRecoveryTests.browser
    observe = generation.GenerationRecoveryTests.observe
    jobs = generation.GenerationRecoveryTests.jobs
    failed_task = generation.GenerationRecoveryTests.failed_task

    def test_confirmed_failure_needs_actual_fresh_credit_before_new_generation(self):
        service, client, row, message=self.failed_task()
        confirmed_failure(service,client,message); current=service.store.get(row['id'])
        service.execute(RetryNowTests.command(self,current))
        current=service.store.get(row['id'])
        self.assertEqual(current['status'],'queued')
        self.assertEqual(current['payload']['generation_recovery']['count'],1)
        self.assertEqual(sum(p['action']=='get_points' for p in service.pending.values()),1)
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))
        self.assertTrue(service.paused); self.assertEqual(service.released,{row['id']})


if __name__ == '__main__': unittest.main()

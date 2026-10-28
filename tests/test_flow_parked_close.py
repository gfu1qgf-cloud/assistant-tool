"""A finished cloud result with abandoned download must not veto safe closure."""
import copy
import json
import unittest

import test_flow_transfer_recovery as transfer
from app_plugins.builtin.flow_assistant import browser_lifecycle as lifecycle
from app_plugins.builtin.flow_assistant.close_receipts import parked, released
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.runtime_close import request_close, response, CAPABILITY


class ParkedCloseTests(unittest.TestCase):
    setUpClass=transfer.TransferTests.__dict__['setUpClass']
    setUp=transfer.TransferTests.setUp
    service=transfer.TransferTests.service
    client=transfer.TransferTests.client
    jobs=transfer.TransferTests.jobs
    bound_row=transfer.TransferTests.bound_row

    def finished(self):
        service=self.service();row,client=self.bound_row(service)
        remote={**row['remote'],'download_skipped':True}
        service.store.update(row['id'],'download_parked',remote=remote)
        client.update(credits=5,credits_local=True,page_ready=True,identity_unconfirmed=False,
            capabilities=[CAPABILITY,lifecycle.CAPABILITY],saved_project=remote['page'],project=remote['page'])
        service.preferences.setdefault('profile_projects',{})[client['profile']]=remote['page']
        return service,service.store.get(row['id']),client

    def test_safe_closure_includes_only_completed_parked_media_receipt(self):
        service,row,client=self.finished()
        self.assertTrue(request_close(service,client,automatic_reason='low_credit'))
        command=json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(command['params']['released_batches'],[row['attempt']])
        self.assertEqual(service.store.get(row['id']),row,'retain original media and download history')

    def test_uncertain_generation_unready_and_foreign_media_cannot_authorize_close(self):
        for case in ('unfinished','unready','unverified','other_browser','not_parked','not_skipped'):
            with self.subTest(case=case):
                self.setUp();service,row,client=self.finished();remote=copy.deepcopy(row['remote'])
                status=row['status'];browser=row['browser']
                if case=='unfinished':remote['cloud_finished']=False
                elif case=='unready':remote['bindings'][0]['readyIds']=[]
                elif case=='unverified':remote['bindings'][0]['source']='dom'
                elif case=='other_browser':browser='another-browser'
                elif case=='not_parked':status='download_failed'
                else:remote['download_skipped']=False
                service.store.update(row['id'],status,browser=browser,remote=remote)
                self.assertEqual(parked(service,client),[])
                self.assertEqual(released(service,client),[])

    def test_old_close_limit_gets_only_one_bounded_recovery_cycle_for_new_safe_receipts(self):
        service,row,client=self.finished()
        service.preferences['browser_lifecycle']={client['profile']:{'kind':'low_credit','state':'close_failed','close_attempts':3}}
        self.assertTrue(lifecycle.watch(service))
        entry=service.preferences['browser_lifecycle'][client['profile']]
        self.assertTrue(entry['parked_close_fix']);self.assertEqual(entry['state'],'closing')
        service.pending.clear()
        entry.update(state='close_failed',close_attempts=3)
        self.assertFalse(lifecycle.watch(service),'no endless reset of automatic close retries')

    def test_close_refusal_reason_survives_restart_preferences_and_is_visible(self):
        service,row,client=self.finished();request_close(service,client,automatic_reason='low_credit')
        response(service,client,{'success':False,'error':'此配置还有2个非本项目标签页，已保留浏览器'})
        saved=service.store.settings()
        self.assertIn('非本项目标签页',saved['browser_lifecycle'][client['profile']]['last_error'])
        status=browser_status(client,saved,service.store.rows())
        self.assertEqual(status['state'],'自动关闭受阻');self.assertIn('非本项目标签页',status['reason'])


if __name__=='__main__':unittest.main()

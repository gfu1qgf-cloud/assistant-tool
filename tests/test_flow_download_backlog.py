"""Completed generation can release accounts while durable downloads recover."""
import copy,json,time,unittest
from unittest.mock import patch
import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant import download_backlog as backlog
from app_plugins.builtin.flow_assistant.store import QueueStore,choose_client
from app_plugins.builtin.flow_assistant.credit_packs import active_count
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info


class DownloadBacklogTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    bound_row=fixtures.TransferTests.bound_row
    client=fixtures.TransferTests.client

    def ready(self,status='download_failed'):
        service=self.service();service.timer.stop();row,client=self.bound_row(service,status=status)
        return service,row,client

    def messages(self,client):return [json.loads(c.args[0]) for c in client['socket'].sendTextMessage.call_args_list]

    def test_finished_download_releases_generation_lane_and_original_account(self):
        service,row,client=self.ready('downloading');before=copy.deepcopy(row['remote']['bindings'])
        self.assertEqual(active_count(service.store.rows()),1)
        self.assertTrue(backlog.watch(service));parked=service.store.get(row['id'])
        self.assertEqual(parked['status'],'download_parked');self.assertEqual(active_count(service.store.rows()),0)
        self.assertEqual(choose_client([client],service.store.rows(),1)['id'],client['id'])
        self.assertEqual(parked['remote']['bindings'],before);self.assertEqual(parked['attempt'],row['attempt'])

    def test_cloud_uncertainty_is_not_mislabeled_as_download_only(self):
        service,row,client=self.ready();remote=copy.deepcopy(row['remote']);remote['bindings'][0]['readyIds']=[]
        remote['cloud_finished']=True;service.store.update(row['id'],'download_failed',remote=remote)
        self.assertFalse(backlog.watch(service));self.assertNotIn('download_backlog',service.store.get(row['id'])['remote'])

    def test_download_retry_and_reply_remain_outside_generation_slots(self):
        service,row,client=self.ready();backlog.watch(service);start=time.time()
        with patch('time.time',return_value=start+6):self.assertTrue(backlog.watch(service))
        key=next(k for k,p in service.pending.items() if p['action']=='retry_download')
        service._response(client,{'request_id':key,'success':True,'data':{'downloadId':42}})
        current=service.store.get(row['id']);self.assertEqual(current['status'],'download_parked')
        self.assertEqual(retry_info(current,'download')['download_id'],42);self.assertEqual(active_count(service.store.rows()),0)
        self.assertEqual(self.messages(client)[-1]['action'],'retry_download')
        self.assertNotIn('run_tasks',[m.get('action') for m in self.messages(client)])

    def test_waiting_downloads_survive_store_recovery_and_resume_same_browser(self):
        service,row,client=self.ready();backlog.watch(service)
        service.transfer_failed(service.store.get(row['id']),'download','temporary',permanent=True)
        restored=QueueStore(self.namespace);restored.recover();saved=restored.get(row['id'])
        self.assertEqual(saved['status'],'download_parked');self.assertTrue(retry_info(saved,'download')['stopped'])
        client['connected']=time.time()+5;self.assertTrue(backlog.reconnect(service,client));self.assertFalse(backlog.reconnect(service,client))
        current=service.store.get(row['id']);self.assertFalse(retry_info(current,'download')['stopped'])
        with patch('time.time',return_value=time.time()+20):backlog.watch(service)
        command=next(m for m in self.messages(client) if m['action']=='retry_download')
        self.assertEqual(command['params']['batch_id'],row['attempt'])
        self.assertEqual(command['params']['restore']['bindings'],row['remote']['bindings'])

    def test_automatic_reopen_does_not_require_the_previous_video_detail_tab(self):
        service,row,client=self.ready();remote={**row['remote'],'download_refresh':{
            'project':row['remote']['page'],'media_id':'media','video_page':row['remote']['page']+'/edit/media'}}
        service.store.update(row['id'],'download_failed',remote=remote);backlog.watch(service)
        client['connected']=time.time()+2;backlog.reconnect(service,client)
        with patch('time.time',return_value=time.time()+20):backlog.watch(service)
        command=next(m for m in self.messages(client) if m['action']=='retry_download')
        self.assertNotIn('video_page',command['params']['restore'])
        self.assertEqual(command['params']['restore']['media_id'],'media')

    def test_old_reply_cannot_overwrite_new_generation_on_same_browser(self):
        service,row,client=self.ready();backlog.watch(service);service.retry_download(service.store.get(row['id']))
        key=next(k for k,p in service.pending.items() if p['action']=='retry_download')
        service.store.enqueue(self.jobs(prompt='new task',budget=0));new=service.store.rows()[-1]
        service.store.update(new['id'],'preparing',attempt='b'*32,browser=client['id'],remote={'account':client['account']})
        before=copy.deepcopy(service.store.get(new['id']))
        service._response(client,{'request_id':key,'success':True,'data':{'downloadId':6}})
        self.assertEqual(service.store.get(new['id']),before);self.assertEqual(active_count(service.store.rows()),1)

    def test_never_finishing_native_download_is_parked_once_without_touching_generation(self):
        service,row,client=self.ready('downloading');client['capabilities']=['download_backlog_v1']
        backlog.watch(service);since=service.store.get(row['id'])['remote']['download_backlog']['since']
        with patch('time.time',return_value=since+121):self.assertTrue(backlog.watch(service))
        command=next(m for m in self.messages(client) if m['action']=='skip_download')
        self.assertEqual(command['params'],{'batch_id':row['attempt'],'park_native_downloads':True})
        count=len(self.messages(client))
        with patch('time.time',return_value=since+300):backlog.watch(service)
        self.assertEqual(len(self.messages(client)),count);self.assertEqual(active_count(service.store.rows()),0)

    def test_busy_generation_and_changed_account_defer_background_recovery(self):
        for flag in ('native_busy','batch_running','different_account'):
            self.setUp();service,row,client=self.ready();backlog.watch(service)
            if flag=='different_account':client['account']='other@example.test'
            else:client[flag]=True
            with patch('time.time',return_value=time.time()+20):backlog.watch(service)
            self.assertNotIn('retry_download',[m['action'] for m in self.messages(client)])

    def test_eight_pending_downloads_cannot_fill_four_generation_lanes(self):
        service,row,client=self.ready('downloading');clients=[client]
        template=copy.deepcopy(row['remote'])
        for index in range(1,8):
            other=self.client(service,'Profile '+str(index));clients.append(other)
            service.store.enqueue(self.jobs(prompt='pending '+str(index),budget=0));added=service.store.rows()[-1]
            service.store.update(added['id'],'downloading',attempt=f'{index:032x}',browser=other['id'],
                remote={**template,'account':other['account'],'profile':other['profile']})
        self.assertEqual(active_count(service.store.rows()),8);backlog.watch(service)
        self.assertEqual(active_count(service.store.rows()),0)
        available=[]
        service.store.enqueue(self.jobs(count=4,prompt='new generation',budget=0))
        for new in [r for r in service.store.rows() if r['status']=='queued']:
            selected=choose_client(clients,service.store.rows(),4)
            self.assertIsNotNone(selected);available.append(selected['id'])
            service.store.update(new['id'],'preparing',attempt='f'*32,browser=selected['id'],
                remote={'account':selected['account'],'profile':selected['profile']})
        self.assertEqual(len(set(available)),4);self.assertEqual(active_count(service.store.rows()),4)

    def test_confirmed_cancel_reopens_a_download_blocked_close_check(self):
        service,row,client=self.ready('downloading');client['capabilities']=['download_backlog_v1']
        backlog.watch(service);since=service.store.get(row['id'])['remote']['download_backlog']['since']
        service.preferences['browser_lifecycle']={client['profile']:{'state':'close_failed','kind':'low_credit','close_attempts':3,
            'next_at':time.time()+600,'last_error':'此浏览器仍有下载未完成'}}
        with patch('time.time',return_value=since+121):backlog.watch(service)
        key=next(k for k,p in service.pending.items() if p.get('backlog_attempt'))
        service._response(client,{'request_id':key,'success':True,'data':{'parked':True,'cancelled':1}})
        self.assertFalse(retry_info(service.store.get(row['id']),'download')['active'])
        self.assertEqual(service.preferences['browser_lifecycle'][client['profile']]['next_at'],0)

    def test_packed_download_wait_is_released_for_each_attempt_not_only_last(self):
        service,row,client=self.ready('downloading');client.update(batch_running=True,active_batch='c'*32,
            capabilities=['skip_download_v1','credit_pack_submit_v1'])
        remote={**row['remote'],'credit_pack':{'id':'pack','account':client['account']}}
        service.store.update(row['id'],'downloading',remote=remote)
        for index,attempt in enumerate(('b'*32,'c'*32)):
            service.store.enqueue(self.jobs(prompt='pack '+str(index),budget=0));new=service.store.rows()[-1]
            service.store.update(new['id'],'downloading',attempt=attempt,browser=client['id'],remote=remote)
        for attempt in ('a'*32,'b'*32,'c'*32):
            service.watch_transfer_retries()
            key=next(k for k,p in service.pending.items() if p['action']=='skip_download')
            self.assertEqual(self.messages(client)[-1]['params']['batch_id'],attempt)
            service._response(client,{'request_id':key,'success':True})
        waits=client['skip_download_waits'];self.assertEqual(len(waits),3)
        self.assertTrue(all(e['count']==1 for e in waits.values()))


if __name__=='__main__':unittest.main()

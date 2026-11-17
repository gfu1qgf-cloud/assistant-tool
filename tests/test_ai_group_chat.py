import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from PyQt6 import sip
from qt_compat import QtCore, QtWidgets
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client.group_service import default_group,validate_group,group_messages,preflight_group
from app_plugins.builtin.ai_model_client.group_ui import GroupSettings,GroupChatDialog


class GroupConfigTests(unittest.TestCase):
    def test_round_and_member_limits_and_unique_names(self):
        config=default_group('mistral','mistral-small-latest')
        self.assertEqual(len(validate_group(config)['members']),4)
        for count in (0,6,100):
            with self.assertRaises(ValueError):validate_group({**config,'round_limit':count})
        with self.assertRaises(ValueError):validate_group({**config,'members':config['members'][:1]})
        duplicate=copy.deepcopy(config);duplicate['members'][1]['name']=duplicate['members'][0]['name']
        with self.assertRaises(ValueError):validate_group(duplicate)


class GroupUITests(unittest.TestCase):
    setUpClass=base.UITests.__dict__['setUpClass']
    setUp=base.UITests.setUp
    wait=base.UITests.wait
    result=base.UITests.result

    def open_group(self,rounds=2):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        group=self.dialog.open_group_chat()
        self.assertIsNotNone(group,self.dialog.status.text())
        group.consent=True
        config=default_group('mistral','mistral-small-latest')
        config['members']=config['members'][:2]
        config['round_limit']=rounds
        group.group_config=config
        return group

    def tearDown(self):
        self.stop_group_workers()
        base.UITests.tearDown(self)

    def stop_group_workers(self):
        # Normal group windows deliberately keep the current API request alive
        # when hidden. A test must cancel/join those children before deleting
        # its temporary store, including when an assertion just failed.
        groups=self.dialog.findChildren(GroupChatDialog)
        for group in groups:
            if not sip.isdeleted(group):
                group.pause_group()
                if group.handle:group.handle.requestInterruption()
        for group in groups:
            if not sip.isdeleted(group):
                self.wait(lambda current=group: current.handle is None,timeout=15)
                if not sip.isdeleted(group):group.close()

    def test_cleanup_cancels_and_joins_child_request_before_store_removal(self):
        group=self.open_group(rounds=1)
        started,canceled=threading.Event(),threading.Event()
        def chat(*args,**_kwargs):
            job=args[-1];started.set()
            while not job.is_canceled():time.sleep(.003)
            canceled.set();job.check_canceled()
        with patch.object(group.client,'chat',side_effect=chat) as caller:
            group.input.setPlainText('cleanup fixture');group.send()
            self.wait(started.is_set)
            self.stop_group_workers()
            self.assertTrue(canceled.is_set())
            self.assertIsNone(group.handle)
            self.assertIsNone(group._group_plan)
            self.assertEqual(caller.call_count,1)
            self.assertTrue(group.store.messages(group.current_id))

    def settled(self,group):
        total=group.group_config['round_limit']*len(group.group_config['members'])
        deadline=time.monotonic()+20+2*total
        last_step=None;last_progress=time.monotonic()
        while group.handle is not None or group._group_plan is not None:
            plan=group._group_plan
            step=plan['step'] if plan else total
            now=time.monotonic()
            if step!=last_step:last_step,last_progress=step,now
            detail=f'step={step}/{total}, status={group.status.text()!r}'
            self.assertLess(now-last_progress,10,'group stalled: '+detail)
            self.assertLess(now,deadline,'group exceeded total deadline: '+detail)
            self.app.processEvents();time.sleep(.003)

    def test_wait_accepts_progressing_thirty_call_discussion_beyond_old_total_limit(self):
        now=[0.];group=SimpleNamespace(handle=object(),_group_plan={'step':0},
            group_config={'round_limit':5,'members':list(range(6))},status=SimpleNamespace(text=lambda:'synthetic'))
        def advance():
            now[0]+=.7;group._group_plan['step']+=1
            if group._group_plan['step']==30:group.handle=group._group_plan=None
        app=self.app
        try:
            self.app=SimpleNamespace(processEvents=advance)
            with patch.object(time,'monotonic',side_effect=lambda:now[0]),patch.object(time,'sleep'):
                self.settled(group)
        finally:self.app=app
        self.assertGreater(now[0],15)

    def test_wait_rejects_a_stalled_discussion_even_before_total_deadline(self):
        now=[0.];group=SimpleNamespace(handle=object(),_group_plan={'step':0},
            group_config={'round_limit':5,'members':list(range(6))},status=SimpleNamespace(text=lambda:'synthetic'))
        app=self.app
        try:
            self.app=SimpleNamespace(processEvents=lambda:now.__setitem__(0,now[0]+2))
            with patch.object(time,'monotonic',side_effect=lambda:now[0]),patch.object(time,'sleep'):
                with self.assertRaisesRegex(AssertionError,'group stalled'):
                    self.settled(group)
        finally:self.app=app
        self.assertLess(now[0],15)

    def test_two_members_two_rounds_then_followup_and_separate_history(self):
        group=self.open_group()
        calls=[]
        def chat(provider,model,messages,options,config,job,**kwargs):
            calls.append((messages,options))
            text='发言'+str(len(calls))
            if kwargs.get('on_delta'):kwargs['on_delta'](text)
            return {**self.result(),'text':text}
        with patch.object(group.client,'chat',side_effect=chat):
            group.input.setPlainText('我要开一家咖啡店')
            group.send();self.settled(group)
            self.assertEqual(len(calls),4)
            rows=group.store.messages(group.current_id)
            self.assertEqual([r['metadata'].get('group_member') for r in rows[1:]],['策划','挑错专家','策划','挑错专家'])
            self.assertEqual([r['metadata'].get('group_round') for r in rows[1:]],[1,1,2,2])
            self.assertIn('发言1',calls[1][0][0]['text'])
            self.assertIn('策划',calls[1][0][0]['text'])
            self.assertIn('挑错专家',calls[1][1]['system'])
            self.assertEqual(rows[0]['metadata']['group_discussion']['calls_started'],4)
            self.assertEqual(rows[0]['metadata']['group_discussion']['state'],'complete')
            group.input.setPlainText('预算只有10万，重新讨论')
            group.send();self.settled(group)
        self.assertEqual(len(calls),8)
        self.assertIn('预算只有10万',calls[4][0][0]['text'])
        self.assertEqual(len(self.store.messages(self.dialog.current_id)),0)
        self.assertIn('策划',group.transcript.toPlainText())
        self.assertIn('第2轮',group.transcript.toPlainText())

    def test_pause_after_current_does_not_call_next_member(self):
        group=self.open_group()
        started,release=threading.Event(),threading.Event()
        def chat(*args,**kwargs):started.set();release.wait(4);return self.result()
        try:
            with patch.object(group.client,'chat',side_effect=chat) as caller:
                group.input.setPlainText('讨论一个话题');group.send()
                self.wait(started.is_set)
                group.pause_group();release.set();self.settled(group)
                self.assertEqual(caller.call_count,1)
        finally:release.set()
        root=group.store.messages(group.current_id)[0]
        self.assertEqual(root['metadata']['group_discussion']['state'],'paused')

    def test_interjection_after_current_starts_bounded_new_turn(self):
        group=self.open_group(rounds=1)
        started,release=threading.Event(),threading.Event()
        count=0;contents=[]
        def chat(provider,model,messages,*args,**kwargs):
            nonlocal count
            count+=1;contents.append(messages[0]['text'])
            if count==1:started.set();release.wait(4)
            return {**self.result(),'text':'回答'+str(count)}
        try:
            with patch.object(group.client,'chat',side_effect=chat):
                group.input.setPlainText('原话题');group.send();self.wait(started.is_set)
                group.input.setPlainText('补充：预算只有10万');group.send()
                release.set();self.settled(group)
        finally:release.set()
        self.assertEqual(count,3)
        self.assertIn('预算只有10万',contents[1])
        self.assertEqual(sum(row['role']=='user' for row in group.store.messages(group.current_id)),2)

    def test_error_keeps_successful_members_and_stops_without_retry(self):
        group=self.open_group()
        with patch.object(group.client,'chat',side_effect=[{**self.result(),'text':'第一位完成'},base.AIProviderError('模拟超时')]) as chat:
            group.input.setPlainText('话题');group.send();self.settled(group)
        self.assertEqual(chat.call_count,2)
        rows=group.store.messages(group.current_id)
        self.assertIn('第一位完成',[row['text'] for row in rows])
        self.assertEqual(rows[0]['metadata']['group_discussion']['state'],'failed')
        self.assertEqual(rows[-1]['status'],'failed')

    def test_group_settings_persist_and_retry_keeps_old_discussion_branch(self):
        group=self.open_group(rounds=1)
        group.save_draft()
        original_config=copy.deepcopy(group.group_config)
        with patch.object(group.client,'chat',return_value={**self.result(),'text':'原发言'}):
            group.input.setPlainText('原问题');group.send();self.settled(group)
        original=group.store.messages(group.current_id)[0]['id']
        with patch.object(group.client,'chat',return_value={**self.result(),'text':'新发言'}) as chat:
            group.retry_request();self.settled(group)
        self.assertEqual(chat.call_count,2)
        group.switch_branch(original)
        self.assertEqual(group.store.messages(group.current_id)[-1]['text'],'原发言')
        self.assertEqual(group.store.get(group.current_id)['options']['group_chat'],original_config)

    def test_private_group_uses_memory_and_is_not_registered_in_normal_history(self):
        private=self.dialog.open_private_chat()
        private.provider.setCurrentIndex(private.provider.findData('mistral'));private.model.setCurrentText('mistral-small-latest')
        group=private.open_group_chat()
        self.assertTrue(group.is_private)
        self.assertIsNone(group.store.path)
        store=group.store
        group.close()
        self.assertTrue(store.closed)
        private.close()

    def test_open_and_member_config_do_not_generate(self):
        with patch.object(self.context.ai_client,'chat') as chat:
            group=self.open_group()
            self.app.processEvents()
            chat.assert_not_called()

    def test_group_duplicate_step_claim_does_not_allow_a_second_call(self):
        group=self.open_group(rounds=1)
        root=group.store.begin_request(group.current_id,'问题',[],'mistral','test')
        group.store.begin_group(group.current_id,root,group.group_config)
        group.store.claim_group_step(group.current_id,root,0)
        with self.assertRaises(ValueError):group.store.claim_group_step(group.current_id,root,0)
        group.store.finish_group(group.current_id,root,'paused')

    def test_cancel_stops_stream_and_failed_partial_is_not_future_context(self):
        group=self.open_group(rounds=1)
        started=threading.Event()
        def chat(*args,**kwargs):
            kwargs['on_delta']('不可重放的失败片段')
            started.set()
            while not args[-1].is_canceled():time.sleep(.003)
            args[-1].check_canceled()
        with patch.object(group.client,'chat',side_effect=chat) as caller:
            group.input.setPlainText('原问题');group.send();self.wait(started.is_set)
            group.stop_request();self.settled(group)
        self.assertEqual(caller.call_count,1)
        rows=group.store.messages(group.current_id)
        self.assertEqual(rows[-1]['status'],'failed')
        self.assertIn('不可重放',rows[-1]['text'])
        self.assertEqual(rows[0]['metadata']['group_discussion']['state'],'paused')
        with patch.object(group.client,'chat',return_value=self.result()) as caller:
            group.input.setPlainText('新问题');group.send();self.settled(group)
        self.assertNotIn('不可重放',caller.call_args.args[2][0]['text'])

    def test_close_normal_group_stops_following_members_and_queued_input(self):
        group=self.open_group()
        started,release=threading.Event(),threading.Event()
        def chat(*args,**kwargs):started.set();release.wait(4);return self.result()
        try:
            with patch.object(group.client,'chat',side_effect=chat) as caller:
                group.input.setPlainText('原问题');group.send();self.wait(started.is_set)
                group.input.setPlainText('不要在后台自动发送');group.send()
                group.close();release.set();self.settled(group)
                self.assertEqual(caller.call_count,1)
        finally:release.set()
        self.assertEqual(group.input.toPlainText(),'不要在后台自动发送')
        self.assertEqual(group.store.messages(group.current_id)[0]['metadata']['group_discussion']['state'],'paused')

    def test_edit_question_creates_complete_sibling_discussion(self):
        group=self.open_group(rounds=1)
        with patch.object(group.client,'chat',return_value=self.result()):
            group.input.setPlainText('原问题');group.send();self.settled(group)
            original=group.store.messages(group.current_id)[0]['id']
            group.input.setPlainText('未发送草稿')
            group.send(edit_id=original,edited_text='修改后的问题');self.settled(group)
        self.assertEqual(group.input.toPlainText(),'未发送草稿')
        rows=group.store.messages(group.current_id)
        self.assertEqual(rows[0]['text'],'修改后的问题')
        self.assertEqual(len(rows),3)
        group.switch_branch(original)
        self.assertEqual(group.store.messages(group.current_id)[0]['text'],'原问题')
        self.assertEqual(len(group.store.messages(group.current_id,all_branches=True)),6)

    def test_context_zero_old_topics_keeps_current_members_and_char_limit(self):
        group=self.open_group(rounds=1);store=group.store;identifier=group.current_id
        old=store.begin_request(identifier,'旧话题标记',[],'mistral','test')
        store.complete_request(identifier,old,{**self.result(),'text':'旧讨论标记'})
        root=store.begin_request(identifier,'当前问题标记',[],'mistral','test')
        store.begin_group(identifier,root,group.group_config)
        member=group.group_config['members'][0]
        leaf=store.append_group_message(identifier,root,root,{**self.result(),'text':'当前成员观点标记'},member,1)
        options={**group.options(),'rounds':0,'context_chars':1000}
        messages,system=group_messages(store,identifier,root,leaf,member,1,options)
        self.assertIn('当前成员观点标记',messages[0]['text'])
        self.assertNotIn('旧话题标记',messages[0]['text'])
        options['context_chars']=len(system)+len('用户最新问题：\n当前问题标记\n\n现在请“策划”进行第1轮发言。')
        messages,system=group_messages(store,identifier,root,leaf,member,1,options)
        self.assertIn('当前问题标记',messages[0]['text'])
        self.assertNotIn('当前成员观点标记',messages[0]['text'])
        self.assertLessEqual(len(system)+len(messages[0]['text']),options['context_chars'])
        store.finish_group(identifier,root,'paused')

    def test_preflight_all_members_before_first_paid_generation(self):
        group=self.open_group(rounds=1)
        config=copy.deepcopy(group.group_config);config['members'][1]['role']='角色'*2000
        with self.assertRaises(ValueError):preflight_group('问题',config,{**group.options(),'context_chars':1000})
        group.group_config=config;group.context_chars.setValue(1000)
        with patch.object(group.client,'chat') as caller:
            group.input.setPlainText('保留原文');group.send();self.app.processEvents()
            caller.assert_not_called()
        self.assertIsNone(group._group_plan)
        self.assertEqual(group.input.toPlainText(),'保留原文')

    def test_settings_order_limits_and_models_are_local_only(self):
        group=self.open_group()
        with patch.object(group.client,'chat') as caller:
            dialog=GroupSettings(group.group_config,group)
            dialog.name.setText('新名字');dialog.move(1)
            self.assertEqual(dialog.value['members'][1]['name'],'新名字')
            dialog.round_limit.setValue(5)
            for _ in range(8):dialog.add_member()
            self.assertEqual(len(dialog.value['members']),6)
            self.assertIn('30',dialog.cost.text())
            dialog.use_current();dialog.accept_config()
            self.assertEqual(dialog.result(),QtWidgets.QDialog.DialogCode.Accepted)
            caller.assert_not_called()
            dialog.deleteLater()

    def test_reopening_group_store_marks_running_discussion_interrupted_without_calls(self):
        group=self.open_group(rounds=1)
        root=group.store.begin_request(group.current_id,'被中断的问题',[],'mistral','test')
        group.store.begin_group(group.current_id,root,group.group_config)
        group.save_draft();group.close()
        reopened=base.ChatStore(group.store.root)
        with patch.object(group.client,'chat') as caller:
            other=GroupChatDialog(self.context,reopened,self.dialog)
            self.app.processEvents();caller.assert_not_called()
            row=reopened.messages(group.current_id)[0]
            self.assertEqual(row['metadata']['group_discussion']['state'],'interrupted')
            self.assertIsNone(other._group_plan)
            other.close();other.deleteLater()

    def test_group_permanent_delete_removes_all_discussion_branches(self):
        group=self.open_group(rounds=1)
        with patch.object(group.client,'chat',return_value=self.result()):
            group.input.setPlainText('删除测试的群聊');group.send();self.settled(group)
            group.retry_request();self.settled(group)
        identifier=group.current_id
        self.assertEqual(len(group.store.messages(identifier,all_branches=True)),6)
        group.store.delete(identifier)
        self.assertEqual(group.store.messages(identifier,all_branches=True),[])
        self.assertEqual(len(self.store.messages(self.dialog.current_id)),0)

    def test_members_use_their_own_provider_model_role_and_pinned_connection(self):
        group=self.open_group(rounds=1)
        group.group_config['members'][1].update(provider='openai',model='gpt-4.1-mini',role='独立核算成本')
        seen=[]
        def chat(provider,model,messages,options,config,job,**kwargs):
            seen.append((provider,model,options['system'],config))
            return {**self.result(),'provider':provider,'model':model}
        with patch('app_plugins.builtin.ai_model_client.group_ui.pin_provider_config',side_effect=lambda snapshot,provider:{'pinned':provider}) as pin:
            with patch.object(group.client,'chat',side_effect=chat):
                group.input.setPlainText('比较方案');group.send();self.settled(group)
        self.assertEqual(pin.call_count,2)
        self.assertEqual([(s[0],s[1]) for s in seen],[('mistral','mistral-small-latest'),('openai','gpt-4.1-mini')])
        self.assertIn('独立核算成本',seen[1][2])
        self.assertNotIn('独立核算成本',seen[0][2])
        self.assertEqual(seen[1][3],{'pinned':'openai'})

    def test_private_group_close_cancels_worker_and_clears_memory_without_next_member(self):
        normal=self.open_group(rounds=1)
        group=normal.open_private_chat();group.consent=True
        group.group_config=copy.deepcopy(normal.group_config)
        started=threading.Event();store=group.store
        def chat(*args,**kwargs):
            started.set()
            while not args[-1].is_canceled():time.sleep(.003)
            args[-1].check_canceled()
        with patch.object(group.client,'chat',side_effect=chat) as caller:
            group.input.setPlainText('无痕问题');group.send();self.wait(started.is_set)
            group.close();self.wait(lambda:store.closed)
            self.assertEqual(caller.call_count,1)
        self.assertIsNone(store.path)
        self.assertEqual(normal.store.messages(normal.current_id),[])

    def test_largest_discussion_stops_at_thirty_calls(self):
        group=self.open_group(rounds=5)
        config=default_group('mistral','mistral-small-latest')
        for number in range(2):
            config['members'].append({**config['members'][0],'id':'extra'+str(number),'name':'顾问'+str(number)})
        config['round_limit']=5;group.group_config=config
        with patch.object(group.client,'chat',return_value=self.result()) as caller:
            group.input.setPlainText('验证讨论上限');group.send()
            self.settled(group)
        self.assertEqual(caller.call_count,30)
        root=group.store.messages(group.current_id)[0]['metadata']['group_discussion']
        self.assertEqual((root['calls_started'],root['calls_done'],root['state']),(30,30,'complete'))

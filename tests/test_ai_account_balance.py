import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import patch
from qt_compat import QtCore, QtGui
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client.account_balance import query_openrouter,dollars,LINKS


class BalanceServiceTests(unittest.TestCase):
    def setUp(self):self.client=base.AIProviderClient();self.job=base.Job()

    def query(self,data,kind='key',code=200):
        response=base.Response(data,code)
        with patch('model.AIProviderClient.requests.request',return_value=response) as request:
            result=query_openrouter(dict(kind=kind,token='synthetic-balance-secret'),self.job,client=self.client)
        self.assertTrue(response.closed)
        self.assertEqual(request.call_args.args[:2],('GET','https://openrouter.ai/api/v1/'+('key' if kind=='key' else 'credits')))
        self.assertFalse(request.call_args.kwargs['allow_redirects'])
        self.assertIsNone(request.call_args.kwargs['json'])
        return result

    def test_key_amounts_do_not_become_account_balance(self):
        result=self.query({'data':{'limit':100,'limit_remaining':74.5,'usage':25.5,'usage_daily':0,'usage_weekly':2.3,'usage_monthly':25.5,'limit_reset':'monthly','label':'ignored'}})
        self.assertEqual(result['limit_remaining'],Decimal('74.5'))
        self.assertEqual(result['usage_daily'],0)
        self.assertNotIn('remaining',result)
        self.assertNotIn('label',result)
        self.assertEqual(dollars(result['usage_daily']),'$0.00')

    def test_unlimited_key_and_missing_values_are_not_infinite_money_or_zero(self):
        result=self.query({'data':{'limit':None,'limit_remaining':None,'usage':0}})
        self.assertTrue(result['unlimited'])
        self.assertIsNone(result['limit_remaining'])
        self.assertEqual(dollars(result['usage_daily']),'接口未提供')

    def test_account_credit_snapshot_uses_decimal(self):
        result=self.query({'data':{'total_credits':100.5,'total_usage':25.75}},kind='account')
        self.assertEqual(result['remaining'],Decimal('74.75'))
        self.assertEqual(self.query({'data':{'total_credits':0.3,'total_usage':0.1}},kind='account')['remaining'],Decimal('0.2'))

    def test_overspent_account_can_report_negative_and_not_silently_clamp(self):
        result=self.query({'data':{'total_credits':1,'total_usage':1.1}},kind='account')
        self.assertEqual(dollars(result['remaining']),'$-0.10')

    def test_invalid_missing_or_nonfinite_amounts_fail_without_fabricating_balance(self):
        for data in ({'data':{}},{'data':{'total_credits':10}}, {'data':{'total_credits':True,'total_usage':0}},
                     {'data':{'total_credits':'NaN','total_usage':0}}, {'data':{'total_credits':1,'total_usage':-1}}):
            with self.subTest(data=data),self.assertRaises(base.AIProviderError):self.query(data,kind='account')

    def test_forbidden_credits_explains_management_key_no_retry(self):
        response=base.Response({'error':{'message':'Only management keys can perform this operation'}},403)
        with patch('model.AIProviderClient.requests.request',return_value=response) as request:
            with self.assertRaisesRegex(base.AIProviderError,'管理Key'):
                query_openrouter(dict(kind='account',token='synthetic-balance-secret'),self.job,client=self.client)
        self.assertEqual(request.call_count,1);self.assertTrue(response.closed)

    def test_error_echo_cannot_expose_query_key(self):
        with patch('model.AIProviderClient.requests.request',return_value=base.Response({'error':{'message':'synthetic-balance-secret'}},401)):
            with self.assertRaises(base.AIProviderError) as error:
                query_openrouter(dict(kind='key',token='synthetic-balance-secret'),self.job,client=self.client)
        self.assertNotIn('synthetic-balance-secret',str(error.exception))

    def test_offline_and_cancellation_prevent_http(self):
        with patch('app_plugins.builtin.ai_model_client.account_balance.require_online',side_effect=base.OfflineModeError('离线')):
            with patch('model.AIProviderClient.requests.request') as request:
                with self.assertRaises(base.OfflineModeError):query_openrouter(dict(kind='key',token='test'),self.job,client=self.client)
                request.assert_not_called()
        self.job.canceled=True
        with patch('model.AIProviderClient.requests.request') as request:
            with self.assertRaises(base.BackgroundTaskCanceled):query_openrouter(dict(kind='key',token='test'),self.job,client=self.client)
            request.assert_not_called()


class BalanceUITests(unittest.TestCase):
    setUpClass=base.UITests.__dict__['setUpClass']
    setUp=base.UITests.setUp
    tearDown=base.UITests.tearDown
    wait=base.UITests.wait

    def open_panel(self):
        panel=self.dialog.open_balance()
        self.assertIsNotNone(panel,self.dialog.status.text())
        return panel

    def settled(self,panel):self.wait(lambda:panel.handle is None)

    def key_reply(self):return {'data':{'limit':100,'limit_remaining':74.5,'usage':25.5,'usage_daily':0,'usage_weekly':2,'usage_monthly':25.5}}

    def test_visible_entry_and_openai_official_link_no_request_on_open(self):
        with patch.object(self.dialog.client,'chat') as chat,patch.object(self.dialog.client,'_request') as request:
            panel=self.open_panel();self.app.processEvents()
            self.assertEqual(self.dialog.balance_button.text(),'余额 / 用量')
            self.assertIn('官方账单',panel.openai_value.text())
            self.assertIs(panel,self.dialog.open_balance())
            with patch.object(QtGui.QDesktopServices,'openUrl',return_value=True) as browser:
                panel.openai_button.click()
            self.assertEqual(browser.call_args.args[0].toString(),LINKS['openai_balance'])
            chat.assert_not_called();request.assert_not_called()

    def test_key_query_uses_current_key_and_does_not_modify_config_or_chat(self):
        panel=self.open_panel();before=self.context.load_config();rows=self.store.messages(self.dialog.current_id)
        with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-current-key'):
            with patch.object(panel.client,'_request',return_value=self.key_reply()) as request:
                panel.refresh_key();self.settled(panel)
        self.assertEqual(request.call_args.args[1],'synthetic-current-key')
        self.assertIn('$74.50',panel.key_remaining.text())
        self.assertIn('$0.00',panel.key_periods.text())
        self.assertIn('尚未查询',panel.account_remaining.text())
        self.assertEqual(self.context.load_config(),before)
        self.assertEqual(self.store.messages(self.dialog.current_id),rows)

    def test_unlimited_label_does_not_claim_infinite_balance(self):
        panel=self.open_panel()
        with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
            with patch.object(panel.client,'_request',return_value={'data':{'limit':None,'usage':0}}):
                panel.refresh_key();self.settled(panel)
        self.assertIn('不代表账户余额无限',panel.key_remaining.text())

    def test_management_token_is_transient_and_worker_payload_cleared(self):
        panel=self.open_panel();before=self.context.load_config()
        panel.management_key.setText('synthetic-management-secret')
        with patch.object(panel.client,'_request',return_value={'data':{'total_credits':100,'total_usage':7}}):
            panel.refresh_account();worker=panel.handle
            self.assertEqual(panel.management_key.text(),'')
            self.settled(panel)
            self.assertEqual(worker.payload,{})
            self.assertIsNone(worker.runner)
        self.assertIn('$93.00',panel.account_remaining.text())
        self.assertEqual(before,self.context.load_config())
        self.assertNotIn('synthetic-management-secret',str(self.context.logs))

    def test_refresh_failure_clears_previous_success_and_missing_key_keeps_ui_responsive(self):
        panel=self.open_panel()
        with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
            with patch.object(panel.client,'_request',return_value=self.key_reply()):
                panel.refresh_key();self.settled(panel)
            with patch.object(panel.client,'_request',side_effect=base.AIProviderError('模拟超时')):
                panel.refresh_key();self.settled(panel)
        self.assertNotIn('$74.50',panel.key_remaining.text())
        self.assertIn('模拟超时',panel.status.text())
        self.assertTrue(panel.key_button.isEnabled())

    def test_changed_key_during_query_ignores_late_result(self):
        panel=self.open_panel();started,release=threading.Event(),threading.Event()
        def reply(*args,**kwargs):started.set();release.wait(3);return self.key_reply()
        try:
            with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
                with patch('app_plugins.builtin.ai_model_client.balance_ui.provider_key_identity',side_effect=['old','new']):
                    with patch.object(panel.client,'_request',side_effect=reply):
                        panel.refresh_key();self.wait(started.is_set);release.set();self.settled(panel)
        finally:release.set()
        self.assertNotIn('$74.50',panel.key_remaining.text())
        self.assertIn('已忽略',panel.status.text())

    def test_close_cancels_query_and_root_tracks_it_until_native_finish(self):
        private=self.dialog.open_private_chat();panel=private.open_balance()
        started=threading.Event()
        def reply(*args,**kwargs):
            job=args[-1];started.set()
            while not job.is_canceled():time.sleep(.003)
            job.check_canceled()
        with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
            with patch.object(panel.client,'_request',side_effect=reply):
                panel.refresh_key();self.wait(started.is_set)
                self.assertIn(panel,self.dialog.child_chat_windows())
                private.close()
                self.assertTrue(panel._closing)
                self.assertEqual(panel.management_key.text(),'')
                self.wait(lambda:panel.handle is None)

    def test_group_window_has_balance_entry_and_does_not_start_discussion(self):
        group=self.dialog.open_group_chat()
        with patch.object(group.client,'chat') as chat:
            panel=group.open_balance();self.assertIsNotNone(panel)
            self.assertIn(panel,self.dialog.child_chat_windows())
            self.assertIsNone(group._group_plan)
            chat.assert_not_called();panel.close()

    def test_plugin_exit_waits_for_query_even_after_private_chat_closed(self):
        private=self.dialog.open_private_chat();panel=private.open_balance()
        started,release=threading.Event(),threading.Event()
        def reply(*args,**kwargs):
            started.set();release.wait(3);args[-1].check_canceled()
            return self.key_reply()
        try:
            with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
                with patch.object(panel.client,'_request',side_effect=reply):
                    panel.refresh_key();self.wait(started.is_set);private.close();self.app.processEvents()
                    plugin=base.AIModelClientPlugin();plugin.dialog=self.dialog
                    self.assertFalse(plugin.can_close()[0])
                    release.set();self.wait(lambda:panel.handle is None)
                    self.assertTrue(plugin.can_close()[0])
        finally:release.set()

    def test_lookup_can_complete_while_chat_request_is_still_running(self):
        panel=self.open_panel();self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('mistral'))
        self.dialog.model.setCurrentText('mistral-small-latest')
        started,release=threading.Event(),threading.Event()
        def chat(*args,**kwargs):started.set();release.wait(3);return base.UITests.result(self)
        try:
            with patch.object(self.dialog.client,'chat',side_effect=chat):
                self.dialog.input.setPlainText('聊天继续');self.dialog.send();self.wait(started.is_set)
                with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',return_value='synthetic-key'):
                    with patch.object(panel.client,'_request',return_value=self.key_reply()):
                        panel.refresh_key();self.settled(panel)
                self.assertIsNotNone(self.dialog.handle)
                self.assertIn('$74.50',panel.key_remaining.text())
                release.set();self.wait(lambda:self.dialog.handle is None)
        finally:release.set()

    def test_offline_panel_does_not_query_or_display_fake_balance(self):
        panel=self.open_panel();panel.management_key.setText('synthetic-management-key')
        with patch('app_plugins.builtin.ai_model_client.balance_ui.is_offline',return_value=True):
            with patch.object(panel.client,'_request') as request:
                panel.refresh_account();request.assert_not_called()
        self.assertIsNone(panel.handle)
        self.assertIn('离线',panel.status.text())
        self.assertIn('尚未查询',panel.account_remaining.text())

    def test_missing_key_and_management_permission_errors_do_not_strand_worker(self):
        panel=self.open_panel()
        with patch('app_plugins.builtin.ai_model_client.balance_ui.read_provider_key',side_effect=ValueError('请先添加Key')):
            panel.refresh_key()
        self.assertIsNone(panel.handle)
        self.assertIn('请先添加',panel.status.text())
        panel.management_key.setText('synthetic-management-key')
        with patch.object(panel.client,'_request',side_effect=base.AIProviderError('forbidden',status=403)):
            panel.refresh_account();self.settled(panel)
        self.assertIn('管理Key',panel.status.text())
        self.assertTrue(panel.account_button.isEnabled())

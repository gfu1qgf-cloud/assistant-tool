import unittest
import test_flow_audit_state_machine as fixtures
from app_plugins.builtin.flow_assistant.account_policy import classify_issue, mark_account, account_hold, reset_accounts
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.policy import capable


class AccountIsolationTests(unittest.TestCase):
    setUpClass = fixtures.FlowAuditStateTests.__dict__['setUpClass']
    setUp = fixtures.FlowAuditStateTests.setUp
    service = fixtures.FlowAuditStateTests.service
    jobs = fixtures.FlowAuditStateTests.jobs
    client = fixtures.FlowAuditStateTests.client
    browser = fixtures.FlowAuditStateTests.browser
    job = fixtures.FlowAuditStateTests.job
    receipt = fixtures.FlowAuditStateTests.receipt

    def test_classifier_does_not_call_timeouts_or_server_errors_a_ban(self):
        for text in ('request timeout', 'HTTP 503', 'disabled button', '无法确认当前 Flow 登录账号', '积分不足'):
            self.assertEqual(classify_issue(text), '')
        self.assertEqual(classify_issue('Your Google account has been disabled'), 'disabled')
        self.assertEqual(classify_issue('账号已被封禁'), 'disabled')
        self.assertEqual(classify_issue('Please sign in to continue'), 'login')
        self.assertEqual(classify_issue('CAPTCHA required'), 'verification')

    def test_ban_is_persistent_shared_by_account_and_does_not_block_other_accounts(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Profile 2')
        other = self.browser(service, 'Profile 3')
        two['account'] = one['account'].upper()
        mark_account(service, one, 'Your Google account is disabled')
        for client in (one, two):
            self.assertTrue(account_hold(client, service.preferences))
            self.assertFalse(capable(client, self.jobs(duration=8)[0], service.preferences, 12))
        self.assertTrue(capable(other, self.jobs(duration=8)[0], service.preferences, 12))
        self.assertTrue(service.store.settings()['account_blocks'])
        service.resume_browser({'keys': ['profile:'+one['profile']]})
        self.assertTrue(account_hold(one, service.preferences), 'generic browser resume must not erase an account hold')
        self.assertTrue(service.paused)

    def test_reset_is_selected_only_and_requires_fresh_detection(self):
        service = self.service(); one = self.browser(service); other = self.browser(service, 'Profile 3')
        one['credits_ready_failures'] = 3
        mark_account(service, one, 'account disabled'); mark_account(service, other, 'account suspended')
        reset_accounts(service, ['profile:'+one['profile']])
        self.assertFalse(account_hold(one, service.preferences))
        self.assertTrue(account_hold(other, service.preferences))
        self.assertIsNone(one['credits']); self.assertTrue(one['credit_check']['hold_dispatch'])
        self.assertEqual(one['credits_ready_failures'], 0)
        self.assertTrue(service.paused)
        self.assertEqual({p['action'] for p in service.pending.values()}, {'get_points', 'snapshot'})

    def test_reset_does_not_requeue_uncertain_paid_task_or_refund_credits(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'uncertain')
        service.store.reserve(row['id'], row['attempt'], 12, 'duration_rule')
        mark_account(service, client, 'account disabled')
        reset_accounts(service, ['profile:'+client['profile']])
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 12)
        self.assertEqual(client['credits'], 50)
        self.assertFalse(service.pending)

    def test_pre_submit_ban_reassigns_task_but_post_submit_failure_stays_uncertain(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client)
        reply = self.receipt(service, client, row, 'prepare', success=False,
                             error='account disabled', code='FLOW_ACCOUNT_DISABLED')
        service._response(client, reply)
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertFalse(service.safety_paused, 'one disabled account must not stop other accounts')
        row = self.job(service, client, 'submitting')
        reply = self.receipt(service, client, row, 'run_tasks', success=False, error='account disabled')
        service._response(client, reply)
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_offline_profile_can_be_marked_and_reset_without_changing_queue(self):
        service = self.service()
        service.execute({'action': 'mark_account_unavailable', 'keys': ['profile:Profile 104']})
        profile = {'profile': 'Profile 104'}
        self.assertTrue(account_hold(profile, service.preferences))
        self.assertEqual(browser_status(profile, service.preferences)['state'], '账号已隔离')
        service.execute({'action': 'reset_account_state', 'keys': ['profile:Profile 104']})
        self.assertFalse(account_hold(profile, service.preferences))
        self.assertTrue(service.paused)

    def test_new_login_does_not_erase_the_previous_banned_account_record(self):
        service = self.service(); client = self.browser(service)
        old = client['account']; mark_account(service, client, 'account disabled')
        client['account'] = 'new@example.test'
        self.assertFalse(account_hold(client, service.preferences))
        self.assertIn(old, service.preferences['account_blocks'])
        client['account'] = old
        self.assertTrue(account_hold(client, service.preferences))

    def test_login_error_does_not_downgrade_a_ban(self):
        service = self.service(); client = self.browser(service)
        mark_account(service, client, 'account disabled')
        mark_account(service, client, 'Please sign in to continue')
        self.assertEqual(account_hold(client, service.preferences)['kind'], 'disabled')

    def test_account_isolation_during_upload_prevents_paid_submission_on_success_ack(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'uploading', {'cost': 12, 'cost_source':'duration_rule'})
        reply = self.receipt(service, client, row, 'upload_images')
        mark_account(service, client, 'account disabled')
        service._response(client, reply)
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertEqual(client['credits'], 50)
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_explicit_reset_clears_only_legacy_account_blocks(self):
        service = self.service(); client = self.browser(service)
        key = 'profile:' + client['profile']
        service.preferences['browser_blocks'] = {key:'account disabled', 'profile:other':'需要登录',
                                                'browser:'+client['id']:'当前模型不支持8秒'}
        reset_accounts(service, [key])
        self.assertNotIn(key, service.preferences['browser_blocks'])
        self.assertIn('profile:other', service.preferences['browser_blocks'])
        self.assertIn('browser:'+client['id'], service.preferences['browser_blocks'])

    def test_reset_does_not_adopt_a_credit_response_started_before_reset(self):
        service = self.service(); client = self.browser(service)
        old_account = client['account']
        service.request(client, 'get_points'); old_key = next(iter(service.pending))
        mark_account(service, client, 'account disabled')
        reset_accounts(service, ['profile:'+client['profile']])
        service._response(client, {'request_id':old_key, 'success':True,
            'data':{'email':old_account, 'remaining_points':1500}})
        self.assertIsNone(client['credits'])
        self.assertFalse(client['account'])
        fresh = next(k for k,p in service.pending.items() if p['action'] == 'get_points')
        self.assertNotEqual(fresh, old_key)
        service._response(client, {'request_id':fresh,'success':True,
            'data':{'email':'fresh@example.test','remaining_points':70}})
        self.assertEqual(client['credits'], 70)
        self.assertEqual(client['account'],'fresh@example.test')


if __name__ == '__main__': unittest.main()

"""Explicit persistent permission replaces the ambiguous re-ask menu."""
import time
import unittest

from qt_compat import QtWidgets
import test_flow_high_credit as fixtures
import test_flow_browser_table as tables
from app_plugins.builtin.flow_assistant.high_credit import allowed, record, set_permission, watch, decide, note_normal_spend
from app_plugins.builtin.flow_assistant.credit_cache import with_balance


class HighPermissionTests(unittest.TestCase):
    setUpClass=fixtures.HighCreditTests.__dict__['setUpClass']
    setUp=fixtures.HighCreditTests.setUp
    service=fixtures.HighCreditTests.service
    client=fixtures.HighCreditTests.client
    browser=fixtures.HighCreditTests.browser
    jobs=fixtures.HighCreditTests.jobs

    def permission(self, service, client, value):
        service.execute({'action':'set_high_credit_permission','keys':['profile:'+client['profile']],
            'permission':value})

    def test_direct_allow_survives_ordinary_spending_session_changes_and_restart(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038)
        self.permission(service,rich,'allow'); self.assertTrue(allowed(rich,service.preferences))
        normal=self.browser(service,'Normal'); note_normal_spend(service,normal)
        service.preferences['high_credit_session']='new-session'
        self.assertTrue(allowed(rich,service.preferences))
        self.assertEqual(service.store.settings()['high_credit_accounts'][rich['account']]['permission'],'allow')
        restarted=self.service(); self.assertTrue(allowed(rich,restarted.preferences))

    def test_deny_is_durable_and_never_produces_another_automatic_prompt(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038)
        self.permission(service,rich,'deny'); service.store.enqueue(self.jobs(duration=8,budget=0))
        service.paused=False; prompts=[]; service.high_credit_confirmation_requested.connect(prompts.append)
        for _ in range(3): service._tick()
        self.assertFalse(allowed(rich,service.preferences)); self.assertFalse(prompts)
        self.assertEqual(service.store.rows()[0]['status'],'queued')

    def test_setting_can_target_offline_known_account_without_opening_browser(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038)
        service.preferences.update(profiles=['Rich'])
        service.preferences=with_balance(service.preferences,rich,service.clients.values())
        service.clients.pop(rich['socket']); launches=[]; service.launch_requested.connect(launches.append)
        self.permission(service,rich,'allow')
        self.assertTrue(allowed({'profile':'Rich'},service.preferences)); self.assertFalse(launches)

    def test_setting_selected_account_does_not_alter_other_accounts_or_existing_tasks(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038); other=self.browser(service,'Other',points=500)
        service.store.enqueue(self.jobs(duration=8,budget=0)); row=service.store.rows()[0]
        service.store.update(row['id'],'generating',browser=rich['id'],attempt='a'*32,
            remote={'profile':'Rich','account':rich['account']})
        before=service.store.rows(); self.permission(service,rich,'deny')
        self.assertEqual(service.store.rows(),before); self.assertFalse(service.pending)
        self.assertFalse(record(other,service.preferences).get('permission'))

    def test_late_prompt_callback_cannot_override_direct_setting(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038)
        service.store.enqueue(self.jobs(duration=8,budget=0)); service.paused=False
        watch(service,service.store.rows()); pending=service.high_credit_pending
        self.assertIsNotNone(pending)
        self.permission(service,rich,'deny'); self.assertIsNone(service.high_credit_pending)
        self.assertFalse(decide(service,{'nonce':pending['nonce'],'approved':True}))
        self.assertEqual(record(rich,service.preferences)['permission'],'deny')

    def test_restoring_auto_removes_persistent_setting_and_waits_for_normal_prompt_rules(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038)
        self.permission(service,rich,'allow'); self.permission(service,rich,'auto')
        self.assertFalse(allowed(rich,service.preferences)); self.assertNotIn('permission',record(rich,service.preferences))

    def test_allowed_high_account_still_runs_after_usable_ordinary_account(self):
        service=self.service(); rich=self.browser(service,'Rich',points=1038); normal=self.browser(service,'Normal',points=50)
        self.permission(service,rich,'allow'); service.store.enqueue(self.jobs(duration=8,budget=0)); service.paused=False
        service._tick(); self.assertEqual(service.store.rows()[0]['browser'],normal['id'])


class PermissionMenuTests(unittest.TestCase):
    setUpClass=tables.BrowserTableTests.__dict__['setUpClass']
    setUp=tables.BrowserTableTests.setUp
    ui=tables.BrowserTableTests.ui
    table=tables.BrowserTableTests.table
    profiles=tables.BrowserTableTests.profiles
    row=tables.BrowserTableTests.row

    def test_menu_sets_permission_directly_without_confirmation_or_connection_requirement(self):
        dialog,_,commands=self.table(); dialog.plugin.high_credit_permission_api=1
        dialog.browsers.selectRow(self.row(dialog,'Profile 10'))
        menu=QtWidgets.QMenu(); permission=dialog.high_credit_permission_menu(menu)
        self.assertTrue(permission.isEnabled()); permission.actions()[0].trigger()
        self.assertEqual(commands[-1],{'action':'set_high_credit_permission','permission':'allow','keys':['profile:Profile 10']})
        permission.actions()[1].trigger(); self.assertEqual(commands[-1]['permission'],'deny')
        dialog.browsers.selectRow(self.row(dialog,'Profile 2'))
        menu=QtWidgets.QMenu(); self.assertFalse(dialog.high_credit_permission_menu(menu).isEnabled())

    def test_offline_menu_keeps_persistent_permission_checked(self):
        dialog,_,_=self.table(); dialog.plugin.high_credit_permission_api=1
        dialog.state['preferences'].update(high_credit_accounts={'user10@example.test':{'permission':'deny'}},
            high_credit_links={'profile:Profile 10':'user10@example.test'})
        dialog.state['clients']=[]; dialog.update_browsers()
        dialog.browsers.selectRow(self.row(dialog,'Profile 10'))
        menu=QtWidgets.QMenu(); permission=dialog.high_credit_permission_menu(menu)
        self.assertTrue(permission.isEnabled()); self.assertTrue(permission.actions()[1].isChecked())


if __name__=='__main__': unittest.main()

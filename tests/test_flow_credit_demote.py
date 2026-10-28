"""Current balance replaces lifetime high-account classification."""
import copy
import math
import unittest
import test_flow_high_credit as fixtures
from app_plugins.builtin.flow_assistant.high_credit import is_high,allowed,reason,with_marker,new_session_entries
from app_plugins.builtin.flow_assistant.credit_policy import debit_balance
from app_plugins.builtin.flow_assistant.credit_cooldown import remember_balance
from app_plugins.builtin.flow_assistant.policy import capable


class DemotionTests(unittest.TestCase):
    setUpClass=fixtures.HighCreditTests.__dict__['setUpClass']
    setUp=fixtures.HighCreditTests.setUp
    service=fixtures.HighCreditTests.service
    jobs=fixtures.HighCreditTests.jobs
    client=fixtures.HighCreditTests.client
    browser=fixtures.HighCreditTests.browser
    observe=fixtures.HighCreditTests.observe
    queue=fixtures.HighCreditTests.queue

    def test_100_is_high_99_is_ordinary_even_with_an_old_deny(self):
        service=self.service();client=self.browser(service,points=100);self.observe(service,client,100)
        service.execute({'action':'set_high_credit_permission','keys':['profile:'+client['profile']],'permission':'deny'})
        self.assertTrue(is_high(client,service.preferences));self.assertFalse(allowed(client,service.preferences))
        self.observe(service,client,99,manual=True)
        self.assertFalse(is_high(client,service.preferences));self.assertTrue(allowed(client,service.preferences))
        self.assertEqual(reason(client,service.preferences),'')
        self.assertTrue(capable(client,self.jobs(duration=6,budget=0)[0],service.preferences,10))

    def test_local_debit_downgrades_and_persists_without_an_extra_probe(self):
        service=self.service();client=self.browser(service,points=100);self.observe(service,client,100)
        debit_balance(client,service.clients.values(),12);remember_balance(service,client,source='local_reservation')
        self.assertFalse(is_high(client,service.preferences))
        self.assertEqual(service.preferences['high_credit_accounts'][client['account']]['classification'],'ordinary')
        self.assertFalse(is_high({'profile':client['profile']},service.preferences))

    def test_pending_confirmation_is_cancelled_and_late_click_cannot_reapprove_it(self):
        service=self.service();client=self.browser(service,points=150);self.observe(service,client,150)
        self.queue(service);service._tick();pending=copy.deepcopy(service.high_credit_pending)
        self.assertIsNotNone(pending)
        self.observe(service,client,88,manual=True)
        self.assertIsNone(service.high_credit_pending)
        before=copy.deepcopy(service.preferences)
        service.execute({'action':'high_credit_decision','nonce':pending['nonce'],'approved':True})
        self.assertEqual(service.preferences,before)

    def test_old_saved_low_balance_migrates_without_manual_change(self):
        service=self.service();client=self.browser(service,points=100);self.observe(service,client,100)
        self.observe(service,client,88)
        prefs=copy.deepcopy(service.preferences)
        prefs['high_credit_accounts'][client['account']].pop('classification')
        entries=new_session_entries(prefs)
        self.assertEqual(entries[client['account']]['classification'],'ordinary')
        self.assertFalse(is_high({'profile':client['profile']},prefs))

    def test_refill_restores_high_threshold_and_unknown_values_do_not_fake_demotion(self):
        service=self.service();client=self.browser(service,points=150);self.observe(service,client,150)
        for bad in (None,True,-1,float('nan')):
            self.assertTrue(is_high({**client,'credits':bad},service.preferences))
        self.observe(service,client,80);self.assertFalse(is_high(client,service.preferences))
        self.observe(service,client,100);self.assertTrue(is_high(client,service.preferences))
        self.assertFalse(allowed(client,service.preferences))

    def test_manual_unavailable_is_preserved_after_automatic_demotion(self):
        service=self.service();client=self.browser(service,points=150);self.observe(service,client,150)
        service.execute({'action':'mark_account_unavailable','keys':['profile:'+client['profile']]})
        self.observe(service,client,88,manual=True)
        self.assertFalse(is_high(client,service.preferences))
        self.assertFalse(capable(client,self.jobs(duration=6)[0],service.preferences,10))


if __name__=='__main__':unittest.main()

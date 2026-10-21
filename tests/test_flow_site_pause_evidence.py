"""Confirmed website warnings remain protected; wrong-page events do not pause."""
import json
import unittest
import test_flow_credit_cooldown as fixtures


class SitePauseEvidenceTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser

    def scenario(self):
        service=self.service();client=self.browser(service)
        client.update(capabilities=['site_pause_evidence_v1'],page_instance='current-page')
        service.paused=False;service.released={'selected'}
        message={'type':'site_pause','source':'visible_site_alert','code':'FLOW_SITE_UNUSUAL_ACTIVITY',
            'url':fixtures.PROJECT,'page_instance':'current-page'}
        return service,client,message

    def test_matching_visible_site_warning_still_pauses_global_dispatch(self):
        service,client,message=self.scenario()
        service._message(client['socket'],json.dumps(message))
        self.assertTrue(service.safety_paused);self.assertTrue(service.paused);self.assertFalse(service.released)

    def test_old_page_wrong_project_or_untyped_new_bridge_warning_is_ignored(self):
        for change in ({'page_instance':'old-page'},{'url':'https://flow.google.com/project/other'},
                {'source':'body_text'},{'code':''},{'url':''},{'page_instance':''}):
            service,client,message=self.scenario()
            service._message(client['socket'],json.dumps({**message,**change}))
            self.assertFalse(service.safety_paused,change);self.assertFalse(service.paused)
            self.assertEqual(service.released,{'selected'})

    def test_legacy_bridge_warning_keeps_existing_conservative_protection(self):
        service,client,message=self.scenario();client['capabilities']=[]
        service._message(client['socket'],json.dumps({'type':'site_pause'}))
        self.assertTrue(service.safety_paused)


if __name__=='__main__':unittest.main()

import copy
import unittest
from unittest.mock import patch
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.queue_selection import stage_failed_retries
from test_export_total_list import TotalListTests,job


class RetryFailedTests(TotalListTests):
    def failures(self):
        state=self.store.load()
        state['jobs']=[dict(job('bad'),status='需处理',error='old error',render_assignment={'version':1,'sources':{},'background':'','voice':'','music':None}),dict(job('done'),status='已完成',render_checked=True),job('pending')]
        state['profiles'][1]['data']['jobs']=[dict(job('other-bad'),status='需处理'),job('other-pending')]
        self.dialog.state=self.store.save(state,state['revision']);self.dialog.refresh_tables()
        return copy.deepcopy(self.store.load())

    def test_retry_button_submits_only_failed_ids_and_preserves_other_checks_and_assignments(self):
        before=self.failures();self.assertTrue(self.flat.retry.isVisible());self.assertTrue(self.flat.retry.isEnabled())
        self.flat.retry.click();self.app.processEvents()
        records=self.manager.records();self.assertEqual(len(records),2)
        after=self.store.load()
        self.assertEqual({tuple(p['ids']) for p in after['render_plans'] if p['status']=='submitted'},{('bad',),('other-bad',)})
        self.assertEqual(after['jobs'][0]['render_assignment'],before['jobs'][0]['render_assignment'])
        self.assertEqual(after['jobs'][0]['status'],'需处理')
        self.assertEqual(after['jobs'][1]['render_checked'],before['jobs'][1]['render_checked'])
        self.assertEqual(after['jobs'][2].get('render_checked'),before['jobs'][2].get('render_checked'))

    def test_filtered_retry_excludes_other_projects_and_running_project_is_locked(self):
        self.failures();self.flat.search.setText('项目乙');self.flat.retry.click();self.app.processEvents()
        self.assertEqual(set(self.dialog._host_plans),{self.other})
        self.assertFalse(self.flat.retry.isEnabled())
        self.flat.search.clear();self.assertTrue(self.flat.retry.isEnabled())

    def test_retry_transaction_rejects_stale_or_active_profile_atomically(self):
        before=self.failures();snapshot=copy.deepcopy(before)
        with self.assertRaises(ValueError):stage_failed_retries(before,{'default':['bad'],self.other:['other-bad']},{'default':999,self.other:0})
        self.assertEqual(before,snapshot)
        before['render_plans'].append(dict(id='active',profile_id=self.other,status='running',ids=['other-bad']))
        snapshot=copy.deepcopy(before)
        with self.assertRaises(ValueError):stage_failed_retries(before,{'default':['bad'],self.other:['other-bad']})
        self.assertEqual(before,snapshot)

    def test_completed_or_pending_tasks_are_not_retried_by_failed_action(self):
        state=self.failures();snapshot=copy.deepcopy(state)
        for identifier in ('done','pending'):
            with self.assertRaises(ValueError):stage_failed_retries(state,{'default':[identifier]})
        self.assertEqual(state,snapshot)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in RetryFailedTests.__dict__:
        if name.startswith('test_'):suite.addTest(RetryFailedTests(name))
    return suite

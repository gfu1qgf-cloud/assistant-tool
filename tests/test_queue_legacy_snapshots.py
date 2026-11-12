import copy
import unittest
from unittest.mock import patch
from qt_compat import QtWidgets
from model.AppDataStorage import data_file
from app_plugins.builtin.batch_text_video.pools import snapshot
from app_plugins.builtin.batch_text_video.queue_selection import remove_jobs, apply_checks
from app_plugins.builtin.batch_text_video.store import fresh_state
from test_export_total_list import TotalListTests, job


class LegacySnapshotTests(unittest.TestCase):
    def legacy(self):
        state=fresh_state();state['jobs']=[job('one'),job('two')]
        state['resource_orders']={'backgrounds':['b.mp4','a.mp4']}
        state['backgrounds']=['a.mp4','b.mp4']
        data=snapshot(state);data.pop('resource_orders')
        state['profiles'].append(dict(id='old',name='旧项目',revision=3,data=data))
        return state

    def test_missing_optional_order_is_empty_without_mutating_input(self):
        legacy=self.legacy()['profiles'][1]['data'];before=copy.deepcopy(legacy)
        result=snapshot(legacy)
        self.assertEqual(result['resource_orders'],{})
        self.assertEqual(legacy,before)
        self.assertEqual({k:v for k,v in result.items() if k!='resource_orders'},legacy)

    def test_existing_order_is_deep_copied_without_resetting(self):
        state=self.legacy();before=copy.deepcopy(state)
        result=snapshot(state)
        self.assertEqual(result['resource_orders'],state['resource_orders'])
        result['resource_orders']['backgrounds'].append('new.mp4')
        self.assertEqual(state,before)

    def test_delete_new_and_legacy_projects_keeps_their_independent_resources(self):
        state=self.legacy();before=copy.deepcopy(state)
        result,changed=remove_jobs(state,{'default':['one'],'old':['one']})
        self.assertTrue(changed);self.assertEqual(state,before)
        self.assertEqual([j['id'] for j in result['jobs']],['two'])
        old=result['profiles'][1]['data']
        self.assertEqual([j['id'] for j in old['jobs']],['two'])
        self.assertEqual(old['resource_orders'],{})
        self.assertEqual(result['resource_orders'],state['resource_orders'])
        self.assertEqual(old['backgrounds'],state['profiles'][1]['data']['backgrounds'])

    def test_checkbox_changes_support_legacy_inactive_project(self):
        state=self.legacy();before=copy.deepcopy(state)
        result,changed=apply_checks(state,{'old':{'one':False}})
        self.assertTrue(changed);self.assertEqual(state,before)
        self.assertFalse(result['profiles'][1]['data']['jobs'][0]['render_checked'])
        self.assertEqual(result['profiles'][1]['data']['resource_orders'],{})

    def test_required_resource_field_still_fails_without_partial_mutation(self):
        state=self.legacy();state['profiles'][1]['data'].pop('music');before=copy.deepcopy(state)
        with self.assertRaises(KeyError):remove_jobs(state,{'default':['one'],'old':['one']})
        self.assertEqual(state,before)


class LegacyQueueUiTests(TotalListTests):
    def make_legacy(self,plan=False):
        raw=data_file(self.store.path).read_json()
        data=next(p for p in raw['profiles'] if p['id']==self.other)['data']
        data.pop('resource_orders',None)
        if plan:
            data['jobs'][0]['render_checked']=True
            raw['render_plans'].append(dict(id='legacy-plan',profile_id=self.other,name='旧项目',ids=['same'],known_ids=['same'],status='staged',task_id='',error=''))
        data_file(self.store.path).write_json(raw,indent=2)
        self.dialog.state=self.store.load();self.dialog.refresh_tables()

    def test_delete_button_works_on_real_legacy_storage_shape_without_error_popup(self):
        self.make_legacy();self.flat.table.selectAll();self.app.processEvents()
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes),patch.object(QtWidgets.QMessageBox,'warning') as warning:
            self.flat.delete.click()
        warning.assert_not_called()
        self.assertEqual(self.flat.table.rowCount(),0)
        raw=data_file(self.store.path).read_json()
        self.assertEqual(raw['jobs'],[])
        old=next(p for p in raw['profiles'] if p['id']==self.other)['data']
        self.assertEqual(old['jobs'],[]);self.assertEqual(old['resource_orders'],{})

    def test_checkbox_commit_supports_legacy_project_without_switching(self):
        self.make_legacy()
        self.assertTrue(self.flat.commit({self.other:{'same':False}}))
        self.assertEqual(self.dialog.state['active_profile'],'default')
        raw=data_file(self.store.path).read_json()
        old=next(p for p in raw['profiles'] if p['id']==self.other)['data']
        self.assertFalse(old['jobs'][0]['render_checked'])

    def test_clear_selection_and_remove_plan_support_legacy_inactive_snapshot(self):
        for mode in ('clear','remove'):
            with self.subTest(mode=mode):
                self.make_legacy(plan=True)
                if mode=='clear':self.store.clear_render_selection(self.other)
                else:self.store.remove_render_plan('legacy-plan')
                raw=data_file(self.store.path).read_json()
                old=next(p for p in raw['profiles'] if p['id']==self.other)['data']
                self.assertEqual(old['resource_orders'],{})
                self.assertFalse(old['jobs'][0]['render_checked'])
                self.assertEqual(len(old['jobs']),1)
                self.assertFalse(any(p['id']=='legacy-plan' for p in raw['render_plans']))


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(LegacySnapshotTests))
    for name in LegacyQueueUiTests.__dict__:
        if name.startswith('test_'):suite.addTest(LegacyQueueUiTests(name))
    return suite

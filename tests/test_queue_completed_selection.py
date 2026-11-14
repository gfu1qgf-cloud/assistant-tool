import copy
import os
from pathlib import Path
import unittest
from app_plugins.builtin.batch_text_video.store import ProfileStore
from test_export_total_list import TotalListTests,job


class CompletedSelectionTests(TotalListTests):
    def mixed(self):
        state=self.store.load()
        state['jobs']=[dict(job('done'),status='已完成',render_checked=False,render_assignment={'version':1,'sources':{},'voice':'','background':'','music':None}),dict(job('pending'),render_checked=True),dict(job('failed'),status='需处理',render_checked=True)]
        state['profiles'][1]['data']['jobs']=[dict(job('other-done'),status='已完成',render_checked=False),dict(job('other-pending'),render_checked=True)]
        self.dialog.state=self.store.save(state,state['revision']);self.dialog.refresh_tables()

    def flags(self,pid='default'):
        data=self.store.load() if pid=='default' else ProfileStore(self.store.directory,pid).load()
        return {j['id']:j.get('render_checked') for j in data['jobs']}

    def test_menu_checks_completed_across_projects_and_repeated_action_is_idempotent(self):
        self.mixed();self.assertEqual(self.flat.unfinished.text(),'批量勾选')
        assignment=copy.deepcopy(self.store.load()['jobs'][0]['render_assignment'])
        self.assertIn('仅勾选已完成',[a.text() for a in self.flat.bulk_menu.actions()])
        self.flat.bulk_actions['completed'].trigger();self.app.processEvents()
        self.assertEqual(self.flags(),{'done':True,'pending':False,'failed':False})
        self.assertEqual(self.flags(self.other),{'other-done':True,'other-pending':False})
        self.flat.bulk_actions['completed'].trigger()
        self.assertEqual(self.flags(),{'done':True,'pending':False,'failed':False})
        done=self.store.load()['jobs'][0]
        self.assertEqual(done['status'],'已完成');self.assertEqual(done['render_assignment'],assignment)
        plans=self.store.load()['render_plans']
        self.assertEqual({tuple(p['ids']) for p in plans if p['status']=='staged'},{('done',),('other-done',)})

    def test_filter_visible_and_clear_actions_keep_hidden_project_untouched(self):
        self.mixed();before=self.flags()
        self.flat.search.setText('项目乙');self.flat.bulk_actions['completed'].trigger()
        self.assertEqual(self.flags(),before)
        self.assertEqual(self.flags(self.other),{'other-done':True,'other-pending':False})
        self.flat.bulk_actions['visible'].trigger()
        self.assertEqual(self.flags(self.other),{'other-done':True,'other-pending':True})
        self.flat.bulk_actions['clear'].trigger()
        self.assertEqual(self.flags(self.other),{'other-done':False,'other-pending':False})
        self.assertEqual(self.flags(),before)
        self.flat.bulk_actions['unfinished'].trigger()
        self.assertEqual(self.flags(self.other),{'other-done':False,'other-pending':True})

    def test_running_project_is_skipped_while_idle_project_can_check_completed(self):
        self.mixed()
        self.flat.commit({'default':{'done':False,'pending':True,'failed':False},self.other:{'other-done':False,'other-pending':False}})
        self.flat.start();before=self.flags()
        self.assertIn('default',self.dialog._host_plans)
        self.flat.bulk_actions['completed'].trigger()
        self.assertEqual(self.flags(),before)
        self.assertEqual(self.flags(self.other),{'other-done':True,'other-pending':False})

    def test_new_menu_preserves_sidebar_layout_and_does_not_start_an_export(self):
        self.mixed();self.flat.bulk_actions['completed'].trigger();self.app.processEvents()
        self.assertEqual(self.manager.records(),[])
        self.assertTrue(all(self.dialog.splitter.sizes()))
        self.assertTrue(self.flat.unfinished.isVisible())
        if os.environ.get('LZX_TOTAL_LIST_QA'):
            self.assertTrue(self.dialog.grab().save(str(Path(os.environ['LZX_TOTAL_LIST_QA'])/'completed-selection.png')))

    def test_visible_select_all_and_clear_buttons_include_completed_without_starting_export(self):
        self.mixed();self.flat.check_all.click()
        self.assertEqual(self.flags(),{'done':True,'pending':True,'failed':True})
        self.assertEqual(self.flags(self.other),{'other-done':True,'other-pending':True})
        self.flat.clear_all.click()
        self.assertTrue(all(v is False for v in self.flags().values()))
        self.assertTrue(all(v is False for v in self.flags(self.other).values()))
        self.assertFalse(self.manager.records())
        self.assertTrue(self.flat.check_all.isVisible());self.assertTrue(self.flat.clear_all.isVisible())

    def test_select_all_buttons_respect_search_and_skip_running_projects(self):
        self.mixed();self.flat.clear_all.click()
        self.flat.search.setText('项目乙');self.flat.check_all.click()
        self.assertTrue(all(v is False for v in self.flags().values()))
        self.assertTrue(all(v is True for v in self.flags(self.other).values()))
        self.flat.start();before=self.flags(self.other)
        self.flat.search.clear();self.flat.clear_all.click()
        self.assertEqual(self.flags(self.other),before)
        self.assertTrue(all(v is False for v in self.flags().values()))


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in CompletedSelectionTests.__dict__:
        if name.startswith('test_'):suite.addTest(CompletedSelectionTests(name))
    return suite

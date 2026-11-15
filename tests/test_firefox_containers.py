import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock

from model import FirefoxContainers as core


def document(version=5):
    return {'version':version,'lastUserContextId':5,'identities':[
        {'userContextId':1,'public':True,'name':'Existing','color':'blue','icon':'fingerprint','unfamiliar':{'keep':True}},
        {'userContextId':5,'public':False,'name':'internal','color':'','icon':''},
        {'userContextId':core.MAX_ID,'public':False,'name':'reserved','extra':'preserve'}],
        'siteAssociations':{'example.invalid':1},'unknown':{'value':[1,2,3]}}


def make_profile(root,name='profile',value=None):
    path=Path(root)/name;path.mkdir(parents=True)
    (path/'containers.json').write_text(json.dumps(document() if value is None else value,ensure_ascii=False),encoding='utf8')
    return path


class FirefoxContainerCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=make_profile(self.root)

    def state(self):return core.snapshot(self.path)

    def test_numbered_names_and_invalid_input(self):
        self.assertEqual(core.numbered_names('工作',8,3,3),['工作008','工作009','工作010'])
        for args in (('',1,2,2),('A',True,2,2),('A',1,0,2),('A',1,1001,2)):
            with self.assertRaises(ValueError):core.numbered_names(*args)

    def test_additive_plan_preserves_existing_ids_unknown_fields_and_reserved_id(self):
        state=self.state();original=copy.deepcopy(state['data'])
        plan=core.build_plan(state,['One','Two'])
        self.assertEqual([item['userContextId'] for item in plan['added']],[6,7])
        self.assertEqual(plan['data']['identities'][:3],original['identities'])
        self.assertEqual(plan['data']['unknown'],original['unknown'])
        self.assertEqual(plan['data']['siteAssociations'],original['siteAssociations'])
        self.assertEqual(state['data'],original)

    def test_deleted_ids_are_not_reused_and_stale_counter_is_reconciled(self):
        state=self.state();state['data']['lastUserContextId']=50
        self.assertEqual(core.build_plan(state,['new'])['added'][0]['userContextId'],51)
        state['data']['lastUserContextId']=1
        self.assertEqual(core.build_plan(state,['new'])['added'][0]['userContextId'],6)

    def test_repeated_names_are_skipped_without_overwriting_old_names(self):
        plan=core.build_plan(self.state(),[' existing ','New','new','NEW'])
        self.assertEqual([item['name'] for item in plan['added']],['New'])
        self.assertEqual(plan['skipped'],['existing','new','NEW'])

    def test_supported_versions_preserved_and_color_vocabulary_matches(self):
        for version in range(3,9):
            state=self.state();state['data']['version']=version
            plan=core.build_plan(state,['A','B'])
            self.assertEqual(plan['data']['version'],version)
            self.assertEqual(plan['added'][1]['color'],'cyan' if version>=6 else 'turquoise')

    def test_unknown_version_bad_id_and_duplicate_json_fields_are_rejected(self):
        for value in ({**document(),'version':999},{**document(),'lastUserContextId':True}):
            with self.assertRaises(ValueError):core.validate(value)
        value=document();value['identities'].append(copy.deepcopy(value['identities'][0]))
        with self.assertRaises(ValueError):core.validate(value)
        (self.path/'containers.json').write_text('{"version":5,"version":6}',encoding='utf8')
        with self.assertRaises(ValueError):self.state()

    def test_invalid_names_and_id_exhaustion_are_rejected(self):
        for names in ([],['\n'],['A\0B'],['x'*121],[1]):
            with self.assertRaises(ValueError):core.build_plan(self.state(),names)
        state=self.state();state['data']['lastUserContextId']=core.MAX_ID-1
        with self.assertRaises(ValueError):core.build_plan(state,['new'])

    def test_discovery_distinguishes_same_name_profiles_relative_absolute_and_custom(self):
        home=self.root/'Firefox';home.mkdir()
        first=make_profile(home/'Profiles','one');second=make_profile(self.root,'absolute');third=make_profile(self.root,'custom')
        (home/'profiles.ini').write_text('[Profile0]\nName=Same\nIsRelative=1\nPath=Profiles/one\nDefault=1\n'
            f'[Profile1]\nName=Same\nIsRelative=0\nPath={second}\n[InstallABC]\nDefault=Profiles/one\n',encoding='utf8')
        result=core.discover_profiles(home,extra=[str(third),str(first)])
        self.assertEqual(len(result['profiles']),3)
        same=[item for item in result['profiles'] if item['name']=='Same']
        self.assertEqual(len(same),2);self.assertNotEqual(same[0]['key'],same[1]['key'])
        self.assertEqual(sum(item['default'] for item in same),1)
        self.assertEqual({item['path'] for item in result['profiles']},{str(first),str(second),str(third)})

    def test_missing_or_uninitialized_profiles_are_listed_with_clear_reason(self):
        home=self.root/'Firefox';home.mkdir()
        (home/'profiles.ini').write_text('[Profile0]\nName=Missing\nIsRelative=1\nPath=gone\n',encoding='utf8')
        result=core.discover_profiles(home)
        self.assertEqual(result['profiles'][0]['name'],'Missing')
        self.assertIn('不存在',result['profiles'][0]['error'])
        empty=self.root/'fresh';empty.mkdir();(empty/'prefs.js').touch()
        with self.assertRaisesRegex(ValueError,'初始化'):core.snapshot(empty)

    def test_snapshot_default_localized_labels_are_supported(self):
        value=document();value['identities'][0].pop('name');value['identities'][0]['l10nId']='user-context-personal'
        (self.path/'containers.json').write_text(json.dumps(value),encoding='utf8')
        self.assertEqual(self.state()['containers'][0]['name'],'个人')
        self.assertEqual(core.build_plan(self.state(),['个人'])['skipped'],['个人'])

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_write_changes_only_selected_profile_and_creates_exact_backup(self):
        other=make_profile(self.root,'other');other_before=(other/'containers.json').read_bytes()
        (self.path/'cookies.sqlite').write_bytes(b'untouched cookie fixture')
        before=(self.path/'containers.json').read_bytes();state=self.state()
        result=core.create_containers(self.path,['Alpha','Beta'],expected_sha=state['sha256'])
        self.assertTrue(result['committed']);self.assertEqual(Path(result['backup']).read_bytes(),before)
        self.assertEqual((other/'containers.json').read_bytes(),other_before)
        self.assertEqual((self.path/'cookies.sqlite').read_bytes(),b'untouched cookie fixture')
        after=self.state();self.assertEqual(after['data']['identities'][:3],state['data']['identities'])
        self.assertEqual(len(after['containers']),3)

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_busy_target_rejected_but_other_open_profile_does_not_block(self):
        other=make_profile(self.root,'other');state=self.state();before=state['raw']
        with core.profile_lock(self.path):
            with self.assertRaisesRegex(ValueError,'占用'):core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertEqual((self.path/'containers.json').read_bytes(),before)
        with core.profile_lock(other):
            result=core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertTrue(result['committed'])

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_stale_lock_file_is_not_a_false_busy_signal_and_is_not_truncated(self):
        lock=self.path/'parent.lock';lock.write_bytes(b'old-lock')
        state=self.state();core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertEqual(lock.read_bytes(),b'old-lock')

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_changed_file_after_preview_is_not_overwritten(self):
        state=self.state();target=self.path/'containers.json';target.write_bytes(state['raw']+b'\n')
        before=target.read_bytes()
        with self.assertRaisesRegex(ValueError,'变化'):core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertEqual(target.read_bytes(),before);self.assertEqual(list(self.path.glob('*.bak')),[])

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_backup_failure_never_writes_main_file(self):
        from model.AppDataStorage import ManagedFile
        state=self.state();original=ManagedFile.open
        def opening(file,mode='rb',**options):
            if mode=='xb':raise OSError('synthetic backup full')
            return original(file,mode,**options)
        with patch.object(ManagedFile,'open',opening):
            with self.assertRaises(OSError):core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertEqual((self.path/'containers.json').read_bytes(),state['raw'])

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_cancel_during_atomic_write_leaves_original_intact_and_releases_lock(self):
        state=self.state();calls=0
        def cancel():
            nonlocal calls
            calls+=1
            if calls==5:raise RuntimeError('synthetic cancellation')
        with self.assertRaisesRegex(RuntimeError,'cancellation'):
            core.create_containers(self.path,['new'],expected_sha=state['sha256'],check=cancel)
        self.assertEqual((self.path/'containers.json').read_bytes(),state['raw'])
        self.assertEqual(list(self.path.glob('.containers-assistant-*')),[])
        with core.profile_lock(self.path):pass

    @unittest.skipUnless(os.name=='nt','native Windows Firefox profile lock')
    def test_all_duplicates_is_no_write_and_no_backup(self):
        state=self.state();result=core.create_containers(self.path,['Existing'],expected_sha=state['sha256'])
        self.assertFalse(result['committed']);self.assertEqual(list(self.path.glob('*.bak')),[])
        self.assertEqual((self.path/'containers.json').read_bytes(),state['raw'])

    @unittest.skipUnless(os.name=='nt','native Windows profile commit')
    def test_atomic_replace_failure_preserves_original_and_verified_backup(self):
        state=self.state()
        with patch('model.AppDataStorage.os.replace',side_effect=OSError('synthetic replace denied')):
            with self.assertRaises(OSError):core.create_containers(self.path,['new'],expected_sha=state['sha256'])
        self.assertEqual((self.path/'containers.json').read_bytes(),state['raw'])
        backups=list(self.path.glob('*.bak'));self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),state['raw'])
        self.assertEqual(list(self.path.glob('.containers-assistant-*')),[])


class FirefoxContainerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_no_default_profile_selected_and_confirmation_contains_exact_target(self):
        from PYUI.firefox_containers_pyui import FirefoxContainersPanel
        from qt_compat import QtWidgets
        with tempfile.TemporaryDirectory() as root:
            a=make_profile(root,'a');b=make_profile(root,'b');panel=FirefoxContainersPanel()
            try:
                result={'action':'scan',**core.discover_profiles(root=Path(root)/'missing',extra=[str(a),str(b)])}
                for item in result['profiles']:item['name']='Same'
                panel._completed(result)
                self.assertIsNone(panel._selected());self.assertFalse(panel.create_button.isEnabled())
                with patch.object(panel,'_submit') as submit:panel.profiles.selectRow(1)
                self.assertEqual(submit.call_args.args[0]['path'],str(b))
                state=core.snapshot(b);state.pop('raw');panel._completed({'action':'inspect','state':state});panel._enable(True)
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes) as ask, \
                     patch.object(panel,'_submit') as submit:
                    panel.create()
                self.assertIn(str(b),ask.call_args.args[2]);self.assertIn('Same',ask.call_args.args[2])
                self.assertEqual(submit.call_args.args[0]['path'],str(b))
                self.assertEqual(submit.call_args.args[0]['expected_sha'],state['sha256'])
            finally:panel.dispose();panel.deleteLater();self.app.processEvents()

    def test_multi_line_input_and_canceled_confirmation_do_not_write(self):
        from PYUI.firefox_containers_pyui import FirefoxContainersPanel
        from qt_compat import QtWidgets
        with tempfile.TemporaryDirectory() as root:
            path=make_profile(root);panel=FirefoxContainersPanel()
            try:
                panel._completed({'action':'scan',**core.discover_profiles(root=Path(root)/'missing',extra=[str(path)])})
                with patch.object(panel,'_submit'):panel.profiles.selectRow(0)
                state=core.snapshot(path);state.pop('raw');panel._completed({'action':'inspect','state':state});panel._enable(True)
                panel.mode.setCurrentIndex(1);panel.names.setPlainText('One\nTwo\nExisting')
                self.assertEqual(panel.create_button.text(),'创建 2 个容器')
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No), \
                     patch.object(panel,'_submit') as submit:panel.create()
                submit.assert_not_called()
            finally:panel.dispose();panel.deleteLater();self.app.processEvents()

    def test_custom_profile_directory_is_remembered_in_plugin_storage_only(self):
        from PYUI.firefox_containers_pyui import FirefoxContainersPanel
        from model.AppDataStorage import ApplicationStorage
        from qt_compat import QtWidgets
        with tempfile.TemporaryDirectory() as root:
            path=make_profile(root);before=(path/'containers.json').read_bytes()
            storage=ApplicationStorage(Path(root)/'private').for_plugin('chrome_launcher')
            panel=FirefoxContainersPanel(storage=storage)
            try:
                with patch.object(QtWidgets.QFileDialog,'getExistingDirectory',return_value=str(path)), \
                     patch.object(panel,'refresh'):
                    panel.add_directory();panel.add_directory()
                self.assertEqual(storage.file('firefox/custom-profiles.json').read_json(),[str(path)])
                self.assertEqual((path/'containers.json').read_bytes(),before)
                restored=FirefoxContainersPanel(storage=storage)
                try:self.assertEqual(restored._extra,[str(path)])
                finally:restored.dispose();restored.deleteLater()
            finally:panel.dispose();panel.deleteLater();self.app.processEvents()

    def test_launcher_tab_is_lazy_and_menu_routes_to_the_correct_panel(self):
        from PYUI.chrome_runner_pyui import ChromeRunnerDialog
        from PYUI.firefox_containers_pyui import FirefoxContainersPanel
        from app_plugins.builtin.chrome_launcher import ChromeLauncherPlugin
        with tempfile.TemporaryDirectory() as root,patch.object(ChromeRunnerDialog,'load_profiles',lambda _self:None), \
             patch.object(FirefoxContainersPanel,'refresh') as refresh:
            dialog=ChromeRunnerDialog(config_path=str(Path(root)/'config.json'))
            try:
                refresh.assert_not_called()
                self.assertGreaterEqual(dialog.environment_tabs.indexOf(dialog.firefox_containers_panel),0)
                plugin=ChromeLauncherPlugin();plugin.dialog=dialog;plugin.open_launcher=Mock()
                plugin.open_firefox_containers()
                self.assertIs(dialog.environment_tabs.currentWidget(),dialog.firefox_containers_panel)
                plugin.open_launcher.assert_called_once()
            finally:dialog.firefox_containers_panel.dispose();dialog.close();dialog.deleteLater();self.app.processEvents()

    @unittest.skipUnless(os.name=='nt','native Windows profile commit')
    def test_real_background_create_and_followup_inspect_are_not_stuck_or_misdirected(self):
        import time
        from PYUI.firefox_containers_pyui import FirefoxContainersPanel
        from qt_compat import QtWidgets
        def wait(predicate):
            end=time.monotonic()+8
            while not predicate() and time.monotonic()<end:self.app.processEvents();time.sleep(.003)
            self.app.processEvents();self.assertTrue(predicate())
        with tempfile.TemporaryDirectory() as root:
            path=make_profile(root);other=make_profile(root,'other');other_raw=(other/'containers.json').read_bytes()
            panel=FirefoxContainersPanel()
            try:
                panel._completed({'action':'scan',**core.discover_profiles(Path(root)/'missing',extra=[str(path)])})
                panel.profiles.selectRow(0);wait(lambda:not panel.is_busy())
                self.assertIsNotNone(panel._state,panel.status.text());panel.prefix.setText('Test');panel.count.setValue(2)
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):panel.create()
                wait(lambda:not panel.is_busy())
                self.assertIsNotNone(panel._state,panel.status.text())
                self.assertEqual([item['name'] for item in panel._state['containers']],['Existing','Test01','Test02'])
                self.assertIn('创建 2 个',panel.result.text())
                self.assertEqual((other/'containers.json').read_bytes(),other_raw)
                self.assertFalse(panel.create_button.isEnabled())
            finally:
                wait(lambda:not panel.is_busy());panel.dispose();panel.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()

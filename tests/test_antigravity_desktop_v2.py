"""Synthetic v2 packages/credentials only; no real native credential API calls."""
import base64
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.antigravity_account_switcher import clients, native_auth, launcher
from app_plugins.builtin.antigravity_account_switcher.package_metadata import packaged_metadata
from app_plugins.builtin.antigravity_account_switcher.native_auth import NATIVE_AUTH_KEY, NativeAuthError
from app_plugins.builtin.antigravity_account_switcher.quota import inspect_auth_records, inspect_profile_credentials
from app_plugins.builtin.antigravity_account_switcher.store import AntigravityProfileStore, AccountSwitcherError
from app_plugins.builtin.antigravity_account_switcher.dialog import AntigravitySwitcherDialog
from app_plugins.builtin.antigravity_account_switcher.plugin import AntigravityAccountSwitcherPlugin
from model.AppDataStorage import ApplicationStorage
import test_antigravity_identity_sources as source_fixtures
from test_antigravity_account_switcher_plugin import create_mock_vscdb


def login_record(email='actual@example.test', **claim_changes):
    claims={'iss':'https://accounts.google.com','sub':'synthetic-user', 'email_verified':True,
            'email':email,'name':'Synthetic User', **claim_changes}
    encode=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    token=encode({'alg':'RS256'})+'.'+encode(claims)+'.synthetic-signature'
    blob=json.dumps({'token':{'access_token':'synthetic-access','refresh_token':'synthetic-refresh',
                              'token_type':'Bearer','expiry':'2099-01-01T00:00:00Z'},'id_token':token,'auth_method':'oauth'})
    return json.dumps({'version':1,'blob':base64.b64encode(blob.encode()).decode(),
                       'username':'antigravity','comment':'','persist':2})


def packaged_client(executable, **overrides):
    executable.parent.mkdir(parents=True,exist_ok=True)
    executable.write_bytes(b'synthetic executable, never launched')
    package={'name':'antigravity','productName':'Antigravity','version':'2.24.0',
             'main':'dist/main.js','author':{'name':'Google'}, **overrides}
    data=json.dumps(package).encode()
    header=json.dumps({'files':{'package.json':{'size':len(data),'offset':'0'}}}).encode()
    padding=b'\0'*(-len(header)%4)
    header_size=8+len(header)+len(padding)
    archive=executable.parent/'resources/app.asar'
    archive.parent.mkdir(parents=True,exist_ok=True)
    archive.write_bytes(struct.pack('<4I',4,header_size,header_size-4,len(header))+header+padding+data)
    return clients.installed_client(executable)


class FakeBackend:
    def __init__(self, record=None): self.record=record; self.writes=[]
    def read(self): return self.record
    def write(self, record): self.writes.append(record); self.record=record


class NativeIdentityTests(unittest.TestCase):
    def test_reads_only_google_id_token_email_not_unrelated_fields(self):
        raw=login_record()
        metadata,blob=native_auth.unpack_record(raw)
        payload=json.loads(blob); payload.update(email='wrong@example.test',name='Wrong')
        metadata['blob']=base64.b64encode(json.dumps(payload).encode()).decode()
        info=native_auth.inspect_native_record(json.dumps(metadata))
        self.assertEqual(info['email'],'actual@example.test')
        self.assertEqual(info['name'],'Synthetic User')
        self.assertTrue(info['has_token'])

    def test_unknown_issuer_missing_email_and_unverified_claims_are_rejected(self):
        for changes in ({'iss':'https://other.test'},{'email':None},{'email_verified':False},{'sub':None}):
            with self.subTest(changes=list(changes)),self.assertRaises(NativeAuthError):
                native_auth.inspect_native_record(login_record(**changes))

    def test_malformed_native_record_does_not_fall_back_to_old_ide_identity(self):
        info=inspect_auth_records({NATIVE_AUTH_KEY:'invalid', 'antigravityUnifiedStateSync.userStatus':'old'})
        self.assertIsNone(info['email']); self.assertFalse(info['has_token'])
        self.assertIn('不改读',info['identity_error'])

    def test_missing_refresh_token_and_bad_envelope_are_rejected_without_values_in_errors(self):
        metadata,blob=native_auth.unpack_record(login_record())
        data=json.loads(blob); del data['token']['refresh_token']
        metadata['blob']=base64.b64encode(json.dumps(data).encode()).decode()
        for raw in (json.dumps(metadata),'not-json',json.dumps({'version':1,'persist':2,'blob':'%%'})):
            with self.subTest(raw=len(raw)),self.assertRaises(NativeAuthError) as error:
                native_auth.inspect_native_record(raw)
            self.assertNotIn('synthetic-access',str(error.exception))

    def test_windows_api_is_lazy_and_never_enumerates_credentials(self):
        source=Path(native_auth.__file__).read_text('utf8')
        self.assertNotIn('CredEnumerate',source)
        backend=native_auth.WindowsCredentialBackend()
        with patch.object(native_auth,'_api',side_effect=NativeAuthError('not available')):
            with self.assertRaises(NativeAuthError): backend.read()

    def test_native_api_marshalling_uses_only_known_target_and_frees_read_buffer(self):
        raw=login_record(); metadata,blob=native_auth.unpack_record(raw)
        buffer=(ctypes.c_ubyte*len(blob)).from_buffer_copy(blob)
        record=native_auth._Credential(Type=1,TargetName=native_auth.TARGET,UserName=metadata['username'],
            CredentialBlobSize=len(blob),CredentialBlob=buffer,Persist=2)
        def read(target,kind,flags,output):
            self.assertEqual((target,kind,flags),(native_auth.TARGET,1,0))
            ctypes.cast(output,ctypes.POINTER(ctypes.POINTER(native_auth._Credential)))[0]=ctypes.pointer(record)
            return True
        written=[]
        def write(pointer,flags):
            value=ctypes.cast(pointer,ctypes.POINTER(native_auth._Credential)).contents
            written.append((value.TargetName,value.Type,value.Persist,value.UserName,
                            ctypes.string_at(value.CredentialBlob,value.CredentialBlobSize),flags))
            return True
        api=SimpleNamespace(CredReadW=read,CredFree=Mock(),CredWriteW=write,CredDeleteW=Mock(return_value=True))
        backend=native_auth.WindowsCredentialBackend()
        with patch.object(native_auth,'_api',return_value=api):
            restored=backend.read()
            self.assertEqual(native_auth.unpack_record(restored),(metadata,blob))
            backend.write(restored)
            backend.write(None)
            with self.assertRaises(NativeAuthError):backend.write('invalid')
        api.CredFree.assert_called_once()
        self.assertEqual(written,[(native_auth.TARGET,1,2,'antigravity',blob,0)])
        api.CredDeleteW.assert_called_once_with(native_auth.TARGET,1,0)

    def test_native_api_unknown_attributes_are_rejected_and_buffer_is_freed(self):
        record=native_auth._Credential(Type=1,TargetName=native_auth.TARGET,AttributeCount=1,CredentialBlobSize=1)
        def read(_target,_kind,_flags,output):
            ctypes.cast(output,ctypes.POINTER(ctypes.POINTER(native_auth._Credential)))[0]=ctypes.pointer(record)
            return True
        api=SimpleNamespace(CredReadW=read,CredFree=Mock())
        with patch.object(native_auth,'_api',return_value=api),self.assertRaises(NativeAuthError):
            native_auth.WindowsCredentialBackend().read()
        api.CredFree.assert_called_once()


class PackagedSourceTests(unittest.TestCase):
    setUp=source_fixtures.ClientSourceTests.setUp
    tearDown=source_fixtures.ClientSourceTests.tearDown
    installed=source_fixtures.ClientSourceTests.installed

    def hub(self):
        return packaged_client(self.root/'local/Programs/antigravity/Antigravity.exe')

    def test_running_packaged_desktop_is_selected_instead_of_old_ide(self):
        old=self.installed('Antigravity IDE'); current=self.hub()
        create_mock_vscdb(old.database,'old-ide','old@example.test')
        before=old.database.read_bytes()
        process=SimpleNamespace(info={'name':'Antigravity.exe'},exe=lambda:str(current.executable),
                                cmdline=lambda:[str(current.executable)])
        with patch.object(clients.psutil,'process_iter',return_value=[process]):
            selected=clients.default_client()
        self.assertEqual(selected.identity,current.identity)
        self.assertEqual(selected.auth_kind,'windows')
        self.assertEqual(selected.version,'2.24.0')
        self.assertTrue(selected.running)
        self.assertEqual(before,old.database.read_bytes())
        self.assertIn('Windows',selected.auth_source)

    def test_unrecognized_running_client_blocks_old_ide_fallback(self):
        self.installed('Antigravity IDE')
        unknown=self.root/'local/Programs/antigravity/Antigravity.exe'
        unknown.parent.mkdir(parents=True,exist_ok=True); unknown.write_bytes(b'orphan')
        process=SimpleNamespace(info={'name':'Antigravity.exe'},exe=lambda:str(unknown),cmdline=lambda:[str(unknown)])
        with patch.object(clients.psutil,'process_iter',return_value=[process]),self.assertRaisesRegex(clients.ClientSelectionError,'旧 IDE'):
            clients.default_client()

    def test_process_probe_failure_never_falls_back_to_saved_ide(self):
        self.installed('Antigravity IDE')
        with patch.object(clients.psutil,'process_iter',side_effect=clients.psutil.AccessDenied()),self.assertRaises(clients.ClientSelectionError):
            clients.default_client()

    def test_packaged_custom_user_data_preserves_native_auth_kind(self):
        current=self.hub(); custom=self.root/'custom'
        process=SimpleNamespace(info={'name':'Antigravity.exe'},exe=lambda:str(current.executable),
            cmdline=lambda:[str(current.executable),'--user-data-dir',str(custom)])
        with patch.object(clients.psutil,'process_iter',return_value=[process]): selected=clients.default_client()
        self.assertEqual(selected.user_data,custom)
        self.assertEqual(selected.auth_kind,'windows')

    def test_unrecognized_package_or_future_major_version_is_not_assumed_compatible(self):
        exe=self.root/'hub/Antigravity.exe'
        for overrides in ({'author':{'name':'other'}},{'version':'3.0.0'},{'main':'unexpected.js'}):
            packaged_client(exe,**overrides)
            self.assertIsNone(packaged_metadata(exe))
        archive=exe.parent/'resources/app.asar'
        archive.write_bytes(struct.pack('<4I',4,2**31,2**31-4,10))
        self.assertIsNone(packaged_metadata(exe))

    def test_native_backend_process_blocks_writes_even_after_window_exit(self):
        current=self.hub()
        backend=current.executable.parent/'resources/bin/language_server.exe'
        process=SimpleNamespace(info={'name':'language_server.exe'},exe=lambda:str(backend))
        with patch.object(launcher.psutil,'process_iter',return_value=[process]):
            self.assertTrue(launcher.is_antigravity_running(current))
        process.exe=lambda:str(self.root/'unrelated/language_server.exe')
        with patch.object(launcher.psutil,'process_iter',return_value=[process]):
            self.assertFalse(launcher.is_antigravity_running(current))


class NativeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.backend=FakeBackend(login_record())
        self.client=clients.AntigravityClient('Antigravity',self.root/'Antigravity.exe',self.root/'electron-data',
                                             auth_kind='windows',version='2.24.0')
        self.store=AntigravityProfileStore(self.root/'private',client=self.client,client_running=lambda:False,
                                          credential_backend=self.backend)
        self.initial=self.backend.record
        self.first=self.store.adopt_current('A')

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def second(self):
        profile=self.store.add_profile('B')
        self.store._write_auth(self.store.profile_db_path(profile),{NATIVE_AUTH_KEY:login_record('second@example.test')})
        return profile

    def test_adoption_uses_known_system_credential_without_creating_or_reading_live_sqlite(self):
        self.assertFalse(self.store.live_db_path.exists())
        self.assertEqual(self.backend.writes,[])
        self.assertEqual(self.store._identity_at(self.store.profile_db_path(self.first))['email'],'actual@example.test')
        self.assertTrue(self.store.has_auth(self.first))
        self.assertEqual(set(self.store._read_auth(self.store.profile_db_path(self.first))),{NATIVE_AUTH_KEY})

    def test_switch_and_binding_use_only_same_system_credential(self):
        second=self.second(); self.store.switch_to(second.profile_id)
        self.assertEqual(native_auth.inspect_native_record(self.backend.record)['email'],'second@example.test')
        new=self.store.add_profile('C'); self.store.switch_to(new.profile_id,allow_unbound=True)
        self.assertIsNone(self.backend.record)
        self.assertFalse(self.store.has_auth(new)); self.assertFalse(self.store.capture_current())
        self.backend.record=login_record('third@example.test')
        self.assertTrue(self.store.capture_current())
        self.assertIsNone(self.store.binding_profile_id)
        self.assertEqual(self.store._identity_at(self.store.profile_db_path(new))['email'],'third@example.test')
        self.assertEqual(self.store._identity_at(self.store.profile_db_path(self.first))['email'],'actual@example.test')

    def test_failed_native_write_restores_previous_record_and_index(self):
        second=self.second(); before=deepcopy(self.store._data)
        original=self.backend.write
        count=0
        def fail_once(record):
            nonlocal count
            count+=1
            if count==1: raise NativeAuthError('synthetic write failure')
            original(record)
        with patch.object(self.backend,'write',side_effect=fail_once),self.assertRaisesRegex(AccountSwitcherError,'已恢复'):
            self.store.switch_to(second.profile_id)
        self.assertEqual(self.backend.record,self.initial); self.assertEqual(self.store._data,before)

    def test_native_write_readback_mismatch_is_not_claimed_successful(self):
        second=self.second(); original=self.backend.read
        def incorrect():
            value=original()
            return login_record('unexpected@example.test') if value!=self.initial else value
        with patch.object(self.backend,'read',side_effect=incorrect),self.assertRaisesRegex(AccountSwitcherError,'已恢复'):
            self.store.switch_to(second.profile_id)
        self.assertEqual(self.backend.record,self.initial)

    def test_final_index_failure_restores_system_credential_and_saved_index(self):
        second=self.second(); original=self.store.save; before=deepcopy(self.store._data)
        calls=0
        def fail_final():
            nonlocal calls
            calls+=1
            if calls==2: raise OSError('synthetic final index failure')
            original()
        with patch.object(self.store,'save',side_effect=fail_final),self.assertRaisesRegex(AccountSwitcherError,'已恢复'):
            self.store.switch_to(second.profile_id)
        self.assertEqual(self.backend.record,self.initial)
        self.assertEqual(self.store._data,before)

    def test_double_failure_preserves_recovery_journal_and_snapshot(self):
        second=self.second()
        with patch.object(self.backend,'write',side_effect=NativeAuthError('synthetic failure')),self.assertRaisesRegex(AccountSwitcherError,'恢复也未完成'):
            self.store.switch_to(second.profile_id)
        self.assertTrue(self.store.needs_recovery); self.assertTrue(self.store.rollback_db_path.is_file())
        reopened=AntigravityProfileStore(self.store.root,client=self.client,client_running=lambda:False,
                                        credential_backend=self.backend)
        self.assertTrue(reopened.needs_recovery)
        reopened._recover_interrupted_switch()
        self.assertEqual(self.backend.record,self.initial); self.assertFalse(reopened.needs_recovery)

    def test_volume_check_failure_prevents_native_writes(self):
        with patch('app_plugins.builtin.antigravity_account_switcher.store.data_directory',side_effect=OSError('volume absent')):
            with self.assertRaises(OSError): self.store._write_auth(self.store.live_db_path,{NATIVE_AUTH_KEY:self.initial})
        self.assertEqual(self.backend.writes,[])

    def test_unknown_current_record_never_overwrites_saved_account(self):
        before=self.store.profile_db_path(self.first).read_bytes()
        self.backend.record='unknown record'
        with self.assertRaises(AccountSwitcherError): self.store.capture_current()
        self.assertEqual(self.store.profile_db_path(self.first).read_bytes(),before)
        self.assertEqual(self.backend.writes,[])

    def test_old_ide_record_is_not_accepted_for_native_switch(self):
        with self.assertRaisesRegex(AccountSwitcherError,'旧 IDE'):
            self.store._write_auth(self.store.live_db_path,{'antigravityUnifiedStateSync.oauthToken':'old'})
        self.assertEqual(self.backend.writes,[])

    def test_backend_must_be_closed_before_switch(self):
        second=self.second(); self.store._client_running=lambda:True
        with self.assertRaisesRegex(AccountSwitcherError,'退出'): self.store.switch_to(second.profile_id)
        self.assertEqual(self.backend.writes,[])

    def test_archive_inspector_and_live_reader_report_same_native_account(self):
        info=inspect_profile_credentials(self.first.profile_id,self.first.home_path)
        self.assertEqual(info.email,'actual@example.test')
        self.assertTrue(info.success)
        dialog=AntigravitySwitcherDialog(self.store,clients=[self.client])
        try:
            import time
            deadline=time.monotonic()+3
            while dialog._quota_worker is not None and time.monotonic()<deadline:
                self.app.processEvents(); time.sleep(.005)
            self.assertIsNone(dialog._quota_worker)
            self.assertIn('actual@example.test',dialog.active_label.text())
            self.assertIn('Windows 凭据管理器',dialog.source_label.text())
            self.assertEqual(dialog.table.item(0,1).text(),'actual@example.test')
        finally: dialog.deleteLater()

    def test_reopening_plugin_rechecks_running_source_instead_of_reusing_old_store(self):
        storage=ApplicationStorage(self.root/'host')
        context=SimpleNamespace(storage=storage.for_plugin('antigravity_account_switcher'),
                                parent_widget=None,register_command=Mock(),log=Mock())
        plugin=AntigravityAccountSwitcherPlugin(); plugin.register(context)
        old_client=clients.AntigravityClient('Antigravity IDE',self.root/'IDE.exe',self.root/'old-data')
        plugin.store=SimpleNamespace(client=old_client)
        plugin.clients=[self.client,old_client]
        with patch.object(plugin,'_choose_client',return_value=self.client),patch.object(plugin,'_client_store',return_value=self.store) as factory:
            dialog=plugin.open_manager()
        try:
            self.drain(dialog)
            self.assertIs(plugin.store,self.store); factory.assert_called_once_with(self.client)
            self.assertIn('actual@example.test',dialog.active_label.text())
            with patch.object(plugin,'_choose_client',side_effect=clients.ClientSelectionError('unknown running version')),patch.object(QtWidgets.QMessageBox,'critical'):
                self.assertIsNone(plugin.open_manager())
            self.assertFalse(dialog.isVisible())
            self.assertEqual(self.backend.writes,[])
        finally:plugin.stop()

    def test_old_saved_wrong_ide_choice_is_not_silently_reused_when_both_clients_idle(self):
        storage=ApplicationStorage(self.root/'host')
        area=storage.for_plugin('antigravity_account_switcher')
        plugin=AntigravityAccountSwitcherPlugin()
        plugin.register(SimpleNamespace(storage=area,parent_widget=None,register_command=Mock()))
        old=clients.AntigravityClient('Antigravity IDE',self.root/'IDE.exe',self.root/'old-data')
        area.file('selected_client.json').write_json({'client_id':old.identity})
        with patch('app_plugins.builtin.antigravity_account_switcher.plugin.discover_clients',return_value=[self.client,old]),patch.object(QtWidgets.QInputDialog,'getItem',return_value=(self.client.title,True)) as choose:
            self.assertEqual(plugin._choose_client().identity,self.client.identity)
        choose.assert_called_once()

    def test_new_explicit_choice_can_be_retained_without_reprompting(self):
        storage=ApplicationStorage(self.root/'host')
        area=storage.for_plugin('antigravity_account_switcher')
        plugin=AntigravityAccountSwitcherPlugin()
        plugin.register(SimpleNamespace(storage=area,parent_widget=None,register_command=Mock()))
        old=clients.AntigravityClient('Antigravity IDE',self.root/'IDE.exe',self.root/'old-data')
        plugin._save_client_choice(self.client)
        with patch('app_plugins.builtin.antigravity_account_switcher.plugin.discover_clients',return_value=[self.client,old]),patch.object(QtWidgets.QInputDialog,'getItem') as choose:
            self.assertEqual(plugin._choose_client().identity,self.client.identity)
        choose.assert_not_called()

    def drain(self,dialog):
        import time
        deadline=time.monotonic()+3
        while dialog._quota_worker is not None and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.005)
        self.assertIsNone(dialog._quota_worker)

    def test_failed_refresh_does_not_keep_stale_current_email_on_screen(self):
        dialog=AntigravitySwitcherDialog(self.store)
        try:
            self.drain(dialog)
            self.assertIn('actual@example.test',dialog.active_label.text())
            with patch.object(self.backend,'read',side_effect=NativeAuthError('source unavailable')),patch.object(QtWidgets.QMessageBox,'warning'):
                dialog.refresh_all()
            self.assertIsNone(dialog._live_info)
            self.assertNotIn('actual@example.test',dialog.active_label.text())
        finally:dialog.deleteLater()


if __name__=='__main__': unittest.main()

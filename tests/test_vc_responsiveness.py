"""Bounded synthetic tests: no user data, real mount, password or network."""
import json
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from model.VeraCryptVolumes import VeraCryptVolumes,WindowsDriver,VolumeError
from model.ProtectedStorage import VolumeSession,StorageUnavailable
from model.PrivateDataCatalog import no_reparse
from model.AppDataStorage import ApplicationStorage
from model.ConfigHotReload import ConfigHotReload
from PYUI.protected_storage_pyui import StorageMonitor
from test_protected_storage import FakeDriver,Provider


class RuntimeVerificationTests(unittest.TestCase):
    def verifier(self):
        driver=FakeDriver()
        driver.version=Mock(wraps=driver.version)
        driver.request=Mock(wraps=driver.request)
        provider=VeraCryptVolumes(driver)
        volume=provider.mounted()[0]
        verifier=provider.open_verifier(volume)
        self.addCleanup(verifier.close)
        return driver,verifier

    def test_per_operation_live_identity_without_version_or_other_volume_rescans(self):
        driver,verifier=self.verifier()
        driver.version.reset_mock();driver.request.reset_mock()
        for _ in range(200):verifier.check()
        driver.version.assert_not_called()
        self.assertEqual([call.args[1] for call in driver.request.call_args_list],[7]*200)

    def test_remount_identity_mapping_and_readonly_changes_fail_on_next_operation(self):
        for change in ('unique_id','identity','mapping','readonly','disabled','hidden','length','removed'):
            with self.subTest(change=change):
                driver,verifier=self.verifier()
                if change=='unique_id':driver.props.unique_id+=1
                elif change=='identity':driver.props.volume_id[0]=99
                elif change=='mapping':driver.mapping=r'\Device\HarddiskVolume9'
                elif change=='readonly':driver.props.readonly=1
                elif change=='disabled':driver.props.mount_disabled=1
                elif change=='hidden':driver.props.hidden_protection=2
                elif change=='length':driver.props.length+=1
                else:driver.request.side_effect=VolumeError('unmounted')
                with self.assertRaises(VolumeError):verifier.check()

    def test_retained_driver_is_closed_once_and_does_not_silently_reopen(self):
        driver,verifier=self.verifier();count=driver.closed
        verifier.close();verifier.close()
        self.assertEqual(driver.closed,count+1)
        with self.assertRaises(VolumeError):verifier.check()

    def test_boundary_checks_use_one_lstat_per_component_without_realpath(self):
        with tempfile.TemporaryDirectory() as root:
            boundary=Path(root);target=boundary/'private'/'nested'/'missing.json'
            with patch.object(Path,'lstat',autospec=True,side_effect=Path.lstat) as query, \
                    patch.object(Path,'resolve',side_effect=AssertionError('redundant disk walk')):
                self.assertEqual(no_reparse(target,boundary),target)
                self.assertEqual(query.call_count,4)

    def test_boundary_still_rejects_parent_reparse_points_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            boundary=Path(root);target=boundary/'private'/'state.json'
            original=Path.lstat
            def query(path):
                if path==target.parent:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
                return original(path)
            with patch.object(Path,'lstat',query),self.assertRaisesRegex(ValueError,'重解析'):
                no_reparse(target,boundary)
            with self.assertRaises(ValueError):no_reparse(boundary/'..'/'outside',boundary)

    @unittest.skipUnless(os.name=='nt','Windows sharing contract')
    def test_marker_read_lease_denies_write_delete_and_replace_until_closed(self):
        with tempfile.TemporaryDirectory() as root:
            marker=Path(root)/'marker.json';marker.write_text('identity')
            driver=WindowsDriver();handle=driver.open_handle(marker,shared_write=False)
            try:
                self.assertEqual(marker.read_text(),'identity')
                with self.assertRaises(OSError):marker.write_text('changed')
                with self.assertRaises(OSError):marker.unlink()
                replacement=Path(root)/'replacement';replacement.write_text('other')
                with self.assertRaises(OSError):os.replace(replacement,marker)
            finally:driver.close(handle)
            marker.write_text('released')
            self.assertEqual(marker.read_text(),'released')

    def test_a_slow_path_check_does_not_hold_the_process_wide_session_lock(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);provider=Provider(base);guard=VolumeSession(provider,provider.volume,base)
            self.addCleanup(guard.close)
            entered=threading.Event();release=threading.Event();errors=[]
            def path_check(path,_boundary):
                if Path(path).name=='slow':entered.set();release.wait(2)
            def slow():
                try:guard.check(base/'slow')
                except Exception as error:errors.append(error)
            with patch('model.ProtectedStorage.no_reparse',side_effect=path_check):
                worker=threading.Thread(target=slow);worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    begin=time.monotonic();guard.check(base/'fast')
                    self.assertLess(time.monotonic()-begin,.2)
                finally:release.set();worker.join(2)
            self.assertEqual(errors,[])

    def test_concurrent_failure_invalidates_a_previously_started_check(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);provider=Provider(base);guard=VolumeSession(provider,provider.volume,base)
            self.addCleanup(guard.close)
            entered=threading.Event();release=threading.Event();errors=[]
            def path_check(path,_boundary):entered.set();release.wait(2)
            def slow():
                try:guard.check(base/'slow')
                except StorageUnavailable:errors.append('stopped')
            with patch('model.ProtectedStorage.no_reparse',side_effect=path_check):
                worker=threading.Thread(target=slow);worker.start()
                try:
                    self.assertTrue(entered.wait(1));provider.available=False
                    with self.assertRaises(StorageUnavailable):guard.check(base/'other')
                finally:release.set();worker.join(2)
            self.assertEqual(errors,['stopped'])

    def test_incoming_write_does_not_repeat_capacity_and_identity_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            guard=Mock();storage=ApplicationStorage(root,guard=guard)
            storage.check_capacity(Path(root)/'state.json',1024)
            guard.ensure_capacity.assert_called_once()
            guard.check.assert_not_called()

    def test_each_sql_statement_still_verifies_but_not_twice(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);provider=Provider(base);guard=VolumeSession(provider,provider.volume,base)
            self.addCleanup(guard.close)
            storage=ApplicationStorage(base,guard=guard)
            self.addCleanup(storage.close)
            db=storage.file_at(base/'db.sqlite3').connect()
            try:
                with patch.object(guard,'check',wraps=guard.check) as checked:
                    db.execute('create table items(value)');self.assertEqual(checked.call_count,1)
                    checked.reset_mock()
                    db.executemany('insert into items values (?)',[(1,),(2,)])
                    self.assertEqual(checked.call_count,1)
                    checked.reset_mock();db.commit();self.assertEqual(checked.call_count,1)
                provider.available=False
                with self.assertRaises(StorageUnavailable):db.execute('select * from items')
            finally:db.close()


class AsyncMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_slow_health_probe_keeps_qt_responsive_and_does_not_stack_workers(self):
        window=QtWidgets.QWidget();window.background_tasks=Mock()
        entered=threading.Event();release=threading.Event();calls=[]
        def health():calls.append(threading.get_ident());entered.set();release.wait(2);return 10**10
        monitor=StorageMonitor(window,SimpleNamespace(guard=SimpleNamespace(health=health)))
        monitor.timer.stop();ticks=[]
        timer=QtCore.QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(1)
        try:
            begin=time.monotonic();monitor.poll();monitor.timer.stop()
            self.assertLess(time.monotonic()-begin,.2)
            self.assertTrue(entered.wait(1));worker=monitor.worker
            for _ in range(20):monitor.poll();self.app.processEvents();time.sleep(.002)
            self.assertIs(monitor.worker,worker);self.assertGreaterEqual(len(ticks),2)
            self.assertEqual(len(calls),1);self.assertNotEqual(calls[0],threading.get_ident())
        finally:
            release.set();monitor.worker.join(2);timer.stop();window.deleteLater()

    def watcher(self,root):
        path=Path(root)/'config.json';path.write_text('{"safe":1}')
        runtime={'safe':1};applied=[]
        def apply(old,value):
            self.assertEqual(QtCore.QThread.currentThread(),self.app.thread())
            applied.append(value);runtime.update(value)
        monitor=ConfigHotReload(path,lambda:dict(runtime),apply,lambda *_:None)
        monitor.timer.stop();self.addCleanup(monitor.deleteLater)
        return monitor,path,runtime,applied

    def test_destroying_window_during_probe_does_not_touch_deleted_qt_objects(self):
        from PyQt6 import sip
        entered=threading.Event();release=threading.Event()
        def health():entered.set();release.wait(2);return 10**10
        window=QtWidgets.QWidget();window.background_tasks=Mock()
        monitor=StorageMonitor(window,SimpleNamespace(guard=SimpleNamespace(health=health)))
        monitor.timer.stop();monitor.poll();monitor.timer.stop()
        worker=monitor.worker
        try:
            self.assertTrue(entered.wait(1));sip.delete(window)
        finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_destroying_config_watcher_during_read_has_no_qt_worker_callback(self):
        from PyQt6 import sip
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'config.json';path.write_text('{"safe":1}')
            monitor=ConfigHotReload(path,lambda:{'safe':1},lambda *_:None,lambda *_:None)
            monitor.timer.stop();entered=threading.Event();release=threading.Event()
            def probe(path,results,generation,_stamp,_pending):
                entered.set();release.wait(2);results.put((generation,None,None,None,None))
            with patch('model.ConfigHotReload._probe_config',side_effect=probe):
                monitor.request_poll();monitor.timer.stop();worker=monitor.worker
                try:
                    self.assertTrue(entered.wait(1));sip.delete(monitor)
                finally:release.set();worker.join(2)
                self.assertFalse(worker.is_alive())

    def cycle(self,monitor):
        monitor.request_poll();monitor.timer.stop()
        worker=monitor.worker;worker.join(2);self.assertFalse(worker.is_alive())
        monitor.request_poll();monitor.timer.stop()

    def test_async_config_preserves_debounce_and_only_applies_on_gui_thread(self):
        with tempfile.TemporaryDirectory() as root:
            monitor,path,runtime,applied=self.watcher(root)
            before=path.stat();path.write_text('{"safe":2}')
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+10**9))
            self.cycle(monitor);self.assertEqual(applied,[])
            self.cycle(monitor);self.assertEqual(runtime,{'safe':2})
            self.cycle(monitor);self.assertEqual(len(applied),1)

    def test_async_config_has_no_ui_filesystem_access_and_discards_pre_save_result(self):
        with tempfile.TemporaryDirectory() as root:
            monitor,path,runtime,applied=self.watcher(root)
            entered=threading.Event();release=threading.Event()
            def probe(_path,results,generation,_stamp,_pending):
                entered.set();release.wait(2)
                old={'safe':9};results.put((generation,(1,1),old,monitor._digest(old),None))
            with patch('model.ConfigHotReload._probe_config',side_effect=probe), \
                    patch.object(monitor,'_stamp',side_effect=AssertionError('UI stat')), \
                    patch.object(monitor,'read',side_effect=AssertionError('UI read')):
                begin=time.monotonic();monitor.request_poll();monitor.timer.stop()
                self.assertLess(time.monotonic()-begin,.2);self.assertTrue(entered.wait(1))
                worker=monitor.worker
                for _ in range(20):monitor.request_poll()
                self.assertIs(monitor.worker,worker)
            try:
                path.write_text('{"safe":3}');runtime['safe']=3;monitor.note_written()
            finally:release.set();worker.join(2)
            monitor.request_poll();monitor.timer.stop()
            self.assertEqual(applied,[]);self.assertEqual(monitor.observed,{'safe':3})

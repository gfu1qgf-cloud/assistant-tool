"""Real Qt task handles, missed-finish recovery, and no false unlocks."""
import threading,time
import unittest
from unittest.mock import Mock,patch
from model.BackgroundTasks import BackgroundTaskManager
from app_plugins.builtin.ai_agent_assistant.lifecycle import watch_finished,reclaim,settled
import test_agent_delivery as fixture


class AgentLifecycleTests(unittest.TestCase):
    setUpClass=fixture.ReceiptMonitorTests.__dict__['setUpClass']
    stage=fixture.ReceiptMonitorTests.stage

    def setUp(self):
        fixture.ReceiptMonitorTests.setUp(self)
        self.stage();self.plugin.store.save_settings({'auto_download':False})
        self.manager=BackgroundTaskManager(logger=lambda *_:None)
        self.addCleanup(self.clean_manager)

    def clean_manager(self):
        self.plugin.receipts.running=False
        self.manager.cancel_all();self.wait(lambda:not self.manager.is_busy())
        self.manager.shutdown();self.manager.deleteLater()
        fixture.ReceiptMonitorTests.tearDown(self)

    def wait(self,predicate,seconds=4):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:
            self.app.processEvents();time.sleep(.002)
        self.app.processEvents();self.assertTrue(predicate())

    def submit(self,owner,runner=None):
        owner.handle=self.manager.submit(owner='ai_agent_assistant',title='synthetic lifecycle',
            runner=runner or (lambda *_:{'message':'done'}),payload={})
        watch_finished(owner,owner.handle)
        return owner.handle

    def test_real_finished_callback_releases_each_owner_without_sender_lookup(self):
        for owner in (self.plugin,self.plugin.receipts,self.plugin.notices):
            handle=self.submit(owner)
            self.wait(lambda:owner.handle is None)
            self.assertTrue(handle.isSettled());self.assertFalse(owner._handle_watch.isActive())
        self.assertTrue(self.plugin.can_close()[0])

    def test_finished_signal_lost_watchdog_recovers_all_three_owners(self):
        for owner in (self.plugin,self.plugin.receipts,self.plugin.notices):
            handle=self.submit(owner);handle.finished.disconnect()
            if owner is self.plugin.notices:
                # Recovery of a missing snapshot asks for a fresh snapshot;
                # use an observable stub to avoid creating another job here.
                with patch.object(owner,'refresh') as refresh:
                    self.wait(lambda:owner.handle is None)
                    refresh.assert_called_once()
            else:self.wait(lambda:owner.handle is None)
        self.assertTrue(self.plugin.can_close()[0])

    def test_terminal_handle_cannot_permanently_block_exit_or_manual_receipt(self):
        h=self.manager.submit(owner='ai_agent_assistant',title='finished before binding',runner=lambda *_:{},payload={})
        self.wait(lambda:h.isSettled())
        self.plugin.handle=h;self.plugin.receipts.handle=h;self.plugin.notices.handle=h
        self.assertTrue(self.plugin.can_close()[0]);self.assertTrue(self.plugin.stop())
        self.plugin.receipts.handle=h;self.plugin.handle=h
        with patch.object(self.plugin.receipts,'_check') as check:
            self.plugin.receipts.check(manual=True);check.assert_called_once()
        self.assertIsNone(self.plugin.receipts.handle);self.assertIsNone(self.plugin.handle)

    def test_live_worker_and_result_callback_still_block_exit(self):
        release=threading.Event()
        handle=self.submit(self.plugin,lambda *_:release.wait(2))
        self.wait(lambda:handle.isRunning())
        self.assertFalse(settled(handle));self.assertFalse(self.plugin.can_close()[0])
        observed=[];handle.completed.connect(lambda _:observed.append((handle.isSettled(),self.plugin.can_close()[0])))
        release.set();self.wait(lambda:self.plugin.handle is None)
        self.assertEqual(observed,[(False,False)])

    def test_old_finish_cannot_release_a_newer_handle(self):
        old=self.submit(self.plugin);self.wait(lambda:self.plugin.handle is None)
        release=threading.Event();new=self.submit(self.plugin,lambda *_:release.wait(2))
        self.plugin.finished(old);self.assertIs(self.plugin.handle,new)
        release.set();self.wait(lambda:self.plugin.handle is None)

    def test_stop_requests_all_owners_not_only_first_busy_handle(self):
        handles=[]
        for owner in (self.plugin,self.plugin.receipts,self.plugin.notices):
            owner.handle=fixture.Handle();owner.handle.canceled=False;handles.append(owner.handle)
        self.assertFalse(self.plugin.stop())
        self.assertTrue(all(h.canceled for h in handles))
        for owner in (self.plugin,self.plugin.receipts,self.plugin.notices):owner.handle=None

    def test_unknown_handle_is_never_assumed_finished(self):
        self.plugin.handle=fixture.Handle()
        self.assertFalse(self.plugin.can_close()[0]);self.assertFalse(reclaim(self.plugin))
        self.plugin.handle=None

    def test_native_qthread_cannot_use_the_handle_cleanup_helper(self):
        thread=fixture.QtCore.QThread()
        with self.assertRaises(TypeError):watch_finished(self.plugin,thread)
        thread.deleteLater()

    def test_lost_receipt_finish_rearms_single_catchup_but_stop_does_not(self):
        monitor=self.plugin.receipts;monitor.running=True
        self.plugin.store.save_settings({'auto_download':True,'poll_minutes':60})
        handle=self.manager.submit(owner='ai_agent_assistant',title='missed finish',runner=lambda *_:{},payload={})
        self.wait(lambda:handle.isSettled());monitor.handle=handle
        self.assertTrue(reclaim(monitor));self.assertEqual(monitor.timer.interval(),0)
        monitor.timer.stop();monitor.running=False;monitor.handle=handle
        self.assertTrue(reclaim(monitor,closing=True));self.assertFalse(monitor.timer.isActive())

    def test_an_unrelated_worker_does_not_prevent_terminal_handle_cleanup(self):
        old=self.manager.submit(owner='ai_agent_assistant',title='finished',runner=lambda *_:{},payload={})
        self.wait(lambda:old.isSettled())
        release=threading.Event()
        other=self.manager.submit(owner='other',title='unrelated',runner=lambda *_:release.wait(2),payload={})
        self.wait(lambda:other.isRunning());self.plugin.handle=old
        self.assertTrue(self.plugin.can_close()[0]);self.assertIsNone(self.plugin.handle)
        release.set();self.wait(lambda:other.isSettled())

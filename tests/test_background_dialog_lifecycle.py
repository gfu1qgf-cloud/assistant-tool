"""Run lifecycle regressions in native child processes: Qt may abort Python."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from PYUI import background_tasks_pyui
from model import BackgroundTasks


class BackgroundDialogLifecycleTests(unittest.TestCase):
    def native(self, scenario):
        script=textwrap.dedent('''
            import atexit,faulthandler,importlib.util,os,sys,tempfile,time
            from pathlib import Path
            from unittest.mock import patch
            faulthandler.enable()
            actual,source,scenario=sys.argv[1:]
            sys.path.insert(0,actual)
            previous=os.getcwd(); temporary=tempfile.TemporaryDirectory()
            atexit.register(lambda:os.chdir(previous)); os.chdir(temporary.name)
            import app_paths
            app_paths.APP_ROOT=app_paths.DATA_ROOT=Path(temporary.name)
            from qt_compat import QtCore,QtWidgets
            from PyQt6 import sip
            from model.BackgroundTasks import BackgroundTaskManager
            spec=importlib.util.spec_from_file_location('native_view',source)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            app=QtWidgets.QApplication([])
            for iteration in range(25):
                manager=BackgroundTaskManager()
                handle=manager.submit(owner='test',title='synthetic',runner=lambda *_:None)
                deadline=time.monotonic()+3
                while manager.is_busy() and time.monotonic()<deadline:
                    app.processEvents();time.sleep(.001)
                assert not manager.is_busy()
                view=module.BackgroundTasksWidget(manager);view.show()
                card=view._cards[handle.task_id]
                if scenario=='double-delete':
                    card.delete_button.click()
                    assert not card.delete_button.isEnabled()
                    card.delete_button.click()
                    card.delete_task()
                    assert not view._messages
                elif scenario=='pending-delete':
                    manager._notifying.add(handle.task_id)
                    card.delete_task()
                    manager._notifying.clear()
                    assert len(view._messages)==1
                    manager.remove(handle.task_id)
                elif scenario=='save-error':
                    with patch.object(manager,'flush_history',return_value=False):
                        card.delete_task()
                    assert len(view._messages)==1
                    assert manager.can_remove(handle.task_id)
                    manager.remove(handle.task_id)
                else:
                    getattr(card,scenario)()
                    assert len(view._messages)==1
                    getattr(card,scenario)()  # Identical messages are coalesced.
                    assert len(view._messages)==1
                    manager.remove(handle.task_id)
                messages=list(view._messages.values())
                assert all(message.parentWidget() is view for message in messages)
                view.refresh()
                QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
                assert sip.isdeleted(card)
                assert all(not sip.isdeleted(message) for message in messages)
                # Half the time dismiss dialogs, half close the entire panel.
                if iteration%2:
                    for message in messages:message.accept()
                    QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
                    app.processEvents()
                    assert not view._messages
                manager.shutdown();view.close();view.deleteLater();manager.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
                assert all(sip.isdeleted(message) for message in messages)
            os.chdir(previous);temporary.cleanup()
            print('native-lifecycle-ok',scenario)
        ''')
        result=subprocess.run([sys.executable,'-c',script,
            str(Path(BackgroundTasks.__file__).parents[1]),str(Path(background_tasks_pyui.__file__)),scenario],
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1'},
            capture_output=True,text=True,encoding='utf8',timeout=40)
        self.assertEqual(result.returncode,0,
            f'native exit {result.returncode:#x}:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}')
        self.assertIn('native-lifecycle-ok',result.stdout)

    def test_rapid_double_delete_and_stale_callback(self):self.native('double-delete')
    def test_pending_delete_message_survives_card_refresh(self):self.native('pending-delete')
    def test_delete_storage_error_message_and_retry_remain_safe(self):self.native('save-error')
    def test_detail_message_survives_card_removal_and_panel_close(self):self.native('show_details')
    def test_missing_result_message_survives_card_removal(self):self.native('open_result')
    def test_retry_error_message_survives_card_removal(self):self.native('retry_task')


if __name__=='__main__':unittest.main()

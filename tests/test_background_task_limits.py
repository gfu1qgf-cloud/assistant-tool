"""Host admission settings with running/queued jobs, safe locks and real UI."""
import copy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from model.BackgroundTaskSettings import CONFIG_KEY,DEFAULTS,VIDEO_PRIORITY,normalize_settings,manager_options
from model.BackgroundTasks import BackgroundTaskManager,directory_resource
from PYUI.background_task_settings_pyui import BackgroundTaskSettingsPage
from PYUI.background_tasks_pyui import BackgroundTasksWidget
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.main_pyui import MainDialog
from app_plugins.host import PluginHost


class LimitSchemaTests(unittest.TestCase):
    def test_defaults_and_video_priority_are_independent_and_model_cpu_remain_fixed(self):
        first=normalize_settings();first['kind_limits']['media']=8
        self.assertEqual(normalize_settings(),DEFAULTS)
        value={**VIDEO_PRIORITY,'kind_limits':{**VIDEO_PRIORITY['kind_limits'],'model':8,'cpu':8}}
        options=manager_options(value)
        self.assertEqual(options['max_parallel'],6)
        self.assertEqual(options['kind_limits'],{'media':4,'network':2,'cpu':1,'model':1})

    def test_invalid_values_are_rejected_not_silently_enabled(self):
        for value in ([],False,{'kind_limits':[]},{'max_parallel':True},{'max_parallel':9},
                      {'max_parallel':0},{'kind_limits':{'media':2.5}},{'kind_limits':{'network':'8'}}):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_settings(value)


class SchedulingLimitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.manager=BackgroundTaskManager();self.releases={};self.entered=[]
    def tearDown(self):
        for event in self.releases.values():event.set()
        self.manager.cancel_all();self.wait(lambda:not self.manager.is_busy())
        self.manager.shutdown();self.manager.deleteLater();self.app.processEvents()
    def wait(self,predicate):
        end=time.monotonic()+4
        while not predicate() and time.monotonic()<end:self.app.processEvents();time.sleep(.002)
        self.app.processEvents();self.assertTrue(predicate())
    def settle(self):
        for _ in range(8):self.app.processEvents();time.sleep(.002)
    def submit(self,name,kind='media',resources=(),execution='parallel'):
        self.releases[name]=threading.Event()
        def runner(payload,context):
            self.entered.append(payload)
            while not self.releases[payload].wait(.01):context.check_canceled()
            return {'value':payload}
        return self.manager.submit(owner='test',title=name,runner=runner,payload=name,
            kind=kind,resources=resources,execution=execution)

    def test_raise_starts_queued_media_and_lower_never_interrupts_running_workers(self):
        handles=[self.submit(str(i)) for i in range(6)]
        self.wait(lambda:len(self.entered)==2)
        self.manager.apply_limits(VIDEO_PRIORITY)
        self.wait(lambda:len(self.entered)==4)
        workers=dict(self.manager._workers)
        self.manager.apply_limits({'max_parallel':2,'kind_limits':{'media':1,'network':2}})
        self.settle();self.assertEqual(self.manager._workers,workers)
        self.assertEqual(len(self.entered),4)
        for name in ('0','1','2'):self.releases[name].set()
        self.wait(lambda:len(self.manager._workers)==1)
        self.settle();self.assertEqual(len(self.entered),4)
        self.releases['3'].set();self.wait(lambda:len(self.entered)==5)
        self.assertEqual(self.manager.get(handles[5].task_id)['status'],'queued')
        self.manager.cancel(handles[5].task_id)
        self.assertEqual(self.manager.get(handles[5].task_id)['status'],'canceled')

    def test_high_limits_preserve_directory_model_cpu_and_exclusive_barriers(self):
        self.manager.apply_limits({'max_parallel':8,'kind_limits':{'media':8,'network':8,'model':8,'cpu':8}})
        first=self.submit('writer',resources=['directory:a'])
        same=self.submit('same-directory',resources=['directory:a'])
        model=self.submit('model-one',kind='model');second_model=self.submit('model-two',kind='model')
        cpu=self.submit('cpu-one',kind='cpu');second_cpu=self.submit('cpu-two',kind='cpu')
        other=self.submit('other',resources=['directory:b'])
        self.wait(lambda:len(self.entered)==4)
        self.assertIn('共用资源',self.manager.waiting_reason(same.task_id))
        self.assertIn('模型并发名额（1/1）',self.manager.waiting_reason(second_model.task_id))
        self.assertIn('计算并发名额（1/1）',self.manager.waiting_reason(second_cpu.task_id))
        barrier=self.submit('exclusive',execution='exclusive')
        late=self.submit('after-exclusive',kind='network')
        self.settle();self.assertNotIn('after-exclusive',self.entered)
        self.assertEqual(self.manager.get(first.task_id)['status'],'running')

    def test_total_limit_still_caps_combined_categories(self):
        self.manager.apply_limits({'max_parallel':3,'kind_limits':{'media':8,'network':8}})
        self.submit('media-one');self.submit('net-one',kind='network');self.submit('media-two')
        queued=self.submit('net-two',kind='network')
        self.wait(lambda:len(self.entered)==3)
        self.assertIn('后台并发名额（3/3）',self.manager.waiting_reason(queued.task_id))

    def test_apply_requires_main_thread_and_closed_manager_does_not_reopen(self):
        errors=[]
        def update():
            try:self.manager.apply_limits(VIDEO_PRIORITY)
            except RuntimeError as error:errors.append(error)
        worker=threading.Thread(target=update);worker.start();worker.join(1)
        self.assertEqual(len(errors),1);self.assertEqual(self.manager.max_parallel,3)
        self.manager.stop_accepting();self.manager.apply_limits(VIDEO_PRIORITY)
        self.assertFalse(self.manager.is_accepting())

    def test_background_view_shows_current_limits_without_wide_toolbar(self):
        view=BackgroundTasksWidget(self.manager);self.addCleanup(view.deleteLater)
        self.manager.apply_limits(VIDEO_PRIORITY);view.refresh()
        self.assertIn('总6／媒体4／联网2',view.limits_label.text())
        self.assertTrue(view.limits_label.wordWrap())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Set NODE_FFMPEG for native media regression')
    def test_four_media_tasks_render_independent_videos_and_finish_normally(self):
        self.manager.apply_limits(VIDEO_PRIORITY)
        gate=threading.Event();entered=[]
        with tempfile.TemporaryDirectory() as root:
            outputs=[]
            for i in range(4):
                directory=Path(root)/str(i);directory.mkdir()
                outputs.append(directory/'video.mp4')
            def runner(path,context):
                entered.append(path)
                while not gate.wait(.01):context.check_canceled()
                result=subprocess.run([os.environ['NODE_FFMPEG'],'-hide_banner','-loglevel','error',
                    '-f','lavfi','-i','color=c=blue:s=160x90:r=10:d=0.5',
                    '-c:v','libx264','-preset','ultrafast','-threads','1','-y',path],
                    capture_output=True,timeout=15)
                if result.returncode:raise ValueError(result.stderr.decode('utf8','replace'))
                return {'paths':[path]}
            handles=[self.manager.submit(owner='test',title=f'video {i}',runner=runner,kind='media',
                payload=str(path),resources=[directory_resource(path.parent)]) for i,path in enumerate(outputs)]
            try:self.wait(lambda:len(entered)==4)
            finally:
                gate.set();self.wait(lambda:not self.manager.is_busy())
            self.assertTrue(all(self.manager.get(h.task_id)['status']=='succeeded' for h in handles))
            self.assertTrue(all(path.stat().st_size>0 for path in outputs))


class SettingsLimitUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_page_effective_caps_and_preset_are_explicit_and_fixed_fields_not_editable(self):
        page=BackgroundTaskSettingsPage();self.addCleanup(page.deleteLater)
        self.assertEqual(set(page.controls),{'max_parallel','media','network'})
        page.controls['media'].setValue(8)
        self.assertEqual(page.settings()['kind_limits']['media'],8)
        self.assertIn('媒体最多 3 个',page.effective.text())
        button=next(button for button in page.findChildren(QtWidgets.QPushButton) if button.text().startswith('视频优先'))
        button.click();self.assertEqual(page.settings(),VIDEO_PRIORITY)
        if os.environ.get('LZX_LIMITS_SCREENSHOT'):
            page.resize(700,620);page.show();self.app.processEvents()
            self.assertTrue(page.grab().save(os.environ['LZX_LIMITS_SCREENSHOT']))

    def test_real_settings_save_roundtrip_keeps_private_data_and_cancel_does_not_write(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'config.json'
            original={'run_upload':False,'elevenlabs_api_keys':['synthetic-ele'],
                'gemini_api_keys':['synthetic-gemini'],'kept':{'data':'unchanged'},CONFIG_KEY:DEFAULTS}
            path.write_text(json.dumps(original),encoding='utf8')
            with patch('PYUI.main_setting_pyui.CONFIG_PATH',str(path)):
                dialog=MainSettingDialog()
                try:
                    page=dialog.background_task_settings
                    nav=dialog.settings_navigation
                    index=dialog.settingTabWidget.indexOf(page)
                    self.assertIs(nav.items[index].parent(),nav.program_group)
                    page.set_settings(VIDEO_PRIORITY);self.assertTrue(dialog.save_config())
                    saved=json.loads(path.read_text('utf8'))
                    self.assertEqual(saved[CONFIG_KEY],VIDEO_PRIORITY)
                    for key in ('kept','elevenlabs_api_keys','gemini_api_keys'):self.assertEqual(saved[key],original[key])
                finally:dialog.deleteLater()
                restored=MainSettingDialog()
                try:
                    self.assertEqual(restored.background_task_settings.settings(),VIDEO_PRIORITY)
                    before=path.read_bytes();restored.background_task_settings.set_settings(DEFAULTS);restored.reject()
                    self.assertEqual(path.read_bytes(),before)
                finally:restored.deleteLater()

    def test_host_loads_saved_limits_and_invalid_config_falls_back_without_rewriting(self):
        class Main(QtWidgets.QWidget):
            def load_config(self):return {CONFIG_KEY:copy.deepcopy(self.config)}
            def appendLog(self,*args,**kwargs):self.logs.append(args)
        for value,expected in ((VIDEO_PRIORITY,6),({'max_parallel':100},3)):
            window=Main();window.config=value;window.logs=[];host=PluginHost(window)
            try:
                manager=host.background_tasks
                self.assertEqual(manager.max_parallel,expected)
                self.assertEqual(manager.kind_limits['model'],1)
                self.assertEqual(window.config,value)
                if expected==3:self.assertTrue(window.logs)
            finally:manager.shutdown();window.deleteLater();self.app.processEvents()

    def test_only_background_changes_apply_without_restarting_other_plugins_or_models(self):
        before={CONFIG_KEY:DEFAULTS};after={CONFIG_KEY:VIDEO_PRIORITY}
        host=Mock();host.apply_background_task_settings.return_value=VIDEO_PRIORITY
        window=SimpleNamespace(load_config=Mock(side_effect=[before,after]),plugin_host=host,
            gemini_keys=Mock(),appendLog=Mock(),config_reload=None)
        with patch('PYUI.main_pyui.MainSettingDialog.get_settings',return_value=after), \
             patch('PYUI.main_pyui.globalValue.loaded_whisper_model_name',return_value='large-v3'):
            MainDialog.openSettings(window)
        host.apply_background_task_settings.assert_called_once_with(after)
        host.apply_settings.assert_not_called()

    def test_failed_settings_save_does_not_change_running_limits_or_file(self):
        class Main(QtWidgets.QWidget):
            def load_config(self):return {CONFIG_KEY:DEFAULTS}
            def appendLog(self,*args,**kwargs):pass
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'config.json';path.write_text(json.dumps({'run_upload':False,CONFIG_KEY:DEFAULTS}))
            window=Main();host=PluginHost(window);manager=host.background_tasks
            with patch('PYUI.main_setting_pyui.CONFIG_PATH',str(path)):
                dialog=MainSettingDialog(plugin_host=host)
                try:
                    dialog.background_task_settings.set_settings(VIDEO_PRIORITY);before=path.read_bytes()
                    with patch('PYUI.main_setting_pyui._write_config_with_rolling_backups',side_effect=OSError('synthetic disk failure')), \
                         patch('PYUI.main_setting_pyui.QMessageBox.critical') as notice:
                        self.assertFalse(dialog.save_config());notice.assert_called_once()
                    self.assertEqual(manager.max_parallel,3);self.assertEqual(manager.kind_limits['media'],2)
                    self.assertEqual(path.read_bytes(),before)
                finally:
                    dialog.deleteLater();manager.shutdown();window.deleteLater();self.app.processEvents()

    def test_external_hot_reload_applies_only_safe_admission_limits(self):
        host=Mock();window=SimpleNamespace(plugin_host=host,appendLog=Mock(),showDesktopNotification=Mock())
        retry=MainDialog.applyExternalConfig(window,{CONFIG_KEY:DEFAULTS},{CONFIG_KEY:VIDEO_PRIORITY})
        host.apply_background_task_settings.assert_called_once_with({CONFIG_KEY:VIDEO_PRIORITY})
        host.apply_settings.assert_not_called();self.assertEqual(retry,set())

"""Executable documentation contract; no user accounts, tasks or paid APIs."""
import importlib.util
import os
from pathlib import Path
import re
from runpy import run_path
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from app_plugins.host import PluginHost
from model.AppDataStorage import ApplicationStorage

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs' / 'developer'
spec = importlib.util.spec_from_file_location('developer_sample_plugin', DOCS / 'examples' / 'sample_plugin.py')
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)


class DemoWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = {}
        self.messages = []

    def load_config(self):
        return dict(self.config)

    def appendLog(self, message, **kwargs):
        self.messages.append(str(message))


class DeveloperDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = ApplicationStorage(self.temporary.name)
        self.storage_patch = patch('model.AppDataStorage.application_storage', return_value=self.storage)
        self.storage_patch.start()
        self.window = DemoWindow()
        self.host = PluginHost(self.window)
        self.plugin = self.host.install(sample.ExamplePlugin())

    def tearDown(self):
        self.assertTrue(self.host.stop_all())
        self.window.deleteLater()
        self.app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.storage_patch.stop()
        self.temporary.cleanup()

    def wait_handle(self, handle):
        deadline = time.monotonic() + 5
        while (handle.isRunning() or self.plugin.handle is not None) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(handle.isRunning(), 'Sample did not settle within five seconds')

    def test_real_host_registers_commands_ui_and_node(self):
        self.host.attach_main_menu(self.window.menuBar())
        self.assertIn('developer_example.open', self.host._commands)
        self.assertIn('developer_example.uppercase', self.host.available_nodes().definitions)
        center = QtWidgets.QWidget(self.window)
        layout = QtWidgets.QVBoxLayout(center)
        self.window.setCentralWidget(center)
        self.host.attach_main_widget_area(center, layout)
        tabs = QtWidgets.QTabWidget(center)
        self.host.attach_tab_area(tabs)
        self.assertEqual(tabs.tabText(0), '开发示例分页')
        self.assertIsNotNone(self.host.main_widget_frame('developer_example', 'quick'))
        self.host.invoke('developer_example.selected', rows=(1, 2))
        self.assertTrue(any(message.endswith('所选任务数量：2') for message in self.window.messages))

    def test_settings_validate_load_and_update_without_persisting(self):
        tabs = QtWidgets.QTabWidget()
        entries = self.host.create_settings_pages(self.window, tabs)
        controller = entries[0][2]
        self.host.load_settings_pages({'developer_example': {'prefix': '完成'}})
        self.assertEqual(controller.prefix.text(), '完成')
        controller.prefix.clear()
        self.assertIsNotNone(self.host.validate_settings_pages())
        controller.prefix.setText('结果')
        self.assertIsNone(self.host.validate_settings_pages())
        config = {'unrelated': 123}
        self.host.update_settings_config(config)
        self.assertEqual(config['unrelated'], 123)
        self.assertEqual(config['developer_example']['prefix'], '结果')
        self.assertEqual(self.window.config, {})
        tabs.deleteLater()

    def test_function_node_runs_through_registered_builder(self):
        definition = self.host.available_nodes().definitions['developer_example.uppercase']
        from model.BackgroundTasks import TaskRunContext
        from threading import Event
        context = TaskRunContext(Event(), lambda *args: None, lambda *args: None)
        self.assertEqual(definition.build({'text': 'Hello'}, {}, context), {'result': 'HELLO'})

    def test_real_background_task_completes_after_window_hides(self):
        self.host.start_all()
        dialog = self.plugin.open_dialog()
        handle = self.plugin.submit('Hello')
        dialog.editor.setPlainText('Different text after submission')
        dialog.close()
        self.wait_handle(handle)
        self.assertEqual(self.plugin.last_result, {'characters': 5})
        self.assertIsNone(self.plugin.handle)
        self.assertTrue(dialog.run_button.isEnabled())
        self.assertTrue(self.plugin.can_close()[0])

    def test_runner_honors_cancel_before_work(self):
        from threading import Event
        from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
        canceled = Event()
        canceled.set()
        run = TaskRunContext(canceled, lambda *args: None, lambda *args: None)
        with self.assertRaises(BackgroundTaskCanceled):
            sample.count_text({'text': 'abc'}, run)

    def test_rejected_submission_does_not_escape_gui_or_disable_controls(self):
        dialog = self.plugin.open_dialog()
        self.host.background_tasks.stop_accepting()
        self.assertIsNone(self.plugin.submit('abc'))
        self.assertIsNone(self.plugin.handle)
        self.assertTrue(dialog.run_button.isEnabled())
        self.assertTrue(dialog.status.text().startswith('失败：'))

    def test_markdown_links_resolve(self):
        # Overlay tests may read unchanged source links from the real source root.
        base = Path(os.environ.get('LZX_TEST_BASE_ROOT', ROOT))
        for file in DOCS.glob('*.md'):
            for target in re.findall(r'\]\(([^)]+)\)', file.read_text(encoding='utf-8')):
                if '://' in target or target.startswith('#'):
                    continue
                target = target.split('#', 1)[0]
                path = (file.parent / target).resolve()
                if path.exists():
                    continue
                relative = Path(os.path.relpath(path, ROOT))
                self.assertTrue((base / relative).exists(), f'{file.name}: {target}')

    def test_python_snippets_are_syntactically_valid(self):
        import ast
        for file in DOCS.glob('*.md'):
            for code in re.findall(r'```python\n(.*?)```', file.read_text(encoding='utf-8'), re.S):
                ast.parse(code, filename=file.name)

    @unittest.skipUnless(os.environ.get('LZX_DOC_SCREENSHOTS') == '1', 'Explicit screenshot generation only')
    def test_render_documentation_screenshots(self):
        assets = DOCS / 'assets'
        assets.mkdir(exist_ok=True)
        self.window.setWindowTitle('隔离开发示例 · 非生产界面')
        self.window.resize(800, 350)
        self.host.attach_main_menu(self.window.menuBar())
        center = QtWidgets.QWidget(self.window)
        layout = QtWidgets.QVBoxLayout(center)
        self.window.setCentralWidget(center)
        layout.addWidget(QtWidgets.QLabel('隔离开发示例：宿主挂载插件内容，不读取用户任务。'))
        self.host.attach_main_widget_area(center, layout)
        tabs = QtWidgets.QTabWidget(center)
        self.host.attach_tab_area(tabs)
        layout.addWidget(tabs)
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(assets / 'plugin-surfaces.png')))
        settings = QtWidgets.QDialog(self.window)
        settings.setWindowTitle('程序设置 · 隔离示例')
        settings.resize(650, 230)
        sl = QtWidgets.QVBoxLayout(settings)
        st = QtWidgets.QTabWidget(settings)
        sl.addWidget(st)
        self.host.create_settings_pages(settings, st)
        self.host.load_settings_pages({})
        settings.show()
        self.app.processEvents()
        self.assertTrue(settings.grab().save(str(assets / 'plugin-settings.png')))
        dialog = self.plugin.open_dialog()
        self.app.processEvents()
        self.assertTrue(dialog.grab().save(str(assets / 'plugin-dialog.png')))
        settings.hide()


class DocumentationPackagingTests(unittest.TestCase):
    def test_developer_docs_are_rejected_but_dependency_readme_allowed(self):
        api = run_path(str(ROOT / 'packaging' / 'check_release_artifact.py'))
        required = api['REQUIRED_FILES']
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'release.zip'
            for extra in ('docs/developer/README.md', '_internal/docs/developer/examples/sample_plugin.py',
                          '_internal/DOCS/DEVELOPER/assets/plugin-dialog.png',
                          '_internal/vendor/docs/README.md'):
                with self.subTest(extra=extra):
                    with ZipFile(archive, 'w') as output:
                        for name in required | {extra}:
                            output.writestr(name, 'test')
                    if 'developer' in extra.casefold():
                        with self.assertRaises(ValueError):
                            api['validate_release_archive'](archive)
                    else:
                        api['validate_release_archive'](archive)

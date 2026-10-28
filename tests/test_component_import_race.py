import importlib
import importlib.abc
import importlib.util
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from app_plugins.builtin.batch_text_video import component_inspector as inspector

Panel = type('CurrentPanel', (), {'_inspector_edited': lambda *args: None})


class ComponentImportRaceTests(unittest.TestCase):
    def test_real_inflight_import_is_not_an_old_api_and_does_not_block_gui(self):
        name=inspector.__package__+'._test_inflight_provider'
        started,release=threading.Event(),threading.Event()
        errors=[]
        class Loader(importlib.abc.Loader):
            def create_module(self,spec):return None
            def exec_module(self,module):
                started.set()
                if not release.wait(2):raise RuntimeError('fixture release timed out')
                module.factory=lambda:None
        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self,fullname,path=None,target=None):
                return importlib.util.spec_from_loader(name,Loader()) if fullname==name else None
        finder=Finder();sys.meta_path.insert(0,finder)
        def load():
            try:importlib.import_module(name)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=load)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(sys.modules[name].__spec__._initializing)
            with patch.dict(inspector.EDITOR_FACTORIES,{'synthetic':('._test_inflight_provider','factory')},clear=True):
                before=time.monotonic()
                inspector.require_editor_api(Panel)
                self.assertLess(time.monotonic()-before,.5,'Do not wait for unrelated background imports')
                release.set();thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                inspector.require_editor_api(Panel)
        finally:
            release.set();thread.join(2)
            sys.meta_path.remove(finder);sys.modules.pop(name,None)

    def test_completed_stale_provider_still_requires_restart(self):
        name=inspector.__package__+'._test_stale_provider'
        module=types.ModuleType(name)
        module.__spec__=importlib.util.spec_from_loader(name,loader=None)
        with patch.dict(sys.modules,{name:module}),patch.dict(inspector.EDITOR_FACTORIES,
                {'synthetic':('._test_stale_provider','factory')},clear=True):
            with self.assertRaisesRegex(RuntimeError,'重启'):inspector.require_editor_api(Panel)

    def test_old_panel_still_fails_and_unloaded_provider_is_not_imported(self):
        with patch.dict(inspector.EDITOR_FACTORIES,{'synthetic':('._not_loaded','factory')},clear=True), \
                patch.object(inspector.importlib,'import_module',side_effect=AssertionError('not needed')):
            inspector.require_editor_api(Panel)
            with self.assertRaisesRegex(RuntimeError,'重启'):inspector.require_editor_api(type('OldPanel',(),{}))

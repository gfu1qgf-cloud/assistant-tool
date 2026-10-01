from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from model.ConfigHotReload import ConfigHotReload, merge_runtime_changes


class MergeTests(unittest.TestCase):
    def test_stale_runtime_does_not_overwrite_external(self):
        base = {'plugin': {'value': 1}}
        external = {'plugin': {'value': 9}}
        merged, conflicts = merge_runtime_changes(base, base, base, external)
        self.assertEqual(merged, external)
        self.assertEqual(conflicts, [])

    def test_disjoint_nested_changes_merge(self):
        base = {'plugin': {'a': 1, 'b': 2}, 'old': 5}
        local = {'plugin': {'a': 10, 'b': 2}, 'old': 5}
        external = {'plugin': {'a': 1, 'b': 20}, 'new': 7}
        merged, conflicts = merge_runtime_changes(base, local, base, external)
        self.assertEqual(merged, {'plugin': {'a': 10, 'b': 20}, 'new': 7})
        self.assertEqual(conflicts, [])

    def test_same_leaf_conflict_preserves_external(self):
        base, local, external = {'a': 1}, {'a': 2}, {'a': 3}
        merged, conflicts = merge_runtime_changes(base, local, base, external)
        self.assertEqual(merged, external)
        self.assertEqual(conflicts, ['a'])

    def test_missing_default_can_be_changed(self):
        memory = {'plugin': {'enabled': True, 'minutes': 15}}
        local = {'plugin': {'enabled': True, 'minutes': 20}}
        merged, conflicts = merge_runtime_changes(memory, local, {}, {})
        self.assertEqual(merged, {'plugin': {'minutes': 20}})
        self.assertEqual(conflicts, [])

    def test_external_delete_not_reintroduced(self):
        before = {'a': 1, 'b': 2}
        self.assertEqual(merge_runtime_changes(before, before, before, {'a': 1})[0], {'a': 1})


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'config.json'
        self.path.write_text('{"safe": 1, "model": "large"}', encoding='utf-8')
        self.runtime = {'safe': 1, 'model': 'large'}
        self.log = Mock()
        self.applied = []

        def apply(old, new):
            self.applied.append(new)
            self.runtime['safe'] = new['safe']

        self.monitor = ConfigHotReload(self.path, lambda: deepcopy(self.runtime), apply, self.log)
        self.monitor.timer.stop()

    def tearDown(self):
        self.monitor.deleteLater()
        self.temp.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding='utf-8')

    def test_debounce_and_no_repeated_application(self):
        self.write({'safe': 2, 'model': 'large'})
        self.monitor.poll()
        self.assertEqual(len(self.applied), 0)
        self.monitor.poll()
        self.assertEqual(len(self.applied), 1)
        self.monitor.poll()
        self.assertEqual(len(self.applied), 1)

    def test_deferred_model_preserved_when_saving(self):
        self.write({'safe': 2, 'model': 'new-model'})
        self.monitor.poll(force=True)
        # Runtime deliberately keeps old model, but a later save must not revert the file.
        merged, conflicts = self.monitor.merge_for_save(dict(self.runtime, safe=3))
        self.assertEqual(merged['model'], 'new-model')
        self.assertEqual(merged['safe'], 3)
        self.assertEqual(conflicts, [])

    def test_invalid_partial_json_never_applied(self):
        self.path.write_text('{', encoding='utf-8')
        self.monitor.poll(force=True)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.runtime['safe'], 1)
        self.assertIn('未应用', self.log.call_args[0][0])

    def test_internal_save_does_not_trigger_external_reload(self):
        self.write({'safe': 2, 'model': 'large'})
        self.runtime['safe'] = 2
        self.monitor.note_written()
        self.monitor.poll()
        self.assertEqual(self.applied, [])

    def test_disabled_watcher_still_has_manual_entry(self):
        self.write({'safe': 2, 'model': 'large', 'config_hot_reload': {'enabled': False}})
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.applied, [])
        self.monitor.poll(force=True)
        self.assertEqual(len(self.applied), 1)

    def test_busy_plugin_retries_after_finishing(self):
        busy = [True]

        def apply(old, new):
            if busy[0]:
                return {'safe'}
            self.runtime['safe'] = new['safe']
            return set()

        self.monitor.apply = apply
        self.write({'safe': 9, 'model': 'large'})
        self.monitor.poll(force=True)
        self.assertEqual(self.runtime['safe'], 1)
        busy[0] = False
        self.monitor.poll()
        self.assertEqual(self.runtime['safe'], 9)


if __name__ == '__main__':
    unittest.main()

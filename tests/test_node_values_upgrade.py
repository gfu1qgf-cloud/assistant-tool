import copy
import random
import unittest
from types import SimpleNamespace
from model.node_workflow.contracts import ColorValue
from model.node_workflow.value_nodes import fit, rgb, random_value
from model.node_workflow.code_node import DEFAULT_INPUTS, DEFAULT_OUTPUTS, run_code, signature, ports, clear_code_approvals


class ValueUpgradeTests(unittest.TestCase):
    def context(self, **kw):
        return SimpleNamespace(node_id='one', report=lambda *_: None, check_canceled=lambda: None, **kw)

    def test_fit_reverse_clamp_and_errors(self):
        params = dict(value=2, source_min=0, source_max=1, target_min=64, target_max=35, clamp='yes')
        self.assertEqual(fit({}, params, None)['number'], 35)
        self.assertEqual(fit({'value': .5}, params, None)['number'], 49.5)
        params['source_max'] = 0
        with self.assertRaises(ValueError): fit({}, params, None)
        params['source_max'] = 1
        with self.assertRaises(ValueError): fit({'value': True}, params, None)

    def test_rgb_units_alpha_and_range(self):
        params = dict(red=255, green=128, blue=0, alpha=128, unit='byte', range='error')
        color = rgb({}, params, None)['color']
        self.assertEqual(color.hex_rgb, '#ff8000'); self.assertAlmostEqual(color.alpha, 128/255)
        self.assertEqual(ColorValue.from_hex('#ff800080'), color)
        with self.assertRaises(ValueError): rgb({'red': 256}, params, None)
        params['range'] = 'clamp'
        self.assertEqual(rgb({'red': 256}, params, None)['color'].red, 1)

    def test_random_repeatable_task_scope_and_no_global_changes(self):
        params = dict(seed='3', scope='task', mode='float', minimum=0, maximum=1)
        context = self.context(task=SimpleNamespace(key='task-one', directory='one'))
        state = random.getstate()
        value = random_value({}, params, context)
        self.assertEqual(value, random_value({}, params, context)); self.assertEqual(state, random.getstate())
        context.task.key = 'task-two'; self.assertNotEqual(value, random_value({}, params, context))
        params.update(scope='fixed', mode='shuffle'); items = list(range(12))
        result = random_value({'items': items}, params, context)['value']
        self.assertEqual(sorted(result), items); self.assertEqual(items, list(range(12)))
        with self.assertRaises(ValueError): random_value({'items': []}, params, context)

    def params(self, code):
        values = dict(code=code, inputs=copy.deepcopy(DEFAULT_INPUTS), outputs=copy.deepcopy(DEFAULT_OUTPUTS), timeout=5)
        values['approved'] = signature(values)
        return values

    def test_code_isolated_defaults_logs_and_errors(self):
        params = self.params('print("hello"); outputs["value"] = inputs["value"] * 2')
        self.assertEqual(run_code({'in_value': 4}, params, self.context()), {'out_value': 8})
        params['code'] = 'outputs["value"] = "wrong"'; params['approved'] = signature(params)
        with self.assertRaisesRegex(TypeError, 'number'): run_code({'in_value': 1}, params, self.context())
        params['code'] = 'raise RuntimeError("expected")'; params['approved'] = signature(params)
        with self.assertRaisesRegex(RuntimeError, '节点代码.*line 1'): run_code({'in_value': 1}, params, self.context())

    def test_code_trust_dynamic_ports_and_timeout(self):
        params = self.params('while True: pass'); params['timeout'] = 1
        with self.assertRaises(TimeoutError): run_code({'in_value': 1}, params, self.context())
        original_id = ports(params, 'inputs')[0].key
        params['inputs'][0]['name'] = 'renamed'; params['inputs'][0]['title'] = '中文标签'
        self.assertEqual(ports(params, 'inputs')[0].key, original_id)
        with self.assertRaisesRegex(ValueError, '授权'): run_code({'in_value': 1}, params, self.context())
        graph = {'nodes': [{'kind': 'core.python_code', 'params': params}]}; clear_code_approvals(graph)
        self.assertEqual(params['approved'], '')

    def test_code_color_public_dto_and_live_object_rejected(self):
        params = self.params('outputs["value"] = ColorValue(1, 0, .5, .7)')
        params['outputs'][0]['type'] = 'color'; params['approved'] = signature(params)
        self.assertEqual(run_code({'in_value': 4}, params, self.context())['out_value'], ColorValue(1, 0, .5, .7))
        params['code'] = 'outputs["value"] = lambda: 3'; params['approved'] = signature(params)
        with self.assertRaisesRegex(RuntimeError, '函数|function'): run_code({'in_value': 4}, params, self.context())

    def test_code_does_not_inherit_host_secret_environment(self):
        from unittest.mock import patch
        params = self.params('import os; outputs["value"] = int("GEMINI_API_KEY" in os.environ or "MISTRAL_KEY" in os.environ)')
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'synthetic-only', 'MISTRAL_KEY': 'synthetic-only'}):
            self.assertEqual(run_code({'in_value': 1}, params, self.context())['out_value'], 0)

    def test_code_inherits_best_effort_offline_policy(self):
        from unittest.mock import patch
        params = self.params('import socket\noutputs["value"] = len(socket.getaddrinfo("example.com", 443))')
        with patch('model.OfflineMode.is_offline', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '离线'):
                run_code({'in_value': 1}, params, self.context())

    @unittest.skipUnless(__import__('os').name == 'nt', 'windowed stdio uses Windows inherited handles')
    def test_worker_protocol_when_sys_standard_streams_are_none(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        from model.node_workflow.code_node import encode, decode
        import inspect
        from model.node_workflow import code_worker
        # The protocol is a separate -I process; locate its actual source,
        # rather than assuming a partial test overlay is a complete checkout.
        project = Path(inspect.getfile(code_worker)).resolve().parents[2]
        script = f'import sys; sys.path.insert(0,{str(project)!r}); sys.stdin=sys.stdout=sys.stderr=None; from model.node_workflow.code_worker import main; main()'
        request = json.dumps({'code': 'outputs["value"] = len(inputs["value"])', 'inputs': encode({'value': 'a'*200000})}).encode('utf8')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script], input=request, capture_output=True, timeout=10, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decode(json.loads(result.stdout.decode('utf8'))['outputs']), {'value': 200000})

    def test_code_concurrent_workers_do_not_share_globals(self):
        from concurrent.futures import ThreadPoolExecutor
        params = self.params('counter = inputs["value"]; outputs["value"] = counter * 7')
        with ThreadPoolExecutor(3) as pool:
            values = list(pool.map(lambda value: run_code({'in_value': value}, params, self.context())['out_value'], (2, 4, 8)))
        self.assertEqual(values, [14, 28, 56])

    def test_random_in_scoped_loop_changes_only_by_connected_iteration(self):
        from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, evaluate_graph
        from model.node_workflow.loops import add_loop_pair
        registry = NodeRegistry(); register_core(registry); graph = empty_graph()
        begin, end = add_loop_pair(graph, registry)
        end['params']['count'] = 4
        random_node = new_node(registry.definitions['core.random']); random_node['params'].update(mode='float', scope='fixed')
        graph['nodes'].append(random_node)
        graph['connections'] += [[begin['id'], 'iteration', random_node['id'], 'iteration'], [random_node['id'], 'value', end['id'], 'result']]
        context = self.context(); context.prepared = {}
        first = evaluate_graph(graph, registry, context, root_id=end['id'])['results']
        self.assertEqual(first, evaluate_graph(graph, registry, context, root_id=end['id'])['results'])
        self.assertEqual(len(set(first.items)), 4)

    def test_code_cancellation_reaps_worker(self):
        import time
        params = self.params('while True: pass')
        start = time.monotonic()
        def check():
            if time.monotonic()-start > .35: raise InterruptedError('test canceled')
        context = self.context(); context.check_canceled = check
        with self.assertRaisesRegex(InterruptedError, 'canceled'):
            run_code({'in_value': 1}, params, context)

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows Job lifecycle')
    def test_code_child_process_is_reaped_on_success(self):
        import ctypes
        from ctypes import wintypes
        params = self.params('import subprocess, sys\nchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)\noutputs["value"] = child.pid')
        pid = run_code({'in_value': 1}, params, self.context())['out_value']
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            exit_code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)); kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle(handle)
            self.assertNotEqual(exit_code.value, 259)

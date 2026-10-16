"""Network guard tests run in children; irreversible hooks must not leak."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from model import OfflineMode


ROOT = Path(__file__).resolve().parents[1]


class OfflineModeTests(unittest.TestCase):
    def child(self, script):
        env = os.environ.copy()
        env.pop(OfflineMode.OFFLINE_ENV_KEY, None)
        env.update(QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_normal_mode_does_not_patch_network_or_environment(self):
        self.child('''
            import socket, os
            from model.OfflineMode import configure_offline, is_offline
            from pathlib import Path
            import tempfile
            original = socket.socket.connect
            environment = dict(os.environ)
            with tempfile.TemporaryDirectory() as folder:
                assert not configure_offline(Path(folder)/'absent.json', [], {})
            assert not is_offline()
            assert socket.socket.connect is original
            assert dict(os.environ) == environment
        ''')

    def test_config_is_read_only_and_default_is_online(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (False, True, "false", "true"):
                path.write_text(json.dumps({"offline_mode": value, "gemini_api_keys": ["fixture"]}), encoding="utf-8")
                before = path.read_bytes()
                with mock.patch.object(OfflineMode, "enable_offline") as install:
                    OfflineMode.configure_offline(path, [], {})
                self.assertEqual(install.called, OfflineMode.offline_setting(value))
                self.assertEqual(path.read_bytes(), before)

    def test_flag_and_inherited_child_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for args, env in ((["main.py", "--offline"], {}), ([], {"LZX_OFFLINE_MODE": "1"})):
                with mock.patch.object(OfflineMode, "enable_offline") as install:
                    OfflineMode.configure_offline(path, args, env)
                install.assert_called_once_with()
            self.assertFalse(path.exists())

    def test_tcp_ipv6_udp_dns_and_saved_native_reference_are_blocked(self):
        self.child('''
            import socket, _socket
            from model.OfflineMode import enable_offline, OfflineModeError
            saved_connect = _socket.socket.connect
            saved_dns = socket.getaddrinfo
            enable_offline()
            enable_offline()
            def blocked(fn):
                try: fn()
                except OfflineModeError: return
                raise AssertionError('network operation was not blocked')
            with socket.socket() as sock:
                blocked(lambda: sock.connect(('127.0.0.1', 9)))
                blocked(lambda: sock.connect_ex(('192.0.2.1', 443)))
                blocked(lambda: sock.bind(('0.0.0.0', 0)))
                blocked(lambda: sock.send(b'test'))
            raw = _socket.socket()
            try:
                blocked(lambda: saved_connect(raw, ('127.0.0.1', 9)))
            finally:
                raw.close()
            with socket.socket(socket.AF_INET6) as sock:
                blocked(lambda: sock.connect(('::1', 9)))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                blocked(lambda: sock.sendto(b'test', ('192.0.2.1', 9)))
            blocked(lambda: socket.getaddrinfo('example.invalid', 443))
            blocked(lambda: saved_dns('example.invalid', 443))
            blocked(lambda: socket.gethostbyname('example.invalid'))
        ''')

    def test_http_libraries_and_https_fail_without_connection(self):
        self.child('''
            import http.client, urllib.request, requests, httpx, socket, time
            import _socket
            from unittest import mock
            from model.OfflineMode import enable_offline
            enable_offline()
            requests_session = requests.Session()
            requests_session.trust_env = False
            operations = [
                lambda: http.client.HTTPConnection('127.0.0.1', 9, timeout=1).connect(),
                lambda: urllib.request.urlopen('http://127.0.0.1:9', timeout=1),
                lambda: requests_session.get('https://127.0.0.1:9', timeout=1),
                lambda: httpx.get('http://127.0.0.1:9', timeout=1, trust_env=False),
            ]
            start = time.monotonic()
            for operation in operations:
                try: operation()
                except Exception as error:
                    assert '离线' in str(error), str(error)
                else: raise AssertionError('HTTP request was not blocked')
            assert time.monotonic() - start < 5
        ''')

    def test_asyncio_socketpair_and_local_file_operations_still_work(self):
        self.child('''
            import asyncio, socket, tempfile
            from pathlib import Path
            from model.OfflineMode import enable_offline
            enable_offline()
            left, right = socket.socketpair()
            left.sendall(b'local')
            assert right.recv(5) == b'local'
            left.close(); right.close()
            async def local_work():
                await asyncio.sleep(.01)
                return await asyncio.to_thread(lambda: 42)
            assert asyncio.run(local_work()) == 42
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'data.txt'
                path.write_text('local', encoding='utf-8')
                assert path.read_text(encoding='utf-8') == 'local'
        ''')

    def test_child_environment_and_local_encoder_execution(self):
        self.child('''
            import subprocess, sys, json, os
            from model.OfflineMode import enable_offline, OfflineModeError
            enable_offline()
            output = subprocess.check_output([sys.executable, '-c',
                "import os; print(os.environ['LZX_OFFLINE_MODE'])"], text=True)
            assert output.strip() == '1'
            for args in (['curl', 'https://example.invalid'], ['chrome.exe'],
                         ['ffmpeg', '-i', 'https://example.invalid/a.mp4'],
                         ['powershell.exe', '-Command', 'exit']):
                try: subprocess.run(args)
                except OfflineModeError: pass
                else: raise AssertionError('network subprocess was not blocked')
            try: os.system('echo test')
            except OfflineModeError: pass
            else: raise AssertionError('shell command was not blocked')
        ''')

    def test_cached_models_only_and_redacted_rate_limited_logging(self):
        result = self.child('''
            import os, socket, logging
            from model.OfflineMode import enable_offline, OfflineModeError
            enable_offline()
            for name in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_DATASETS_OFFLINE',
                         'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK'):
                assert os.environ[name] == '1'
            for _ in range(5):
                try: socket.create_connection(('secret-customer.invalid', 443))
                except OfflineModeError: pass
        ''')
        self.assertNotIn("secret-customer", result.stderr)
        self.assertEqual(result.stderr.count("已拦截"), 1)

    def test_cloud_api_preflight_keeps_credentials_and_existing_audio(self):
        self.child('''
            from model.OfflineMode import enable_offline, OfflineModeError
            from model.GoogleDriveHelper import load_drive_service
            from model.GoogleSheetsHelper import load_sheets_service
            from model.GeminiKeyManager import GeminiKeyManager
            from model.AudioHelper import _save_audio_with_api_keys, CreateTTSAudio
            from pathlib import Path
            import tempfile
            manager = GeminiKeyManager({'gemini_api_keys': ['fixture-key']})
            before = manager.snapshot()
            enable_offline()
            assert manager.request_keys('fixture-model') == []
            assert '离线' in manager.unavailable_message()
            assert manager.snapshot() == before
            with tempfile.TemporaryDirectory() as folder:
                audio = Path(folder)/'audio.mp3'
                audio.write_bytes(b'existing')
                for call in (load_drive_service, lambda: load_sheets_service({}),
                    lambda: _save_audio_with_api_keys(['fixture'], str(audio), lambda _: (_ for _ in ()).throw(AssertionError())),
                    lambda: CreateTTSAudio('text', '男声', str(audio))):
                    try: call()
                    except OfflineModeError: pass
                    else: raise AssertionError('cloud API did not stop at preflight')
                assert audio.read_bytes() == b'existing'
                assert not list(Path(folder).glob('*.part'))
        ''')

    def test_qt_network_and_remote_urls_blocked_but_pipe_ipc_is_preserved(self):
        self.child('''
            from qt_compat import QtCore, QtGui, QtNetwork, QtWidgets
            from model.OfflineMode import enable_offline, install_qt_network_guard, OfflineModeError
            from unittest import mock
            app = QtWidgets.QApplication([])
            pipe_method = QtNetwork.QLocalSocket.__dict__.get('connectToServer')
            with mock.patch.object(QtGui.QDesktopServices, 'openUrl', return_value=True) as original:
                enable_offline()
                install_qt_network_guard(QtCore, QtGui, QtNetwork)
                install_qt_network_guard(QtCore, QtGui, QtNetwork)
                assert QtNetwork.QLocalSocket.__dict__.get('connectToServer') is pipe_method
                assert not QtGui.QDesktopServices.openUrl(QtCore.QUrl('https://example.invalid'))
                assert QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile('C:/local.txt'))
                assert original.call_count == 1
                import os
                pipe = QtNetwork.QLocalServer()
                name = 'lzx-offline-test-' + str(os.getpid())
                assert pipe.listen(name)
                client = QtNetwork.QLocalSocket()
                client.connectToServer(name)
                assert client.waitForConnected(1000)
                app.processEvents()
                assert pipe.hasPendingConnections() or pipe.waitForNewConnection(1000)
                peer = pipe.nextPendingConnection()
                client.write(b'local')
                client.flush()
                app.processEvents()
                assert peer.bytesAvailable() or peer.waitForReadyRead(1000)
                assert bytes(peer.readAll()) == b'local'
                client.abort(); peer.abort(); pipe.close()
                for call in (
                    lambda: QtNetwork.QNetworkAccessManager().get(QtNetwork.QNetworkRequest(QtCore.QUrl('https://example.invalid'))),
                    lambda: QtNetwork.QTcpSocket().connectToHost('127.0.0.1', 9),
                    lambda: QtNetwork.QHostInfo.fromName('example.invalid'),
                ):
                    try: call()
                    except OfflineModeError: pass
                    else: raise AssertionError('Qt request was not blocked')
        ''')

    def test_real_local_ffmpeg_and_ffprobe_still_work(self):
        self.child('''
            import subprocess, tempfile, json
            from pathlib import Path
            from model.OfflineMode import enable_offline
            root = Path.cwd()
            ffmpeg = root/'runtime'/'ffmpeg'/'ffmpeg.exe'
            ffprobe = root/'runtime'/'ffmpeg'/'ffprobe.exe'
            assert ffmpeg.is_file() and ffprobe.is_file(), 'bundled media runtime missing'
            enable_offline()
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder)/'local.mp4'
                subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'color=c=black:s=64x96:d=0.2',
                    '-c:v', 'libx264', '-y', str(output)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                result = subprocess.check_output([str(ffprobe), '-v', 'error',
                    '-show_entries', 'stream=width,height', '-of', 'json', str(output)])
                stream = json.loads(result)['streams'][0]
                assert (stream['width'], stream['height']) == (64, 96)
        ''')

    def test_settings_entry_and_save_keep_existing_keys_and_business_fields(self):
        self.child('''
            import tempfile, os, json, contextlib
            from pathlib import Path
            from qt_compat import QtWidgets, QtCore
            from PYUI.main_setting_pyui import MainSettingDialog
            app = QtWidgets.QApplication([])
            @contextlib.contextmanager
            def working_directory(folder):
                previous = Path.cwd()
                os.chdir(folder)
                try: yield
                finally: os.chdir(previous)
            with tempfile.TemporaryDirectory() as folder, working_directory(folder):
                path = Path('config.json')
                original = {'offline_mode': False, 'gemini_api_keys': ['fixture-gemini'],
                    'elevenlabs_api_keys': ['fixture-eleven'],
                    'daily_link_history': {'fixture': 'keep'}, 'custom_business_field': [1, 2, 3]}
                path.write_text(json.dumps(original), encoding='utf-8')
                dialog = MainSettingDialog()
                assert dialog.settingTabWidget.indexOf(dialog.network_tab) >= 0
                assert not dialog.offline_mode_checkbox.isChecked()
                dialog.offline_mode_checkbox.setChecked(True)
                assert dialog.get_config()['offline_mode']
                assert dialog.save_config()
                saved = json.loads(path.read_text(encoding='utf-8'))
                assert saved['offline_mode']
                for key in ('gemini_api_keys', 'elevenlabs_api_keys', 'daily_link_history', 'custom_business_field'):
                    assert saved[key] == original[key], key
                dialog.close(); dialog.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
        ''')

    def test_startup_installs_guard_before_sdk_or_worker_import(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertLess(source.index('configure_offline(APP_ROOT'), source.index('from globalValue'))
        self.assertLess(source.index('configure_offline(APP_ROOT'), source.index('from davinci_remote_worker'))
        self.assertLess(source.index('install_qt_network_guard(QtCore'), source.index('from PYUI.main_pyui'))

    def test_whisper_uses_local_files_only_in_offline_mode(self):
        from globalValue import GlobalValue
        with mock.patch('globalValue.is_offline', return_value=True), \
             mock.patch('globalValue.WhisperModel') as loader:
            value = GlobalValue()
            value.get_whisper_model('small')
        self.assertTrue(loader.call_args.kwargs['local_files_only'])

    def test_monitors_pause_without_mutating_enabled_settings(self):
        from PYUI.main_pyui import MainDialog
        from app_plugins.builtin.inventory import InventoryPlugin
        from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
        from app_plugins.builtin.waste_reminder.plugin import WasteReminderPlugin
        window = mock.Mock()
        window.google_sheet_monitor_settings = {'enabled': True}
        window.flow_guard_settings = {'enabled': True}
        with mock.patch('PYUI.main_pyui.is_offline', return_value=True):
            MainDialog.startGoogleSheetMonitor(window)
            MainDialog.startFlowParameterGuard(window)
            MainDialog.startTaskReferenceDownloads(window, 'local')
        self.assertEqual(window.google_sheet_monitor_status, '离线暂停')
        self.assertTrue(window.google_sheet_monitor_settings['enabled'])
        self.assertTrue(window.flow_guard_settings['enabled'])
        inventory = mock.Mock()
        inventory.material_sync_settings = {'enabled': True, 'interval_minutes': 5}
        with mock.patch('app_plugins.builtin.inventory.is_offline', return_value=True):
            InventoryPlugin._configure_material_sync_timer(inventory)
            self.assertFalse(InventoryPlugin.trigger_material_sync(inventory))
        inventory.material_sync_timer.start.assert_not_called()
        self.assertTrue(inventory.material_sync_settings['enabled'])
        controller = mock.Mock()
        controller.review_status_settings = {'review_status_monitor_enabled': True}
        with mock.patch('model.OfflineMode.is_offline', return_value=True):
            TaskDeliveryController.start_review_status_monitor(controller, force_once=True)
        self.assertEqual(controller.review_status_state, '离线暂停')
        self.assertTrue(controller.review_status_settings['review_status_monitor_enabled'])
        waste = mock.Mock()
        with mock.patch('app_plugins.builtin.waste_reminder.plugin.is_offline', return_value=True):
            WasteReminderPlugin.check_official(waste, manual=True)
        waste.is_updating.assert_not_called()

    def test_changing_only_offline_setting_does_not_restart_workers_or_models(self):
        from PYUI.main_pyui import MainDialog
        from PYUI.main_setting_pyui import MainSettingDialog
        window = mock.Mock()
        window.load_config.side_effect = [{'offline_mode': False}, {'offline_mode': True}]
        with mock.patch.object(MainSettingDialog, 'get_settings', return_value={'offline_mode': True}):
            MainDialog.openSettings(window)
        window.plugin_host.apply_settings.assert_not_called()
        window.restartFlowParameterGuard.assert_not_called()
        window.updateOfflineModeIndicator.assert_called_once_with({'offline_mode': True})


if __name__ == '__main__':
    unittest.main()

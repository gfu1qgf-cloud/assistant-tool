"""Public RFC vectors and synthetic seeds only; never read real credentials."""
import ast
import base64
import builtins
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from qt_compat import QtCore,QtGui,QtWidgets,QtTest
from app_plugins.builtin.temporary_totp import core
from app_plugins.builtin.temporary_totp.clipboard import OtpClipboard,TOKEN_MIME
from app_plugins.builtin.temporary_totp.ui import TemporaryTotpDialog
from app_plugins.builtin.temporary_totp.plugin import TemporaryTotpPlugin
from model.ClipboardHelper import INTERNAL_CLIPBOARD_MIME

PUBLIC_SECRET=base64.b32encode(b'12345678901234567890').decode('ascii')


class TotpCoreTests(unittest.TestCase):
    def test_all_eighteen_rfc6238_vectors(self):
        # RFC 6238 Appendix B, with algorithm-appropriate 20/32/64-byte seeds.
        vectors=[(59,('94287082','46119246','90693936')),
            (1111111109,('07081804','68084774','25091201')),
            (1111111111,('14050471','67062674','99943326')),
            (1234567890,('89005924','91819424','93441116')),
            (2000000000,('69279037','90698825','38618901')),
            (20000000000,('65353130','77737706','47863826'))]
        for now,expected in vectors:
            for index,(algorithm,length) in enumerate((('SHA1',20),('SHA256',32),('SHA512',64))):
                with self.subTest(now=now,algorithm=algorithm):
                    seed=(b'1234567890'*7)[:length]
                    settings=core.TotpSettings(bytearray(seed),algorithm,8,30)
                    self.assertEqual(core.code_at(settings,now)[0],expected[index]);settings.wipe()

    def test_plain_base32_case_spaces_padding_and_default_parameters(self):
        settings=core.parse_input(' \n'+PUBLIC_SECRET.lower()[:12]+' '+PUBLIC_SECRET.lower()[12:]+'\t')
        self.assertEqual(bytes(settings.secret),b'12345678901234567890')
        self.assertEqual((settings.algorithm,settings.digits,settings.period),('SHA1',6,30))
        self.assertEqual(core.code_at(settings,59)[0],'287082');settings.wipe()
        self.assertEqual(core.decode_secret('MY======'),bytearray(b'f'))

    def test_uri_parameters_are_read_without_network_or_rich_label_interpretation(self):
        uri='otpauth://totp/%3Cb%3EPublic%3C%2Fb%3E?secret='+PUBLIC_SECRET+'&algorithm=SHA-256&digits=8&period=60&issuer=Example&image=https%3A%2F%2Fexample.invalid%2Fnever-fetch'
        settings=core.parse_input(uri)
        self.assertEqual((settings.algorithm,settings.digits,settings.period),('SHA256',8,60))
        self.assertEqual(settings.label,'<b>Public</b>');settings.wipe()

    def test_uri_defaults_do_not_silently_inherit_old_custom_settings(self):
        settings=core.parse_input('otpauth://totp/Example?secret='+PUBLIC_SECRET,algorithm='SHA512',digits=8,period=60)
        self.assertEqual((settings.algorithm,settings.digits,settings.period),('SHA1',6,30));settings.wipe()

    def test_rollover_and_remaining_are_exact_at_boundaries(self):
        settings=core.parse_input(PUBLIC_SECRET)
        before=core.code_at(settings,59.999);after=core.code_at(settings,60)
        self.assertNotEqual(before[0],after[0]);self.assertEqual(after[1],30)
        self.assertLess(before[1],.002);self.assertEqual((before[2],after[2]),(1,2));settings.wipe()

    def test_invalid_inputs_are_rejected_without_echoing_them(self):
        bad=['private-sentinel-not-base32','otpauth://hotp/Private?secret='+PUBLIC_SECRET,
            'otpauth://totp/Private?secret='+PUBLIC_SECRET+'&secret='+PUBLIC_SECRET,
            'otpauth://totp/Private?secret='+PUBLIC_SECRET+'&algorithm=MD5',
            'otpauth://totp/Private?secret='+PUBLIC_SECRET+'&digits=5',
            'otpauth://totp/Private?secret='+PUBLIC_SECRET+'&period=0',
            'otpauth://totp/Private?secret='+PUBLIC_SECRET+'&encoder=steam',
            'https://example.invalid/?secret='+PUBLIC_SECRET,'MZ','MY=', '0O1I','A'*8192]
        for value in bad:
            with self.subTest(kind=value[:10]),self.assertRaises(core.TotpError) as caught:core.parse_input(value)
            self.assertNotIn(PUBLIC_SECRET,str(caught.exception));self.assertNotIn('private-sentinel',str(caught.exception))

    def test_invalid_clock_and_parameters_never_produce_a_code(self):
        settings=core.parse_input(PUBLIC_SECRET)
        for value in (-1,float('nan'),float('inf'),True,2**100):
            with self.assertRaises(core.TotpError):core.code_at(settings,value)
        for kwargs in ({'digits':True},{'digits':5},{'period':0},{'period':3601},{'algorithm':'MD5'}):
            with self.assertRaises(core.TotpError):core.parse_input(PUBLIC_SECRET,**kwargs)
        settings.wipe()
        with self.assertRaises(core.TotpError):core.code_at(settings,59)

    def test_session_clear_overwrites_retained_buffer_and_repr_never_contains_seed(self):
        session=core.TotpSession();session.settings=core.parse_input(PUBLIC_SECRET)
        buffer=session.settings.secret
        self.assertNotIn(PUBLIC_SECRET,repr(session.settings));self.assertNotIn('1234567890',repr(session.settings))
        session.clear();self.assertIsNone(session.settings);self.assertTrue(buffer);self.assertTrue(all(value==0 for value in buffer))


class FakeClipboard(QtCore.QObject):
    dataChanged=QtCore.pyqtSignal()
    def __init__(self):super().__init__();self.mime=None;self.clears=0
    def setMimeData(self,mime):self.mime=mime;self.dataChanged.emit()
    def mimeData(self):return self.mime
    def text(self):return self.mime.text() if self.mime else ''
    def setText(self,text):
        mime=QtCore.QMimeData();mime.setText(text);self.setMimeData(mime)
    def clear(self):self.clears+=1;self.mime=None;self.dataChanged.emit()


class TotpUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.clip=FakeClipboard();self.guard=OtpClipboard(clipboard=self.clip);self.dialog=TemporaryTotpDialog(self.guard)
    def tearDown(self):
        try:self.dialog.close();self.dialog.deleteLater()
        except RuntimeError:pass
        self.guard.clear_owned();self.guard.deleteLater();self.clip.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete);self.app.processEvents()
    def generate(self):
        self.dialog.input.setText(PUBLIC_SECRET)
        with patch('app_plugins.builtin.temporary_totp.ui.code_at',side_effect=lambda settings:core.code_at(settings,59)):
            self.dialog.generate()

    def test_input_hidden_then_cleared_on_load_and_enter_generates_once(self):
        self.assertEqual(self.dialog.input.echoMode(),QtWidgets.QLineEdit.EchoMode.Password)
        self.dialog.input.setText(PUBLIC_SECRET)
        with patch('app_plugins.builtin.temporary_totp.ui.code_at',side_effect=lambda settings:core.code_at(settings,59)):
            QtTest.QTest.keyClick(self.dialog.input,QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.dialog.input.text(),'');self.assertIsNotNone(self.dialog.settings)
        self.assertFalse(self.dialog.input.isUndoAvailable());self.assertEqual(self.dialog.code.text(),'287 082')

    def test_close_esc_clear_and_direct_destruction_wipe_seed(self):
        for action in ('close','reject','clear_session'):
            self.generate();buffer=self.dialog.settings.secret
            getattr(self.dialog,action)()
            self.assertIsNone(self.dialog.settings);self.assertTrue(all(value==0 for value in buffer))
            self.assertFalse(self.dialog.timer.isActive());self.assertEqual(self.dialog.input.text(),'')
        self.generate();buffer=self.dialog.settings.secret;session=self.dialog.session
        self.dialog.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.assertIsNone(session.settings);self.assertTrue(all(value==0 for value in buffer))

    def test_new_invalid_input_does_not_leave_old_code_active(self):
        self.generate();buffer=self.dialog.settings.secret
        self.dialog.input.setText('bad sentinel!');self.dialog.generate()
        self.assertIsNone(self.dialog.settings);self.assertFalse(self.dialog.code.isEnabled())
        self.assertTrue(all(value==0 for value in buffer));self.assertNotIn('sentinel',self.dialog.status.text())

    def test_paste_generate_is_explicit_and_does_not_erase_external_clipboard(self):
        self.clip.setText(PUBLIC_SECRET)
        self.assertIsNone(self.dialog.settings)
        self.dialog.paste_button.click();self.assertIsNotNone(self.dialog.settings)
        self.assertEqual(self.clip.text(),PUBLIC_SECRET)

    def test_copy_recalculates_at_click_and_excludes_app_history_and_windows_cloud(self):
        self.generate()
        with patch('app_plugins.builtin.temporary_totp.ui.code_at',side_effect=lambda settings:core.code_at(settings,60)):
            self.dialog.copy_code()
        expected=core.code_at(self.dialog.settings,60)[0]
        self.assertEqual(self.clip.text(),expected);self.assertNotEqual(expected,'287082')
        self.assertTrue(self.clip.mimeData().hasFormat(INTERNAL_CLIPBOARD_MIME))
        self.assertTrue(self.clip.mimeData().hasFormat(TOKEN_MIME))
        for name in ('CanIncludeInClipboardHistory','CanUploadToCloudClipboard'):
            self.assertEqual(bytes(self.clip.mimeData().data('application/x-qt-windows-mime;value="'+name+'"')),b'\0'*4)
        self.assertLessEqual(self.guard.timer.interval(),20000)

    def test_expiry_clears_only_our_own_still_matching_content(self):
        self.guard.copy_code('123456',30);self.guard.timer.timeout.emit()
        self.assertEqual(self.clip.text(),'')
        self.guard.copy_code('123456',30);self.clip.setText('user copied something else')
        self.guard.clear_owned();self.assertEqual(self.clip.text(),'user copied something else')
        self.guard.copy_code('123456',30);self.clip.setText('123456')
        self.guard.clear_owned();self.assertEqual(self.clip.text(),'123456','Same digits without our token are not ours to erase')

    def test_close_keeps_copied_code_temporarily_for_paste_then_timer_clears(self):
        self.guard.copy_code('123456',30);self.generate();self.dialog.close()
        self.assertEqual(self.clip.text(),'123456');self.assertTrue(self.guard.timer.isActive())
        self.guard.timer.timeout.emit();self.assertEqual(self.clip.text(),'')

    def test_custom_uri_settings_are_visible_and_fully_editable(self):
        self.dialog.input.setText('otpauth://totp/%3Cb%3EPublic%3C%2Fb%3E?secret='+PUBLIC_SECRET+'&algorithm=SHA512&digits=8&period=60')
        self.dialog.generate();self.assertEqual(self.dialog.settings.algorithm,'SHA512')
        self.assertEqual(self.dialog.label.textFormat(),QtCore.Qt.TextFormat.PlainText)
        self.assertEqual(self.dialog.label.text(),'<b>Public</b>')
        self.dialog.algorithm.setCurrentIndex(0);self.dialog.digits.setCurrentIndex(0);self.dialog.period.setValue(30)
        self.assertEqual((self.dialog.settings.algorithm,self.dialog.settings.digits,self.dialog.settings.period),('SHA1',6,30))
        self.assertTrue(self.dialog.algorithm.isEnabled())

    def test_generation_and_copy_never_use_storage_logging_or_network(self):
        with patch.object(builtins,'open',side_effect=AssertionError('NO FILES')), \
             patch('model.AppDataStorage.data_file',side_effect=AssertionError('NO STORAGE')), \
             patch('logging.Logger._log',side_effect=AssertionError('NO LOGS')):
            self.generate()
            with patch('app_plugins.builtin.temporary_totp.ui.code_at',side_effect=lambda settings:core.code_at(settings,59)):
                self.dialog.copy_code()
            self.dialog.clear_session()

    def test_invalid_system_time_does_not_claim_a_loaded_session_or_leave_timer_running(self):
        self.dialog.input.setText(PUBLIC_SECRET)
        with patch('app_plugins.builtin.temporary_totp.ui.code_at',side_effect=core.TotpError('系统时间不可用')):self.dialog.generate()
        self.assertIsNone(self.dialog.settings);self.assertFalse(self.dialog.timer.isActive())
        self.assertIn('系统时间',self.dialog.status.text())


class TotpPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_registration_uses_no_settings_storage_workers_or_credentials_and_stop_clears(self):
        context=SimpleNamespace(parent_widget=None,register_command=Mock(),storage=Mock(),load_config=Mock(),log=Mock())
        plugin=TemporaryTotpPlugin();plugin.register(context);plugin.start()
        try:
            self.assertEqual(context.register_command.call_count,1)
            command=context.register_command.call_args.args[0];self.assertIn('tools_menu',command.locations)
            with patch.object(QtWidgets.QApplication,'clipboard',return_value=FakeClipboard()):dialog=plugin.open_dialog()
            dialog.input.setText(PUBLIC_SECRET);dialog.generate();buffer=dialog.settings.secret
            plugin.stop();self.assertTrue(all(value==0 for value in buffer));self.assertIsNone(plugin.dialog)
            context.storage.assert_not_called();context.load_config.assert_not_called();context.log.assert_not_called()
        finally:plugin.stop();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_builtin_and_main_window_install_are_wired(self):
        import app_plugins.builtin as builtin
        self.assertIs(builtin.TemporaryTotpPlugin,TemporaryTotpPlugin)
        import PYUI.main_pyui as main
        tree=ast.parse(Path(main.__file__).read_text(encoding='utf-8-sig'))
        installs=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
            and node.func.attr=='install' and node.args and isinstance(node.args[0],ast.Call)
            and isinstance(node.args[0].func,ast.Name) and node.args[0].func.id=='TemporaryTotpPlugin']
        self.assertEqual(len(installs),1)


if __name__=='__main__':unittest.main()

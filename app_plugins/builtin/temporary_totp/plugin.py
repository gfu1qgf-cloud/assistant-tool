from app_plugins.api import MAIN_MENU,TOOLS_MENU,PluginCommand


class TemporaryTotpPlugin:
    plugin_id='temporary_totp'
    display_name='临时TOTP验证码'
    version='1.0'
    required_api_version=1

    def __init__(self):self.context=None;self.dialog=None;self.clipboard_guard=None

    def register(self,context):
        self.context=context
        context.register_command(PluginCommand('open','临时TOTP验证码…',lambda _rows:self.open_dialog(),
            frozenset({MAIN_MENU,TOOLS_MENU}),order=90,tooltip='临时粘贴密钥生成动态验证码；不联网、不保存，关闭即清除会话'))

    def start(self):return None

    def open_dialog(self):
        from qt_compat import QtWidgets
        from .clipboard import OtpClipboard
        from .ui import TemporaryTotpDialog
        if self.clipboard_guard is None:
            self.clipboard_guard=OtpClipboard(QtWidgets.QApplication.instance())
            QtWidgets.QApplication.instance().aboutToQuit.connect(self.clipboard_guard.clear_owned)
        if self.dialog is None:
            self.dialog=TemporaryTotpDialog(self.clipboard_guard,self.context.parent_widget)
            QtWidgets.QApplication.instance().aboutToQuit.connect(self.dialog.clear_session)
        self.dialog.show();self.dialog.raise_();self.dialog.activateWindow();self.dialog.input.setFocus()
        return self.dialog

    def apply_settings(self,_config):return True
    def can_close(self):return True,''

    def stop(self):
        if self.dialog is not None:
            self.dialog.session.clear()
            try:self.dialog.clear_session();self.dialog.close();self.dialog.deleteLater()
            except RuntimeError:pass
            self.dialog=None
        if self.clipboard_guard is not None:
            try:
                self.clipboard_guard.clear_owned()
                self.clipboard_guard.clipboard.dataChanged.disconnect(self.clipboard_guard._changed)
                self.clipboard_guard.deleteLater()
            except (RuntimeError,TypeError):pass
            self.clipboard_guard=None
        return True

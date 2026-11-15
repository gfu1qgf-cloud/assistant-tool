"""Short-lived owned OTP clipboard. Never copies a seed or saves history."""
import math
import struct
from uuid import uuid4
from qt_compat import QtCore,QtWidgets
from model.ClipboardHelper import INTERNAL_CLIPBOARD_MIME

TOKEN_MIME='application/x-assistant-temporary-totp'


class OtpClipboard(QtCore.QObject):
    def __init__(self,parent=None,*,clipboard=None):
        super().__init__(parent)
        self.clipboard=clipboard or QtWidgets.QApplication.clipboard()
        self.token=b'';self.code='';self._setting=False
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.clear_owned)
        self.clipboard.dataChanged.connect(self._changed)

    def _matches(self):
        try:
            mime=self.clipboard.mimeData()
            return bool(self.token and mime and bytes(mime.data(TOKEN_MIME))==self.token and mime.text()==self.code)
        except RuntimeError:return False

    def _forget(self):self.timer.stop();self.token=b'';self.code=''

    def _changed(self):
        if not self._setting and not self._matches():self._forget()

    def copy_code(self,code,remaining):
        if not isinstance(code,str) or not code.isascii() or not code.isdigit() or len(code) not in (6,7,8):
            raise ValueError('只能复制数字验证码。')
        if not math.isfinite(remaining) or remaining<=0:raise ValueError('验证码已经到期，请重新复制。')
        self.clear_owned();self.token=uuid4().hex.encode('ascii');self.code=code
        mime=QtCore.QMimeData();mime.setText(code)
        mime.setData(INTERNAL_CLIPBOARD_MIME,b'1');mime.setData(TOKEN_MIME,self.token)
        # Windows' documented clipboard history/cloud opt-out formats, exposed
        # through Qt's native-format MIME mapping. Third-party capture is outside
        # this plugin's control; original seed clipboard history is not erased.
        for name in ('CanIncludeInClipboardHistory','CanUploadToCloudClipboard'):
            mime.setData('application/x-qt-windows-mime;value="'+name+'"',struct.pack('<I',0))
        self._setting=True
        try:self.clipboard.setMimeData(mime)
        except Exception:self._forget();raise
        finally:self._setting=False
        lifetime=max(1,math.floor(min(20,remaining)*1000));self.timer.start(lifetime)
        return lifetime

    def clear_owned(self):
        try:
            if self._matches():self.clipboard.clear()
        finally:self._forget()

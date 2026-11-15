"""A disposable in-memory TOTP session. No storage or background job payloads."""
import math
from qt_compat import QtCore,QtGui,QtWidgets
from .core import TotpError,TotpSession,parse_input,code_at


class TemporaryTotpDialog(QtWidgets.QDialog):
    def __init__(self,clipboard_guard,parent=None):
        super().__init__(parent);self.guard=clipboard_guard;self.session=TotpSession()
        self.destroyed.connect(self.session.clear)
        self.setWindowTitle('临时TOTP验证码');self.setMinimumWidth(490)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose,False)
        layout=QtWidgets.QVBoxLayout(self)
        note=QtWidgets.QLabel('粘贴Base32密钥或otpauth://totp/链接。只在当前会话生成，不联网、不保存。',self)
        note.setWordWrap(True);layout.addWidget(note)
        self.input=QtWidgets.QLineEdit(self);self.input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.input.setMaxLength(8192)  # Above parser bound: oversize input is rejected, never silently used.
        self.input.setPlaceholderText('密钥或otpauth链接（默认隐藏）')
        self.input.setInputMethodHints(QtCore.Qt.InputMethodHint.ImhHiddenText|QtCore.Qt.InputMethodHint.ImhSensitiveData|
                                      QtCore.Qt.InputMethodHint.ImhNoPredictiveText)
        self.input.returnPressed.connect(self.generate);self.input.textChanged.connect(self._input_changed)
        layout.addWidget(self.input)
        actions=QtWidgets.QHBoxLayout()
        self.paste_button=QtWidgets.QPushButton('粘贴并生成',self);self.paste_button.clicked.connect(self.paste_generate)
        self.generate_button=QtWidgets.QPushButton('生成',self);self.generate_button.clicked.connect(self.generate)
        self.clear_button=QtWidgets.QPushButton('清空',self);self.clear_button.clicked.connect(self.clear_session)
        for button in (self.paste_button,self.generate_button,self.clear_button):
            button.setAutoDefault(False);actions.addWidget(button)
        layout.addLayout(actions)
        self.label=QtWidgets.QLabel(self);self.label.setTextFormat(QtCore.Qt.TextFormat.PlainText);self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.code=QtWidgets.QPushButton('— — — — — —',self);self.code.setEnabled(False)
        self.code.setAutoDefault(False)
        font=QtGui.QFont('Consolas',30);font.setStyleHint(QtGui.QFont.StyleHint.Monospace);self.code.setFont(font)
        self.code.setMinimumHeight(85);self.code.setToolTip('点击复制验证码；复制时会重新计算，避免复制到过期画面。')
        self.code.clicked.connect(self.copy_code);layout.addWidget(self.code)
        self.countdown=QtWidgets.QProgressBar(self);self.countdown.setRange(0,30000);self.countdown.setValue(0)
        self.countdown.setFormat('等待密钥');layout.addWidget(self.countdown)
        self.mode_note=QtWidgets.QLabel(self);layout.addWidget(self.mode_note)
        self.advanced_toggle=QtWidgets.QToolButton(self);self.advanced_toggle.setText('高级设置')
        self.advanced_toggle.setCheckable(True);self.advanced_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon);layout.addWidget(self.advanced_toggle)
        self.advanced=QtWidgets.QWidget(self);form=QtWidgets.QFormLayout(self.advanced);form.setContentsMargins(0,0,0,0)
        self.algorithm=QtWidgets.QComboBox(self)
        for label,key in (('SHA-1','SHA1'),('SHA-256','SHA256'),('SHA-512','SHA512')):self.algorithm.addItem(label,key)
        self.digits=QtWidgets.QComboBox(self)
        for number in (6,7,8):self.digits.addItem(str(number)+'位',number)
        self.period=QtWidgets.QSpinBox(self);self.period.setRange(1,3600);self.period.setValue(30);self.period.setSuffix(' 秒')
        form.addRow('算法',self.algorithm);form.addRow('位数',self.digits);form.addRow('周期',self.period)
        layout.addWidget(self.advanced);self.advanced.hide();self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.algorithm.currentIndexChanged.connect(self._parameters_changed);self.digits.currentIndexChanged.connect(self._parameters_changed)
        self.period.valueChanged.connect(self._parameters_changed)
        self.status=QtWidgets.QLabel('点击大号验证码可复制。关闭窗口即清除当前会话。',self)
        self.status.setWordWrap(True);self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText);layout.addWidget(self.status)
        privacy=QtWidgets.QLabel('复制后约20秒或到期时清空；不会清除后来复制的其他内容。\n原密钥的系统剪贴板历史、输入法和系统内存转储不由本插件控制。验证码不匹配时请检查系统时间。',self)
        privacy.setWordWrap(True);layout.addWidget(privacy)
        self.timer=QtCore.QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self._tick)
        self._parameters_changed()

    def _toggle_advanced(self,visible):
        self.advanced.setVisible(visible)
        self.advanced_toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if visible else QtCore.Qt.ArrowType.RightArrow)
        self.adjustSize()

    @property
    def settings(self):return self.session.settings

    @settings.setter
    def settings(self,value):self.session.settings=value

    def _discard_key(self):
        self.timer.stop()
        self.session.clear()
        self.label.clear();self.code.setText('— — — — — —');self.code.setEnabled(False)
        self.countdown.setValue(0);self.countdown.setFormat('等待密钥')

    def _input_changed(self,_text):
        self._discard_key();self.status.setText('输入后点击生成；未保存输入。')

    def paste_generate(self):
        self.input.setText(self.guard.clipboard.text());self.generate()

    def generate(self):
        self._discard_key()
        try:
            settings=parse_input(self.input.text(),algorithm=self.algorithm.currentData(),digits=self.digits.currentData(),period=self.period.value())
        except TotpError as error:self.status.setText(str(error));return
        self.settings=settings
        blockers=[QtCore.QSignalBlocker(widget) for widget in (self.input,self.algorithm,self.digits,self.period)]
        self.input.setText('')  # setText also resets the line edit's undo/redo history.
        self.algorithm.setCurrentIndex(self.algorithm.findData(settings.algorithm));self.digits.setCurrentIndex(self.digits.findData(settings.digits))
        self.period.setValue(settings.period);del blockers
        self.label.setText(settings.label)
        self._parameters_changed()
        if self.settings is None:return
        self.timer.start()
        if self._tick() is None:return
        self.status.setText('临时密钥已载入，输入框已清空；点击验证码复制。关闭窗口即清除会话。')

    def _parameters_changed(self,*_args):
        self.mode_note.setText(f'{self.algorithm.currentText()} · {self.digits.currentText()} · {self.period.value()}秒（参数可在高级设置修改）')
        if self.settings is not None:
            self.settings.algorithm=self.algorithm.currentData();self.settings.digits=self.digits.currentData();self.settings.period=self.period.value()
            self._tick()

    def _tick(self):
        if self.settings is None:return None
        try:code,remaining,counter=code_at(self.settings)
        except TotpError as error:self._discard_key();self.status.setText(str(error));return None
        split=3 if len(code)==6 else 4
        self.code.setText(code[:split]+' '+code[split:]);self.code.setEnabled(True)
        self.countdown.setRange(0,self.settings.period*1000);self.countdown.setValue(max(1,math.ceil(remaining*1000)))
        self.countdown.setFormat(f'剩余 {math.ceil(remaining)} 秒')
        return code,remaining

    def copy_code(self):
        current=self._tick()
        if current is None:return
        try:ms=self.guard.copy_code(*current)
        except (ValueError,RuntimeError):self.status.setText('复制失败，请重试。');return
        self.status.setText(f'已复制验证码，约{math.ceil(ms/1000)}秒后仅清空本次复制内容。'+(' 即将到期，可等下一轮再复制。' if current[1]<3 else ''))

    def clear_session(self):
        self._discard_key();blockers=[QtCore.QSignalBlocker(widget) for widget in (self.input,self.algorithm,self.digits,self.period)]
        self.input.setText('');self.algorithm.setCurrentIndex(0);self.digits.setCurrentIndex(0);self.period.setValue(30);del blockers
        self._parameters_changed();self.status.setText('会话已清空，未保存密钥或验证码。')

    def reject(self):self.clear_session();super().reject()
    def closeEvent(self,event):self.clear_session();event.accept()

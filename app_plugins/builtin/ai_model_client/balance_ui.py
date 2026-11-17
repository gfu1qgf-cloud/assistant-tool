"""A transient, independent window so account lookups never block chatting."""
import functools
from datetime import datetime
from qt_compat import QtCore, QtGui, QtWidgets
from model.AIProviderKeys import read_provider_key, selected_credential, provider_key_identity
from model.OfflineMode import is_offline
from model.SensitiveData import redact_sensitive_text
from model.QtThreadLifecycle import connect_thread_finished
from .ui import ClientWorker, safe_ui
from .chat_style import apply_chat_style
from .account_balance import LINKS, dollars, query_openrouter, validate_token


class BalanceDialog(QtWidgets.QDialog):
    def __init__(self, context, client):
        # No QObject parent: a closing private chat may be destroyed before a
        # canceled HTTP read finishes. The root chat tracks this window instead.
        super().__init__()
        self.context,self.client=context,client
        self.handle=None;self.operation=None;self._closing=False;self._identity=None
        self.setObjectName('aiChatWindow');self.setWindowTitle('API 余额 / 用量')
        self.resize(790,790);self.setMinimumSize(590,460)
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(18,16,18,16)
        heading=self.label('API 余额 / 用量');heading.setObjectName('conversationTitle');layout.addWidget(heading)
        note=self.label('查询余额不会生成回答；仅手动刷新，不自动轮询。金额单位为美元，可能有更新延迟。')
        layout.addWidget(note)
        scroll=QtWidgets.QScrollArea(self);scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame);layout.addWidget(scroll,1)
        canvas=QtWidgets.QWidget(scroll);canvas.setObjectName('balanceCanvas');scroll.setWidget(canvas)
        body=QtWidgets.QVBoxLayout(canvas);body.setContentsMargins(0,0,8,0);body.setSpacing(16)
        openai=QtWidgets.QGroupBox('OpenAI · 赠送额度',canvas);form=QtWidgets.QVBoxLayout(openai)
        openai.setObjectName('balanceCard')
        self.openai_value=self.label('剩余赠送额度：请在官方账单查看')
        form.addWidget(self.openai_value)
        form.addWidget(self.label('当前没有已核实的公开接口可用普通聊天Key读取剩余赠金。点击下方按钮后，登录并选择该Key所属的组织，查看额度与到期时间。'))
        links=QtWidgets.QHBoxLayout();form.addLayout(links)
        self.openai_button=self.link_button('查看OpenAI赠送额度','openai_balance',links)
        self.link_button('查看OpenAI用量','openai_usage',links)
        body.addWidget(openai)
        keybox=QtWidgets.QGroupBox('OpenRouter · 当前聊天Key',canvas);keylayout=QtWidgets.QVBoxLayout(keybox)
        keybox.setObjectName('balanceCard')
        self.key_name=self.label('使用连接设置里当前选中的OpenRouter Key；不会切换聊天模型。');keylayout.addWidget(self.key_name)
        self.key_remaining=self.label('Key剩余消费上限：尚未查询');keylayout.addWidget(self.key_remaining)
        self.key_remaining.setObjectName('balanceAmount')
        self.key_usage=self.label('Key已用：尚未查询');keylayout.addWidget(self.key_usage)
        self.key_periods=self.label('');keylayout.addWidget(self.key_periods)
        self.key_time=self.label('');keylayout.addWidget(self.key_time)
        keylayout.addWidget(self.label('Key消费上限与账户余额不同。未设置Key上限，不代表账户余额无限；这里也不表示免费模型还可请求多少次。'))
        row=QtWidgets.QHBoxLayout();keylayout.addLayout(row)
        self.key_button=QtWidgets.QPushButton('刷新当前Key用量',self);row.addWidget(self.key_button)
        self.key_button.clicked.connect(self.refresh_key)
        self.link_button('打开OpenRouter用量','openrouter_activity',row)
        body.addWidget(keybox)
        accountbox=QtWidgets.QGroupBox('OpenRouter · 账户总余额',canvas);account=QtWidgets.QVBoxLayout(accountbox)
        accountbox.setObjectName('balanceCard')
        self.account_remaining=self.label('账户剩余：尚未查询');account.addWidget(self.account_remaining)
        self.account_remaining.setObjectName('balanceAmount')
        self.account_usage=self.label('');account.addWidget(self.account_usage)
        self.account_time=self.label('');account.addWidget(self.account_time)
        account.addWidget(self.label('自动查询需管理Key。结果属于这把管理Key对应的账户，可能与上方聊天Key不同。管理Key仅用于本次查询，不保存，也不会用于聊天。'))
        self.management_key=QtWidgets.QLineEdit(self)
        self.management_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.management_key.setMaxLength(4096);self.management_key.setPlaceholderText('OpenRouter管理Key（可选；查询后清空输入框）')
        account.addWidget(self.management_key)
        row=QtWidgets.QHBoxLayout();account.addLayout(row)
        self.account_button=QtWidgets.QPushButton('查询账户总余额',self);row.addWidget(self.account_button)
        self.account_button.clicked.connect(self.refresh_account)
        self.link_button('管理Key页面','openrouter_management',row)
        self.link_button('直接打开账户余额','openrouter_balance',row)
        body.addWidget(accountbox);body.addStretch(1)
        self.status=self.label('尚未查询。OpenAI账单在浏览器中查看；OpenRouter可在此刷新。');layout.addWidget(self.status)
        footer=QtWidgets.QHBoxLayout();layout.addLayout(footer)
        self.cancel_button=QtWidgets.QPushButton('取消查询',self);self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel);footer.addWidget(self.cancel_button)
        footer.addStretch(1)
        close=QtWidgets.QPushButton('关闭',self);close.clicked.connect(self.close);footer.addWidget(close)
        self.apply_style()

    def apply_style(self):
        apply_chat_style(self)
        panel,text,border,background=('#20242c','#edf0f5','#343b48','#181b21') if self._chat_dark else ('#ffffff','#202c40','#dfe5ee','#f5f7fb')
        self.setStyleSheet(self.styleSheet()+f'''
            #balanceCanvas {{ background: {background}; }}
            QGroupBox#balanceCard {{ background: {panel}; color: {text}; border: 1px solid {border};
                border-radius: 10px; margin-top: 12px; padding: 14px 12px 10px; }}
            QGroupBox#balanceCard::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}
            #balanceCard QLabel {{ color: {text}; background: transparent; }}
            #balanceAmount {{ font-size: 18px; font-weight: 600; }}
        ''')

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.PaletteChange,QtCore.QEvent.Type.ApplicationPaletteChange) and hasattr(self,'_chat_dark'):
            dark=QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness()<128
            if dark!=self._chat_dark:self.apply_style()

    def label(self,text):
        widget=QtWidgets.QLabel(text,self);widget.setWordWrap(True)
        widget.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        widget.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        return widget

    def link_button(self,title,name,layout):
        button=QtWidgets.QPushButton(title,self)
        button.clicked.connect(lambda _checked=False,key=name:self.open_link(key))
        layout.addWidget(button);return button

    def open_link(self,name):
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(LINKS[name])):
            self.status.setText('未能打开浏览器，请手动访问：'+LINKS[name])

    def child_chat_windows(self):return []

    def clear_key(self):
        self.key_remaining.setText('Key剩余消费上限：尚未查询')
        self.key_usage.setText('Key已用：尚未查询');self.key_periods.clear();self.key_time.clear()

    def clear_account(self):
        self.account_remaining.setText('账户剩余：尚未查询')
        self.account_usage.clear();self.account_time.clear()

    @safe_ui
    def refresh_key(self,_checked=False):
        if self.handle:return
        self.clear_key()
        config=self.context.load_config()
        selected=selected_credential(config,'openrouter')
        self.key_name.setText('查询时Key：'+((selected['name']+' · '+selected['hint']) if selected else 'OPENROUTER_API_KEY 环境变量'))
        token=read_provider_key(config,'openrouter')
        self._identity=provider_key_identity(config,'openrouter')
        self.start_query('key',token)

    @safe_ui
    def refresh_account(self,_checked=False):
        if self.handle:return
        self.clear_account()
        token=validate_token(self.management_key.text())
        self.management_key.clear()
        self.start_query('account',token)

    def start_query(self,kind,token):
        if is_offline():raise ValueError('当前为离线模式，不能查询余额。')
        self.operation={'type':'balance','kind':kind}
        worker=ClientWorker(functools.partial(query_openrouter,client=self.client),dict(kind=kind,token=token),self)
        self.handle=worker
        queued=QtCore.Qt.ConnectionType.QueuedConnection
        worker.completed.connect(lambda result,current=worker:self.completed(current,result),queued)
        worker.failed.connect(lambda message,current=worker:self.failed(current,message),queued)
        # No diagnostic/error body or credentials are written to application logs.
        connect_thread_finished(worker,self,'handle',self.finished,pass_thread=True)
        self.key_button.setEnabled(False);self.account_button.setEnabled(False)
        self.management_key.setEnabled(False);self.cancel_button.setEnabled(True)
        self.status.setText('正在只读查询OpenRouter，请稍候…');worker.start()

    @safe_ui
    def completed(self,worker,result):
        if worker is not self.handle or self._closing:return
        if worker.canceled.is_set():
            self.status.setText('查询已取消。');return
        now=datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
        if result['kind']=='key':
            if self._identity!=provider_key_identity(self.context.load_config(),'openrouter'):
                self.clear_key();self.status.setText('当前Key已更改，已忽略旧Key结果，请重新刷新。');return
            remaining='未设置Key上限（不代表账户余额无限）' if result['unlimited'] else dollars(result['limit_remaining'])
            self.key_remaining.setText('Key剩余消费上限：'+remaining)
            self.key_usage.setText('Key已用：'+dollars(result['usage'])+'   /   Key上限：'+('未设置' if result['unlimited'] else dollars(result['limit'])))
            self.key_periods.setText('今日已用 '+dollars(result['usage_daily'])+' · 本周 '+dollars(result['usage_weekly'])+' · 本月 '+dollars(result['usage_monthly']))
            reset={None:'不自动重置或接口未提供','daily':'每日','weekly':'每周','monthly':'每月'}[result['reset']]
            self.key_time.setText('查询时间：'+now+' · Key上限重置：'+reset)
        else:
            self.account_remaining.setText('账户剩余：'+dollars(result['remaining']))
            self.account_usage.setText('接口额度总额 '+dollars(result['total'])+' − 已用 '+dollars(result['used']))
            self.account_time.setText('查询时间：'+now+' · 管理Key对应账户')
        self.status.setText('查询完成。以上为查询时的快照，继续使用后可手动刷新。')

    def failed(self,worker,message):
        if worker is self.handle and not self._closing:
            self.status.setText('查询未完成：'+redact_sensitive_text(message))

    def finished(self,worker):
        if worker is not self.handle:return
        worker.payload.clear();worker.runner=None
        self.handle=self.operation=None
        if self._closing:self.deleteLater();return
        self.key_button.setEnabled(True);self.account_button.setEnabled(True)
        self.management_key.setEnabled(True);self.cancel_button.setEnabled(False)

    def cancel(self,_checked=False):
        if self.handle:
            self.handle.requestInterruption();self.cancel_button.setEnabled(False)
            self.status.setText('正在取消查询，请等待网络读取安全结束…')

    def reject(self):self.close()

    def closeEvent(self,event):
        self._closing=True
        self.management_key.clear();self.clear_key();self.clear_account();self.key_name.clear()
        if self.handle:self.handle.requestInterruption()
        else:self.deleteLater()
        event.accept()

from model.AppDataStorage import data_file
import functools
import copy
import html
import logging
import threading
import time
import traceback
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from model.AIProviderClient import PROVIDERS, validate_model, AIProviderError
from model.AIProviderKeys import KEYS_CONFIG_KEY, pin_provider_config, selected_credential
from model.AIChatSources import text_with_sources
from model.ClipboardHelper import set_internal_clipboard_text
from model.OfflineMode import is_offline, require_online
from model.SensitiveData import redact_sensitive_text
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.QtThreadLifecycle import connect_thread_finished
from .settings import CONFIG_KEY, ClientSettingsPage, default_options
from .rendering import is_web_link
from .catalog import openai_identity
from .advanced_options import AdvancedOptions
from .option_layout import OptionsGrid,inline_field
from .chat_view import Transcript
from .chat_style import apply_chat_style
from .version import APP_VERSION


def safe_ui(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except Exception as error:
            self.status.setText(redact_sensitive_text(str(error)))
            if not getattr(self, 'is_private', False):
                self.context.log('AI客户端操作失败（' + type(error).__name__ + '）；未将对话内容写入日志。', logging.ERROR)
            return None
    return wrapped


class StreamBuffer:
    """Worker writes ordinary data; the GUI reads it on its own timer."""
    def __init__(self):
        self.lock = threading.Lock()
        self.parts = []

    def append(self, text):
        with self.lock:
            self.parts.append(text)

    def text(self):
        with self.lock:
            return "".join(self.parts)


class ClientWorker(QtCore.QThread):
    """Private request thread, deliberately absent from the host task queue."""
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    diagnostic = QtCore.pyqtSignal(str)
    progressChanged = QtCore.pyqtSignal(str, object)

    def __init__(self, runner, payload, parent):
        super().__init__(parent)
        self.runner, self.payload = runner, copy.deepcopy(payload)
        self.canceled = threading.Event()

    def requestInterruption(self):
        self.canceled.set()
        super().requestInterruption()

    def run(self):
        job = TaskRunContext(self.canceled, self.progressChanged.emit, lambda _text: None)
        try:
            job.check_canceled()
            result = self.runner(self.payload, job)
            job.check_canceled()
            self.completed.emit(result)
        except BackgroundTaskCanceled:
            self.failed.emit("操作已取消。")
        except AIProviderError as error:
            self.failed.emit(redact_sensitive_text(str(error)))
        except Exception as error:
            self.diagnostic.emit(redact_sensitive_text(traceback.format_exc()))
            self.failed.emit(redact_sensitive_text(f"{type(error).__name__}: {error}"))


def run_chat(payload, job, *, store, client, stream=None):
    try:
        job.check_canceled()
        options = payload['options']
        messages = store.request_messages(payload["conversation"], payload["prompt_id"], options['rounds'],
            text_limit=options.get('context_chars', 0), include_history_images=options.get('history_images', True),
            system=options.get('system', ''))
        keywords = {"on_delta": stream.append} if stream is not None else {}
        result = client.chat(payload["provider"], payload["model"], messages,
                             payload["options"], payload["config"], job, **keywords)
        job.check_canceled()
        store.complete_request(payload["conversation"], payload["prompt_id"], result)
        return {"conversation": payload["conversation"], "result": result}
    except Exception as error:
        partial = {"text": stream.text(), "provider": payload["provider"], "model": payload["model"]} if stream is not None else None
        store.fail_request(payload["conversation"], payload["prompt_id"], str(error), job.is_canceled(), partial=partial)
        raise


def run_images(payload, job, *, store):
    images = []
    for index, source in enumerate(payload["sources"]):
        job.report(f"处理图片 {index+1}/{len(payload['sources'])}…", int(index * 100 / len(payload["sources"])))
        images.append(store.import_image(source))
    return {"conversation": payload["conversation"], "images": images}


def run_purge(payload, job, *, store):
    job.check_canceled()
    store.purge_conversation(payload['conversation'])
    store.resume_purges()
    return {'conversation': payload['conversation']}


class ChatInput(QtWidgets.QPlainTextEdit):
    imagesAdded = QtCore.pyqtSignal(list)
    sendRequested = QtCore.pyqtSignal()
    inputError = QtCore.pyqtSignal(str)

    def canInsertFromMimeData(self, source):
        return source.hasImage() or (source.hasUrls() and any(url.isLocalFile() for url in source.urls())) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()] if source.hasUrls() else []
        if paths:
            self.imagesAdded.emit(paths)
            return
        if source.hasImage():
            try:
                image = QtGui.QImage(source.imageData())
            except Exception:
                self.inputError.emit("剪贴板图片无法读取，请改用附加图片。")
                return
            if image.isNull() or image.width() * image.height() > 40000000:
                self.inputError.emit("剪贴板图片无效或超过4000万像素，请先缩小。")
                return
            buffer = QtCore.QBuffer()
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            if image.save(buffer, "PNG"):
                self.imagesAdded.emit([bytes(buffer.data())])
            return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)


class AIClientDialog(QtWidgets.QDialog):
    def __init__(self, context, store, parent=None):
        super().__init__(parent)
        self.context, self.store = context, store
        self.is_private = bool(getattr(store, 'is_private', False))
        self._private_dialogs = []
        self._group_dialogs = []
        self._account_dialogs = []
        self._privacy_closing = False
        self.client = context.ai_client
        if self.is_private:
            from .private_store import private_client
            self.client = private_client(self.client)
        self.current_id = None
        self.pending_images = []
        self.handle = self.operation = None
        self.stream = None
        self.stream_displayed = 0
        self.stream_start_pos = None
        self.catalogs = {}
        self.catalog_identities = {}
        self.catalog_attempts = set()
        self.catalog_errors = {}
        self.catalog_active_identity = None
        self.loading = False
        self.consent = False
        saved_size = (context.load_config().get(CONFIG_KEY) or {}).get('chat_font_px', 15)
        self.chat_font_px = max(12, min(28, saved_size)) if type(saved_size) is int else 15
        self.setWindowTitle(f"AI 模型试用台 {APP_VERSION} · " + ('无痕聊天' if self.is_private else 'LZX'))
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1180, 820)
        self.setMinimumSize(760, 580)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setObjectName('aiChatWindow')
        apply_chat_style(self)
        menu = QtWidgets.QMenuBar(self)
        layout.setMenuBar(menu)
        conversation_menu = menu.addMenu("对话")
        for title, callback, shortcut in (
            ("新建对话", self.new_conversation, "Ctrl+N"), ("重命名…", self.rename_conversation, ""),
            ("彻底删除本段对话…", self.delete_conversation, ""), ("导出对话文本…", self.export_conversation, ""),
            ("打开本地记录目录", self.open_history_folder, ""), ("复制最新回答", self.copy_answer, ""),
            ('分支记录…', self.show_branches, ''),
            ('重试最新回答', self.retry_request, 'Ctrl+R'),
        ):
            action = conversation_menu.addAction(title)
            action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(QtGui.QKeySequence(shortcut))
        connection_menu = menu.addMenu("连接")
        connection_menu.addAction("连接设置…", self.connection_settings)
        connection_menu.addAction('余额 / 用量…', self.open_balance)
        connection_menu.addAction("管理 Gemini 密钥…", self.open_gemini)
        connection_menu.addAction("刷新模型列表", self.refresh_models)
        if not self.is_private:
            conversation_menu.addAction('打开无痕聊天', self.open_private_chat)
        batch_menu = menu.addMenu('Batch')
        batch_menu.menuAction().setVisible(not self.is_private and not getattr(self,'is_group',False))
        batch_menu.addAction('检查当前对话的Batch', self.check_batch_requests)
        batch_menu.addAction('取消当前对话的Batch…', self.cancel_batch_requests)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.sidebar = QtWidgets.QWidget(splitter)
        self.sidebar.setObjectName('chatSidebar')
        sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(16, 20, 16, 16)
        sidebar_layout.setSpacing(12)
        brand = QtWidgets.QLabel('无痕聊天' if self.is_private else 'LZX  /  AI 试用台', self.sidebar)
        brand.setObjectName('sidebarBrand')
        sidebar_layout.addWidget(brand)
        self.new_button = QtWidgets.QPushButton('＋  新建对话', self.sidebar)
        self.new_button.setObjectName('newChat')
        self.new_button.clicked.connect(self.new_conversation)
        sidebar_layout.addWidget(self.new_button)
        self.group_button = QtWidgets.QPushButton('AI 群聊', self.sidebar)
        self.group_button.clicked.connect(self.open_group_chat)
        sidebar_layout.addWidget(self.group_button)
        if not self.is_private:
            self.private_button = QtWidgets.QPushButton('无痕聊天', self.sidebar)
            self.private_button.clicked.connect(self.open_private_chat)
            sidebar_layout.addWidget(self.private_button)
        self.history_search = QtWidgets.QLineEdit(self.sidebar)
        self.history_search.setPlaceholderText('搜索对话…')
        self.history_search.setClearButtonEnabled(True)
        sidebar_layout.addWidget(self.history_search)
        self.history = QtWidgets.QListWidget(self.sidebar)
        self.history.setObjectName('chatHistory')
        self.history.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.history.setMinimumWidth(180)
        self.history.setMaximumWidth(360)
        self.history.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self.history_menu)
        sidebar_layout.addWidget(self.history, 1)
        self.delete_button = QtWidgets.QPushButton('彻底删除当前对话…', self.sidebar)
        self.delete_button.clicked.connect(self.delete_conversation)
        sidebar_layout.addWidget(self.delete_button)
        settings_button = QtWidgets.QPushButton('连接设置', self.sidebar)
        settings_button.clicked.connect(self.connection_settings)
        sidebar_layout.addWidget(settings_button)
        local_note = QtWidgets.QLabel('无痕：关闭后不保留记录' if self.is_private else '普通对话记录保存在本机', self.sidebar)
        local_note.setObjectName('muted')
        sidebar_layout.addWidget(local_note)
        right = QtWidgets.QWidget(splitter)
        right.setObjectName('chatMain')
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.header = QtWidgets.QWidget(right)
        self.header.setObjectName('chatHeader')
        header_layout = QtWidgets.QVBoxLayout(self.header)
        header_layout.setContentsMargins(22, 16, 22, 14)
        header_layout.setSpacing(12)
        title_row = QtWidgets.QHBoxLayout()
        self.sidebar_button = QtWidgets.QToolButton(self.header)
        self.sidebar_button.setText('会话')
        self.sidebar_button.setToolTip('显示 / 隐藏会话列表')
        self.sidebar_button.clicked.connect(lambda: self.sidebar.setVisible(not self.sidebar.isVisible()))
        title_row.addWidget(self.sidebar_button)
        self.conversation_title = QtWidgets.QLabel('新对话', self.header)
        self.conversation_title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.conversation_title.setObjectName('conversationTitle')
        self.conversation_title.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.conversation_title, 1)
        self.font_size = QtWidgets.QSpinBox(self.header)
        self.font_size.setRange(12, 28)
        self.font_size.setSuffix(' px')
        self.font_size.setValue(self.chat_font_px)
        self.font_size.setPrefix('字号 ')
        self.font_size.setFixedWidth(112)
        self.font_size.setToolTip('聊天字号：同时调整消息、代码及输入区，保存为所有对话通用的偏好。')
        title_row.addWidget(self.font_size)
        self.copy_latest = QtWidgets.QToolButton(self.header)
        self.copy_latest.setText('复制最新回答')
        self.copy_latest.clicked.connect(self.copy_answer)
        title_row.addWidget(self.copy_latest)
        self.balance_button = QtWidgets.QToolButton(self.header)
        self.balance_button.setText('余额 / 用量')
        self.balance_button.clicked.connect(self.open_balance)
        title_row.addWidget(self.balance_button)
        header_layout.addLayout(title_row)
        selectors = QtWidgets.QHBoxLayout()
        self.provider = QtWidgets.QComboBox(right)
        for identifier, title in PROVIDERS.items():
            self.provider.addItem("Cloudflare" if identifier == "cloudflare" else title, identifier)
        self.model = QtWidgets.QComboBox(right)
        self.model.setEditable(True)
        self.model.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model.setMinimumContentsLength(14)
        self.model.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.model.lineEdit().setPlaceholderText("刷新列表，或填写具体模型 ID")
        self.model.setMinimumWidth(140)
        self.refresh_button = QtWidgets.QPushButton("刷新模型", right)
        self.options_button = QtWidgets.QToolButton(right)
        self.options_button.setText("参数 / 联网")
        self.options_button.setCheckable(True)
        selectors.addWidget(self.provider)
        selectors.addWidget(self.model, 1)
        selectors.addWidget(self.refresh_button)
        selectors.addWidget(self.options_button)
        header_layout.addLayout(selectors)
        right_layout.addWidget(self.header)
        if self.is_private:
            private_note = QtWidgets.QLabel('无痕：聊天、草稿和图片仅在内存中，不写本机聊天文件；关闭窗口后清除。内容仍会发送至所选服务商，手动复制也可能留下外部记录。', right)
            private_note.setWordWrap(True)
            private_note.setContentsMargins(18,8,18,8)
            right_layout.addWidget(private_note)
        self.catalog_hint = QtWidgets.QLabel(right)
        self.catalog_hint.setWordWrap(True)
        self.catalog_hint.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        header_layout.addWidget(self.catalog_hint)
        self.options_panel = OptionsGrid(right)
        self.system = QtWidgets.QPlainTextEdit(self.options_panel)
        self.system.setMinimumHeight(56)
        self.system.setMaximumHeight(70)
        self.system.setPlaceholderText("可选：给模型的固定要求；留空使用模型默认行为")
        self.options_panel.add_field('系统提示',self.system,wide=True,top=True)
        self.rounds = QtWidgets.QSpinBox(self.options_panel)
        self.rounds.setRange(0, 50)
        self.rounds.setSuffix(" 轮")
        self.rounds.setSpecialValueText('不带历史')
        self.rounds.setToolTip('只发送最近的成功对话；0表示仅发送当前消息和系统提示。旧记录仍保留。')
        self.maximum = QtWidgets.QSpinBox(self.options_panel)
        self.maximum.setRange(128, 128000)
        self.maximum.setSingleStep(512)
        self.temperature_enabled = QtWidgets.QCheckBox("自定义", self.options_panel)
        self.temperature = QtWidgets.QDoubleSpinBox(self.options_panel)
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(.1)
        self.temperature.setValue(.7)
        for widget in (self.rounds,self.maximum):
            widget.setFixedWidth(160)
            widget.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.temperature.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.options_panel.add_field('上下文轮数',self.rounds)
        self.options_panel.add_field('输出上限',self.maximum)
        self.options_panel.add_field('采样温度',inline_field(self.options_panel,self.temperature_enabled,self.temperature))
        self.advanced = AdvancedOptions(self.options_panel)
        self.context_chars = QtWidgets.QSpinBox(self.options_panel)
        self.context_chars.setRange(0, 512000)
        self.context_chars.setSingleStep(2000)
        self.context_chars.setSpecialValueText('不限制')
        self.context_chars.setFixedWidth(160)
        self.context_chars.setToolTip('限制当前消息、系统提示和历史文字的总字符数，不是Token。超限时按整轮省略最旧历史，绝不截断当前消息。')
        self.history_images = QtWidgets.QCheckBox('携带历史图片', self.options_panel)
        self.history_images.setChecked(True)
        self.history_images.setToolTip('关闭后只发送本次附加的图片，旧图片保留在聊天记录中。')
        self.options_panel.add_field('请求文字上限',self.context_chars)
        self.options_panel.add_field('历史图片',self.history_images)
        self.request_mode = QtWidgets.QComboBox(self.options_panel)
        self.request_mode.addItem('即时聊天', 'instant')
        self.request_mode.addItem('OpenAI Batch · 异步优惠', 'batch')
        self.request_mode.setFixedWidth(260)
        self.request_mode.setToolTip('Batch仅支持OpenAI；按官方适用价格计费，最长等待24小时，不流式。请求会作为文件上传；未知结果不自动重复提交。')
        self.batch_check_button = QtWidgets.QPushButton('检查 Batch 结果', self.options_panel)
        self.batch_check_button.clicked.connect(self.check_batch_requests)
        self.options_panel.add_field('请求方式', self.request_mode)
        self.options_panel.add_field('异步任务', self.batch_check_button)
        economy_row = inline_field(self.options_panel,width=None)
        self.economy_button = QtWidgets.QPushButton('精简上下文',self.options_panel)
        self.economy_button.setToolTip('改为最近5轮、最多16000文字字符、不携带历史图片。保留所选模型、输出上限、思考和联网设置。')
        self.economy_button.clicked.connect(self.apply_economy_context)
        economy_row.layout().addWidget(self.economy_button)
        economy_note = QtWidgets.QLabel('减少历史可降低发送量，也会减少模型能记住的细节。',self.options_panel)
        economy_note.setWordWrap(True)
        economy_row.layout().addWidget(economy_note,1)
        self.options_panel.add_field('用量控制',economy_row,wide=True)
        self.free_only = QtWidgets.QCheckBox("OpenRouter 仅免费", self.options_panel)
        self.foreign_only = QtWidgets.QCheckBox("国外厂商", self.options_panel)
        self.cf_free_only = QtWidgets.QCheckBox("Cloudflare 免费额度", self.options_panel)
        self.cf_free_only.setToolTip("并非零收费保证；Paid账户超出每日10000 Neurons可能继续计费。")
        filters=inline_field(self.options_panel,self.free_only,self.foreign_only,self.cf_free_only,width=None)
        filters.layout().addStretch(1)
        self.options_panel.add_field('模型筛选',filters,wide=True)
        self.streaming = QtWidgets.QCheckBox("流式输出", self.options_panel)
        self.streaming.setChecked(True)
        self.streaming.setToolTip("不支持流式的模型可手动关闭；不会在失败后自动重发或换成付费模型。")
        self.markdown = QtWidgets.QCheckBox("Markdown 排版", self.options_panel)
        self.markdown.setChecked(True)
        self.markdown.setToolTip("仅改变显示；复制最新回答和导出文本仍保留原文。链接需点击才打开，远程图片不会自动加载。")
        display=inline_field(self.options_panel,self.streaming,self.markdown,width=None)
        display.layout().addStretch(1)
        self.options_panel.add_field('显示',display,wide=True)
        self.options_scroll = QtWidgets.QScrollArea(right)
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.options_scroll.setMaximumHeight(360)
        self.options_scroll.setWidget(self.options_panel)
        self.options_panel.bind_viewport(self.options_scroll.viewport())
        self.options_scroll.hide()
        self.options_button.clicked.connect(lambda: self.options_scroll.setVisible(not self.options_scroll.isVisible()))
        right_layout.addWidget(self.options_scroll)
        hint = QtWidgets.QLabel("聊天记录保存在本机；点击发送会把上下文和图片发给所选云服务。图片会保存静态副本（长边不超过2048），原文件不变。", right)
        hint.setWordWrap(True)
        self.options_panel.add_field('数据说明',hint,wide=True)
        self.billing_hint = QtWidgets.QLabel("", right)
        self.billing_hint.setWordWrap(True)
        self.options_panel.add_field('',self.billing_hint,wide=True)
        self.transcript = Transcript(right)
        self.transcript.set_font_size(self.chat_font_px)
        self.transcript.anchorClicked.connect(self.open_attachment)
        self.transcript.regenerateRequested.connect(self.regenerate_answer)
        self.transcript.branchRequested.connect(self.switch_branch)
        self.transcript.editRequested.connect(self.edit_message)
        right_layout.addWidget(self.transcript, 1)
        self.jump_button = QtWidgets.QToolButton(right)
        self.jump_button.setText('↓  回到最新消息')
        self.jump_button.clicked.connect(self.jump_to_latest)
        self.jump_button.hide()
        right_layout.addWidget(self.jump_button, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.transcript.verticalScrollBar().valueChanged.connect(self.update_jump_button)
        self.composer = QtWidgets.QFrame(right)
        self.composer.setObjectName('chatComposer')
        composer_layout = QtWidgets.QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(18, 12, 18, 12)
        composer_layout.setSpacing(8)
        self.attachments = QtWidgets.QListWidget(right)
        self.attachments.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.attachments.setIconSize(QtCore.QSize(84, 65))
        self.attachments.setMaximumHeight(105)
        self.attachments.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.attachments.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.attachments.customContextMenuRequested.connect(self.attachment_menu)
        self.attachments.hide()
        composer_layout.addWidget(self.attachments)
        self.input = ChatInput(right)
        self.input.setObjectName('chatInput')
        self.input.setPlaceholderText("写下你的问题…  可粘贴或拖入图片")
        self.input.setFixedHeight(80)
        composer_layout.addWidget(self.input)
        self.request_hint = QtWidgets.QLabel(self.composer)
        self.request_hint.setObjectName('muted')
        self.request_hint.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.request_hint.setWordWrap(True)
        self.request_hint.setToolTip('仅做本地统计，不请求模型。字符数不等于Token，图片和服务商内部格式另计。旧历史不会从本地记录删除。')
        composer_layout.addWidget(self.request_hint)
        buttons = QtWidgets.QHBoxLayout()
        self.attach_button = QtWidgets.QPushButton("附加图片…", right)
        self.retry_button = QtWidgets.QPushButton("重试回答", right)
        self.retry_button.setToolTip('重试当前分支最后一条失败请求，或重新生成最新回答；成功回答的新版本保存在独立分支。也可按Ctrl+R。')
        self.stop_button = QtWidgets.QPushButton("停止", right)
        self.send_button = QtWidgets.QPushButton("发送 ↑", right)
        self.send_button.setObjectName('sendChat')
        self.send_button.setDefault(False)
        self.send_button.setAutoDefault(False)
        for button in (self.attach_button, self.retry_button, self.stop_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        buttons.addStretch(1)
        shortcut_hint = QtWidgets.QLabel('Ctrl+Enter 发送 · Enter 换行', self.composer)
        shortcut_hint.setObjectName('muted')
        buttons.addWidget(shortcut_hint)
        buttons.addWidget(self.send_button)
        composer_layout.addLayout(buttons)
        right_layout.addWidget(self.composer)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(right)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([250, 930])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = QtWidgets.QLabel(self)
        self.status.setObjectName('chatStatus')
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.draft_timer = QtCore.QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.setInterval(700)
        self.draft_timer.timeout.connect(self.save_draft)
        self.elapsed_timer = QtCore.QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self.show_elapsed)
        self.stream_timer = QtCore.QTimer(self)
        self.stream_timer.setInterval(160)
        self.stream_timer.timeout.connect(self.show_stream)
        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(200)
        self.preview_timer.timeout.connect(self.update_request_hint)
        self.font_size.valueChanged.connect(self.font_size_changed)
        for text, shortcut, delta in (('放大聊天文字', 'Ctrl++', 1), ('缩小聊天文字', 'Ctrl+-', -1)):
            action = QtGui.QAction(text, self)
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.triggered.connect(lambda _checked=False, step=delta: self.font_size.stepBy(step))
            self.addAction(action)
        self.history.currentItemChanged.connect(self.select_conversation)
        self.history_search.textChanged.connect(self.filter_history)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.model.currentTextChanged.connect(self.schedule_draft)
        self.input.textChanged.connect(self.schedule_draft)
        self.system.textChanged.connect(self.schedule_draft)
        for widget in (self.rounds, self.maximum, self.temperature, self.context_chars):
            widget.valueChanged.connect(self.schedule_draft)
        self.history_images.toggled.connect(self.schedule_draft)
        self.request_mode.currentIndexChanged.connect(self.request_mode_changed)
        for widget in (self.free_only, self.foreign_only, self.cf_free_only):
            widget.toggled.connect(self.filter_changed)
        self.temperature_enabled.toggled.connect(self.schedule_draft)
        self.streaming.toggled.connect(self.schedule_draft)
        self.markdown.toggled.connect(self.presentation_changed)
        self.temperature_enabled.toggled.connect(self.temperature.setEnabled)
        self.advanced.changed.connect(self.capability_changed)
        self.model.currentTextChanged.connect(self.capability_changed)
        self.maximum.valueChanged.connect(self.capability_changed)
        self.temperature_enabled.toggled.connect(self.capability_changed)
        self.refresh_button.clicked.connect(self.refresh_models)
        self.send_button.clicked.connect(self.send)
        self.input.sendRequested.connect(self.send)
        self.stop_button.clicked.connect(self.stop_request)
        self.attach_button.clicked.connect(self.choose_images)
        self.input.imagesAdded.connect(self.add_images)
        self.input.inputError.connect(self.status.setText)
        self.retry_button.clicked.connect(self.retry_request)
        self.catalog_timer = QtCore.QTimer(self)
        self.catalog_timer.setSingleShot(True)
        self.catalog_timer.timeout.connect(self.ensure_openai_catalog)
        existing = store.conversations()
        self.reload_history(existing[0]["id"] if existing else None)
        if not existing:
            self.new_conversation()
        self.set_busy(False)
        self.request_mode_changed()
        self.status.setText("离线模式：可以查看历史，不能请求模型。" if is_offline() else "从“连接 → 连接设置”添加服务密钥，再刷新并选择模型。")

    def showEvent(self, event):
        super().showEvent(event)
        self.catalog_timer.start(0)
        self.preview_timer.start()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            dark = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
            if hasattr(self, '_chat_dark') and dark != self._chat_dark:
                apply_chat_style(self)

    def settings_changed(self):
        # Also called when credentials were saved from the main settings page.
        self.catalog_timer.start(0)

    def open_private_chat(self, _checked=False):
        if self.is_private:
            return
        from .private_store import PrivateChatStore
        dialog = type(self)(self.context, PrivateChatStore(), self)
        self._private_dialogs.append(dialog)
        dialog.destroyed.connect(lambda *_args, child=dialog: self._private_dialogs.remove(child) if child in self._private_dialogs else None)
        dialog.show()
        return dialog

    @safe_ui
    def open_group_chat(self, _checked=False):
        if self._group_dialogs:
            dialog=self._group_dialogs[0]
            dialog.show();dialog.raise_();dialog.activateWindow();return dialog
        from .group_ui import GroupChatDialog
        if self.is_private:
            from .private_store import PrivateChatStore
            store=PrivateChatStore()
        else:
            from .store import ChatStore
            store=ChatStore(self.store.root / 'GroupChats')
        dialog=GroupChatDialog(self.context,store,self)
        self._group_dialogs.append(dialog)
        dialog.destroyed.connect(lambda *_args,child=dialog:self._group_dialogs.remove(child) if child in self._group_dialogs else None)
        dialog.show();return dialog

    @safe_ui
    def open_balance(self, _checked=False):
        from .balance_ui import BalanceDialog
        existing=next((window for window in self._account_dialogs if not window._closing),None)
        if existing is not None:
            existing.show();existing.raise_();existing.activateWindow();return existing
        root=self
        while isinstance(root.parent(),AIClientDialog):root=root.parent()
        dialog=BalanceDialog(self.context,self.client)
        for owner in ({self,root}):
            owner._account_dialogs.append(dialog)
            dialog.destroyed.connect(lambda *_args,owner=owner,child=dialog:owner._account_dialogs.remove(child) if child in owner._account_dialogs else None)
        dialog.show();return dialog

    def child_chat_windows(self):
        result=[]
        for child in [*self._private_dialogs,*self._group_dialogs,*self._account_dialogs]:
            result.append(child);result.extend(child.child_chat_windows())
        return result

    @safe_ui
    def font_size_changed(self, pixels):
        self.chat_font_px = pixels
        apply_chat_style(self)
        self.transcript.set_font_size(pixels)
        snapshot = self.context.load_config()
        settings = dict(snapshot.get(CONFIG_KEY) or {})
        settings['chat_font_px'] = pixels
        if not self.context.patch_config({CONFIG_KEY: settings}, expected=snapshot):
            self.status.setText('字号已应用，但偏好未保存；请稍后重新调整。')

    def apply_economy_context(self):
        self.rounds.setValue(5)
        self.context_chars.setValue(16000)
        self.history_images.setChecked(False)
        self.schedule_draft()
        self.update_request_hint()

    def request_mode_changed(self, *_args):
        batch = self.request_mode.currentData() == 'batch'
        self.streaming.setEnabled(not batch)
        self.streaming.setToolTip('Batch不流式；此选项保留用于即时聊天。' if batch else '流式只改变显示速度，不减少输入文字量。')
        self.send_button.setText('提交 Batch' if batch else '发送 ↑')
        self.schedule_draft()
        self.update_request_hint()

    @safe_ui
    def update_request_hint(self):
        if not self.current_id or self.loading or not self.isVisible():
            return
        try:
            info = self.store.context_preview(self.current_id, self.input.toPlainText().strip(), self.pending_images, self.options())
            text = (f"本次携带 {info['history_rounds']} 轮历史 · 文字 {info['text_chars']} 字符 · 图片 {info['images']} 张")
            if info['omitted_messages']:
                text += f" · 已省略 {info['omitted_messages']} 条旧历史"
            if self.request_mode.currentData() == 'batch':
                text = 'Batch异步优惠 · 最长24小时 · 不流式\n' + text
            self.request_hint.setText(text)
        except ValueError as error:
            self.request_hint.setText(str(error))

    @safe_ui
    def regenerate_answer(self, assistant_id):
        if self.handle:
            self.status.setText('请等待当前请求完成，再重新生成。')
            return
        self.send(regenerate_id=assistant_id)

    @safe_ui
    def edit_message(self, prompt_id):
        if self.handle:
            self.status.setText('请等待当前操作完成后再编辑消息。')
            return
        row = self.store.editable_prompt(self.current_id, prompt_id)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('编辑我的消息')
        dialog.resize(720, 500)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel('修改并重新发送会创建新分支；原问题、原回答和后续对话都保留。', dialog)
        note.setWordWrap(True)
        layout.addWidget(note)
        editor = QtWidgets.QPlainTextEdit(dialog)
        editor.setObjectName('editChatMessage')
        font = editor.font()
        font.setPixelSize(self.chat_font_px)
        editor.setFont(font)
        editor.setPlainText(row['text'])
        layout.addWidget(editor, 1)
        if row['images']:
            attachments = QtWidgets.QLabel('沿用本条消息的图片：' + '、'.join(image['name'] for image in row['images']), dialog)
            attachments.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            attachments.setWordWrap(True)
            layout.addWidget(attachments)
        identity = QtWidgets.QLabel('本次使用：' + PROVIDERS.get(self.provider.currentData(), '') + ' / ' + self.model_id(), dialog)
        identity.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        identity.setWordWrap(True)
        layout.addWidget(identity)
        error_label = QtWidgets.QLabel(dialog)
        error_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        error_label.setWordWrap(True)
        layout.addWidget(error_label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('保存并重新发送')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        def submit():
            result = self.send(edit_id=prompt_id, edited_text=editor.toPlainText())
            if result is not None:
                dialog.accept()
            else:
                error_label.setText(self.status.text() or '尚未发送，修改仍保留在编辑框中。')
        buttons.accepted.connect(submit)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        editor.setFocus()
        dialog.exec()

    @safe_ui
    def check_batch_requests(self, _checked=False, *, cancel=False):
        if self.is_private:
            self.status.setText('无痕聊天不保存Batch记录，仅支持即时请求。')
            return
        if self.handle:
            self.status.setText('请等待当前操作完成后再检查Batch。')
            return
        if not self.store.batches(self.current_id):
            self.status.setText('当前对话没有Batch记录；可在“参数 / 联网 → 请求方式”选择OpenAI Batch。')
            return
        from .batch_chat import check_batches
        config = pin_provider_config(self.connection_snapshot(), 'openai')
        self.submit('检查AI聊天Batch', functools.partial(check_batches, store=self.store),
            {'conversation': self.current_id, 'config': config, 'cancel': cancel}, {'type': 'batch_check', 'conversation': self.current_id})

    def cancel_batch_requests(self, _checked=False):
        if QtWidgets.QMessageBox.question(self, '取消云端Batch', '取消当前对话尚在运行的Batch？已完成的部分仍可能计费。关闭聊天窗口本身不会取消云端任务。') == QtWidgets.QMessageBox.StandardButton.Yes:
            self.check_batch_requests(cancel=True)

    @safe_ui
    def switch_branch(self, branch_id):
        self.save_draft()
        self.store.select_branch(self.current_id, branch_id)
        self._displayed_conversation = None
        self.render_transcript()
        self.update_request_hint()
        self.status.setText('已切换分支；后续请求只携带当前分支的上下文。')

    @safe_ui
    def show_branches(self, _checked=False):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('分支记录 · 选择一条历史继续')
        dialog.resize(820, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel('重新生成会保留原分支。选择一个节点，可切换到该分支及其最近的后续对话；切换不会请求模型。', dialog)
        note.setWordWrap(True)
        layout.addWidget(note)
        tree = QtWidgets.QTreeWidget(dialog)
        tree.setHeaderLabels(['对话节点', '模型', '状态'])
        tree.setColumnWidth(0, 470)
        tree.setColumnWidth(1, 190)
        layout.addWidget(tree, 1)
        nodes = {}
        leaf = self.store.get(self.current_id)['active_leaf']
        for row in self.store.messages(self.current_id, all_branches=True):
            title = ('你：' if row['role'] == 'user' else 'AI：') + row['text'].replace('\n', ' ')[:90]
            item = QtWidgets.QTreeWidgetItem([title, row['model'], row['status']])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row['id'])
            parent = nodes.get(row['parent_id'])
            if parent:
                parent.addChild(item)
            else:
                tree.addTopLevelItem(item)
            nodes[row['id']] = item
            if row['id'] == leaf:
                tree.setCurrentItem(item)
                ancestor = parent
                while ancestor:
                    ancestor.setExpanded(True)
                    ancestor = ancestor.parent()
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Open | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Open).setText('切换分支')
        def choose():
            item = tree.currentItem()
            if item:
                self.switch_branch(item.data(0, QtCore.Qt.ItemDataRole.UserRole))
                dialog.accept()
        buttons.accepted.connect(choose)
        buttons.rejected.connect(dialog.reject)
        tree.itemDoubleClicked.connect(lambda *_args: choose())
        layout.addWidget(buttons)
        dialog.exec()

    def sync_openai_identity(self, config):
        identity = openai_identity(config)
        if identity != self.catalog_active_identity:
            self.catalog_active_identity = identity
            self.catalogs.pop('openai', None)
            self.catalog_errors.pop('openai', None)
            self.catalog_attempts.discard(identity)
            if self.provider.currentData() == 'openai':
                self.fill_models(self.model_id())
        return identity

    @safe_ui
    def ensure_openai_catalog(self):
        if not self.isVisible() or self.handle or is_offline() or self.provider.currentData() != 'openai':
            return
        config=self.connection_snapshot()
        try: selected_credential(config,'openai')
        except ValueError as error:
            self.catalogs.pop('openai',None)
            self.catalog_errors['openai']=redact_sensitive_text(str(error))
            self.fill_models(self.model_id())
            return
        identity = self.sync_openai_identity(config)
        if identity and identity not in self.catalog_attempts:
            self.refresh_models()

    def options(self):
        return {"system": self.system.toPlainText(), "rounds": self.rounds.value(),
                'context_chars': self.context_chars.value(), 'history_images': self.history_images.isChecked(),
                'request_mode': self.request_mode.currentData(),
                "max_tokens": self.maximum.value(),
                "temperature": self.temperature.value() if self.temperature_enabled.isChecked() else None,
                "free_only": self.free_only.isChecked(), "foreign_only": self.foreign_only.isChecked(),
                "cf_free_only": self.cf_free_only.isChecked(), "stream": self.streaming.isChecked(),
                "render_markdown": self.markdown.isChecked(), **self.advanced.values()}

    def selected_model_row(self):
        return next((row for row in self.catalogs.get(self.provider.currentData(),[])
                     if row['id']==self.model_id()),None)

    @safe_ui
    def capability_changed(self, *_args):
        if self.loading: return
        self.sync_capabilities()
        self.schedule_draft()

    def sync_capabilities(self):
        options={**self.options(),**self.advanced.values()}
        caps=self.advanced.sync(self.provider.currentData(),self.model_id(),self.selected_model_row(),options)
        self.temperature_enabled.setEnabled(caps['sampling'])
        self.temperature.setEnabled(caps['sampling'] and self.temperature_enabled.isChecked())
        self.temperature_enabled.setToolTip('模型／思考等级不支持时保留原值，但不发送温度。')
        provider=self.provider.currentData()
        self.request_mode.model().item(1).setEnabled(provider == 'openai' and not self.is_private)
        if (provider != 'openai' or self.is_private) and self.request_mode.currentData() == 'batch':
            self.request_mode.setCurrentIndex(0)
        self.free_only.setEnabled(provider=='openrouter')
        self.cf_free_only.setEnabled(provider=='cloudflare')
        self.foreign_only.setEnabled(provider in {'openrouter','cloudflare','mistral','groq'})
        return caps

    def request_options(self):
        options=self.options()
        options.update(self.advanced.effective(self.provider.currentData(),self.model_id(),self.selected_model_row(),options))
        caps=self.sync_capabilities()
        if not caps['sampling']: options['temperature']=None
        return options

    def model_id(self):
        row = self.model.currentData()
        if isinstance(row, dict) and self.model.currentText() == self.model.itemText(self.model.currentIndex()):
            return row["id"]
        return self.model.currentText().strip()

    def connection_snapshot(self):
        config = self.context.load_config()
        return {key: config[key] for key in (KEYS_CONFIG_KEY, CONFIG_KEY) if key in config}

    @safe_ui
    def open_gemini(self, _checked=False):
        self.context.open_gemini_key_manager()

    def reload_history(self, identifier=None):
        selected = identifier or self.current_id
        self.history.blockSignals(True)
        self.history.clear()
        chosen = None
        for row in self.store.conversations():
            item = QtWidgets.QListWidgetItem(row["title"] + "\n" + time.strftime("%m-%d %H:%M", time.localtime(row["updated"])) + " · " + PROVIDERS.get(row["provider"], row["provider"]))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(row["title"] + "\n" + row["model"])
            self.history.addItem(item)
            if row["id"] == selected:
                chosen = item
        if chosen:
            self.history.setCurrentItem(chosen)
        self.history.blockSignals(False)
        self.filter_history()
        if chosen and selected != self.current_id:
            self.select_conversation(chosen)

    @safe_ui
    def new_conversation(self, _checked=False):
        self.save_draft()
        config = self.context.load_config().get(CONFIG_KEY) or {}
        options = default_options()
        options.update({key: config[key] for key in ("free_only", "foreign_only", "cf_free_only") if key in config})
        identifier = self.store.create(self.provider.currentData(), self.model_id(), options)
        self.reload_history(identifier)

    @safe_ui
    def select_conversation(self, item, _previous=None):
        if not item:
            return
        self.save_draft()
        self.draft_timer.stop()
        self.loading = True
        try:
            self.current_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            row = self.store.get(self.current_id)
            index = self.provider.findData(row["provider"])
            self.provider.setCurrentIndex(max(0, index))
            options = {**default_options(), **row["options"]}
            self.conversation_options_loaded(options)
            self.system.setPlainText(options["system"])
            self.rounds.setValue(options["rounds"])
            self.context_chars.setValue(options.get('context_chars', 0))
            self.history_images.setChecked(options.get('history_images', True))
            self.request_mode.setCurrentIndex(max(0,self.request_mode.findData(options.get('request_mode','instant'))))
            self.maximum.setValue(options["max_tokens"])
            self.temperature_enabled.setChecked(options["temperature"] is not None)
            self.temperature.setEnabled(options["temperature"] is not None)
            self.temperature.setValue(options["temperature"] if options["temperature"] is not None else .7)
            self.free_only.setChecked(options["free_only"])
            self.foreign_only.setChecked(options["foreign_only"])
            self.cf_free_only.setChecked(options["cf_free_only"])
            self.streaming.setChecked(options.get("stream", True))
            self.markdown.setChecked(options.get("render_markdown", True))
            self.advanced.load(options)
            self.fill_models(row["model"])
            self.input.setPlainText(row["draft"])
            self.pending_images = row["images"]
            self.render_attachments()
            self.render_transcript()
        finally:
            self.loading = False
        self.catalog_timer.start(0)
        self.update_request_hint()

    def conversation_options_loaded(self,options):
        pass

    def schedule_draft(self, *_args):
        if not self.loading and self.current_id:
            self.draft_timer.start()
            self.preview_timer.start()

    @safe_ui
    def presentation_changed(self, *_args):
        if not self.loading:
            self.render_transcript()
            self.schedule_draft()

    @safe_ui
    def save_draft(self, *_args):
        if not self.loading and self.current_id:
            self.draft_timer.stop()
            self.store.update(self.current_id, provider=self.provider.currentData(), model=self.model_id(),
                              options=self.options(), draft=self.input.toPlainText(), images=self.pending_images)
            # Refresh this item's provider label in place; do not reset scroll,
            # rebuild all conversations or make a changed GPT chat say Gemini.
            row=self.store.get(self.current_id)
            for index in range(self.history.count()):
                item=self.history.item(index)
                if item.data(QtCore.Qt.ItemDataRole.UserRole)==self.current_id:
                    item.setText(row['title']+'\n'+time.strftime('%m-%d %H:%M',time.localtime(row['updated']))+
                                 ' · '+PROVIDERS.get(row['provider'],row['provider']))
                    item.setToolTip(row['title']+'\n'+row['model'])
                    break

    @safe_ui
    def provider_changed(self, *_args):
        if not self.loading:
            self.fill_models("")
            self.schedule_draft()
            self.catalog_timer.start(0)

    @safe_ui
    def filter_changed(self, *_args):
        if not self.loading:
            self.fill_models(self.model_id())
            self.schedule_draft()

    def fill_models(self, selected=""):
        provider = self.provider.currentData()
        rows = self.client.filter_models(provider, self.catalogs.get(provider, []), free_only=self.free_only.isChecked(), foreign_only=self.foreign_only.isChecked(), cf_free_only=self.cf_free_only.isChecked())
        billing_notes = {
            "openai": "OpenAI 官方 API：按用量计费，赠送额度以 API 平台账单为准。默认不自动重试或切换模型；输出上限包含推理 token，空回答可适当提高上限。",
            "cloudflare": "Cloudflare：每天10000 Neurons（00:00 UTC重置），不是tokens。Free账户超额停止；Paid账户超额可能计费。此处不显示/猜测剩余额度。",
            "groq": "Groq：Free 账号有组织级速率及每日限额；Developer 账号可能计费。此处不猜测剩余额度；图片模型若未列出，可关闭“国外厂商”筛选。",
        }
        self.billing_hint.setText(billing_notes.get(provider, ""))
        self.billing_hint.setVisible(provider in billing_notes)
        if provider == 'openai':
            if provider in self.catalogs:
                note = f'已读取账号的 {len(rows)} 个聊天模型候选；是否可调用以实际权限为准。' if rows else '账号模型列表未返回可选聊天模型；可手动填写具体模型 ID。'
            else:
                note = '尚未读取账号模型列表。当前填写的默认模型不是全部模型；配置 Key 后会自动读取，也可手动刷新。'
            if self.catalog_errors.get(provider):
                note += '\n模型列表读取失败：' + self.catalog_errors[provider] + ' 可手动刷新或直接填写模型聊天。'
            self.catalog_hint.setText(note)
        else:
            self.catalog_hint.setText('')
        self.catalog_hint.setVisible(provider == 'openai')
        self.model.blockSignals(True)
        self.model.clear()
        for row in rows:
            label = row["id"] + ("  [图片]" if row["vision"] else "") + ("  [免费]" if row["free"] else "") + ("  [免费额度适用]" if row.get("allowance") else "")
            self.model.addItem(label, row)
            self.model.setItemData(self.model.count()-1, label + "\n" + row["name"], QtCore.Qt.ItemDataRole.ToolTipRole)
        target = next((index for index, row in enumerate(rows) if row["id"] == selected), None)
        if target is not None:
            self.model.setCurrentIndex(target)
        elif selected:
            self.model.setCurrentText(selected)
        elif rows:
            self.model.setCurrentIndex(0)
        else:
            self.model.setCurrentText({"openai": "gpt-6-luna", "mistral": "mistral-small-latest",
                                       "groq": "openai/gpt-oss-120b"}.get(provider, ""))
        self.model.blockSignals(False)
        self.model.lineEdit().setCursorPosition(0)
        self.model.setToolTip(self.model_id() + '\n切换仅影响下一条请求；已有对话内容保留，不能用模型自我介绍判断真实模型。')
        self.sync_capabilities()

    @safe_ui
    def refresh_models(self, _checked=False):
        if self.handle:
            return
        provider, config = self.provider.currentData(), self.connection_snapshot()
        identity = self.sync_openai_identity(config) if provider == 'openai' else None
        if provider == 'openai':
            if not identity:
                raise ValueError('请先在程序设置 → AI 模型试用台中保存 OpenAI Key。')
            self.catalog_attempts.add(identity)
            self.catalog_errors.pop(provider, None)
        # Capture a plain service, not this dialog, in the worker closure.
        client = self.client
        runner = lambda payload, job: {"provider": payload["provider"], "models": client.list_models(payload["provider"], payload["config"], job, force=True)}
        self.submit("读取 " + PROVIDERS[provider] + " 模型", runner, {"provider": provider, "config": pin_provider_config(config,provider)},
                    {"type": "models", "provider": provider, "identity": identity})

    def submit(self, title, runner, payload, operation):
        handle = ClientWorker(runner, payload, self)
        self.handle, self.operation = handle, operation
        self.last_status = "操作结束。"
        self.started = time.monotonic()
        queued = QtCore.Qt.ConnectionType.QueuedConnection
        handle.completed.connect(lambda result, current=handle: self.completed(current, result), queued)
        handle.failed.connect(lambda message, current=handle: self.failed(current, message), queued)
        handle.diagnostic.connect(lambda _message: None if self.is_private else self.context.log('AI试用台后台异常；未将对话内容写入日志。', logging.ERROR), queued)
        connect_thread_finished(handle, self, "handle", self.finished, pass_thread=True)
        handle.progressChanged.connect(lambda text, _percent, current=handle: self.progress(current, text), queued)
        self.set_busy(True)
        handle.start()

    def set_busy(self, busy):
        self.transcript.set_busy(busy)
        self.batch_check_button.setEnabled(not busy and not is_offline())
        self.send_button.setEnabled(not busy and not is_offline())
        self.stop_button.setEnabled(busy)
        self.stop_button.setVisible(busy)
        self.refresh_button.setEnabled(not busy and not is_offline())
        self.attach_button.setEnabled(not busy)
        self.retry_button.setEnabled(not busy and not is_offline())
        self.delete_button.setEnabled(not busy)
        purging = busy and self.operation and self.operation.get('type') == 'purge'
        self.history.setEnabled(not purging)
        self.input.setReadOnly(bool(purging))
        if purging:
            self.stop_button.setEnabled(False)
        if busy:
            self.stage = '正在彻底删除本机对话与备份' if purging else '正在请求模型'
            self.elapsed_timer.start()
        else:
            self.elapsed_timer.stop()

    def show_elapsed(self):
        suffix = ('；关闭无痕窗口会取消本机请求并清除内存记录。' if self.is_private else '；关闭此窗口不会丢失请求。')
        if self.operation and self.operation.get('type') == 'purge':
            suffix = '；正在完成删除清理。'
        self.status.setText(self.stage + f" · {int(time.monotonic()-self.started)} 秒" + suffix)

    @safe_ui
    def show_stream(self):
        if not self.stream or not self.operation or self.operation.get("conversation") != self.current_id:
            return
        text = self.stream.text()
        if len(text) <= self.stream_displayed:
            return
        self.transcript.update_stream(text, self.markdown.isChecked())
        self.stream_displayed = len(text)
        self.stage = "正在流式输出"
        self.show_elapsed()

    def progress(self, handle, text):
        if handle is self.handle:
            self.stage = text
            self.show_elapsed()

    @safe_ui
    def send(self, _checked=False, retry_id=None, regenerate_id=None, edit_id=None, edited_text=None):
        if self.handle:
            return
        provider = self.provider.currentData()
        model = validate_model(provider, self.model_id())
        source_prompt_id = retry_id
        if edit_id is not None:
            row = self.store.editable_prompt(self.current_id, edit_id)
            if not isinstance(edited_text, str):
                raise ValueError('请填写修改后的消息。')
            text, images, source_prompt_id = edited_text.strip(), row['images'], row['id']
        elif regenerate_id is not None:
            row = self.store.regeneration_prompt(self.current_id, regenerate_id)
            text, images, source_prompt_id = row['text'], row['images'], row['id']
        elif retry_id is not None:
            row = next(row for row in self.store.messages(self.current_id) if row["id"] == retry_id)
            text, images = row["text"], row["images"]
        else:
            text, images = self.input.toPlainText().strip(), list(self.pending_images)
        if not text and not images:
            self.status.setText("先输入文字或添加图片。")
            return
        if len(text) > 128000:
            raise ValueError("单条消息超过128000字符，请分段或缩短。")
        if not self.consent:
            answer = QtWidgets.QMessageBox.question(self, "确认发送到云端",
                "文字、上下文和图片将发送到所选服务商，不是本机模型。API计费及数据使用规则由服务商和账号决定。\n\n本次打开客户端后不再重复提示。是否继续？")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.consent = True
        # Validate and pin the chosen Key before creating a pending message.
        # An unselected/deleted active key must not strand a fake running turn.
        request_config=pin_provider_config(self.connection_snapshot(),provider)
        request_options=self.request_options()
        batch = request_options.get('request_mode') == 'batch'
        if batch and provider != 'openai':
            raise ValueError('Batch目前只支持OpenAI官方接口。')
        if batch and request_options.get('web_search'):
            raise ValueError('本版Batch暂不支持联网工具，请关闭联网或使用即时聊天。')
        self.store.context_preview(self.current_id, text, images, request_options, before_prompt_id=source_prompt_id)
        if self.current_id is None:
            self.current_id = self.store.create(provider, model, request_options)
            self.reload_history()
        self.save_draft()
        identifier = self.current_id
        prompt_id = self.store.begin_request(identifier, text, images, provider, model, retry_id, regenerate_id, edit_id)
        if retry_id is None and regenerate_id is None and edit_id is None:
            self.loading = True
            self.input.clear()
            self.pending_images = []
            self.render_attachments()
            self.loading = False
        payload = {"conversation": identifier, "prompt_id": prompt_id, "provider": provider,
                   "model": model, "options": request_options, "config": request_config}
        self.stream = StreamBuffer() if payload["options"]["stream"] and not batch else None
        self.stream_displayed = 0
        try:
            if batch:
                from .batch_chat import submit_batch
                runner = functools.partial(submit_batch, store=self.store)
            else:
                runner = functools.partial(run_chat, store=self.store, client=self.client, stream=self.stream)
            self.submit("AI 对话 · " + PROVIDERS[provider] + " / " + model,
                        runner, payload,
                        {"type": "chat_batch" if batch else 'chat', "conversation": identifier, "prompt_id": prompt_id,
                         "provider": provider, "model": model})
            if self.stream:
                self.stream_timer.start()
        except Exception as error:
            self.stream = None
            self.store.fail_request(identifier, prompt_id, str(error))
            raise
        self.reload_history()
        self.render_transcript()
        self.transcript.follow = True
        return prompt_id

    @safe_ui
    def retry_request(self, _checked=False):
        if self.handle:
            self.status.setText('请等待当前操作完成后再重试。')
            return
        if is_offline():
            self.status.setText('离线模式下不能重试请求。')
            return
        rows = self.store.messages(self.current_id)
        if not rows:
            self.status.setText('当前对话还没有可以重试的消息。')
            return
        latest = rows[-1]
        if latest['role'] == 'assistant' and latest['status'] == 'done':
            self.regenerate_answer(latest['id'])
        elif latest['role'] == 'user' and latest['status'] not in {'done', 'pending'}:
            self.send(retry_id=latest['id'])
        elif latest['status'] == 'pending':
            self.status.setText('当前请求还在等待回答；Batch请求可从顶部Batch菜单检查结果。')
        else:
            self.status.setText('最新消息尚无可重新生成的完整回答，请先检查请求状态。')

    @safe_ui
    def stop_request(self, _checked=False):
        if self.operation and self.operation.get('type') == 'purge':
            self.status.setText('正在完成彻底删除清理，请等待结束。')
            return
        if self.handle:
            self.handle.requestInterruption()
            self.stage = "已请求取消，等待网络步骤安全结束（读取超时60秒）"
            self.show_elapsed()

    @safe_ui
    def choose_images(self, _checked=False):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.jfif *.tif *.tiff *.avif)")
        if paths:
            self.add_images(paths)

    @safe_ui
    def add_images(self, sources):
        if self.handle:
            self.status.setText("请等待当前操作完成后再添加图片。")
            return
        if len(sources) + len(self.pending_images) > 4:
            raise ValueError("每条消息最多4张图片；可分多条消息发送。")
        self.save_draft()
        self.submit("准备聊天图片", functools.partial(run_images, store=self.store),
                    {"conversation": self.current_id, "sources": sources}, {"type": "images"})

    @safe_ui
    def completed(self, handle, output):
        if handle is not self.handle:
            return
        self.stream_timer.stop()
        self.stream = None
        if self._privacy_closing:
            return
        if self.operation['type'] == 'purge':
            self._after_purge()
            self.last_status = '本机程序管理的对话、全部分支、草稿、未共享附件与自动对话备份已清理。' if not self.is_private else '此无痕对话已从内存清除。'
            return
        if self.operation["type"] == "models":
            provider = output["provider"]
            if provider == 'openai' and self.operation.get('identity') != openai_identity(self.connection_snapshot()):
                self.last_status = '连接设置已更改，已忽略旧密钥的模型列表；即将读取新配置。'
                return
            self.catalogs[provider] = output["models"]
            self.catalog_identities[provider] = self.operation.get('identity')
            self.catalog_errors.pop(provider, None)
            if self.provider.currentData() == provider:
                self.fill_models(self.model_id())
            self.last_status = f"已读取 {len(output['models'])} 个模型。列表标注图片/免费信息；可用性仍以实际调用为准。"
        elif self.operation["type"] == "images":
            row = self.store.get(output["conversation"])
            combined = list(row["images"])
            for image in output["images"]:
                if not any(previous["file"] == image["file"] for previous in combined):
                    combined.append(image)
            self.store.update(row["id"], images=combined)
            if row["id"] == self.current_id:
                self.pending_images = combined
                self.render_attachments()
            self.last_status = "图片副本已就绪；右键可移除，不会删除原图。"
        elif self.operation['type'] in {'chat_batch', 'batch_check'}:
            self.last_status = ('Batch记录已更新。云端任务独立运行；稍后用顶部“Batch → 检查”收取结果，未知提交不会自动重发。')
            if output['conversation'] == self.current_id:
                self.render_transcript()
        else:
            result = output["result"]
            usage = result["usage"]
            identity = (f"请求：{result.get('requested_model') or self.operation.get('model') or result['model']} · "
                        f"接口返回：{result.get('reported_model') or '未提供模型 ID'}")
            self.last_status = f"{identity} · {result['elapsed']:.2f}秒 · 输入 {usage['input']} / 输出 {usage['output']} tokens。" + result["warning"]
            if result.get('reasoning_tokens'):
                self.last_status+=f" 思考 Token：{result['reasoning_tokens']}（已包含在输出中）。"
            if result.get('web_enabled'):
                count=result.get('search_calls')
                self.last_status+=f" 联网搜索：{count}次。" if count is not None else ' 已允许联网；接口未提供调用次数。'
            if output["conversation"] == self.current_id:
                self.render_transcript()
        self.reload_history()

    @safe_ui
    def failed(self, handle, message):
        if handle is not self.handle:
            return
        self.stream_timer.stop()
        self.stream = None
        self.last_status = "未完成：" + message
        if not self.is_private:
            self.context.log('AI试用台请求未完成；未将对话或接口错误正文写入日志。', logging.WARNING)
        if self._privacy_closing:
            return
        if self.operation['type'] == 'purge':
            self._after_purge()
            self.last_status = '彻底删除清理尚未全部完成：' + message + '。可再次使用“彻底删除”重试清理。'
            return
        if self.operation["type"] in {'chat', 'chat_batch'}:
            cloud_records = self.store.batches(self.operation['conversation']) if self.operation['type'] == 'chat_batch' else []
            submitted = any(item['prompt_id'] == self.operation['prompt_id'] and item['state'] not in {'failed', 'expired', 'cancelled'} for item in cloud_records)
            if not submitted:
                self.store.fail_request(self.operation["conversation"], self.operation["prompt_id"], message,
                                        "取消" in message)
            self.render_transcript()
        elif self.operation['type'] == 'models' and self.operation.get('provider') == 'openai':
            if self.operation.get('identity') == openai_identity(self.connection_snapshot()):
                self.catalog_errors['openai'] = message
                if self.provider.currentData() == 'openai':
                    self.fill_models(self.model_id())

    @safe_ui
    def finished(self, handle):
        if handle is self.handle:
            self.stream_timer.stop()
            self.stream = None
            self.handle = self.operation = None
            if self._privacy_closing:
                self.store.close_private()
                self.current_id = None
                self.deleteLater()
                return
            self.set_busy(False)
            self.status.setText(getattr(self, "last_status", "操作结束。"))
            self.catalog_timer.start(0)
            self.preview_timer.start()

    def render_attachments(self):
        self.attachments.clear()
        for image in self.pending_images:
            resource = self.store.asset_resource(image)
            pixmap = QtGui.QPixmap()
            if isinstance(resource,bytes):
                pixmap.loadFromData(resource)
            else:
                pixmap.load(str(resource))
            pixmap = pixmap.scaled(84, 65, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            item = QtWidgets.QListWidgetItem(QtGui.QIcon(pixmap), image["name"])
            item.setToolTip(f"{image['name']} · {image['width']}×{image['height']}\n右键移除附件")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, image["file"])
            self.attachments.addItem(item)
        self.attachments.setVisible(bool(self.pending_images))
        if hasattr(self, 'preview_timer'):
            self.preview_timer.start()

    @safe_ui
    def attachment_menu(self, position):
        if self.handle:
            return
        menu = QtWidgets.QMenu(self.attachments)
        remove = menu.addAction("移除所选图片")
        clear = menu.addAction("清空本条附件")
        chosen = menu.exec(self.attachments.mapToGlobal(position))
        selected = {item.data(QtCore.Qt.ItemDataRole.UserRole) for item in self.attachments.selectedItems()}
        if chosen is clear:
            self.pending_images = []
        elif chosen is remove:
            self.pending_images = [image for image in self.pending_images if image["file"] not in selected]
        self.render_attachments()
        self.save_draft()

    def render_transcript(self):
        rows = self.store.messages(self.current_id) if self.current_id else []
        branches = self.store.branch_navigation(self.current_id) if self.current_id else {}
        batches = {item['prompt_id']: item for item in self.store.batches(self.current_id)} if self.current_id else {}
        self.allowed_images = {}
        display_rows = []
        for original in rows:
            row = dict(original)
            row['_branch'] = branches.get(row['id'])
            batch = batches.get(row['id'])
            if batch and row['status'] == 'pending':
                row['status'] = 'Batch · ' + batch['state'] + (' · ' + batch['error'] if batch['error'] else ' · 顶部Batch菜单可检查结果')
            attachments = []
            for image in row["images"]:
                try:
                    path = self.store.asset_resource(image)
                except ValueError:
                    attachments.append("<p>[本地图片已丢失]</p>")
                    continue
                url = ('chat-image:///' + image['file']) if isinstance(path,bytes) else QtCore.QUrl.fromLocalFile(str(path)).toString()
                self.allowed_images[url] = path
                scale = min(180 / max(1, image["width"]), 180 / max(1, image["height"]))
                width, height = max(1, int(image["width"] * scale)), max(1, int(image["height"] * scale))
                attachments.append(f'<p><a href="{html.escape(url, quote=True)}"><img src="{html.escape(url, quote=True)}" width="{width}" height="{height}"></a><br>{html.escape(image["name"])}</p>')
            row['_attachments'] = ''.join(attachments)
            display_rows.append(row)
            partial = row['metadata'].get('partial') or {}
            if partial.get('text'):
                display_rows.append(dict(id='partial-' + str(row['id']), role='assistant',
                    text=partial['text'], provider=partial.get('provider', row['provider']),
                    model=partial.get('model', row['model']), metadata={},
                    status='未完成（不计入后续上下文）'))
        self.stream_start_pos = None
        if self.stream and self.operation and self.operation.get('conversation') == self.current_id and any(
                row['id'] == self.operation.get('prompt_id') and row['status'] == 'pending' for row in rows):
            content = self.stream.text()
            display_rows.append(dict(id='stream', role='assistant', text=content,
                provider=self.operation.get('provider', self.provider.currentData()),
                model=self.operation.get('model', self.model_id()),
                metadata={key:self.operation[key] for key in ('group_member','group_round') if key in self.operation},status='生成中…'))
            self.stream_start_pos = 0
            self.stream_displayed = len(content)
        self.transcript.allowed_images = self.allowed_images.copy()
        changed = getattr(self, '_displayed_conversation', None) != self.current_id
        self._displayed_conversation = self.current_id
        self.transcript.set_messages(display_rows, self.markdown.isChecked(), force_bottom=changed)
        if self.current_id:
            self.conversation_title.setText(self.store.get(self.current_id)['title'])

    def jump_to_latest(self):
        self.transcript.follow = True
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def update_jump_button(self, *_args):
        self.jump_button.setVisible(not self.transcript.near_bottom())

    def filter_history(self, *_args):
        query = self.history_search.text().strip().casefold()
        for index in range(self.history.count()):
            item = self.history.item(index)
            item.setHidden(bool(query) and query not in (item.text() + item.toolTip()).casefold())

    @safe_ui
    def open_attachment(self, url):
        path = self.allowed_images.get(url.toString())
        if isinstance(path,bytes):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle('无痕图片预览')
            layout = QtWidgets.QVBoxLayout(dialog)
            label = QtWidgets.QLabel(dialog)
            pixmap = QtGui.QPixmap(); pixmap.loadFromData(path)
            label.setPixmap(pixmap.scaled(1000,800,QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(label)
            dialog.exec()
        elif path and path.is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        elif is_web_link(url):
            require_online("打开回答中的网页链接")
            QtGui.QDesktopServices.openUrl(url)
        else:
            self.status.setText("此链接无法打开：仅支持网页或本对话中已保存的图片附件。")

    @safe_ui
    def connection_settings(self, _checked=False):
        snapshot = self.context.load_config()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("AI 连接设置")
        dialog.resize(780, 540)
        layout = QtWidgets.QVBoxLayout(dialog)
        page = ClientSettingsPage(self.context, dialog)
        layout.addWidget(page.widget)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
        error_label = QtWidgets.QLabel(dialog)
        error_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        error_label.setWordWrap(True)
        layout.addWidget(error_label)
        def save():
            try:
                import copy
                config = copy.deepcopy(snapshot)
                page.update_config(config)
                updates = {key: config[key] for key in (KEYS_CONFIG_KEY, CONFIG_KEY) if key in config and config.get(key) != snapshot.get(key)}
                if updates and not self.context.patch_config(updates, expected=snapshot):
                    raise ValueError("连接配置未保存，请重新打开设置。")
                dialog.accept()
            except Exception as error:
                error_label.setText(redact_sensitive_text(str(error)))
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.status.setText("连接设置已保存；Gemini 原有密钥未改动。刷新模型即可测试。")
            self.settings_changed()

    @safe_ui
    def rename_conversation(self, _checked=False):
        text, accepted = QtWidgets.QInputDialog.getText(self, "重命名对话", "标题", text=self.store.get(self.current_id)["title"])
        if accepted and text.strip():
            self.store.update(self.current_id, title=text.strip()[:100])
            self.reload_history()

    @safe_ui
    def delete_conversation(self, _checked=False):
        if self.handle:
            self.status.setText('请等待当前操作结束后再彻底删除对话。')
            return
        queued = self.store.pending_purges()
        target = self.current_id or (queued[0] if queued else None)
        if target is None:
            self.status.setText('没有可删除的对话或待完成清理。')
            return
        text = ('永久清除这段对话在本程序中的全部分支、消息、草稿、未共享图片，以及程序自动生成的对话数据库备份中的对应记录。此操作不能撤销。\n\n'
                '范围限本机聊天数据库、自动对话备份及未共享附件；旧版本运行日志、手动导出、系统备份、硬盘底层残留和服务商云端不在此范围。')
        if self.is_private:
            text = '清除此无痕对话及其全部分支、草稿和图片内存数据？不能撤销。'
        pending = [batch for batch in self.store.batches(target) if batch['state'] not in {'completed','failed','expired','cancelled'}]
        if pending:
            text += '\n\n此对话仍有云端Batch任务。删除本机记录不会停止云端任务；如需取消，请先从Batch菜单操作。删除后将无法再从这里收取这些结果。'
        if QtWidgets.QMessageBox.question(self, '彻底删除对话', text,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.draft_timer.stop()
        self.preview_timer.stop()
        self.loading = True
        self.submit('彻底删除本机对话',functools.partial(run_purge,store=self.store),
                    {'conversation':target},{'type':'purge','conversation':target})

    def _after_purge(self):
        self.draft_timer.stop()
        self.preview_timer.stop()
        self.loading = True
        self.current_id = None
        self.pending_images = []
        self.input.clear()
        self.render_attachments()
        self.loading = False
        rows = self.store.conversations()
        self.reload_history(rows[0]['id'] if rows else None)
        if not rows:
            self.render_transcript()
            self.conversation_title.setText('新对话')
            self.request_hint.setText('已清除本机对话。输入新消息时才会建立新记录。')

    @safe_ui
    def export_conversation(self, _checked=False):
        if self.is_private:
            self.status.setText('无痕聊天不导出本机记录。手动复制到其他应用会留下外部副本。')
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出对话（不含密钥）", "对话.txt", "文本 (*.txt)")
        if filename:
            data_file(Path(filename)).write_text(self.store.export_text(self.current_id), encoding="utf-8")
            self.status.setText("对话已导出：" + filename)

    @safe_ui
    def copy_answer(self, _checked=False):
        rows = self.store.messages(self.current_id)
        answers = [row for row in rows if row["role"] == "assistant"]
        if self.stream and self.operation and self.operation.get("conversation") == self.current_id and self.stream.text():
            set_internal_clipboard_text(self.stream.text())
            self.status.setText("当前已收到的回答已复制（尚未完成）。")
            return
        partial = rows[-1]["metadata"].get("partial") if rows else None
        if partial and partial.get("text"):
            set_internal_clipboard_text(partial["text"])
            self.status.setText("最新未完成回答已复制。")
            return
        if answers:
            set_internal_clipboard_text(text_with_sources(answers[-1]["text"],answers[-1]['metadata']))
            self.status.setText("最新回答已复制。")

    @safe_ui
    def open_history_folder(self, _checked=False):
        if self.is_private:
            self.status.setText('无痕聊天仅在内存中，没有本地记录目录。')
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.store.root)))

    @safe_ui
    def history_menu(self, position):
        item = self.history.itemAt(position)
        if item:
            self.history.setCurrentItem(item)
        menu = QtWidgets.QMenu(self.history)
        for title, callback in (("新建对话", self.new_conversation), ("重命名…", self.rename_conversation),
                                ("导出文本…", self.export_conversation), ("彻底删除…", self.delete_conversation)):
            menu.addAction(title, callback)
        menu.exec(self.history.mapToGlobal(position))

    def closeEvent(self, event):
        self.catalog_timer.stop()
        self.preview_timer.stop()
        self.draft_timer.stop()
        for dialog in list(self._account_dialogs):dialog.close()
        if self.is_private:
            self._privacy_closing = True
            if self.handle:
                if not self.operation or self.operation.get('type') != 'purge':
                    self.handle.requestInterruption()
            else:
                self.store.close_private()
                self.current_id = None
                self.deleteLater()
        else:
            self.save_draft()
            for dialog in [*self._private_dialogs,*self._group_dialogs]:
                dialog.close()
        event.accept()

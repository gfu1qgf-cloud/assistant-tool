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
from model.AIProviderKeys import KEYS_CONFIG_KEY
from model.ClipboardHelper import set_internal_clipboard_text
from model.OfflineMode import is_offline, require_online
from model.SensitiveData import redact_sensitive_text
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.QtThreadLifecycle import connect_thread_finished
from .settings import CONFIG_KEY, ClientSettingsPage, default_options
from .rendering import message_html, is_web_link


def safe_ui(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except Exception as error:
            self.status.setText(redact_sensitive_text(str(error)))
            self.context.log("AI 客户端操作失败：\n" + traceback.format_exc(), logging.ERROR)
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
        messages = store.request_messages(payload["conversation"], payload["prompt_id"], payload["options"]["rounds"])
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


class Transcript(QtWidgets.QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.allowed_images = {}

    def loadResource(self, resource_type, name):
        path = self.allowed_images.get(name.toString())
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource.value and path and path.is_file():
            return QtGui.QImage(str(path))
        return None

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):
            set_internal_clipboard_text(self.textCursor().selectedText().replace("\u2029", "\n"))
            return
        super().keyPressEvent(event)


class AIClientDialog(QtWidgets.QDialog):
    def __init__(self, context, store, parent=None):
        super().__init__(parent)
        self.context, self.store = context, store
        self.client = context.ai_client
        self.current_id = None
        self.pending_images = []
        self.handle = self.operation = None
        self.stream = None
        self.stream_displayed = 0
        self.stream_start_pos = None
        self.catalogs = {}
        self.loading = False
        self.consent = False
        self.setWindowTitle("AI 模型试用台 · LZX")
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1180, 820)
        self.setMinimumSize(760, 580)
        layout = QtWidgets.QVBoxLayout(self)
        menu = QtWidgets.QMenuBar(self)
        layout.setMenuBar(menu)
        conversation_menu = menu.addMenu("对话")
        for title, callback, shortcut in (
            ("新建对话", self.new_conversation, "Ctrl+N"), ("重命名…", self.rename_conversation, ""),
            ("删除本段对话…", self.delete_conversation, ""), ("导出对话文本…", self.export_conversation, ""),
            ("打开本地记录目录", self.open_history_folder, ""), ("复制最新回答", self.copy_answer, ""),
        ):
            action = conversation_menu.addAction(title)
            action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(QtGui.QKeySequence(shortcut))
        connection_menu = menu.addMenu("连接")
        connection_menu.addAction("连接设置…", self.connection_settings)
        connection_menu.addAction("管理 Gemini 密钥…", self.open_gemini)
        connection_menu.addAction("刷新模型列表", self.refresh_models)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.history = QtWidgets.QListWidget(splitter)
        self.history.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.history.setMinimumWidth(180)
        self.history.setMaximumWidth(360)
        self.history.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self.history_menu)
        right = QtWidgets.QWidget(splitter)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
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
        self.model.setMinimumWidth(220)
        self.refresh_button = QtWidgets.QPushButton("刷新模型", right)
        self.options_button = QtWidgets.QToolButton(right)
        self.options_button.setText("参数 ▾")
        selectors.addWidget(self.provider)
        selectors.addWidget(self.model, 1)
        selectors.addWidget(self.refresh_button)
        selectors.addWidget(self.options_button)
        right_layout.addLayout(selectors)
        self.options_panel = QtWidgets.QWidget(right)
        options_layout = QtWidgets.QFormLayout(self.options_panel)
        options_layout.setContentsMargins(0, 2, 0, 4)
        self.system = QtWidgets.QPlainTextEdit(self.options_panel)
        self.system.setMaximumHeight(85)
        self.system.setPlaceholderText("可选：给模型的固定要求；留空使用模型默认行为")
        options_layout.addRow("系统提示", self.system)
        numbers = QtWidgets.QHBoxLayout()
        self.rounds = QtWidgets.QSpinBox(self.options_panel)
        self.rounds.setRange(1, 50)
        self.rounds.setSuffix(" 轮")
        self.maximum = QtWidgets.QSpinBox(self.options_panel)
        self.maximum.setRange(128, 32768)
        self.maximum.setSingleStep(512)
        self.temperature_enabled = QtWidgets.QCheckBox("自定温度", self.options_panel)
        self.temperature = QtWidgets.QDoubleSpinBox(self.options_panel)
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(.1)
        self.temperature.setValue(.7)
        numbers.addWidget(QtWidgets.QLabel("上下文", self.options_panel))
        numbers.addWidget(self.rounds)
        numbers.addWidget(QtWidgets.QLabel("输出上限", self.options_panel))
        numbers.addWidget(self.maximum)
        options_layout.addRow(numbers)
        temp_row = QtWidgets.QHBoxLayout()
        temp_row.addWidget(self.temperature_enabled)
        temp_row.addWidget(self.temperature)
        temp_row.addStretch(1)
        options_layout.addRow(temp_row)
        self.free_only = QtWidgets.QCheckBox("OpenRouter 只用免费模型", self.options_panel)
        self.foreign_only = QtWidgets.QCheckBox("筛选国外厂商", self.options_panel)
        self.cf_free_only = QtWidgets.QCheckBox("Cloudflare 仅用已核实免费额度适用款", self.options_panel)
        self.cf_free_only.setToolTip("并非零收费保证；Paid账户超出每日10000 Neurons可能继续计费。")
        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(self.free_only)
        filters.addWidget(self.foreign_only)
        options_layout.addRow(filters)
        options_layout.addRow(self.cf_free_only)
        self.streaming = QtWidgets.QCheckBox("流式显示：边生成边显示", self.options_panel)
        self.streaming.setChecked(True)
        self.streaming.setToolTip("不支持流式的模型可手动关闭；不会在失败后自动重发或换成付费模型。")
        options_layout.addRow(self.streaming)
        self.markdown = QtWidgets.QCheckBox("Markdown 排版（标题、列表、表格、代码块）", self.options_panel)
        self.markdown.setChecked(True)
        self.markdown.setToolTip("仅改变显示；复制最新回答和导出文本仍保留原文。链接需点击才打开，远程图片不会自动加载。")
        options_layout.addRow(self.markdown)
        self.options_panel.hide()
        self.options_button.clicked.connect(lambda: self.options_panel.setVisible(not self.options_panel.isVisible()))
        right_layout.addWidget(self.options_panel)
        hint = QtWidgets.QLabel("聊天记录保存在本机；点击发送会把上下文和图片发给所选云服务。图片会保存静态副本（长边不超过2048），原文件不变。", right)
        hint.setWordWrap(True)
        right_layout.addWidget(hint)
        self.billing_hint = QtWidgets.QLabel("", right)
        self.billing_hint.setWordWrap(True)
        right_layout.addWidget(self.billing_hint)
        self.transcript = Transcript(right)
        self.transcript.setOpenLinks(False)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.anchorClicked.connect(self.open_attachment)
        right_layout.addWidget(self.transcript, 1)
        self.attachments = QtWidgets.QListWidget(right)
        self.attachments.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.attachments.setIconSize(QtCore.QSize(84, 65))
        self.attachments.setMaximumHeight(105)
        self.attachments.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.attachments.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.attachments.customContextMenuRequested.connect(self.attachment_menu)
        self.attachments.hide()
        right_layout.addWidget(self.attachments)
        self.input = ChatInput(right)
        self.input.setPlaceholderText("输入消息；Ctrl+Enter 发送，Enter 换行。可拖入图片或 Ctrl+V 粘贴图片。")
        self.input.setMinimumHeight(80)
        self.input.setMaximumHeight(145)
        right_layout.addWidget(self.input)
        buttons = QtWidgets.QHBoxLayout()
        self.attach_button = QtWidgets.QPushButton("附加图片…", right)
        self.retry_button = QtWidgets.QPushButton("重试未成功消息", right)
        self.stop_button = QtWidgets.QPushButton("停止", right)
        self.send_button = QtWidgets.QPushButton("发送", right)
        self.send_button.setDefault(False)
        self.send_button.setAutoDefault(False)
        for button in (self.attach_button, self.retry_button, self.stop_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.send_button)
        right_layout.addLayout(buttons)
        splitter.addWidget(self.history)
        splitter.addWidget(right)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([230, 900])
        layout.addWidget(splitter, 1)
        self.status = QtWidgets.QLabel(self)
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
        self.history.currentItemChanged.connect(self.select_conversation)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.model.currentTextChanged.connect(self.schedule_draft)
        self.input.textChanged.connect(self.schedule_draft)
        self.system.textChanged.connect(self.schedule_draft)
        for widget in (self.rounds, self.maximum, self.temperature):
            widget.valueChanged.connect(self.schedule_draft)
        for widget in (self.free_only, self.foreign_only, self.cf_free_only):
            widget.toggled.connect(self.filter_changed)
        self.temperature_enabled.toggled.connect(self.schedule_draft)
        self.streaming.toggled.connect(self.schedule_draft)
        self.markdown.toggled.connect(self.presentation_changed)
        self.temperature_enabled.toggled.connect(self.temperature.setEnabled)
        self.refresh_button.clicked.connect(self.refresh_models)
        self.send_button.clicked.connect(self.send)
        self.input.sendRequested.connect(self.send)
        self.stop_button.clicked.connect(self.stop_request)
        self.attach_button.clicked.connect(self.choose_images)
        self.input.imagesAdded.connect(self.add_images)
        self.input.inputError.connect(self.status.setText)
        self.retry_button.clicked.connect(self.retry_request)
        existing = store.conversations()
        self.reload_history(existing[0]["id"] if existing else None)
        if not existing:
            self.new_conversation()
        self.set_busy(False)
        self.status.setText("离线模式：可以查看历史，不能请求模型。" if is_offline() else "从“连接 → 连接设置”添加服务密钥，再刷新并选择模型。")

    def options(self):
        return {"system": self.system.toPlainText(), "rounds": self.rounds.value(),
                "max_tokens": self.maximum.value(),
                "temperature": self.temperature.value() if self.temperature_enabled.isChecked() else None,
                "free_only": self.free_only.isChecked(), "foreign_only": self.foreign_only.isChecked(),
                "cf_free_only": self.cf_free_only.isChecked(), "stream": self.streaming.isChecked(),
                "render_markdown": self.markdown.isChecked()}

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
            self.system.setPlainText(options["system"])
            self.rounds.setValue(options["rounds"])
            self.maximum.setValue(options["max_tokens"])
            self.temperature_enabled.setChecked(options["temperature"] is not None)
            self.temperature.setEnabled(options["temperature"] is not None)
            self.temperature.setValue(options["temperature"] if options["temperature"] is not None else .7)
            self.free_only.setChecked(options["free_only"])
            self.foreign_only.setChecked(options["foreign_only"])
            self.cf_free_only.setChecked(options["cf_free_only"])
            self.streaming.setChecked(options.get("stream", True))
            self.markdown.setChecked(options.get("render_markdown", True))
            self.fill_models(row["model"])
            self.input.setPlainText(row["draft"])
            self.pending_images = row["images"]
            self.render_attachments()
            self.render_transcript()
        finally:
            self.loading = False

    def schedule_draft(self, *_args):
        if not self.loading and self.current_id:
            self.draft_timer.start()

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

    @safe_ui
    def provider_changed(self, *_args):
        if not self.loading:
            self.fill_models("")
            self.schedule_draft()

    @safe_ui
    def filter_changed(self, *_args):
        if not self.loading:
            self.fill_models(self.model_id())
            self.schedule_draft()

    def fill_models(self, selected=""):
        provider = self.provider.currentData()
        rows = self.client.filter_models(provider, self.catalogs.get(provider, []), free_only=self.free_only.isChecked(), foreign_only=self.foreign_only.isChecked(), cf_free_only=self.cf_free_only.isChecked())
        billing_notes = {
            "cloudflare": "Cloudflare：每天10000 Neurons（00:00 UTC重置），不是tokens。Free账户超额停止；Paid账户超额可能计费。此处不显示/猜测剩余额度。",
            "groq": "Groq：Free 账号有组织级速率及每日限额；Developer 账号可能计费。此处不猜测剩余额度；图片模型若未列出，可关闭“国外厂商”筛选。",
        }
        self.billing_hint.setText(billing_notes.get(provider, ""))
        self.billing_hint.setVisible(provider in billing_notes)
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
            self.model.setCurrentText({"mistral": "mistral-small-latest",
                                       "groq": "openai/gpt-oss-120b"}.get(provider, ""))
        self.model.blockSignals(False)
        self.model.lineEdit().setCursorPosition(0)
        self.model.setToolTip(self.model_id())

    @safe_ui
    def refresh_models(self, _checked=False):
        if self.handle:
            return
        provider, config = self.provider.currentData(), self.connection_snapshot()
        # Capture a plain service, not this dialog, in the worker closure.
        client = self.client
        runner = lambda payload, job: {"provider": payload["provider"], "models": client.list_models(payload["provider"], payload["config"], job, force=True)}
        self.submit("读取 " + PROVIDERS[provider] + " 模型", runner, {"provider": provider, "config": config}, {"type": "models"})

    def submit(self, title, runner, payload, operation):
        handle = ClientWorker(runner, payload, self)
        self.handle, self.operation = handle, operation
        self.last_status = "操作结束。"
        self.started = time.monotonic()
        queued = QtCore.Qt.ConnectionType.QueuedConnection
        handle.completed.connect(lambda result, current=handle: self.completed(current, result), queued)
        handle.failed.connect(lambda message, current=handle: self.failed(current, message), queued)
        handle.diagnostic.connect(lambda message: self.context.log("AI 试用台异常：\n" + message, logging.ERROR), queued)
        connect_thread_finished(handle, self, "handle", self.finished, pass_thread=True)
        handle.progressChanged.connect(lambda text, _percent, current=handle: self.progress(current, text), queued)
        self.set_busy(True)
        handle.start()

    def set_busy(self, busy):
        self.send_button.setEnabled(not busy and not is_offline())
        self.stop_button.setEnabled(busy)
        self.refresh_button.setEnabled(not busy and not is_offline())
        self.attach_button.setEnabled(not busy)
        self.retry_button.setEnabled(not busy and not is_offline())
        if busy:
            self.stage = "正在请求模型"
            self.elapsed_timer.start()
        else:
            self.elapsed_timer.stop()

    def show_elapsed(self):
        self.status.setText(self.stage + f" · {int(time.monotonic()-self.started)} 秒；关闭此窗口不会丢失请求。")

    @safe_ui
    def show_stream(self):
        if not self.stream or not self.operation or self.operation.get("conversation") != self.current_id:
            return
        text = self.stream.text()
        if len(text) <= self.stream_displayed:
            return
        scrollbar = self.transcript.verticalScrollBar()
        scroll_value = scrollbar.value()
        follow = scrollbar.value() >= scrollbar.maximum() - 20
        cursor = QtGui.QTextCursor(self.transcript.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if self.markdown.isChecked():
            if self.stream_start_pos is None:
                return
            cursor.setPosition(self.stream_start_pos, QtGui.QTextCursor.MoveMode.KeepAnchor)
            cursor.insertHtml(message_html(text))
        else:
            cursor.insertText(text[self.stream_displayed:])
        self.stream_displayed = len(text)
        self.stage = "正在流式输出"
        if follow:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(scroll_value)
        self.show_elapsed()

    def progress(self, handle, text):
        if handle is self.handle:
            self.stage = text
            self.show_elapsed()

    @safe_ui
    def send(self, _checked=False, retry_id=None):
        if self.handle:
            return
        provider = self.provider.currentData()
        model = validate_model(provider, self.model_id())
        if retry_id is not None:
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
        self.save_draft()
        identifier = self.current_id
        prompt_id = self.store.begin_request(identifier, text, images, provider, model, retry_id)
        if retry_id is None:
            self.loading = True
            self.input.clear()
            self.pending_images = []
            self.render_attachments()
            self.loading = False
        payload = {"conversation": identifier, "prompt_id": prompt_id, "provider": provider,
                   "model": model, "options": self.options(), "config": self.connection_snapshot()}
        self.stream = StreamBuffer() if payload["options"]["stream"] else None
        self.stream_displayed = 0
        try:
            self.submit("AI 对话 · " + PROVIDERS[provider] + " / " + model,
                        functools.partial(run_chat, store=self.store, client=self.client, stream=self.stream), payload,
                        {"type": "chat", "conversation": identifier, "prompt_id": prompt_id})
            if self.stream:
                self.stream_timer.start()
        except Exception as error:
            self.stream = None
            self.store.fail_request(identifier, prompt_id, str(error))
            raise
        self.reload_history()
        self.render_transcript()

    @safe_ui
    def retry_request(self, _checked=False):
        rows = self.store.messages(self.current_id)
        if rows and rows[-1]["role"] == "user" and rows[-1]["status"] not in {"done", "pending"}:
            self.send(retry_id=rows[-1]["id"])
        else:
            self.status.setText("本对话最后一条消息没有需要重试的失败请求。")

    @safe_ui
    def stop_request(self, _checked=False):
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
        if self.operation["type"] == "models":
            provider = output["provider"]
            self.catalogs[provider] = output["models"]
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
        else:
            result = output["result"]
            usage = result["usage"]
            self.last_status = f"{result['model']} · {result['elapsed']:.2f}秒 · 输入 {usage['input']} / 输出 {usage['output']} tokens。" + result["warning"]
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
        self.context.log("AI 试用台：" + message, logging.WARNING)
        if self.operation["type"] == "chat":
            self.store.fail_request(self.operation["conversation"], self.operation["prompt_id"], message,
                                    "取消" in message)
            self.render_transcript()

    @safe_ui
    def finished(self, handle):
        if handle is self.handle:
            self.stream_timer.stop()
            self.stream = None
            self.handle = self.operation = None
            self.set_busy(False)
            self.status.setText(getattr(self, "last_status", "操作结束。"))

    def render_attachments(self):
        self.attachments.clear()
        for image in self.pending_images:
            path = self.store.asset_path(image)
            pixmap = QtGui.QPixmap(str(path)).scaled(84, 65, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            item = QtWidgets.QListWidgetItem(QtGui.QIcon(pixmap), image["name"])
            item.setToolTip(f"{image['name']} · {image['width']}×{image['height']}\n右键移除附件")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, image["file"])
            self.attachments.addItem(item)
        self.attachments.setVisible(bool(self.pending_images))

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
        self.allowed_images = {}
        self.stream_start_pos = None
        parts = []
        for row in rows:
            title = "你" if row["role"] == "user" else "AI"
            parts.append("<p><b>" + html.escape(title + " · " + row["provider"] + " / " + row["model"]) + "</b></p>"
                         + message_html(row["text"], self.markdown.isChecked()))
            for image in row["images"]:
                try:
                    path = self.store.asset_path(image)
                except ValueError:
                    parts.append("<p>[本地图片已丢失]</p>")
                    continue
                url = QtCore.QUrl.fromLocalFile(str(path)).toString()
                self.allowed_images[url] = path
                scale = min(180 / max(1, image["width"]), 180 / max(1, image["height"]))
                width, height = max(1, int(image["width"] * scale)), max(1, int(image["height"] * scale))
                parts.append(f'<p><a href="{html.escape(url, quote=True)}"><img src="{html.escape(url, quote=True)}" width="{width}" height="{height}"></a><br>{html.escape(image["name"])}</p>')
            if row["status"] != "done":
                parts.append("<p><i>" + html.escape("请求中…" if row["status"] == "pending" else row["error"] or row["status"]) + "</i></p>")
            partial = row["metadata"].get("partial") or {}
            if partial.get("text"):
                parts.append("<p><b>AI · 未完成（不计入后续上下文）</b></p>"
                             + message_html(partial["text"], self.markdown.isChecked()))
            if row["role"] == "assistant":
                meta = row["metadata"]
                parts.append("<p><small>" + html.escape(str(meta.get("elapsed", "")) + "秒 · " + str(meta.get("usage", {})) + " " + meta.get("warning", "")) + "</small></p>")
            parts.append("<hr>")
        self.transcript.allowed_images = self.allowed_images.copy()
        self.transcript.setHtml("".join(parts) or "<p>选择服务与模型，输入消息后发送。</p>")
        if self.stream and self.operation and self.operation.get("conversation") == self.current_id and any(
                row["id"] == self.operation.get("prompt_id") and row["status"] == "pending" for row in rows):
            cursor = QtGui.QTextCursor(self.transcript.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertBlock()
            cursor.insertText("AI · 生成中")
            cursor.insertBlock()
            self.stream_start_pos = cursor.position()
            text = self.stream.text()
            if self.markdown.isChecked():
                cursor.insertHtml(message_html(text))
            else:
                cursor.insertText(text)
            self.stream_displayed = len(text)
        scrollbar = self.transcript.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @safe_ui
    def open_attachment(self, url):
        path = self.allowed_images.get(url.toString())
        if path and path.is_file():
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

    @safe_ui
    def rename_conversation(self, _checked=False):
        text, accepted = QtWidgets.QInputDialog.getText(self, "重命名对话", "标题", text=self.store.get(self.current_id)["title"])
        if accepted and text.strip():
            self.store.update(self.current_id, title=text.strip()[:100])
            self.reload_history()

    @safe_ui
    def delete_conversation(self, _checked=False):
        if self.handle:
            self.status.setText("请等待当前操作结束后再删除对话。")
            return
        if QtWidgets.QMessageBox.question(self, "删除对话", "删除这段本地对话？不会删除你添加的原图片。") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.draft_timer.stop()
        self.store.delete(self.current_id)
        self.current_id = None
        rows = self.store.conversations()
        self.reload_history(rows[0]["id"] if rows else None)
        if not rows:
            self.new_conversation()

    @safe_ui
    def export_conversation(self, _checked=False):
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
            set_internal_clipboard_text(answers[-1]["text"])
            self.status.setText("最新回答已复制。")

    @safe_ui
    def open_history_folder(self, _checked=False):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.store.root)))

    @safe_ui
    def history_menu(self, position):
        item = self.history.itemAt(position)
        if item:
            self.history.setCurrentItem(item)
        menu = QtWidgets.QMenu(self.history)
        for title, callback in (("新建对话", self.new_conversation), ("重命名…", self.rename_conversation),
                                ("导出文本…", self.export_conversation), ("删除…", self.delete_conversation)):
            menu.addAction(title, callback)
        menu.exec(self.history.mapToGlobal(position))

    def closeEvent(self, event):
        self.save_draft()
        event.accept()

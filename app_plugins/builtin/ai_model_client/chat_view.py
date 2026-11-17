"""Message cards for the native chat window; no web or file resource loading."""
import math
import re
from qt_compat import QtCore, QtGui, QtWidgets
from model.AIChatSources import text_with_sources
from model.ClipboardHelper import set_internal_clipboard_text
from .rendering import cited_message_html


class MessageBody(QtWidgets.QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.allowed_images = {}
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(2)
        self.document().setDefaultStyleSheet('''
            p { margin-top: 8px; margin-bottom: 10px; line-height: 145%; }
            h1, h2, h3 { margin-top: 16px; margin-bottom: 10px; }
            pre { background-color: palette(alternate-base); padding: 12px; }
            td, th { padding: 7px; } a { text-decoration: none; }
        ''')
        self.document().documentLayout().documentSizeChanged.connect(self.fit_height)
        self.horizontalScrollBar().rangeChanged.connect(self.fit_height)

    def setHtml(self, text):
        self.source_html = text
        palette = QtWidgets.QApplication.palette()
        background = palette.color(QtGui.QPalette.ColorRole.AlternateBase).name()
        link = palette.color(QtGui.QPalette.ColorRole.Link).name()
        # Qt's Markdown exporter hard-codes a pale code background. Adapt the
        # generated display HTML so dark mode keeps code readable.
        text = re.sub(r'background-color:#[0-9a-fA-F]{6}', 'background-color:' + background, text)
        text = re.sub(r'(?<![-\w])color:#0000ff', 'color:' + link, text, flags=re.I)
        scale = getattr(self, 'font_px', 15) / 15
        text = re.sub(r'font-size:([\d.]+)(pt|px)',
                      lambda match: 'font-size:' + str(round(float(match[1]) * scale, 3)) + match[2], text)
        super().setHtml(text)

    def set_font_size(self, pixels):
        self.font_px = pixels
        font = self.font()
        font.setPixelSize(pixels)
        self.document().setDefaultFont(font)
        if hasattr(self, 'source_html'):
            self.setHtml(self.source_html)
        self.fit_height()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.PaletteChange and hasattr(self, 'source_html'):
            self.setHtml(self.source_html)

    def loadResource(self, resource_type, name):
        path = self.allowed_images.get(name.toString())
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource.value and isinstance(path, bytes):
            return QtGui.QImage.fromData(path)
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource.value and path and path.is_file():
            return QtGui.QImage(str(path))
        return None

    def fit_height(self, *_args):
        extra = self.horizontalScrollBar().sizeHint().height() if self.horizontalScrollBar().maximum() else 0
        height = math.ceil(self.document().size().height()) + extra + 8
        if self.height() != max(28, height):
            self.setFixedHeight(max(28, height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):
            set_internal_clipboard_text(self.textCursor().selectedText().replace('\u2029', '\n'))
            return
        super().keyPressEvent(event)


class MessageCard(QtWidgets.QFrame):
    linkClicked = QtCore.pyqtSignal(QtCore.QUrl)
    regenerateRequested = QtCore.pyqtSignal(object)
    branchRequested = QtCore.pyqtSignal(object)
    editRequested = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('messageCard')
        self.row = None
        self.signature = None
        self.markdown = True
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        header = self.header = QtWidgets.QHBoxLayout()
        self.avatar = QtWidgets.QLabel(self)
        self.avatar.setObjectName('chatAvatar')
        self.avatar.setFixedSize(28, 28)
        self.avatar.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.title = QtWidgets.QLabel(self)
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.title.setObjectName('messageTitle')
        self.model = QtWidgets.QLabel(self)
        self.model.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.model.setObjectName('muted')
        self.model.setWordWrap(True)
        header.addWidget(self.avatar)
        header.addWidget(self.title)
        header.addWidget(self.model, 1)
        self.raw_button = QtWidgets.QToolButton(self)
        self.raw_button.setText('原文')
        self.raw_button.setCheckable(True)
        self.raw_button.setToolTip('查看未经 Markdown 解析的完整原文')
        self.raw_button.toggled.connect(self.render_body)
        self.copy_button = QtWidgets.QToolButton(self)
        self.copy_button.setText('复制')
        self.copy_button.clicked.connect(self.copy_text)
        header.addWidget(self.raw_button)
        header.addWidget(self.copy_button)
        layout.addLayout(header)
        self.body = MessageBody(self)
        self.body.anchorClicked.connect(self.linkClicked)
        layout.addWidget(self.body)
        tools = QtWidgets.QHBoxLayout()
        self.branch_previous = QtWidgets.QToolButton(self)
        self.branch_previous.setText('上个')
        self.branch_counter = QtWidgets.QLabel(self)
        self.branch_next = QtWidgets.QToolButton(self)
        self.branch_next.setText('下个')
        self.branch_previous.clicked.connect(lambda: self.choose_branch(-1))
        self.branch_next.clicked.connect(lambda: self.choose_branch(1))
        for widget in (self.branch_previous, self.branch_counter, self.branch_next):
            tools.addWidget(widget)
        self.regenerate_button = QtWidgets.QToolButton(self)
        self.regenerate_button.setText('重试')
        self.regenerate_button.setToolTip('基于这条回答之前的上下文重新请求，可能消耗额度；原回答和后续对话保留在原分支。')
        self.regenerate_button.clicked.connect(lambda: self.regenerateRequested.emit(self.row['id']))
        header.addWidget(self.regenerate_button)
        self.edit_button = QtWidgets.QToolButton(self)
        self.edit_button.setText('编辑')
        self.edit_button.setToolTip('修改这条已发送的消息并重新发送；原问题与后续对话保留在原分支。')
        self.edit_button.clicked.connect(lambda: self.editRequested.emit(self.row['id']))
        header.addWidget(self.edit_button)
        tools.addStretch(1)
        layout.addLayout(tools)
        self.footer = QtWidgets.QLabel(self)
        self.footer.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.footer.setWordWrap(True)
        self.footer.setObjectName('muted')
        self.footer.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.footer)

    def update_message(self, row, markdown, allowed_images):
        signature = (repr(row), markdown, repr(allowed_images))
        if signature == self.signature:
            return
        self.signature, self.row, self.markdown = signature, row, markdown
        user = row['role'] == 'user'
        self.setProperty('speaker', 'user' if user else 'assistant')
        member_name=row.get('metadata',{}).get('group_member','')
        self.avatar.setText('你' if user else (member_name[:1] if member_name else 'AI'))
        self.title.setText('你' if user else (row.get('metadata',{}).get('group_member') or '助手'))
        label=row.get('provider', '') + ' / ' + row.get('model', '')
        if row.get('metadata',{}).get('group_round'):label+=' · 第'+str(row['metadata']['group_round'])+'轮'
        self.model.setText('' if user else label)
        self.header.setDirection(QtWidgets.QBoxLayout.Direction.RightToLeft if user else QtWidgets.QBoxLayout.Direction.LeftToRight)
        self.raw_button.setVisible(not user)
        self.regenerate_button.setVisible(not user and row.get('status') == 'done' and type(row['id']) is int)
        self.edit_button.setVisible(user and type(row['id']) is int)
        branch = row.get('_branch') or {}
        targets, index = branch.get('targets', []), branch.get('index', 0)
        show_branch = len(targets) > 1 and (not user or row.get('status') != 'done')
        for widget in (self.branch_previous, self.branch_counter, self.branch_next):
            widget.setVisible(show_branch)
        self.branch_counter.setText(f'{index + 1} / {len(targets)}')
        self.branch_previous.setEnabled(index > 0)
        self.branch_next.setEnabled(index + 1 < len(targets))
        self.body.allowed_images = allowed_images
        meta = row.get('metadata') or {}
        notes = []
        discussion=meta.get('group_discussion')
        if discussion:
            state=discussion.get('state','')
            state_label={'running':'讨论中','complete':'已完成','paused':'已暂停','failed':'遇到错误，已停止','interrupted':'上次讨论中断'}.get(state,state)
            notes.append('群聊：'+str(discussion.get('calls_done',0))+'/'+str(discussion.get('max_calls',0))+'次发言 · '+state_label)
        if meta.get('group_run'):
            self.regenerate_button.setToolTip('重新讨论这一轮问题，保留原群聊分支；会再次调用已配置的成员。')
        if row.get('status') != 'done':
            notes.append('请求中…' if row.get('status') == 'pending' else row.get('error') or row.get('status', ''))
        if 'requested_model' in meta:
            notes.append('请求模型：' + meta['requested_model'] + '；接口返回：' + (meta.get('reported_model') or '未提供模型 ID'))
        if 'elapsed' in meta:
            usage = meta.get('usage') or {}
            notes.append(str(meta['elapsed']) + ' 秒 · 输入 ' + str(usage.get('input', '—')) + ' / 输出 ' + str(usage.get('output', '—')) + ' tokens')
        if meta.get('warning'):
            notes.append(str(meta['warning']))
        self.footer.setText('\n'.join(notes))
        self.footer.setVisible(bool(notes))
        self.copy_button.setText('复制')
        self.render_body()
        self.style().unpolish(self)
        self.style().polish(self)

    def render_body(self, *_args):
        if self.row is None:
            return
        markdown = self.markdown and not self.raw_button.isChecked() and self.row['role'] != 'user'
        self.body.setHtml(cited_message_html(self.row['text'], self.row.get('metadata') or {}, markdown)
                          + self.row.get('_attachments', ''))
        self.body.fit_height()

    def copy_text(self):
        if self.row is not None:
            set_internal_clipboard_text(text_with_sources(self.row['text'], self.row.get('metadata') or {}))
            self.copy_button.setText('已复制')

    def choose_branch(self, offset):
        branch = self.row.get('_branch') or {}
        index = branch.get('index', 0) + offset
        targets = branch.get('targets', [])
        if 0 <= index < len(targets):
            self.branchRequested.emit(targets[index])


class Transcript(QtWidgets.QScrollArea):
    anchorClicked = QtCore.pyqtSignal(QtCore.QUrl)
    regenerateRequested = QtCore.pyqtSignal(object)
    branchRequested = QtCore.pyqtSignal(object)
    editRequested = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.allowed_images = {}
        self.font_px = 15
        self.busy = False
        self.cards = {}
        self.lanes = {}
        self.order = []
        self.setObjectName('chatTranscript')
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QtWidgets.QWidget(self)
        self.content.setObjectName('chatCanvas')
        self.outer = QtWidgets.QVBoxLayout(self.content)
        self.outer.setContentsMargins(20, 20, 20, 20)
        self.column = QtWidgets.QWidget(self.content)
        self.column.setMaximumWidth(1000)
        self.column.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        self.stack = QtWidgets.QVBoxLayout(self.column)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(18)
        self.outer.addWidget(self.column, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.outer.addStretch(1)
        self.empty = QtWidgets.QLabel('开始一段新对话\n\n选择模型，写下你的问题。', self.column)
        self.empty.setObjectName('emptyChat')
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(210)
        self.stack.addWidget(self.empty)
        self.setWidget(self.content)
        self.scroll_timer = QtCore.QTimer(self)
        self.scroll_timer.setSingleShot(True)
        self.scroll_timer.timeout.connect(self._restore_scroll)
        self.follow = True
        self.saved_scroll = 0

    def near_bottom(self):
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 48

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.column.setFixedWidth(max(160, min(1000, self.viewport().width() - 40)))
        self.resize_cards()

    def resize_cards(self):
        available = self.column.width()
        for card in self.cards.values():
            if card.row is None:
                continue
            if card.row['role'] == 'user':
                metrics = QtGui.QFontMetricsF(card.body.font())
                # Short prompts form compact bubbles; long prompts wrap within
                # the right side instead of spanning the whole chat column.
                natural = max((metrics.horizontalAdvance(line[:2000]) for line in card.row['text'].splitlines()), default=200) + 56
                width = min(available, max(260, min(720, int(available * .78), int(natural))))
            else:
                width = min(available, max(260, min(900, int(available * .92))))
            card.setFixedWidth(width)

    def set_messages(self, rows, markdown, force_bottom=False):
        self.follow = force_bottom or self.near_bottom()
        self.saved_scroll = self.verticalScrollBar().value()
        keys = [row['id'] for row in rows]
        for key in list(self.cards):
            if key not in keys:
                card = self.cards.pop(key)
                lane = self.lanes.pop(key)
                self.stack.removeWidget(lane)
                lane.hide()
                lane.deleteLater()
        self.order = keys
        self.empty.setVisible(not keys)
        for index, row in enumerate(rows):
            card = self.cards.get(row['id'])
            if card is None:
                lane = QtWidgets.QWidget(self.column)
                lane_layout = QtWidgets.QHBoxLayout(lane)
                lane_layout.setContentsMargins(0, 0, 0, 0)
                card = MessageCard(lane)
                card.body.set_font_size(self.font_px)
                if row['role'] == 'user':
                    lane_layout.addStretch(1)
                lane_layout.addWidget(card)
                if row['role'] != 'user':
                    lane_layout.addStretch(1)
                card.linkClicked.connect(self.anchorClicked)
                card.regenerateRequested.connect(self.regenerateRequested)
                card.branchRequested.connect(self.branchRequested)
                card.editRequested.connect(self.editRequested)
                self.cards[row['id']] = card
                self.lanes[row['id']] = lane
                self.stack.insertWidget(index, lane)
            card.update_message(row, markdown, self.allowed_images)
            card.regenerate_button.setEnabled(not self.busy)
            card.edit_button.setEnabled(not self.busy)
        self.resize_cards()
        self.scroll_timer.start(0)

    def set_font_size(self, pixels):
        self.font_px = pixels
        for card in self.cards.values():
            card.body.set_font_size(pixels)
        self.resize_cards()

    def set_busy(self, busy):
        self.busy = busy
        for card in self.cards.values():
            card.regenerate_button.setEnabled(not busy)
            card.edit_button.setEnabled(not busy)

    def _restore_scroll(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum() if self.follow else self.saved_scroll)

    def update_stream(self, text, markdown):
        card = self.cards.get('stream')
        if card is None:
            return
        self.follow = self.near_bottom()
        self.saved_scroll = self.verticalScrollBar().value()
        card.update_message({**card.row, 'text': text}, markdown, self.allowed_images)
        self.scroll_timer.start(0)

    def loadResource(self, resource_type, name):
        path = self.allowed_images.get(name.toString())
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource.value and isinstance(path, bytes):
            return QtGui.QImage.fromData(path)
        if resource_type == QtGui.QTextDocument.ResourceType.ImageResource.value and path and path.is_file():
            return QtGui.QImage(str(path))
        return None

    def document(self):
        """Provide one selectable/exportable document snapshot of visible cards."""
        doc = QtGui.QTextDocument()
        cursor = QtGui.QTextCursor(doc)
        for key in self.order:
            card = self.cards[key]
            cursor.insertText(card.title.text() + ' · ' + card.model.text())
            cursor.insertBlock()
            cursor.insertFragment(QtGui.QTextDocumentFragment(card.body.document()))
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertBlock()
            cursor.insertText(card.footer.text())
            cursor.insertBlock()
        return doc

    def toPlainText(self):
        return self.document().toPlainText()

    def toHtml(self):
        return self.document().toHtml()

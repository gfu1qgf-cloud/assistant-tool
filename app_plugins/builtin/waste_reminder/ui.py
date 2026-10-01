import html
import math
from datetime import timedelta
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from .calendar import ReminderEvent, message
from .details import bind_category_menu
from .translation import readable_text


class TonightPanel(QtWidgets.QWidget):
    """Host-owned main-interface card; always describes tonight, not last night."""
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self._signature = None
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.title = QtWidgets.QLabel()
        self.title.setWordWrap(True)
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.title)
        self.items = QtWidgets.QVBoxLayout()
        self.items.setSpacing(3)
        layout.addLayout(self.items)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.note)
        button = QtWidgets.QPushButton("垃圾日历 / 分类说明 / 最新消息…")
        button.clicked.connect(lambda: plugin.open_dialog())
        layout.addWidget(button)
        self.refresh()

    def refresh(self):
        try:
            now = self.plugin.now()
            day = now.date()+timedelta(days=1)
            calendar = self.plugin.calendar_for(day.year)
            values = calendar.collections(day)
            if values is None:
                raise ValueError(f"{day} 没有日历数据，不能当作不收运")
            active = self.plugin.active_types(calendar)
            record = self.plugin.state.snapshot()["events"].get(calendar.event_key(day, tuple(x for x in values if x in active)), {}) if self.plugin.state else {}
            signature = (id(calendar), day, values, tuple(active), self.plugin.settings["enabled"], self.plugin.settings["reminder_time"],
                         bool(record.get("confirmed")), now.hour >= 22, self.plugin.settings["language"])
            if signature == self._signature:
                return
            self._signature = signature
            while self.items.count():
                item = self.items.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            weekday = "一二三四五六日"[day.weekday()]
            self.title.setText(f"今晚 22:00 后投放 → 明天 {day:%m-%d}（星期{weekday}）收运")
            for key in values:
                suffix = "（提醒未启用）" if key not in active else ""
                button = QtWidgets.QPushButton(calendar.label(key, self.plugin.settings["language"]).split(" ", 1)[-1]+suffix)
                button.clicked.connect(lambda _checked=False, k=key: self.plugin.show_category_details(calendar, k))
                bind_category_menu(button, self.plugin, calendar, key)
                self.items.addWidget(button)
            if not values:
                self.note.setText("今晚无需投放垃圾。")
            elif not any(x in active for x in values):
                self.note.setText("仅有未启用的专项收运，请先确认是否适用。")
            else:
                state = "已确认投放。" if record.get("confirmed") else ("现在可以按规定投放。" if now.hour >= 22 else "还没到投放时间，请勿提前放出。")
                reminder = f"自动提醒：{self.plugin.settings['reminder_time']}" if self.plugin.settings["enabled"] else "自动提醒已关闭"
                self.note.setText(state+f" {reminder} · 次日 06:00 前；点击/右键类别查看说明。")
        except Exception as error:
            self._signature = None
            self.title.setText("今晚投放：需要检查日历")
            while self.items.count():
                item = self.items.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.note.setText(str(error))


class ReminderDialog(QtWidgets.QDialog):
    actionRequested = QtCore.pyqtSignal(bool)

    def __init__(self, calendar, event, now, settings, parent=None, preview=False):
        super().__init__(parent)
        self.setWindowTitle("垃圾投放提醒 / Promemoria rifiuti" + (" · 测试" if preview else ""))
        self.setWindowFlags(QtCore.Qt.WindowType.Dialog | QtCore.Qt.WindowType.WindowTitleHint
                            | QtCore.Qt.WindowType.CustomizeWindowHint | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(490)
        self.resize(600, 410)
        self._allow_close = False
        self.elapsed = QtCore.QElapsedTimer()
        self.elapsed.start()
        self.settings = settings
        self.italian = settings["language"] == "it"
        layout = QtWidgets.QVBoxLayout(self)
        body = QtWidgets.QLabel(message(calendar, event, now, settings["language"]))
        body.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setStyleSheet("font-size:16px; padding:12px;")
        layout.addWidget(body)
        hints = QtWidgets.QLabel(
            "Conferma solo dopo il conferimento. Puoi rimandare; il promemoria rimane attivo."
            if self.italian else "真正放出去后再确认。暂时不方便请点“推迟”，不会被当作已完成。")
        hints.setWordWrap(True)
        layout.addWidget(hints)
        if "secco" in event.types and calendar.year == 2026 and "valle lomellina" in calendar.commune.casefold():
            rule = QtWidgets.QLabel("Secco: sacchi semitrasparenti ben chiusi nel mastello; niente sacchi neri."
                                    if self.italian else "注意：干垃圾用扎好的半透明袋，放进指定桶；不要用黑袋。")
            rule.setWordWrap(True)
            layout.addWidget(rule)
        rules = calendar.document.get("guidance", {}).get("rules", [])
        if rules:
            tip = rules[now.date().toordinal() % len(rules)]
            text = tip.get("it" if self.italian else "zh", "")
            label = QtWidgets.QLabel(("Consiglio del giorno: " if self.italian else "今日小贴士：")+text)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.check = QtWidgets.QCheckBox("Ho conferito correttamente i rifiuti" if self.italian else "我已按规定把本次垃圾投放好")
        if not event.types:
            self.check.setText("Ho letto il promemoria" if self.italian else "我已看过提醒")
        self.check.toggled.connect(self._countdown)
        layout.addWidget(self.check)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        from .sound import ReminderAudio
        self.audio = ReminderAudio(self)
        self.audio.failed.connect(self.error.setText)
        actions = QtWidgets.QHBoxLayout()
        self.snooze = QtWidgets.QPushButton(f"Rimanda {settings['snooze_minutes']} min" if self.italian else f"推迟 {settings['snooze_minutes']} 分钟")
        self.snooze.clicked.connect(lambda: self.actionRequested.emit(False))
        self.confirm = QtWidgets.QPushButton()
        self.confirm.setAutoDefault(False)
        self.confirm.clicked.connect(lambda: self.actionRequested.emit(True))
        actions.addWidget(self.snooze)
        actions.addStretch(1)
        actions.addWidget(self.confirm)
        layout.addLayout(actions)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._countdown)
        self.timer.start()
        self._countdown()

    def add_notice(self, text, url=None):
        label = QtWidgets.QLabel(text)
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setToolTip(url or "")
        self.layout().insertWidget(1, label)
        if url:
            button = QtWidgets.QPushButton("查看最新官方公告")
            button.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)))
            self.layout().insertWidget(2, button)

    def _countdown(self):
        seconds = max(0, math.ceil((30000-self.elapsed.elapsed()) / 1000))
        self.confirm.setEnabled(seconds == 0 and self.check.isChecked())
        self.confirm.setText((f"Conferma ({seconds}s)" if self.italian else f"确定（等待 {seconds} 秒）") if seconds else (
            "Conferma" if self.italian else "已投放，完成"))

    def play_warning(self):
        self.audio.start(self.settings)

    def finish_safely(self):
        self._allow_close = True
        self.timer.stop()
        self.audio.stop()
        super().accept()

    def accept(self):
        if self._allow_close:
            super().accept()

    def reject(self):
        if self._allow_close:
            super().reject()

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
        else:
            event.ignore()


class WasteDashboard(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("垃圾分类每日提醒 · Valle Lomellina")
        self.resize(900, 730)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMaximizeButtonHint
                            | QtCore.Qt.WindowType.WindowMinimizeButtonHint)
        layout = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QtWidgets.QHBoxLayout()
        self.date = QtWidgets.QDateEdit(QtCore.QDate.currentDate().addDays(1))
        self.date.setCalendarPopup(True)
        self.date.dateChanged.connect(self.refresh)
        actions.addWidget(QtWidgets.QLabel("查询日期"))
        actions.addWidget(self.date)
        tomorrow = QtWidgets.QPushButton("明天扔什么？")
        tomorrow.clicked.connect(self._tomorrow)
        actions.addWidget(tomorrow)
        self.update_button = QtWidgets.QPushButton("检查并解析官方新日历")
        self.update_button.clicked.connect(lambda: plugin.check_official(manual=True))
        actions.addWidget(self.update_button)
        test = QtWidgets.QPushButton("测试提醒…")
        test.clicked.connect(lambda: plugin.preview_reminder(self.date.date().toPyDate()))
        actions.addWidget(test)
        actions.addStretch(1)
        layout.addLayout(actions)
        tabs = self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)
        overview = QtWidgets.QWidget()
        overview_layout = QtWidgets.QVBoxLayout(overview)
        self.query = QtWidgets.QLabel()
        self.query.setWordWrap(True)
        self.query.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.query.setStyleSheet("font-size:16px; padding:10px;")
        overview_layout.addWidget(self.query)
        self.week = QtWidgets.QTableWidget(7, 3)
        self.week.setHorizontalHeaderLabels(["收运日期", "当地分类（完整日历）", "提醒/投放情况"])
        self.week.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.week.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.week.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.week.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.week.verticalHeader().hide()
        self.week.itemDoubleClicked.connect(self._choose_row)
        self.week.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.week.customContextMenuRequested.connect(self._week_menu)
        overview_layout.addWidget(self.week, 1)
        self.next = QtWidgets.QLabel()
        self.next.setWordWrap(True)
        overview_layout.addWidget(self.next)
        self.categories = QtWidgets.QWidget()
        self.category_layout = QtWidgets.QGridLayout(self.categories)
        overview_layout.addWidget(self.categories)
        tabs.addTab(overview, "查询与本周")
        self.tips = QtWidgets.QTextBrowser()
        self.tips.setOpenExternalLinks(False)
        self.tips.anchorClicked.connect(self.open_source)
        tabs.addTab(self.tips, "分类小贴士与注意事项")
        news = QtWidgets.QWidget()
        news_layout = QtWidgets.QVBoxLayout(news)
        self.news_status = QtWidgets.QLabel()
        self.news_status.setWordWrap(True)
        news_layout.addWidget(self.news_status)
        self.translate_button = QtWidgets.QPushButton("翻译现有公告 / 重试失败翻译")
        self.translate_button.clicked.connect(lambda: plugin.check_official(translation_only=True))
        news_layout.addWidget(self.translate_button, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)
        self.news_list = QtWidgets.QListWidget()
        self.news_list.setMaximumHeight(180)
        self.news_list.currentItemChanged.connect(self._news_selected)
        news_layout.addWidget(self.news_list, 1)
        self.news_text = QtWidgets.QTextBrowser()
        self.news_text.setOpenExternalLinks(False)
        self.news_text.anchorClicked.connect(self._news_open)
        news_layout.addWidget(self.news_text, 2)
        tabs.addTab(news, "官方最新消息")
        sources = QtWidgets.QWidget()
        source_layout = QtWidgets.QVBoxLayout(sources)
        self.sources = QtWidgets.QListWidget()
        self.sources.itemDoubleClicked.connect(self._open_selected_source)
        source_layout.addWidget(self.sources, 1)
        source_note = QtWidgets.QLabel("双击打开保存的官方原件。年度 JSON 包含每个日期；官网新 PDF 原件、旧版本及解析失败原件都会保留。")
        source_note.setWordWrap(True)
        source_layout.addWidget(source_note)
        buttons = QtWidgets.QHBoxLayout()
        for title, callback in (("打开选中原件", self._open_selected_source), ("打开日历目录", self._open_folder),
                                ("查看当前年度 JSON", self._open_json), ("打开官方公告", self._open_notices)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        source_layout.addLayout(buttons)
        tabs.addTab(sources, "官方原件与年度数据")
        bottom = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel("程序需保持运行。设置：程序设置 → 垃圾分类提醒。首次默认启用 22:00 提醒。")
        note.setWordWrap(True)
        bottom.addWidget(note, 1)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(self.hide)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.refresh()

    def _tomorrow(self):
        day = self.plugin.now().date() + timedelta(days=1)
        self.date.setDate(QtCore.QDate(day.year, day.month, day.day))
        self.refresh()

    def _choose_row(self, item):
        day = self.week.item(item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        self.date.setDate(QtCore.QDate(day.year, day.month, day.day))

    def refresh(self, *_):
        plugin, settings = self.plugin, self.plugin.settings
        self.update_button.setEnabled(not plugin.is_updating())
        self.translate_button.setEnabled(not plugin.is_updating() and settings.get("translate_news", True))
        self.status.setText(f"{settings['commune']} · {settings['timezone']} · {'已启用' if settings['enabled'] else '已关闭'} · "
                            f"{settings['reminder_time']} {'前晚' if settings['mode']=='evening' else '当天早晨'}提醒\n{plugin.status}")
        current_id = self.news_list.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)["id"] if self.news_list.currentItem() else None
        self.news_list.clear()
        checked = plugin.notices.get("checked_at") or "尚未成功联网检查"
        self.news_status.setText(f"官方公告上次成功检查：{checked}\n中文机器译文在前、意大利语原文在后。仅辅助理解，官方原件优先；翻译失败可点上方重试。")
        selected_row = 0
        for i, card in enumerate(plugin.notices.get("items", [])):
            title = card.get("title_zh") or card.get("zh_subject") or card["title"]
            item = QtWidgets.QListWidgetItem(f"{'【重要】' if card['alert'] else '【公告】'} {card['date']} · {title}")
            item.setToolTip(card["title"])
            item.setData(QtCore.Qt.ItemDataRole.UserRole, card)
            self.news_list.addItem(item)
            if card["id"] == current_id:
                selected_row = i
        if self.news_list.count():
            self.news_list.setCurrentRow(selected_row)
        selected = self.date.date().toPyDate()
        self.sources.clear()
        try:
            calendar = plugin.calendar_for(selected.year)
            values = calendar.collections(selected)
            enabled = plugin.active_types(calendar)
            if values is None:
                self.query.setText(f"⚠️ {selected} 没有数据，不代表没有收运。")
            else:
                self.query.setText(f"{selected}\n" + ("\n".join(calendar.label(x, settings["language"]) +
                    ("（提醒未启用）" if x not in enabled else "") for x in values) or "没有收运"))
            for source in calendar.document.get("sources", []):
                item = QtWidgets.QListWidgetItem(source["title"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole, source)
                item.setToolTip(source.get("url", ""))
                self.sources.addItem(item)
            self._refresh_categories(calendar)
            rules = calendar.document.get("guidance", {}).get("rules", [])
            self.tips.setHtml("".join(f"<h3>{html.escape(rule['title'])}</h3><p>{html.escape(rule.get('it' if settings['language']=='it' else 'zh', ''))}</p>"
                f"<p><a href='source:{rule.get('source',0)}'>查看对应官方原件</a></p>" for rule in rules)
                or "新年度日历已解析。投放说明请查看对应年度原始 PDF；本工具不自动猜测规则变更。")
            next_lines = []
            for key in calendar.types:
                later = next((day for i in range(1, 367) if (day := plugin.now().date()+timedelta(days=i)).year == calendar.year
                              and key in (calendar.collections(day) or ())), None)
                if later:
                    next_lines.append(f"{calendar.label(key, settings['language'])}: {later:%m-%d}")
            self.next.setText("下次收运（仅查询，不提前投放）：\n" + "　".join(next_lines))
        except Exception as error:
            self.query.setText(f"⚠️ {error}")
            self.tips.setPlainText("请补充年度日历或检查官网更新；缺少数据不代表无需投放。")
        first = plugin.now().date()
        ledger = plugin.state.snapshot()["events"] if plugin.state else {}
        for row in range(7):
            day = first+timedelta(days=row)
            try:
                calendar = plugin.calendar_for(day.year)
                values = calendar.collections(day)
                text = " / ".join(calendar.label(x, settings["language"]) for x in values) if values else ("没有收运" if values is not None else "⚠️ 数据缺失")
                active = calendar.collections(day, plugin.active_types(calendar))
                record = ledger.get(calendar.event_key(day, active), {}) if active is not None else {}
                status = "已确认投放" if record.get("confirmed") else ("已推迟" if record.get("snooze_until", 0)>plugin.now().timestamp() else "—")
            except Exception:
                text, status = "⚠️ 该年度日历未加载", "—"
            for col, value in enumerate((day.strftime("%Y-%m-%d"), text, status)):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, day)
                self.week.setItem(row, col, item)
        self.week.resizeRowsToContents()

    def _refresh_categories(self, calendar):
        while self.category_layout.count():
            item = self.category_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        active = self.plugin.active_types(calendar)
        for index, key in enumerate(calendar.types):
            check = QtWidgets.QCheckBox(calendar.label(key, self.plugin.settings["language"]))
            check.setChecked(key in active)
            check.toggled.connect(lambda checked, category=key: self.plugin.toggle_type(category, checked))
            bind_category_menu(check, self.plugin, calendar, key)
            self.category_layout.addWidget(check, index//3, index%3)

    def _week_menu(self, position):
        item = self.week.itemAt(position)
        if not item:
            return
        day = self.week.item(item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            calendar = self.plugin.calendar_for(day.year)
            keys = calendar.collections(day) or ()
        except Exception:
            return
        menu = QtWidgets.QMenu(self.week)
        for key in keys:
            action = menu.addAction("查看说明："+calendar.label(key))
            action.triggered.connect(lambda _checked=False, k=key: self.plugin.show_category_details(calendar, k))
        if keys:
            menu.exec(self.week.viewport().mapToGlobal(position))

    def open_source(self, url):
        if url.scheme() == "source":
            try:
                index = int(url.path())
                self.sources.setCurrentRow(index)
                self._open_selected_source()
            except ValueError:
                pass

    def _open_selected_source(self, *_):
        item = self.sources.currentItem()
        if item:
            source = item.data(QtCore.Qt.ItemDataRole.UserRole)
            calendar = self.plugin.calendar_for(self.date.date().year())
            root = calendar.path.parent.resolve()
            candidate = (root/source.get("file", "")).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(candidate)))
            elif source.get("url", "").startswith("https://"):
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(source["url"]))

    def _open_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.plugin.folder)))

    def _open_json(self):
        path = self.plugin.calendar_path(self.date.date().year())
        if path.is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def _open_notices(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://www.teknoserviceitalia.com/avvisi-e-aggiornamenti/"))

    def _news_selected(self, item, *_):
        if not item:
            self.news_text.clear()
            return
        card = item.data(QtCore.Qt.ItemDataRole.UserRole)
        title = card.get("title_zh") or card.get("zh_subject") or "官方公告"
        translated = card.get("text_zh") or card.get("translation_error") or (
            "正文暂未提取，请打开官方原件查看。" if not card.get("text") else "尚未翻译，可点击上方翻译按钮。")
        self.news_text.setHtml(f"<h2>{html.escape(title)}</h2><p>{html.escape(card['date'])}</p>"
            f"<p>中文机器译文 · {html.escape(card.get('translation_provider') or '尚未完成')}（官方原件优先）</p>"
            f"<pre style='white-space:pre-wrap'>{html.escape(translated)}</pre>"
            f"<p><a href='notice:original'>打开保存的原始公告</a>　<a href='notice:online'>查看官网原件</a></p>"
            f"<hr><h3>意大利语原文：{html.escape(card['title'])}</h3>"
            f"<pre style='white-space:pre-wrap'>{html.escape(readable_text(card.get('text')) or '尚未提取正文，请点击原始公告查看。')}</pre>")

    def _news_open(self, url):
        item = self.news_list.currentItem()
        if item:
            card = item.data(QtCore.Qt.ItemDataRole.UserRole)
            path = (self.plugin.folder/card.get("file", "")).resolve()
            if url.path() == "original" and path.is_relative_to(self.plugin.folder.resolve()) and path.is_file():
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(card["url"]))

from pathlib import Path
from qt_compat import QtCore, QtWidgets
from .calendar import clock, CalendarError
from .update import DEFAULT_PAGE, allowed_url

CONFIG_KEY = "waste_reminder"


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    mode = "morning" if value.get("mode") == "morning" else "evening"
    at = str(value.get("reminder_time") or ("05:30" if mode == "morning" else "22:00"))
    try:
        parsed = clock(at)
        if mode == "morning" and parsed >= clock("06:00") or mode == "evening" and parsed < clock("22:00"):
            raise CalendarError("投放窗口以外的时间")
    except CalendarError:
        at = "05:30" if mode == "morning" else "22:00"
    try:
        snooze = max(1, min(120, int(value.get("snooze_minutes", 15))))
    except (ValueError, TypeError):
        snooze = 15
    try:
        volume = max(0, min(100, int(value.get("sound_volume", 60))))
    except (ValueError, TypeError):
        volume = 60
    types = value.get("enabled_types")
    return {"enabled": bool(value.get("enabled", True)), "mode": mode, "reminder_time": at,
            "notify_empty": bool(value.get("notify_empty", False)),
            "language": value.get("language") if value.get("language") in ("zh", "it", "bilingual") else "bilingual",
            "commune": str(value.get("commune") or "Valle Lomellina (PV)"),
            "timezone": str(value.get("timezone") or "Europe/Rome"),
            "calendar_folder": str(value.get("calendar_folder") or "config/waste_reminder"),
            "calendar_pattern": str(value.get("calendar_pattern") or "schedule_{year}.json"),
            "enabled_types": [x for x in types if isinstance(x, str)] if isinstance(types, list) else None,
            "snooze_minutes": snooze, "sound": bool(value.get("sound", True)),
            "sound_file": str(value.get("sound_file") or ""), "sound_volume": volume,
            "translate_news": bool(value.get("translate_news", True)),
            "auto_update": bool(value.get("auto_update", True)),
            "official_page": str(value.get("official_page") or DEFAULT_PAGE)}


class WasteSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        self.enabled = QtWidgets.QCheckBox("启用垃圾分类每日提醒")
        layout.addWidget(self.enabled)
        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("前一天晚上（22:00 后）", "evening")
        self.mode.addItem("收运当天早晨（06:00 前）", "morning")
        self.at = QtWidgets.QTimeEdit()
        self.at.setDisplayFormat("HH:mm")
        self.mode.currentIndexChanged.connect(self._change_mode)
        self.language = QtWidgets.QComboBox()
        for title, key in (("中文＋意大利语", "bilingual"), ("中文", "zh"), ("Italiano", "it")):
            self.language.addItem(title, key)
        self.commune = QtWidgets.QLineEdit()
        self.timezone = QtWidgets.QLineEdit()
        self.folder = QtWidgets.QLineEdit()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.folder)
        browse = QtWidgets.QPushButton("选择…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        self.pattern = QtWidgets.QLineEdit()
        self.page = QtWidgets.QLineEdit()
        self.snooze = QtWidgets.QSpinBox()
        self.snooze.setRange(1, 120)
        self.snooze.setSuffix(" 分钟")
        self.sound_file = QtWidgets.QLineEdit()
        self.sound_file.setPlaceholderText("不选择歌曲时，使用循环警告音")
        song_row = QtWidgets.QHBoxLayout()
        song_row.addWidget(self.sound_file)
        song_browse = QtWidgets.QPushButton("选择歌曲…")
        song_browse.clicked.connect(self._browse_sound)
        song_row.addWidget(song_browse)
        song_clear = QtWidgets.QPushButton("清除")
        song_clear.clicked.connect(self.sound_file.clear)
        song_row.addWidget(song_clear)
        self.volume = QtWidgets.QSpinBox()
        self.volume.setRange(0, 100)
        self.volume.setSuffix(" %")
        form.addRow("提醒方式", self.mode)
        form.addRow("提醒时间（Comune 当地时间）", self.at)
        form.addRow("显示语言", self.language)
        form.addRow("Comune（更换后也必须更换日历）", self.commune)
        form.addRow("时区", self.timezone)
        form.addRow("年度日历目录", row)
        form.addRow("文件名规则（保留 {year}）", self.pattern)
        form.addRow("官方日历网页", self.page)
        form.addRow("推迟时长", self.snooze)
        form.addRow("提醒歌曲（本地文件）", song_row)
        form.addRow("歌曲音量", self.volume)
        layout.addLayout(form)
        self.empty = QtWidgets.QCheckBox("明天没有已启用的收运时也提醒")
        self.sound = QtWidgets.QCheckBox("弹窗播放警告音")
        self.auto = QtWidgets.QCheckBox("每日自动检查官网新日历（后台；断网仍使用本地日历）")
        self.translate = QtWidgets.QCheckBox("自动把官方最新消息翻译成中文（仅发送公开公告；译文缓存到本地）")
        for widget in (self.empty, self.sound, self.auto, self.translate):
            layout.addWidget(widget)
        self.note = QtWidgets.QLabel(
            "默认晚上 22:00 提醒，确定按钮至少等待 30 秒。程序需保持运行；睡眠/重启后在 06:00 前补提醒。"
            "弹窗只阻止本软件，不锁整个电脑。尿布专项默认不提醒，可在插件面板启用。"
            "官网解析目前适配 Valle Lomellina 的 TeknoService 日历格式；遇到格式变化保留旧数据并报错。")
        self.note.setText(self.note.text()+" 提醒音持续循环，推迟/确认后停止。翻译优先 Google 网页接口，失败时用 MyMemory；免费服务可能限流，失败保留原文并可重试。机器译文仅辅助阅读，不自动改写投放规则。")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch(1)
        self.original = {}

    def _change_mode(self):
        morning = self.mode.currentData() == "morning"
        self.at.setTimeRange(QtCore.QTime(0 if morning else 22, 0), QtCore.QTime(5, 59) if morning else QtCore.QTime(23, 59))
        self.at.setTime(QtCore.QTime(5, 30) if morning else QtCore.QTime(22, 0))

    def _browse(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self.widget, "选择年度日历目录")
        if folder:
            self.folder.setText(folder)

    def _browse_sound(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.widget, "选择提醒歌曲", self.sound_file.text(),
            "音频文件 (*.mp3 *.wav *.flac *.ogg *.m4a *.aac);;所有文件 (*)")
        if path:
            self.sound_file.setText(path)

    def load_config(self, config):
        value = normalize_settings(config.get(CONFIG_KEY))
        self.original = value
        self.enabled.setChecked(value["enabled"])
        self.mode.setCurrentIndex(self.mode.findData(value["mode"]))
        self._change_mode()
        self.at.setTime(QtCore.QTime.fromString(value["reminder_time"], "HH:mm"))
        self.language.setCurrentIndex(self.language.findData(value["language"]))
        for widget, key in ((self.commune, "commune"), (self.timezone, "timezone"), (self.folder, "calendar_folder"),
                            (self.pattern, "calendar_pattern"), (self.page, "official_page")):
            widget.setText(value[key])
        self.snooze.setValue(value["snooze_minutes"])
        self.empty.setChecked(value["notify_empty"])
        self.sound.setChecked(value["sound"])
        self.auto.setChecked(value["auto_update"])
        self.translate.setChecked(value["translate_news"])
        self.sound_file.setText(value["sound_file"])
        self.volume.setValue(value["sound_volume"])

    def update_config(self, config):
        pattern = self.pattern.text().strip()
        if "{year}" not in pattern or Path(pattern).name != pattern or any(x in pattern for x in ("/", "\\")):
            raise ValueError("年度日历文件名必须包含 {year}，且不能包含目录。")
        if not QtCore.QTimeZone(self.timezone.text().strip().encode()).isValid():
            raise ValueError("Comune 时区无效。")
        if self.auto.isChecked() and not allowed_url(self.page.text().strip()):
            raise ValueError("自动更新只支持 Valle Lomellina / TeknoService 官方网址。")
        value = dict(self.original, enabled=self.enabled.isChecked(), mode=self.mode.currentData(),
                     reminder_time=self.at.time().toString("HH:mm"), language=self.language.currentData(),
                     commune=self.commune.text().strip(), timezone=self.timezone.text().strip(),
                     calendar_folder=self.folder.text().strip(), calendar_pattern=pattern,
                     official_page=self.page.text().strip(), snooze_minutes=self.snooze.value(),
                     notify_empty=self.empty.isChecked(), sound=self.sound.isChecked(), auto_update=self.auto.isChecked(),
                     sound_file=self.sound_file.text().strip(), sound_volume=self.volume.value(),
                     translate_news=self.translate.isChecked())
        config[CONFIG_KEY] = normalize_settings(value)
        return config

"""Searchable timing settings, with units, purpose and reversible defaults."""
from qt_compat import QtWidgets
from .timing import CONFIG_KEY, SPECS, value, normalize, validate


class TimingSettings(QtWidgets.QWidget):
    def __init__(self, parent=None, *, preferences=None):
        super().__init__(parent)
        self.widget = self
        self.legacy_preferences = preferences or {}
        self.loading, self.dirty = False, False
        self.edits, self.rows, self.groups = {}, {}, {}
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel('余额有效期、低积分冷却和页面失联是三件事。检测失败或页面失联会保留上次余额；'
            '过期后空闲时重新确认，已提交任务继续生成和下载。点击程序设置窗口的“确定”统一保存，主程序采用新参数并同步新版扩展。', self)
        hint.setWordWrap(True); layout.addWidget(hint)
        self.search = QtWidgets.QLineEdit(self); self.search.setPlaceholderText('搜索时间设置，例如：积分、下载、配对、超时')
        layout.addWidget(self.search)
        self.tabs = QtWidgets.QTabWidget(self); layout.addWidget(self.tabs, 1)
        for group in dict.fromkeys(spec.group for spec in SPECS):
            scroll = QtWidgets.QScrollArea(self); scroll.setWidgetResizable(True)
            page = QtWidgets.QWidget(scroll); box = QtWidgets.QVBoxLayout(page)
            form = QtWidgets.QFormLayout(); form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            box.addLayout(form); box.addStretch(); scroll.setWidget(page)
            self.tabs.addTab(scroll, group); self.groups[group] = (scroll, form)
        for spec in SPECS:
            if spec.unit == '秒序列':
                edit = QtWidgets.QLineEdit(self)
                edit.textEdited.connect(self.changed)
            else:
                edit = QtWidgets.QSpinBox(self) if spec.unit == '次' else QtWidgets.QDoubleSpinBox(self)
                divisor = 3600 if spec.unit == '小时' else 60 if spec.unit == '分钟' else 1
                if isinstance(edit, QtWidgets.QDoubleSpinBox): edit.setDecimals(3)
                if spec.unit == '次': edit.setRange(int(spec.minimum), int(spec.maximum))
                else: edit.setRange(spec.minimum / divisor, spec.maximum / divisor)
                edit.setSuffix(' ' + spec.unit)
                if spec.key == 'credit_validity_seconds': edit.setSpecialValueText('不过期（仍核对提交账号）')
                edit.valueChanged.connect(self.changed)
            edit.setObjectName(spec.key); edit.setToolTip(spec.help)
            field = QtWidgets.QWidget(self); row = QtWidgets.QHBoxLayout(field); row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(edit, 0)
            help_label = QtWidgets.QLabel(spec.help, field); help_label.setWordWrap(True); row.addWidget(help_label, 1)
            label = QtWidgets.QLabel(spec.label, self)
            self.groups[spec.group][1].addRow(label, field)
            self.edits[spec.key], self.rows[spec.key] = edit, (label, field)
        buttons = QtWidgets.QHBoxLayout()
        self.defaults_button = QtWidgets.QPushButton('恢复默认值（保存后生效）', self)
        self.defaults_button.clicked.connect(lambda: self.restore_defaults()); buttons.addWidget(self.defaults_button); buttons.addStretch()
        layout.addLayout(buttons)
        self.status = QtWidgets.QLabel('通过程序设置窗口的“确定”统一保存；“取消”会放弃本次修改。', self)
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.search.textChanged.connect(self.filter)
        self.load(self.legacy_preferences)

    def changed(self, *_args):
        if not self.loading:
            self.dirty = True; self.status.setText('有未保存的时间设置。')

    def load(self, preferences):
        self.loading = True
        try:
            for spec in SPECS:
                current = value(preferences, spec.key); edit = self.edits[spec.key]
                if spec.unit == '秒序列': edit.setText(', '.join(f'{item:g}' for item in current))
                else:
                    divisor = 3600 if spec.unit == '小时' else 60 if spec.unit == '分钟' else 1
                    edit.setValue(int(current) if spec.unit == '次' else current / divisor)
            self.dirty = False
        finally: self.loading = False

    def load_config(self, config):
        raw = config.get(CONFIG_KEY)
        self.load({'timing': raw} if isinstance(raw, dict) else self.legacy_preferences)

    def update_config(self, config):
        config[CONFIG_KEY] = self.values()
        return config

    def validate(self):
        self.values()

    def values(self):
        raw = {}
        for spec in SPECS:
            edit = self.edits[spec.key]
            divisor = 3600 if spec.unit == '小时' else 60 if spec.unit == '分钟' else 1
            raw[spec.key] = edit.text() if spec.unit == '秒序列' else edit.value() * divisor
        return validate({'timing': normalize(raw)})

    def restore_defaults(self):
        self.load({}); self.dirty = True
        self.status.setText('已填入默认值；点击程序设置窗口的“确定”后生效。')

    def filter(self, text):
        text = text.strip().casefold()
        matches = {group: False for group in self.groups}
        for spec in SPECS:
            visible = not text or text in (spec.label + spec.help + spec.key).casefold()
            for widget in self.rows[spec.key]: widget.setVisible(visible)
            matches[spec.group] |= visible
        for index, group in enumerate(self.groups): self.tabs.setTabVisible(index, matches[group])
        if not any(matches.values()): self.status.setText('没有匹配的时间设置。')

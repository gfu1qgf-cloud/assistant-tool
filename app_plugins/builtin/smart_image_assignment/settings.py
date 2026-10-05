from qt_compat import QtWidgets

CONFIG_KEY = "smart_image_assignment"
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    result = dict(value)
    result["model"] = str(value.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    for key, default, low, high in (("candidate_count", 6, 3, 12), ("min_fit", 60, 0, 100)):
        try:
            result[key] = min(high, max(low, int(value.get(key, default))))
        except (TypeError, ValueError, OverflowError):
            result[key] = default
    return result


class SmartAssignmentSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        self.model.setEditable(True)
        self.model.addItems([DEFAULT_MODEL, "gemini-3.1-flash-lite"])
        self.candidate_count = QtWidgets.QSpinBox()
        self.candidate_count.setRange(3, 12)
        self.min_fit = QtWidgets.QSpinBox()
        self.min_fit.setRange(0, 100)
        self.min_fit.setToolTip("AI 的主观适配评分，不是准确率；低于此分数不会自动勾选。")
        form.addRow("Gemini 模型", self.model)
        form.addRow("每任务候选图片", self.candidate_count)
        form.addRow("自动推荐最低评分", self.min_fit)
        layout.addLayout(form)
        note = QtWidgets.QLabel(
            "仅处理你勾选的素材／人物素材条目。先复用“智能搜图”的模型和增量索引筛选候选，"
            "再把候选缩略图和完整任务文案发给 Gemini 核对，不发送文件路径或管理员信息。"
            "首次需在智能搜图中下载当前检索模型。匹配结果缓存于本机；确认后才移动文件。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        self.load_config({})

    def load_config(self, config):
        self.original = normalize_settings(config.get(CONFIG_KEY))
        self.model.setCurrentText(self.original["model"])
        for field in ("candidate_count", "min_fit"):
            getattr(self, field).setValue(self.original[field])

    def validate(self):
        if not self.model.currentText().strip():
            raise ValueError("请填写智能图片分配使用的 Gemini 模型。")

    def update_config(self, config):
        self.validate()
        config[CONFIG_KEY] = normalize_settings(dict(
            self.original, model=self.model.currentText().strip(),
            candidate_count=self.candidate_count.value(), min_fit=self.min_fit.value(),
        ))
        return config

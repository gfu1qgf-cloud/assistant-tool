from qt_compat import QtWidgets

CONFIG_KEY = "cooking_assistant"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
CONDIMENTS = ("盐", "食用油", "酱油", "黑胡椒", "醋", "糖", "蒜", "姜", "辣椒", "黄油")


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    result = dict(value)
    for key, default, limit in (("people", 1, 20), ("meals", 2, 6), ("warn_days", 2, 14)):
        try:
            result[key] = max(1, min(limit, int(value.get(key, default))))
        except (TypeError, ValueError):
            result[key] = default
    result["model"] = str(value.get("model") or DEFAULT_MODEL).strip()
    result["notify"] = bool(value.get("notify", True))
    return result


class CookingSettingsPage:
    def __init__(self, context, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        self.model.setEditable(True)
        self.model.addItems([DEFAULT_MODEL, "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"])
        self.model.setToolTip("默认使用轻量 Flash-Lite；仍可手动填写其他可用模型。403 权限错误不能靠降级模型保证解决。")
        self.people, self.meals, self.warn_days = (QtWidgets.QSpinBox() for _ in range(3))
        self.people.setRange(1, 20)
        self.meals.setRange(1, 6)
        self.warn_days.setRange(1, 14)
        form.addRow("Gemini 模型", self.model)
        form.addRow("默认几人吃", self.people)
        form.addRow("默认做几顿", self.meals)
        form.addRow("提前几天提醒检查食材", self.warn_days)
        layout.addLayout(form)
        self.notify = QtWidgets.QCheckBox("临期／待检查食材每天提醒一次（程序运行时）")
        layout.addWidget(self.notify)
        keys = QtWidgets.QPushButton("管理共享 Gemini 密钥…")
        keys.clicked.connect(lambda: context.open_gemini_key_manager())
        layout.addWidget(keys)
        note = QtWidgets.QLabel("库存与最近菜谱保存在本机，不自动联网。点击生成时，只把选中的食材、调料和偏好发给 Gemini。"
                               "检查日期是你设置的提醒，不代表食材一定安全；包装食用截止日与实际储存情况优先。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        self.original = normalize_settings()
        self.load_config({})

    def load_config(self, config):
        self.original = normalize_settings(config.get(CONFIG_KEY))
        self.model.setCurrentText(self.original["model"])
        for key in ("people", "meals", "warn_days"):
            getattr(self, key).setValue(self.original[key])
        self.notify.setChecked(self.original["notify"])

    def validate(self):
        if not self.model.currentText().strip():
            raise ValueError("请填写 Gemini 模型名称。")

    def update_config(self, config):
        self.validate()
        value = dict(self.original, model=self.model.currentText().strip(),
            people=self.people.value(), meals=self.meals.value(), warn_days=self.warn_days.value(),
            notify=self.notify.isChecked())
        config[CONFIG_KEY] = normalize_settings(value)
        return config

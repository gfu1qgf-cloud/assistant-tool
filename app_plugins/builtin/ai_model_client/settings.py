from copy import deepcopy

from qt_compat import QtWidgets
from model.AIProviderKeys import KEYS_CONFIG_KEY, ENV_KEYS, protect_key

CONFIG_KEY = "ai_model_client"


def default_options():
    return {"system": "", "rounds": 20, "max_tokens": 4096,
            "temperature": None, "free_only": True, "foreign_only": True, "cf_free_only": True}


class ClientSettingsPage:
    def __init__(self, context, parent=None):
        self.context = context
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        tip = QtWidgets.QLabel("Gemini 直接复用主程序密钥，下面只配置新增服务。密钥通过 Windows 当前用户加密保存，不写入对话和日志。", self.widget)
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.gemini_button = QtWidgets.QPushButton("管理已有 Gemini 密钥…", self.widget)
        self.gemini_button.clicked.connect(self.open_gemini)
        layout.addWidget(self.gemini_button)
        self.edits, self.clear_checks = {}, {}
        form = QtWidgets.QFormLayout()
        for provider, title in (("mistral", "Mistral"), ("openrouter", "OpenRouter"),
                                ("cloudflare", "Cloudflare"), ("groq", "Groq")):
            row = QtWidgets.QWidget(self.widget)
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QtWidgets.QLineEdit(row)
            edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("填写新 Key；留空不修改")
            clear = QtWidgets.QCheckBox("移除已保存的 Key", row)
            row_layout.addWidget(edit, 1)
            row_layout.addWidget(clear)
            form.addRow(title + " Key", row)
            self.edits[provider], self.clear_checks[provider] = edit, clear
        layout.addLayout(form)
        self.account = QtWidgets.QLineEdit(self.widget)
        self.account.setPlaceholderText("Account ID（32位十六进制）；不是 Token")
        form.addRow("Cloudflare Account ID", self.account)
        cloudflare_tip = QtWidgets.QLabel("Cloudflare：控制台 Workers AI → Use REST API，复制 Account ID 并创建专用 Token（Workers AI Read + Edit，限自己的账号）。无需部署 Worker。每日10000 Neurons，不等于tokens；Free账户超额报错，Paid账户超额可能收费，余额请在Cloudflare控制台核对。", self.widget)
        cloudflare_tip.setWordWrap(True)
        layout.addWidget(cloudflare_tip)
        groq_tip = QtWidgets.QLabel("Groq：在 GroqCloud 控制台创建 API Key。Free 账号有组织级速率/每日限额；Developer 账号可能计费，实际限制与消费以控制台为准。图片模型可能被“国外厂商”筛选隐藏。", self.widget)
        groq_tip.setWordWrap(True)
        layout.addWidget(groq_tip)
        env = QtWidgets.QLabel("也支持 MISTRAL_API_KEY / OPENROUTER_API_KEY / CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / GROQ_API_KEY 环境变量。组员配置不要包含本机加密密钥；换电脑或 Windows 用户后需重新填写。", self.widget)
        env.setWordWrap(True)
        layout.addWidget(env)
        self.free_only = QtWidgets.QCheckBox("OpenRouter 默认只用免费模型（不自动换收费款）", self.widget)
        self.foreign_only = QtWidgets.QCheckBox("默认筛选国外厂商模型", self.widget)
        self.cf_free_only = QtWidgets.QCheckBox("Cloudflare 默认仅选已核实适用每日免费额度的模型（不是零费用保证）", self.widget)
        self.foreign_only.setToolTip("按已知发布者筛选，不代表推理服务器所在国家；未知发布者默认不列出，可手动关闭筛选。")
        layout.addWidget(self.free_only)
        layout.addWidget(self.foreign_only)
        layout.addWidget(self.cf_free_only)
        layout.addStretch(1)
        self.load_config(context.load_config())

    def load_config(self, config):
        keys = config.get(KEYS_CONFIG_KEY, {})
        keys = keys if isinstance(keys, dict) else {}
        for provider, edit in self.edits.items():
            edit.clear()
            edit.setPlaceholderText("已保存加密 Key；留空不修改" if keys.get(provider) else "未保存；可填写 Key 或使用 " + ENV_KEYS[provider])
            self.clear_checks[provider].setChecked(False)
        settings = config.get(CONFIG_KEY) or {}
        settings = settings if isinstance(settings, dict) else {}
        self.free_only.setChecked(settings.get("free_only", True))
        self.foreign_only.setChecked(settings.get("foreign_only", True))
        self.cf_free_only.setChecked(settings.get("cf_free_only", True))
        self.account.setText(str(settings.get("cloudflare_account_id") or ""))

    def open_gemini(self):
        try:
            self.context.open_gemini_key_manager()
        except Exception as error:
            from model.SensitiveData import redact_sensitive_text
            QtWidgets.QMessageBox.warning(self.widget, "Gemini 密钥管理", redact_sensitive_text(str(error)))

    def validate(self):
        import re
        if self.account.text().strip() and not re.fullmatch(r"[0-9a-fA-F]{32}", self.account.text().strip()):
            raise ValueError("Cloudflare Account ID 应为32位十六进制，请不要把 API Token 填到这里。")
        for edit in self.edits.values():
            key = edit.text().strip()
            if key and (len(key) > 4096 or any(c.isspace() for c in key)):
                raise ValueError("API Key 不能含空白，且长度不能超过4096字符。")

    def update_config(self, config):
        self.validate()
        source = config.get(KEYS_CONFIG_KEY, {})
        keys = deepcopy(source) if isinstance(source, dict) else {}
        for provider, edit in self.edits.items():
            if edit.text().strip():
                keys[provider] = protect_key(edit.text())
            elif self.clear_checks[provider].isChecked():
                keys.pop(provider, None)
        if keys or KEYS_CONFIG_KEY in config:
            config[KEYS_CONFIG_KEY] = keys
        previous = config.get(CONFIG_KEY) or {}
        settings = dict(previous) if isinstance(previous, dict) else {}
        settings.update(free_only=self.free_only.isChecked(), foreign_only=self.foreign_only.isChecked(),
                        cf_free_only=self.cf_free_only.isChecked(), cloudflare_account_id=self.account.text().strip().lower())
        config[CONFIG_KEY] = settings

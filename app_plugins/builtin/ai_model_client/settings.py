from copy import deepcopy
import os

from qt_compat import QtWidgets
from model.AIProviderKeys import KEYS_CONFIG_KEY, ENV_KEYS, protect_key

CONFIG_KEY = "ai_model_client"


def default_options():
    return {"system": "", "rounds": 20, "max_tokens": 4096,
            "temperature": None, "free_only": True, "foreign_only": True, "cf_free_only": True,
            'reasoning_effort':'default','reasoning_mode':'default','verbosity':'default',
            'thinking_budget':None,'top_p':None,'web_search':False,'search_mode':'auto','search_limit':3}


class ClientSettingsPage:
    def __init__(self, context, parent=None):
        self.context = context
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        tip = QtWidgets.QLabel("选择要配置的服务商。切换服务商不会丢失尚未保存的输入；最后统一点击程序设置的保存。", self.widget)
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.service = QtWidgets.QComboBox(self.widget)
        self.pages = QtWidgets.QStackedWidget(self.widget)
        self.provider_pages = {}
        self.edits, self.clear_checks, self.key_status = {}, {}, {}
        selector = QtWidgets.QFormLayout()
        selector.addRow("服务商", self.service)
        layout.addLayout(selector)
        layout.addWidget(self.pages)
        notes = {
            'openai': '填写赠送额度所属 API 组织／项目的 Key，仅连接 api.openai.com。保存后打开聊天并选择 OpenAI / GPT，会自动读取账号模型列表；按 API 用量计费，余额以官方账单为准。',
            'gemini': '复用主程序的 Gemini 密钥管理器，不另存一份。点击下方按钮即可管理现有密钥。',
            'mistral': '填写 Mistral 官方 API Key。具体可用模型、限流与费用以账号为准。',
            'openrouter': '填写 OpenRouter Key，可默认只列出已确认零价格的具体模型；不会自动改用收费款。',
            'cloudflare': '同时填写 Account ID 和专用 API Token（Workers AI Read + Edit）。每日额度按 Neurons 计算，不等于 tokens；付费账号超额可能计费。',
            'groq': '填写 GroqCloud API Key。Free 账号有速率／每日限额，Developer 账号可能计费；图片模型可能被国外厂商筛选隐藏。',
        }
        for provider, title in (('openai','OpenAI / GPT'), ('gemini','Gemini'), ('mistral','Mistral'),
                                ('openrouter','OpenRouter'), ('cloudflare','Cloudflare'), ('groq','Groq')):
            self.service.addItem(title, provider)
            page = QtWidgets.QWidget(self.pages)
            self.provider_pages[provider] = page
            body = QtWidgets.QVBoxLayout(page)
            body.setContentsMargins(0, 8, 0, 0)
            heading = QtWidgets.QLabel(title + ' 连接配置', page)
            font = heading.font(); font.setBold(True); heading.setFont(font)
            body.addWidget(heading)
            note = QtWidgets.QLabel(notes[provider], page)
            note.setWordWrap(True); body.addWidget(note)
            if provider == 'gemini':
                self.gemini_button = QtWidgets.QPushButton('管理已有 Gemini 密钥…', page)
                self.gemini_button.clicked.connect(self.open_gemini)
                body.addWidget(self.gemini_button)
            else:
                status = QtWidgets.QLabel(page); body.addWidget(status)
                self.key_status[provider] = status
                form = QtWidgets.QFormLayout()
                edit = QtWidgets.QLineEdit(page)
                edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
                form.addRow('API Token' if provider == 'cloudflare' else 'API Key', edit)
                clear = QtWidgets.QCheckBox('移除已保存的密钥（保存后生效）', page)
                form.addRow('', clear)
                self.edits[provider], self.clear_checks[provider] = edit, clear
                if provider == 'cloudflare':
                    self.account = QtWidgets.QLineEdit(page)
                    self.account.setPlaceholderText('32位十六进制，不是 API Token')
                    form.addRow('Account ID', self.account)
                body.addLayout(form)
                env = QtWidgets.QLabel('也可使用环境变量 ' + ENV_KEYS[provider] +
                    (' / CLOUDFLARE_ACCOUNT_ID' if provider == 'cloudflare' else '') + '。', page)
                env.setWordWrap(True); body.addWidget(env)
            if provider == 'openrouter':
                self.free_only = QtWidgets.QCheckBox('默认只用免费模型（不自动换收费款）', page)
                body.addWidget(self.free_only)
            if provider == 'cloudflare':
                self.cf_free_only = QtWidgets.QCheckBox('默认仅选已核实适用每日额度的模型（非零费用保证）', page)
                body.addWidget(self.cf_free_only)
            body.addStretch(1)
            self.pages.addWidget(page)
        self.service.currentIndexChanged.connect(self.pages.setCurrentIndex)
        common = QtWidgets.QGroupBox('通用默认设置', self.widget)
        common_layout = QtWidgets.QVBoxLayout(common)
        self.foreign_only = QtWidgets.QCheckBox("默认筛选国外厂商模型", common)
        self.foreign_only.setToolTip("按已知发布者筛选，不代表推理服务器所在国家；未知发布者默认不列出，可手动关闭筛选。")
        common_layout.addWidget(self.foreign_only)
        security = QtWidgets.QLabel('密钥使用 Windows 当前用户加密，不写入对话或日志。留空不改动；换电脑／Windows 用户后需重新填写。', common)
        security.setWordWrap(True); common_layout.addWidget(security)
        layout.addWidget(common)
        layout.addStretch(1)
        self.load_config(context.load_config())

    def load_config(self, config):
        keys = config.get(KEYS_CONFIG_KEY, {})
        keys = keys if isinstance(keys, dict) else {}
        for provider, edit in self.edits.items():
            edit.clear()
            edit.setPlaceholderText("已保存加密 Key；留空不修改" if keys.get(provider) else "未保存；可填写 Key 或使用 " + ENV_KEYS[provider])
            self.clear_checks[provider].setChecked(False)
            self.key_status[provider].setText('已保存加密密钥' if keys.get(provider) else
                '已检测到环境变量密钥' if os.environ.get(ENV_KEYS[provider], '').strip() else '尚未配置密钥')
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

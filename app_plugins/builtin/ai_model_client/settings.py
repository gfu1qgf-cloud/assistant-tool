from copy import deepcopy

from qt_compat import QtWidgets
from model.AIProviderKeys import KEYS_CONFIG_KEY, ENV_KEYS
from .key_list import ProviderKeysEditor

CONFIG_KEY = "ai_model_client"


def default_options():
    return {"system": "", "rounds": 20, "max_tokens": 4096,
            'context_chars': 0, 'history_images': True,
            'request_mode': 'instant',
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
        self.editors = {}
        selector = QtWidgets.QFormLayout()
        selector.addRow("服务商", self.service)
        layout.addLayout(selector)
        self.total_keys=QtWidgets.QLabel(self.widget)
        self.total_keys.setWordWrap(True); layout.addWidget(self.total_keys)
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
                editor=ProviderKeysEditor(provider,page,account_provider=lambda:self.account.text())
                self.editors[provider]=editor
                editor.changed.connect(self.update_counts)
                body.addWidget(editor)
                form = QtWidgets.QFormLayout()
                if provider == 'cloudflare':
                    self.account = QtWidgets.QLineEdit(page)
                    self.account.setPlaceholderText('旧密钥的兼容值；新Key可分别填写 Account ID')
                    form.addRow('兼容 Account ID', self.account)
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
        security = QtWidgets.QLabel('列表只显示脱敏标识；密钥使用 Windows 当前用户加密，不写入对话或日志。换电脑／Windows 用户后需重新填写。', common)
        security.setWordWrap(True); common_layout.addWidget(security)
        layout.addWidget(common)
        layout.addStretch(1)
        self.load_config(context.load_config())

    def load_config(self, config):
        keys = config.get(KEYS_CONFIG_KEY, {})
        keys = keys if isinstance(keys, dict) else {}
        # MainSettingDialog.get_config() starts with a fresh partial dict, not
        # the loaded config. Include untouched providers in that projection;
        # otherwise saving one provider deletes all the others on disk.
        self._loaded_keys = deepcopy(keys)
        for provider, editor in self.editors.items():
            editor.load(keys.get(provider))
        settings = config.get(CONFIG_KEY) or {}
        settings = settings if isinstance(settings, dict) else {}
        self.free_only.setChecked(settings.get("free_only", True))
        self.foreign_only.setChecked(settings.get("foreign_only", True))
        self.cf_free_only.setChecked(settings.get("cf_free_only", True))
        self.account.setText(str(settings.get("cloudflare_account_id") or ""))
        self.update_counts()

    def update_counts(self):
        total=sum(len(editor.pool['items']) for editor in self.editors.values())
        self.total_keys.setText(f'共保存 {total} 个 Key（不含环境变量；Gemini 仍由主程序密钥库管理）。')
        names={'openai':'OpenAI / GPT','mistral':'Mistral','openrouter':'OpenRouter','cloudflare':'Cloudflare','groq':'Groq'}
        for provider,editor in self.editors.items():
            index=self.service.findData(provider)
            self.service.setItemText(index,names[provider]+f'（{len(editor.pool["items"])} 个 Key）')

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

    def update_config(self, config):
        self.validate()
        source = config.get(KEYS_CONFIG_KEY, self._loaded_keys)
        keys = deepcopy(source) if isinstance(source, dict) else {}
        for provider, editor in self.editors.items():
            if not editor.dirty: continue
            value=editor.value()
            if (keys.get(provider)!=editor.original and keys.get(provider)!=editor.exported_value
                    and keys.get(provider)!=value):
                raise ValueError('该服务商的密钥列表已被其他窗口修改，请重新打开设置后再操作。')
            if value is not None: keys[provider]=value
            else:
                keys.pop(provider, None)
            editor.exported_value=deepcopy(value)
        if keys or KEYS_CONFIG_KEY in config:
            config[KEYS_CONFIG_KEY] = keys
        previous = config.get(CONFIG_KEY) or {}
        settings = dict(previous) if isinstance(previous, dict) else {}
        settings.update(free_only=self.free_only.isChecked(), foreign_only=self.foreign_only.isChecked(),
                        cf_free_only=self.cf_free_only.isChecked(), cloudflare_account_id=self.account.text().strip().lower())
        config[CONFIG_KEY] = settings

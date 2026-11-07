"""Expose the existing host OpenAI key pool, even if chat plugin is disabled."""
from copy import deepcopy
from qt_compat import QtWidgets
from model.AIProviderKeys import KEYS_CONFIG_KEY
from app_plugins.builtin.ai_model_client.key_list import ProviderKeysEditor


class RemixSettingsPage:
    def __init__(self, context, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        note = QtWidgets.QLabel('与AI模型试用台共用主程序的OpenAI密钥列表，不另存Key。'
            '已提交批次绑定原Key；更换当前Key只影响新批次。删除原Key前请先收取旧结果。', self.widget)
        note.setWordWrap(True); layout.addWidget(note)
        self.editor = ProviderKeysEditor('openai', self.widget)
        layout.addWidget(self.editor); layout.addStretch(1)
        self.load_config(context.load_config())

    def load_config(self, config):
        pools = config.get(KEYS_CONFIG_KEY) or {}
        self.editor.load(pools.get('openai'))

    def validate(self):
        if self.editor.dirty: self.editor.value()

    def update_config(self, config):
        if not self.editor.dirty:
            return
        pools = deepcopy(config.get(KEYS_CONFIG_KEY) or {})
        value = self.editor.value()
        if pools.get('openai') not in (self.editor.original, self.editor.exported_value, value):
            raise ValueError('OpenAI密钥列表已在其他设置页修改，请重新打开设置后统一修改。')
        if value is None: pools.pop('openai', None)
        else: pools['openai'] = value
        config[KEYS_CONFIG_KEY] = pools
        self.editor.exported_value = deepcopy(value)

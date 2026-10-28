"""Chrome is an internal controller of the single Flow/browser host plugin."""
import json
import math
from contextlib import closing

from qt_compat import QtWidgets
from ..chrome_launcher import ChromeLauncherSettingsPage

SKIP_CONFIG_KEY = 'chrome_flow_skip_exhausted'


def launcher_for(plugin):
    launcher = getattr(plugin, 'browser_launcher', None)
    if getattr(launcher, 'dialog', None) is not None: return launcher
    parent = getattr(getattr(plugin, 'context', None), 'parent_widget', None)
    return getattr(parent, 'chrome_plugin', None)


class LauncherContext:
    """One host owner, with the established Chrome private-data namespace."""
    def __init__(self, context):
        self.context = context

    def __getattr__(self, name):
        return getattr(self.context, name)

    @property
    def storage(self):
        namespace = self.context.storage
        application = getattr(namespace, 'storage', None)
        return application.for_plugin('chrome_launcher') if application is not None else namespace


class BrowserManagerSettings(ChromeLauncherSettingsPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.skip_exhausted = QtWidgets.QCheckBox('启动下一个时，跳过 Flow 已确认不足10积分的账号', self.widget)
        self.skip_exhausted.setObjectName('chrome_flow_skip_exhausted')
        self.widget.layout().insertWidget(2, self.skip_exhausted)
        hint = QtWidgets.QLabel('已标记不可用的账号始终跳过，不受上方积分选项影响；需重置账号状态后才恢复迭代。'
            '积分规则使用实时余额及已保存的冷却记录；未知、过期或已到刷新时间的账号允许重新检测。'
            '快捷键、主界面按钮、分组和选中项迭代共用此规则。手动“启动选中”仍可打开指定账号。', self.widget)
        hint.setWordWrap(True)
        self.widget.layout().insertWidget(3, hint)

    def load_config(self, config):
        super().load_config(config)
        self.skip_exhausted.setChecked(skip_setting(config))

    def update_config(self, config):
        super().update_config(config)
        config[SKIP_CONFIG_KEY] = self.skip_exhausted.isChecked()


def skip_setting(config):
    value = config.get(SKIP_CONFIG_KEY, True)
    if type(value) is not bool:
        raise ValueError('启动下一个的积分跳过选项：请选择开启或关闭。')
    return value


def saved_preferences(context):
    saved = context.storage.file('queue.sqlite3')
    if not saved.exists(): return {}
    with closing(saved.connect(read_only=True, timeout=1)) as database:
        row = database.execute('SELECT value FROM settings WHERE key="preferences"').fetchone()
        return json.loads(row[0]) if row else {}


def skip_reason(profile, preferences, clients, *, skip_exhausted=True):
    from .account_policy import hold_reason
    from .credit_policy import balance_ready
    from .credit_cooldown import credit_hold, credit_reason, MIN_CREDITS
    from .credit_refresh import due
    from .image_affinity import cached_profile
    live = [client for client in clients if client.get('profile') == profile and
        client.get('account') and not client.get('identity_unconfirmed')]
    client = max(live, key=lambda item:item.get('credits_at', 0)) if live else cached_profile(profile, preferences)
    unavailable=hold_reason(client,preferences)
    if unavailable:return unavailable
    if not skip_exhausted:return ''
    if not client.get('account') or due(client, preferences): return ''
    hold = credit_hold(client, preferences)
    points = hold.get('balance') if hold else client.get('credits')
    if type(points) not in (int,float) or not math.isfinite(points) or not 0 <= points < MIN_CREDITS:
        return ''
    if hold: return credit_reason(client, preferences)
    return f'Flow已确认剩余 {points:g} 分，低于10分；余额刷新／过期后重新检测' if balance_ready(client, preferences=preferences) else ''

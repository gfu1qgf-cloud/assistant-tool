"""Host integration for the Antigravity multi-account switcher plugin."""

from __future__ import annotations

from qt_compat import QtCore, QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand

from .dialog import AntigravitySwitcherDialog
from .store import AccountSwitcherError, AntigravityProfileStore
from .clients import ClientSelectionError, discover_clients


class AntigravityAccountSwitcherPlugin:
    plugin_id = "antigravity_account_switcher"
    display_name = "Antigravity 多账号切换"
    version = "1.3"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.store = None
        self.dialog = None
        self.clients = []

    def _choose_client(self):
        self.clients = discover_clients()
        if not self.clients:
            raise ClientSelectionError("没有找到可确认的 Antigravity 客户端，未读取旧目录或其他软件的账号。")
        selected = self.context.storage.file('selected_client.json')
        data = selected.read_json() if selected.exists() else {}
        if not isinstance(data, dict):
            raise AccountSwitcherError('客户端选择记录无效，未自动改读其他目录。')
        # 1.2 could save the IDE precisely because it did not recognize Hub.
        # That old choice is not evidence of an explicit, correct user choice.
        saved = data.get('client_id') if data.get('selection_schema') == 2 else None
        active = [client for client in self.clients if client.running]
        if len(active) == 1:
            return active[0]
        for client in self.clients:
            if client.identity == saved:
                return client
        if len(self.clients) == 1:
            return self.clients[0]
        value, accepted = QtWidgets.QInputDialog.getItem(
            self.context.parent_widget, '选择 Antigravity 客户端',
            '检测、保存及启动将使用同一客户端和数据目录：',
            [client.title for client in self.clients], 0, False)
        return next((client for client in self.clients if accepted and client.title == value), None)

    def _client_store(self, client):
        accounts = self.context.storage.directory('accounts')
        store = AntigravityProfileStore(
            root=accounts.directory('clients/' + client.identity).path, client=client)
        store.legacy_archives = accounts.file('desktop-profiles.json').exists()
        return store

    def change_client(self, client):
        store = self._client_store(client)
        if self.dialog is not None and not self.dialog.set_store(store):
            return False
        self.store = store
        self._save_client_choice(client)
        return True

    def _save_client_choice(self, client):
        self.context.storage.file('selected_client.json').write_json({
            'selection_schema': 2, 'client_id': client.identity, 'auth_kind': client.auth_kind})

    def register(self, context):
        self.context = context
        context.register_command(
            PluginCommand(
                command_id="open",
                title="Antigravity 多账号切换…",
                callback=lambda _rows: self.open_manager(),
                locations=frozenset({MAIN_MENU}),
                tooltip="保存、绑定和切换 Antigravity 登录档案；本地状态不是实时剩余额度",
                order=56,
            )
        )

    def start(self):
        return True

    def open_manager(self):
        try:
            from model.QtThreadLifecycle import thread_is_active
            busy = self.dialog is not None and (thread_is_active(getattr(self.dialog, '_quota_worker', None)) or thread_is_active(getattr(self.dialog, '_operation_worker', None)))
            if not busy:
                client = self._choose_client()
                if client is None:
                    return None
                if self.store is None or self.store.client is None or self.store.client.identity != client.identity:
                    if self.dialog is not None:
                        if not self.change_client(client):
                            return None
                    else:
                        self.store = self._client_store(client)
                self._save_client_choice(client)
            if self.dialog is None:
                parent_widget = getattr(self.context, "parent_widget", None)
                self.dialog = AntigravitySwitcherDialog(self.store, parent_widget,
                    clients=self.clients, change_client=self.change_client)
                self.dialog.setModal(False)
                self.dialog.setWindowModality(QtCore.Qt.NonModal)
                self.dialog.destroyed.connect(self._clear_dialog)
            else:
                if not busy:
                    self.dialog.set_clients(self.clients)
                self.dialog.refresh_all()
        except (AccountSwitcherError, ClientSelectionError, OSError, ValueError) as error:
            if self.dialog is not None:
                self.dialog.hide()  # Do not leave an obsolete source displayed as current.
            if self.context and hasattr(self.context, "log"):
                self.context.log(f"无法打开 Antigravity 账号切换器：{error}")
            parent_widget = getattr(self.context, "parent_widget", None)
            QtWidgets.QMessageBox.critical(parent_widget, "账号切换器无法打开", str(error))
            return None

        if self.dialog.isMinimized():
            self.dialog.showNormal()
        else:
            self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def _clear_dialog(self, *_args):
        self.dialog = None

    def can_close(self):
        if self.dialog is not None and not self.dialog.shutdown():
            return False, "Antigravity 账号操作尚未完成，请稍后再退出。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            if not self.dialog.shutdown():
                return False
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

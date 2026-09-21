"""Host integration for the fully migrated Codex account-switcher."""

from __future__ import annotations

from PyQt5 import QtCore

from app_plugins.api import MAIN_MENU, PluginCommand

from .dialog import AccountSwitcherDialog
from .store import AccountProfileStore, AccountSwitcherError


class CodexAccountSwitcherPlugin:
    plugin_id = "codex_account_switcher"
    display_name = "Codex 多账号切换"
    version = "3.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.store = None
        self.dialog = None

    def register(self, context):
        self.context = context
        context.register_command(
            PluginCommand(
                command_id="open",
                title="Codex 多账号切换…",
                callback=lambda _rows: self.open_manager(),
                locations=frozenset({MAIN_MENU}),
                tooltip="切换 ChatGPT/Codex 桌面版账号并检测各账号剩余额度",
                order=55,
            )
        )

    def start(self):
        # Delay storage access until the host finishes creating its main window.
        # A corrupted external profile index is reported when the user opens this
        # plugin and cannot block the rest of the auxiliary tools.
        return True

    def open_manager(self):
        try:
            if self.store is None:
                self.store = AccountProfileStore()
            if self.dialog is None:
                self.dialog = AccountSwitcherDialog(
                    self.store, self.context.parent_widget
                )
                self.dialog.setModal(False)
                self.dialog.setWindowModality(QtCore.Qt.NonModal)
                self.dialog.destroyed.connect(self._clear_dialog)
            else:
                self.dialog.refresh_all()
        except (AccountSwitcherError, OSError) as error:
            self.context.log(f"无法打开账号切换器：{error}")
            from PyQt5 import QtWidgets

            QtWidgets.QMessageBox.critical(
                self.context.parent_widget, "账号切换器无法打开", str(error)
            )
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
        # Quota work is cancelled during stop; auth switching is synchronous and
        # completes before its initiating UI handler returns.
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

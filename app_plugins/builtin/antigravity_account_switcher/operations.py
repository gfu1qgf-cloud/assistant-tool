"""User-requested account changes run off the GUI thread, without forced kills."""
from qt_compat import QtCore
from .launcher import close_antigravity, is_antigravity_running, launch_antigravity
from .store import AccountSwitcherError


class AccountOperationWorker(QtCore.QThread):
    def __init__(self, store, operation, target_id=None, parent=None):
        super().__init__(parent)
        self.store, self.operation, self.target_id = store, operation, target_id
        self.error = ""
        self.changed = False
        self.client = getattr(store, 'client', None)

    def run(self):
        try:
            client_args = (self.client,) if self.client is not None else ()
            if is_antigravity_running(*client_args) and not close_antigravity(*client_args):
                raise AccountSwitcherError("Antigravity 未正常退出，未切换账号。请保存工作并手动退出后重试。")
            if self.operation == "recover":
                self.store._recover_interrupted_switch()
            else:
                self.store.switch_to(self.target_id, allow_unbound=self.operation == "bind")
            self.changed = True
            if self.operation != "recover":
                try:
                    launch_antigravity(**({'client': self.client} if self.client is not None else {}))
                except Exception:
                    self.error = "账号环境已经切换，但未能启动 Antigravity，请手动打开客户端；不要重复绑定。"
        except (OSError, AccountSwitcherError) as error:
            self.error = str(error)
        except Exception:
            # Never include raw auth values or arbitrary database output in UI/logs.
            self.error = "账号操作未完成，请保留账号仓库并核对恢复提示。"

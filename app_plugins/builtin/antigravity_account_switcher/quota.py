"""Quota and account profile inspector for Antigravity accounts."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qt_compat import QtCore
from model.AppDataStorage import data_file
from .identity import IdentityFormatError, has_oauth_token, parse_user_status
from .native_auth import NATIVE_AUTH_KEY, inspect_native_record, NativeAuthError


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class AntigravityQuotaInfo:
    profile_id: str
    success: bool
    status_text: str
    detail_text: str
    email: str | None = None
    name: str | None = None
    plan_type: str | None = None
    tier: str | None = None
    avatar_url: str | None = None
    error_message: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AntigravityQuotaInfo":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})


def inspect_auth_records(rows) -> dict[str, Any]:
    res = {"email": None, "name": None, "plan": None, "tier": None,
           "avatar": None, "has_token": False, "identity_error": None}
    if NATIVE_AUTH_KEY in rows:
        try:
            return inspect_native_record(rows[NATIVE_AUTH_KEY])
        except NativeAuthError:
            res['identity_error'] = '新版系统登录记录格式无法确认，不改读旧 IDE 的账号。'
            return res
    res['has_token'] = has_oauth_token(rows.get('antigravityUnifiedStateSync.oauthToken'))
    raw = rows.get('antigravityUnifiedStateSync.userStatus')
    if raw:
        try:
            res.update(parse_user_status(raw))
        except (IdentityFormatError, UnicodeError, TypeError):
            res['identity_error'] = '本地账号格式无法确认，不从其他文本猜测邮箱。'
    return res


def inspect_vscdb_auth(db_path: Path) -> dict[str, Any]:
    """Parse Antigravity authentication data directly from a state.vscdb file."""
    if not data_file(db_path).exists():
        return {}
    try:
        with closing(data_file(db_path).connect(read_only=True, timeout=.5)) as con:
            rows = dict(con.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'antigravity%'").fetchall())
    except sqlite3.Error:
        return {}

    return inspect_auth_records(rows)


def inspect_profile_credentials(profile_id: str, cred_dir: Path) -> AntigravityQuotaInfo:
    """Inspect stored Antigravity profile credentials and return structured quota/status info."""
    db_path = cred_dir / "state.vscdb"
    if not data_file(db_path).exists():
        return AntigravityQuotaInfo(
            profile_id=profile_id,
            success=False,
            status_text="未绑定",
            detail_text="未找到有效的 Antigravity 登录缓存文件。",
            error_message="auth_file_not_found",
        )

    info = inspect_vscdb_auth(db_path)
    return quota_from_identity(profile_id, info)


def quota_from_identity(profile_id, info):
    if not info.get("has_token") or not info.get('email'):
        return AntigravityQuotaInfo(
            profile_id=profile_id,
            success=False,
            status_text="账号格式未确认" if info.get('identity_error') else "登录状态未确认",
            detail_text=info.get('identity_error') or "未发现可确认的账号身份及完整OAuth缓存，请在所选客户端登录后再刷新。",
            email=info.get('email'),
            error_message="no_token",
        )

    email = info.get("email") or "—"
    name = info.get("name") or "—"
    plan = info.get("plan")
    tier = info.get("tier")

    lines = [
        f"账号: {email}",
        f"用户姓名: {name}",
        f"本地套餐记录: {plan or '未知'} ({tier or '未知'})",
        "登录缓存: 已发现；未联网验证是否过期",
        "实时剩余额度: 尚未接入，不从本地Token推断",
        f"检测时间: {datetime.now().strftime('%H:%M:%S')}",
    ]

    return AntigravityQuotaInfo(
        profile_id=profile_id,
        success=True,
        status_text=f"本地记录：{plan}" if plan else "已保存登录缓存",
        detail_text="\n".join(lines),
        email=email if email != "—" else None,
        name=name if name != "—" else None,
        plan_type=plan,
        tier=tier,
        avatar_url=info.get("avatar"),
        updated_at=utc_now(),
    )


class AntigravityQuotaWorker(QtCore.QThread):
    quota_ready = QtCore.pyqtSignal(str, object)
    all_finished = QtCore.pyqtSignal()

    def __init__(self, targets: list[tuple[str, Path]], parent=None, *, live_reader=None):
        super().__init__(parent)
        self._targets = list(targets)
        self._is_cancelled = False
        self._live_reader = live_reader

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        for profile_id, cred_dir in self._targets:
            if self._is_cancelled:
                break
            try:
                quota = (quota_from_identity(profile_id, inspect_auth_records(self._live_reader()))
                         if profile_id == '__live__' and self._live_reader is not None else
                         inspect_profile_credentials(profile_id, cred_dir))
            except Exception as err:
                quota = AntigravityQuotaInfo(
                    profile_id=profile_id,
                    success=False,
                    status_text="检测异常",
                    detail_text="账号来源暂时不可读，未自动读取其他软件或旧档案。",
                    error_message="account_source_unreadable",
                )
            if not self._is_cancelled:
                self.quota_ready.emit(profile_id, quota)
        if not self._is_cancelled:
            self.all_finished.emit()

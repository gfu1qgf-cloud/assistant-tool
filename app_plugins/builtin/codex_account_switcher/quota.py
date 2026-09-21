"""Quota detection migrated from the standalone account switcher.

Network work always runs in :class:`QuotaWorker`, never in the Qt UI thread.
The service response is treated as optional: every failure returns a displayable
``QuotaInfo`` rather than raising into the dialog.
"""

from __future__ import annotations

import base64
import json
import math
import os
import stat
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PyQt5 import QtCore


CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class QuotaInfo:
    profile_id: str
    success: bool
    status_text: str
    detail_text: str
    plan_type: str | None = None
    email: str | None = None
    primary_used_percent: int | None = None
    primary_remaining_percent: int | None = None
    secondary_used_percent: int | None = None
    secondary_remaining_percent: int | None = None
    limit_reached: bool = False
    reset_after_seconds: int | None = None
    credits_available: int | None = None
    error_message: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaInfo":
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def parse_jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def format_duration(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "未知"
    if not math.isfinite(seconds) or seconds < 0:
        return "未知"
    total = int(seconds)
    if total < 60:
        return f"{total}秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours, remaining = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{remaining}分" if remaining else f"{hours}小时"
    days, remaining = divmod(hours, 24)
    return f"{days}天{remaining}小时" if remaining else f"{days}天"


def format_plan_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "未知"
    mapping = {
        "team": "Team",
        "plus": "Plus",
        "pro": "Pro",
        "enterprise": "Enterprise",
        "free": "Free",
        "prolite": "Pro Lite",
        "business": "Business",
        "edu": "Edu",
    }
    value = value.strip()
    return mapping.get(value.lower(), value.capitalize())


def _number(value: Any, *, percent: bool = False) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0, min(100, int(round(number)))) if percent else max(0, int(number))


def _window_tag(window: dict[str, Any], fallback: str) -> str:
    seconds = _number(window.get("limit_window_seconds")) if window else None
    if not seconds:
        return fallback
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def format_quota_strings(
    plan_type: str | None,
    email: str | None,
    rate_limit: Any,
    reset_credits_count: Any = None,
) -> tuple[str, str, int | None, int | None, int | None, int | None, bool, int | None]:
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    primary = rate_limit.get("primary_window")
    secondary = rate_limit.get("secondary_window")
    if not isinstance(primary, dict) and isinstance(rate_limit.get("windows"), list):
        windows = [item for item in rate_limit["windows"] if isinstance(item, dict)]
        primary = windows[0] if windows else {}
        secondary = windows[1] if len(windows) > 1 else {}
    primary = primary if isinstance(primary, dict) else {}
    secondary = secondary if isinstance(secondary, dict) else {}
    primary_used = _number(primary.get("used_percent"), percent=True)
    secondary_used = _number(secondary.get("used_percent"), percent=True)
    primary_remaining = 100 - primary_used if primary_used is not None else None
    secondary_remaining = 100 - secondary_used if secondary_used is not None else None
    primary_reset = _number(primary.get("reset_after_seconds"))
    secondary_reset = _number(secondary.get("reset_after_seconds"))
    limit_reached = bool(rate_limit.get("limit_reached")) or rate_limit.get("allowed") is False
    if primary_remaining == 0 or secondary_remaining == 0:
        limit_reached = True
    if primary_remaining == 0 and secondary_remaining == 0:
        values = [value for value in (primary_reset, secondary_reset) if value is not None]
        reset_after = max(values) if values else None
    elif secondary_remaining == 0:
        reset_after = secondary_reset
    else:
        reset_after = primary_reset if primary_reset is not None else secondary_reset
    reset_text = f"（{format_duration(reset_after)}后重置）" if reset_after else ""
    primary_tag = _window_tag(primary, "5h")
    secondary_tag = _window_tag(secondary, "7d")
    values = []
    if primary_remaining is not None:
        values.append(f"{primary_tag}:{primary_remaining}%")
    if secondary_remaining is not None:
        values.append(f"{secondary_tag}:{secondary_remaining}%")
    if limit_reached:
        status = "已限流 " + (" / ".join(values) or "额度已用尽") + reset_text
    elif values:
        status = " | ".join(values) + reset_text
    else:
        status = f"正常（{format_plan_name(plan_type)}）{reset_text}"

    lines = []
    if email:
        lines.append(f"账号: {email}")
    lines.append(f"套餐类型: {format_plan_name(plan_type)}")
    lines.append("当前状态: " + ("【已限流】" if limit_reached else "正常可用"))
    for label, window, used, remaining, reset in (
        ("主窗口", primary, primary_used, primary_remaining, primary_reset),
        ("次窗口", secondary, secondary_used, secondary_remaining, secondary_reset),
    ):
        if remaining is None:
            continue
        duration = format_duration(window.get("limit_window_seconds"))
        item = f"{label} ({duration}): 剩余 {remaining}%（已用 {used}%）"
        if reset:
            item += f"，约 {format_duration(reset)} 后重置"
        lines.append(item)
    if primary_remaining is None and secondary_remaining is None:
        lines.append("窗口用量: 服务未提供具体窗口指标。")
    credits = _number(reset_credits_count)
    if credits:
        lines.append(f"可用重置积分: {credits} 次")
    lines.append(f"检测时间: {datetime.now().strftime('%H:%M:%S')}")
    return (
        status,
        "\n".join(lines),
        primary_used,
        primary_remaining,
        secondary_used,
        secondary_remaining,
        limit_reached,
        reset_after,
    )


def refresh_chatgpt_token(refresh_token: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    if not refresh_token:
        return None
    payload = json.dumps({
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if response.status == 200 and isinstance(payload, dict) and payload.get("access_token") else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def atomic_update_auth_tokens(auth_path: Path, new_tokens: dict[str, Any]) -> bool:
    try:
        if not auth_path.is_file() or not auth_path.parent.is_dir():
            return False
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        tokens = payload.get("tokens")
        tokens = dict(tokens) if isinstance(tokens, dict) else {}
        for key in ("access_token", "refresh_token", "id_token", "account_id"):
            value = new_tokens.get(key)
            if value is not None and (not isinstance(value, str) or value.strip()):
                tokens[key] = value
                if key in payload:
                    payload[key] = value
        payload["tokens"] = tokens
        payload["last_refresh"] = utc_now()
        descriptor, temporary_name = tempfile.mkstemp(prefix="auth-refresh-", suffix=".tmp", dir=auth_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, auth_path)
            if os.name != "nt":
                auth_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            return True
        finally:
            if temporary.exists():
                temporary.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def request_chatgpt_usage(
    access_token: str, account_id: str | None = None, timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, dict[str, Any] | str]:
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-Id"] = str(account_id)
    request = urllib.request.Request(CHATGPT_USAGE_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read().decode("utf-8", errors="replace")
        except OSError:
            return error.code, str(error.reason)
    except urllib.error.URLError as error:
        return (-1, "请求超时") if "timed out" in str(error.reason).lower() else (-2, f"网络错误: {error.reason}")
    except (TimeoutError, OSError) as error:
        return -1, f"网络超时: {error}"
    except (ValueError, json.JSONDecodeError) as error:
        return -3, f"响应格式解析失败: {error}"
    except Exception as error:  # The worker must always report a result.
        return -4, f"未知异常: {error}"


def _cancelled(profile_id: str) -> QuotaInfo:
    return QuotaInfo(profile_id, False, "已取消", "检测已取消。", error_message="cancelled")


def fetch_profile_quota(
    profile_id: str,
    auth_path: Path,
    timeout: float = DEFAULT_TIMEOUT,
    on_token_refreshed: Callable[[str, dict[str, Any]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> QuotaInfo:
    """Read one local auth cache and return either quota data or a clear status."""
    try:
        if is_cancelled and is_cancelled():
            return _cancelled(profile_id)
        if not auth_path.is_file() or auth_path.stat().st_size <= 2:
            return QuotaInfo(profile_id, False, "未绑定", "未找到有效的登录缓存文件 (auth.json)。", error_message="auth_file_not_found")
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return QuotaInfo(profile_id, False, "查询失败", f"读取凭据文件失败: {error}", error_message=str(error))
        if not isinstance(auth_data, dict):
            return QuotaInfo(profile_id, False, "查询失败", "凭据格式错误 (非有效 JSON 对象)。", error_message="invalid_format")
        tokens = auth_data.get("tokens")
        if not isinstance(tokens, dict) and auth_data.get("access_token"):
            tokens = auth_data
        tokens = tokens if isinstance(tokens, dict) else {}
        api_key = (
            auth_data.get("OPENAI_API_KEY")
            or auth_data.get("api_key")
            or auth_data.get("apiKey")
            or tokens.get("api_key")
        )
        api_key = str(api_key).strip() if isinstance(api_key, str) else None
        auth_mode = str(auth_data.get("auth_mode") or "").strip().lower()
        if api_key and (auth_mode == "api_key" or not tokens.get("access_token")):
            return QuotaInfo(profile_id, True, "API Key 已配置", "此账号使用 API Key 认证；订阅额度不适用。", plan_type="API Key", updated_at=utc_now())
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            return QuotaInfo(profile_id, False, "未绑定", "未找到有效的访问令牌 (access_token)。请先登录。", error_message="no_access_token")
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        claims = parse_jwt_payload(tokens.get("id_token"))
        claim_auth = claims.get("https://api.openai.com/auth")
        claim_auth = claim_auth if isinstance(claim_auth, dict) else {}
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        plan = claim_auth.get("chatgpt_plan_type") if isinstance(claim_auth.get("chatgpt_plan_type"), str) else None
        account_id = tokens.get("account_id") or claim_auth.get("chatgpt_account_id")
        if is_cancelled and is_cancelled():
            return _cancelled(profile_id)
        status, response = request_chatgpt_usage(access_token, str(account_id) if account_id else None, timeout)
        if status == 401 and refresh_token and not (is_cancelled and is_cancelled()):
            refreshed = refresh_chatgpt_token(refresh_token, timeout)
            if refreshed and refreshed.get("access_token"):
                if is_cancelled and is_cancelled():
                    return _cancelled(profile_id)
                if on_token_refreshed:
                    try:
                        on_token_refreshed(profile_id, refreshed)
                    except Exception:
                        pass
                else:
                    atomic_update_auth_tokens(auth_path, refreshed)
                access_token = str(refreshed["access_token"])
                refreshed_claims = parse_jwt_payload(refreshed.get("id_token"))
                refreshed_auth = refreshed_claims.get("https://api.openai.com/auth")
                refreshed_auth = refreshed_auth if isinstance(refreshed_auth, dict) else {}
                email = refreshed_claims.get("email") or email
                plan = refreshed_auth.get("chatgpt_plan_type") or plan
                account_id = refreshed.get("account_id") or refreshed_auth.get("chatgpt_account_id") or account_id
                status, response = request_chatgpt_usage(access_token, str(account_id) if account_id else None, timeout)
        if is_cancelled and is_cancelled():
            return _cancelled(profile_id)
        if status == 200 and isinstance(response, dict):
            plan = response.get("plan_type") if isinstance(response.get("plan_type"), str) else plan
            email = response.get("email") if isinstance(response.get("email"), str) else email
            credits = response.get("rate_limit_reset_credits")
            credits = credits.get("available_count") if isinstance(credits, dict) else None
            status_text, detail, used_a, remaining_a, used_b, remaining_b, limited, reset = format_quota_strings(
                plan, email, response.get("rate_limit"), credits
            )
            return QuotaInfo(profile_id, True, status_text, detail, plan, email, used_a, remaining_a, used_b, remaining_b, limited, reset, _number(credits), updated_at=utc_now())
        messages = {
            401: ("登录已过期", "登录令牌已过期且自动刷新未通过，请重新登录此账号。"),
            403: ("访问受限", "接口访问受限 (HTTP 403)，可能因权限、地区或网络策略导致。"),
            429: ("查询频繁", "接口请求过于频繁 (HTTP 429)，请稍后重试。"),
            -1: ("查询超时", f"网络请求超时 ({timeout}s)。"),
            -2: ("查询失败", f"网络连接失败: {response}"),
        }
        text, detail = messages.get(status, ("服务异常" if status >= 500 else "查询失败", f"服务器返回异常 (HTTP {status}): {response}"))
        suffix = f"\n本地记录: {email or '—'} ({format_plan_name(plan)})"
        return QuotaInfo(profile_id, False, text, detail + suffix, plan, email, error_message=f"HTTP {status}")
    except Exception as error:
        return QuotaInfo(profile_id, False, "查询失败", f"额度检测过程发生未预期异常: {error}", error_message=str(error))


class QuotaWorker(QtCore.QThread):
    quota_ready = QtCore.pyqtSignal(str, object)
    all_finished = QtCore.pyqtSignal()

    def __init__(
        self,
        targets: list[tuple[str, Path]],
        timeout: float = DEFAULT_TIMEOUT,
        max_workers: int = 4,
        on_token_refreshed: Callable[[str, dict[str, Any]], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._targets = list(targets)
        self._timeout = timeout
        self._max_workers = max(1, max_workers)
        self._on_token_refreshed = on_token_refreshed
        self._is_cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def cancel(self) -> None:
        self._is_cancelled = True

    def run(self) -> None:
        if not self._targets:
            self.all_finished.emit()
            return
        def fetch(target: tuple[str, Path]) -> QuotaInfo:
            return fetch_profile_quota(
                target[0], target[1], self._timeout, self._on_token_refreshed,
                is_cancelled=lambda: self._is_cancelled,
            )
        executor = ThreadPoolExecutor(max_workers=min(self._max_workers, len(self._targets)))
        try:
            future_to_id = {
                executor.submit(fetch, target): target[0] for target in self._targets
            }
            pending = set(future_to_id)
            while pending and not self._is_cancelled:
                done, pending = wait(
                    pending, timeout=0.1, return_when=FIRST_COMPLETED
                )
                for future in done:
                    try:
                        quota = future.result()
                    except Exception as error:
                        quota = QuotaInfo(
                            future_to_id[future],
                            False,
                            "查询失败",
                            f"检测异常: {error}",
                            error_message=str(error),
                        )
                    if not self._is_cancelled and quota.error_message != "cancelled":
                        self.quota_ready.emit(quota.profile_id, quota)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not self._is_cancelled:
            self.all_finished.emit()

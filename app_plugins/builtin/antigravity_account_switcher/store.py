"""Safe local credential profiles for Antigravity account-switcher plugin.

Atomically swaps Antigravity User state and OAuth authentication records.
Local sessions, project settings and workspace files remain shared and intact.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from model.AppDataStorage import application_storage, data_directory, data_file, data_io
from .launcher import is_antigravity_running
from .clients import default_client
from .identity import has_oauth_token
from .native_auth import NATIVE_AUTH_KEY, WindowsCredentialBackend, inspect_native_record

STORE_VERSION = 2
AUTH_KEYS_TO_SYNC = (
    "antigravityUnifiedStateSync.oauthToken",
    "antigravityUnifiedStateSync.userStatus",
    "antigravityUnifiedStateSync.modelCredits",
    "antigravity.profileUrl",
)


class AccountSwitcherError(RuntimeError):
    """Raised before a credential operation that cannot be completed safely."""


@dataclass(frozen=True)
class AntigravityProfile:
    profile_id: str
    name: str
    credential_dir: str
    created_at: str
    last_used_at: str | None = None

    @property
    def home_path(self) -> Path:
        return data_directory(self.credential_dir).path

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "credential_dir": "credentials/" + self.profile_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AntigravityProfile":
        return cls(
            profile_id=str(raw["profile_id"]),
            name=str(raw["name"]),
            credential_dir=str(raw["credential_dir"]),
            created_at=str(raw["created_at"]),
            last_used_at=str(raw["last_used_at"]) if raw.get("last_used_at") else None,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_live_db_path() -> Path:
    return default_client().database


def default_store_root() -> Path:
    return application_storage().for_plugin("antigravity_account_switcher").directory("accounts").path


class AntigravityProfileStore:
    """Manages profile archives and atomically swaps the live state.vscdb auth keys."""

    def __init__(
        self,
        root: Path | None = None,
        live_db_path: Path | None = None,
        *,
        client_running=None,
        client=None,
        credential_backend=None,
    ) -> None:
        self.root = (root or default_store_root()).expanduser().resolve()
        self.client = client
        self.uses_native_credentials = bool(client and client.auth_kind == 'windows')
        self._credential_backend = (credential_backend or WindowsCredentialBackend()) if self.uses_native_credentials else None
        self.live_db_path = (live_db_path or (client.database if client else default_live_db_path())).expanduser().resolve()
        self._client_running = client_running or (lambda: is_antigravity_running(client))

        self.credentials_root = self.root / "credentials"
        self.archive_root = self.root / "archive"
        self.rollback_root = self.root / "switch-backup"
        self.index_path = self.root / "desktop-profiles.json"

        data_directory(self.root).ensure(parents=True, exist_ok=True)
        data_directory(self.credentials_root).ensure(parents=True, exist_ok=True)
        data_directory(self.archive_root).ensure(parents=True, exist_ok=True)
        data_directory(self.rollback_root).ensure(parents=True, exist_ok=True)
        self._data = self._load()
        ids = [profile.profile_id for profile in self.profiles()]
        if (len(ids) != len(set(ids)) or
                self.active_profile_id and self.active_profile_id not in ids or
                self.binding_profile_id and self.binding_profile_id != self.active_profile_id):
            raise AccountSwitcherError("账号档案身份记录不一致，未重置或覆盖原文件。")
        # Opening the plugin must never rewrite a running IDE's database.
        # Recovery is explicit, after the IDE has safely closed.

    @property
    def rollback_db_path(self) -> Path:
        return self.rollback_root / "state.vscdb"

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "active_profile_id": None,
            "profiles": [],
            "transaction": None,
            "binding_profile_id": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.index_path.exists():
            data = self._empty_data()
            self._atomic_write_index(data)
            return data
        try:
            data = json.loads(data_file(self.index_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            backup = self.index_path.with_name(
                "desktop-profiles.broken-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
            )
            try:
                data_io.copy2(self.index_path, backup)
            except OSError:
                pass
            raise AccountSwitcherError("账号档案索引无法读取，已停止操作。") from error
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise AccountSwitcherError("账号档案索引格式无效。")
        data.setdefault("active_profile_id", None)
        data.setdefault("transaction", None)
        data.setdefault("binding_profile_id", None)
        if type(data.get("version", 1)) is not int or data.get("version", 1) not in (1, STORE_VERSION):
            raise AccountSwitcherError("账号档案版本不受支持，未改动原文件。")
        return data

    def _atomic_write_index(self, data: dict[str, Any]) -> None:
        data_file(self.index_path).write_json(data, indent=2)

    def save(self) -> None:
        self._data["version"] = STORE_VERSION
        self._atomic_write_index(self._data)

    def profiles(self) -> list[AntigravityProfile]:
        profiles = []
        for item in self._data["profiles"]:
            try:
                profile = AntigravityProfile.from_dict(item)
                if len(profile.profile_id) != 32 or any(c not in "0123456789abcdef" for c in profile.profile_id):
                    raise ValueError("invalid profile identity")
                expected = data_directory(self.credentials_root / profile.profile_id).path
                stored = Path(profile.credential_dir)
                if stored.is_absolute():
                    if data_directory(stored).path != expected:
                        raise ValueError("credential path outside namespace")
                elif stored.as_posix() != "credentials/" + profile.profile_id:
                    raise ValueError("invalid relative credential path")
                profiles.append(AntigravityProfile(
                    profile.profile_id, profile.name, str(expected),
                    profile.created_at, profile.last_used_at,
                ))
            except (KeyError, TypeError, ValueError) as err:
                raise AccountSwitcherError("账号档案含有无效记录，已停止。") from err
        return profiles

    def profile(self, profile_id: str | None) -> AntigravityProfile | None:
        if not profile_id:
            return None
        return next((p for p in self.profiles() if p.profile_id == profile_id), None)

    @property
    def active_profile_id(self) -> str | None:
        val = self._data.get("active_profile_id")
        return str(val) if val else None

    @property
    def active_profile(self) -> AntigravityProfile | None:
        return self.profile(self.active_profile_id)

    def profile_db_path(self, profile: AntigravityProfile) -> Path:
        return profile.home_path / "state.vscdb"

    def has_auth(self, profile: AntigravityProfile) -> bool:
        if profile.profile_id == self._data.get("binding_profile_id"):
            return False
        if profile.profile_id == self.active_profile_id:
            db_path = self.live_db_path
        else:
            db_path = self.profile_db_path(profile)
        if not self._is_native_live(db_path) and (not data_file(db_path).exists() or db_path.stat().st_size <= 100):
            return False
        try:
            from .quota import inspect_auth_records
            info = inspect_auth_records(self._read_auth(db_path))
            return bool(info.get('has_token') and info.get('email'))
        except (sqlite3.Error, OSError):
            return False

    @staticmethod
    def _has_token(records):
        if NATIVE_AUTH_KEY in records:
            try:
                return inspect_native_record(records[NATIVE_AUTH_KEY])['has_token']
            except OSError:
                return False
        value = records.get(AUTH_KEYS_TO_SYNC[0])
        return has_oauth_token(value)

    @property
    def auth_source(self):
        return self.client.auth_source if self.client else str(self.live_db_path)

    def _is_native_live(self, path):
        return self.uses_native_credentials and Path(path) == self.live_db_path

    def live_records(self):
        return self._read_auth(self.live_db_path)

    def _identity_at(self, path):
        from .quota import inspect_auth_records
        return inspect_auth_records(self._read_auth(path))

    def _read_auth(self, path):
        if self._is_native_live(path):
            value = self._credential_backend.read()
            return {NATIVE_AUTH_KEY: value} if value is not None else {}
        if not data_file(path).exists():
            raise AccountSwitcherError("未找到 Antigravity 登录数据库；请先安装并登录客户端。")
        with closing(data_file(path).connect(read_only=True, timeout=.5)) as con:
            return dict(con.execute(
                f"SELECT key,value FROM ItemTable WHERE key IN ({','.join('?' for _ in (*AUTH_KEYS_TO_SYNC, NATIVE_AUTH_KEY))})",
                (*AUTH_KEYS_TO_SYNC, NATIVE_AUTH_KEY),
            ).fetchall())

    def _write_auth(self, path, records):
        if self._is_native_live(path):
            if records and set(records) != {NATIVE_AUTH_KEY}:
                raise AccountSwitcherError('目标账号不是新版系统登录档案，不能把旧 IDE 数据写入新版。')
            data_directory(self.root).exists()  # Fail closed if the private volume disappeared.
            self._credential_backend.write(records.get(NATIVE_AUTH_KEY))
            # A successful OS call alone is not proof the intended account was written.
            if self._credential_backend.read() != records.get(NATIVE_AUTH_KEY):
                raise AccountSwitcherError('系统登录记录写入核对失败；正在恢复原账号。')
            return
        if self.uses_native_credentials and records and set(records) != {NATIVE_AUTH_KEY}:
            raise AccountSwitcherError('新版账号仓库不能混入旧 IDE 登录档案。')
        if not self.uses_native_credentials and NATIVE_AUTH_KEY in records:
            raise AccountSwitcherError('旧 IDE 不能使用新版系统登录档案。')
        # A small auth-only SQLite snapshot, never a full workspace/history DB.
        with closing(data_file(path).connect(timeout=1)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            con.executemany("DELETE FROM ItemTable WHERE key=?", [(key,) for key in (*AUTH_KEYS_TO_SYNC, NATIVE_AUTH_KEY)])
            con.executemany("INSERT INTO ItemTable (key,value) VALUES (?,?)", list(records.items()))

    def _assert_closed(self):
        if self._client_running():
            raise AccountSwitcherError("请先保存工作并退出 Antigravity，再进行账号绑定／切换。")

    def _validate_current_identity(self):
        profile = self.active_profile
        if profile is None or self.binding_profile_id:
            return
        saved = self.profile_db_path(profile)
        if not saved.is_file():
            return
        previous = self._identity_at(saved).get("email")
        current = self._identity_at(self.live_db_path).get("email")
        if previous and current and previous.casefold() != current.casefold():
            raise AccountSwitcherError("客户端账号与当前档案不一致，未覆盖原档案；请先回到对应账号。")

    @property
    def needs_recovery(self):
        return isinstance(self._data.get("transaction"), dict)

    @property
    def binding_profile_id(self):
        return self._data.get("binding_profile_id")

    def _sync_auth_keys(self, source_db: Path, target_db: Path) -> None:
        """Extract auth records from source_db and update them into target_db."""
        try:
            records = self._read_auth(source_db)
        except sqlite3.Error as err:
            raise AccountSwitcherError("读取登录数据库失败，未修改当前账号。") from err
        if not self._has_token(records):
            raise AccountSwitcherError("源数据库中未找到有效的 Antigravity 登录凭据。")
        from .quota import inspect_auth_records
        if not inspect_auth_records(records).get('email'):
            raise AccountSwitcherError("源账号身份格式无法确认，未覆盖已保存档案。")
        try:
            self._write_auth(target_db, records)
        except sqlite3.Error as err:
            raise AccountSwitcherError("保存登录档案失败，未将操作视为成功。") from err

    @staticmethod
    def _validate_name(name: str) -> str:
        val = " ".join(str(name or "").strip().split())
        if not val:
            raise ValueError("账号名称不能为空。")
        if len(val) > 40:
            raise ValueError("账号名称最多 40 个字符。")
        return val

    def adopt_current(self, name: str) -> AntigravityProfile:
        """Save the currently logged-in account as the initial profile."""
        if self.profiles() or self.active_profile_id:
            raise AccountSwitcherError("已有账号档案，不能再次导入当前账号。")
        if not self.uses_native_credentials and not self.live_db_path.is_file():
            raise AccountSwitcherError("未检测到当前 Antigravity 登录数据库 (state.vscdb)。")
        identity = self._identity_at(self.live_db_path)
        if not identity.get('email') or not identity.get('has_token'):
            raise AccountSwitcherError("所选客户端的账号身份或登录格式无法确认，未保存错误账号。请在该客户端登录后再刷新。")

        profile_id = uuid.uuid4().hex
        cred_dir = self.credentials_root / profile_id
        data_directory(cred_dir).ensure()

        profile = AntigravityProfile(
            profile_id=profile_id,
            name=self._validate_name(name),
            credential_dir=str(cred_dir.resolve()),
            created_at=utc_now(),
        )

        # Snapshot current auth
        self._sync_auth_keys(self.live_db_path, self.profile_db_path(profile))

        self._data["profiles"].append(profile.to_dict())
        self._data["active_profile_id"] = profile.profile_id
        try:
            self.save()
        except Exception:
            self._data = self._empty_data()
            raise
        return profile

    def add_profile(self, name: str) -> AntigravityProfile:
        if not self.active_profile_id:
            raise AccountSwitcherError("请先保存当前登录账号为首个档案。")
        profile_id = uuid.uuid4().hex
        cred_dir = self.credentials_root / profile_id
        data_directory(cred_dir).ensure()

        profile = AntigravityProfile(
            profile_id=profile_id,
            name=self._validate_name(name),
            credential_dir=str(cred_dir.resolve()),
            created_at=utc_now(),
        )
        before = deepcopy(self._data)
        self._data["profiles"].append(profile.to_dict())
        try:
            self.save()
        except Exception:
            self._data = before
            raise
        return profile

    def rename_profile(self, profile_id: str, name: str) -> AntigravityProfile:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到指定账号。")
        updated = AntigravityProfile(
            profile_id=profile.profile_id,
            name=self._validate_name(name),
            credential_dir=profile.credential_dir,
            created_at=profile.created_at,
            last_used_at=profile.last_used_at,
        )
        before = deepcopy(self._data)
        for idx, item in enumerate(self._data["profiles"]):
            if item.get("profile_id") == profile_id:
                self._data["profiles"][idx] = updated.to_dict()
                break
        try:
            self.save()
        except Exception:
            self._data = before
            raise
        return updated

    def archive_profile(self, profile_id: str) -> Path:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到账号。")
        if profile.profile_id == self.active_profile_id:
            raise AccountSwitcherError("不能移除当前活跃账号。")
        dest = self.archive_root / f"credentials-{profile_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        before = deepcopy(self._data)
        data_io.move(str(profile.home_path), str(dest))
        self._data["profiles"] = [p for p in self._data["profiles"] if p.get("profile_id") != profile_id]
        try:
            self.save()
        except Exception:
            self._data = before
            data_io.move(str(dest), str(profile.home_path))
            raise
        return dest

    def next_ready_profile(self) -> AntigravityProfile | None:
        ready = [p for p in self.profiles() if self.has_auth(p)]
        if not ready:
            return None
        for i, p in enumerate(ready):
            if p.profile_id == self.active_profile_id:
                return ready[(i + 1) % len(ready)]
        return ready[0]

    def _mark_used(self, profile_id: str) -> AntigravityProfile:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到账号。")
        updated = AntigravityProfile(
            profile_id=profile.profile_id,
            name=profile.name,
            credential_dir=profile.credential_dir,
            created_at=profile.created_at,
            last_used_at=utc_now(),
        )
        for idx, item in enumerate(self._data["profiles"]):
            if item.get("profile_id") == profile_id:
                self._data["profiles"][idx] = updated.to_dict()
                break
        self.save()
        return updated

    def switch_to(self, target_id: str, *, allow_unbound: bool = False) -> AntigravityProfile:
        self._assert_closed()
        if self.needs_recovery:
            raise AccountSwitcherError("上次切换未完成，请先点击恢复上次账号。")
        current = self.active_profile
        target = self.profile(target_id)
        if not target:
            raise AccountSwitcherError("找不到目标账号。")
        if not current:
            raise AccountSwitcherError("当前没有活跃账号档案。")
        if target.profile_id == current.profile_id and not self.binding_profile_id:
            return self._mark_used(target.profile_id)

        target_db = self.profile_db_path(target)
        target_ready = target_db.is_file() and self.has_auth(target)
        if not target_ready and not allow_unbound:
            raise AccountSwitcherError("目标账号尚未绑定凭据（未登录）。")

        before = deepcopy(self._data)
        live_records = self._read_auth(self.live_db_path)
        if self.binding_profile_id and self._has_token(live_records):
            raise AccountSwitcherError("当前正在绑定新账号，请先点击刷新状态保存登录结果。")
        if not self.binding_profile_id and self._has_token(live_records):
            # Never swallow a failed backup of the account being left.
            self._validate_current_identity()
            self._sync_auth_keys(self.live_db_path, self.profile_db_path(current))
        self._write_auth(self.rollback_db_path, live_records)
        self._data["transaction"] = {
            "from": current.profile_id,
            "to": target.profile_id,
            "binding_before": before.get("binding_profile_id"),
            "started_at": utc_now(),
        }
        try:
            self.save()
        except Exception:
            self._data = before
            raise
        try:
            if target_ready:
                self._sync_auth_keys(target_db, self.live_db_path)
            else:
                # A new login must not inherit the old account's OAuth token.
                self._write_auth(self.live_db_path, {})
            self._data["active_profile_id"] = target.profile_id
            self._data["binding_profile_id"] = None if target_ready else target.profile_id
            self._data["transaction"] = None
            updated = self._mark_used(target.profile_id)
        except Exception as err:
            self._data = deepcopy(before)
            self._data["transaction"] = {
                "from": current.profile_id, "to": target.profile_id,
                "binding_before": before.get("binding_profile_id"),
            }
            try:
                self._write_auth(self.live_db_path, live_records)
                self._data = before
                self.save()
            except Exception as recovery_error:
                # Keep the transaction and small protected backup for recovery.
                self._data = deepcopy(before)
                self._data["transaction"] = {
                    "from": current.profile_id, "to": target.profile_id,
                    "binding_before": before.get("binding_profile_id"),
                }
                raise AccountSwitcherError("切换失败，自动恢复也未完成；已保留恢复记录，请勿继续切换。") from recovery_error
            raise AccountSwitcherError("切换失败，已恢复原账号；未删除账号档案。") from err
        # Cleanup failure is not a failed account switch; retain the backup.
        try:
            data_file(self.rollback_db_path).remove(missing_ok=True)
        except OSError:
            pass
        return updated

    def capture_current(self) -> bool:
        """Persist a completed user login; never rewrite the live IDE database."""
        if self.needs_recovery:
            raise AccountSwitcherError("上次切换未完成，请先恢复上次账号。")
        profile = self.active_profile
        if profile is None:
            return False
        records = self._read_auth(self.live_db_path)
        if not self._has_token(records):
            if records.get(AUTH_KEYS_TO_SYNC[0]) or records.get(NATIVE_AUTH_KEY):
                raise AccountSwitcherError("登录缓存格式无法确认，未保存或替换账号档案。")
            return False
        if self.binding_profile_id:
            email = self._identity_at(self.live_db_path).get("email")
            if not email:
                raise AccountSwitcherError("检测到登录数据，但暂时无法确认账号邮箱；尚未把它绑定为新账号。")
            for other in self.profiles():
                path = self.profile_db_path(other)
                if other.profile_id != profile.profile_id and path.is_file():
                    other_email = self._identity_at(path).get("email")
                    if other_email and other_email.casefold() == email.casefold():
                        raise AccountSwitcherError("登录的仍是已保存账号，请在 Antigravity 中换成新账号后再刷新。")
        else:
            self._validate_current_identity()
        self._sync_auth_keys(self.live_db_path, self.profile_db_path(profile))
        before = deepcopy(self._data)
        self._data["binding_profile_id"] = None
        try:
            self.save()
        except Exception:
            self._data = before
            raise
        return True

    def _recover_interrupted_switch(self) -> None:
        trans = self._data.get("transaction")
        if not isinstance(trans, dict):
            return
        self._assert_closed()
        from_id = trans.get("from")
        source = self.profile(str(from_id))
        if source is None or not self.rollback_db_path.is_file():
            raise AccountSwitcherError("恢复记录不完整，未修改当前登录数据，请保留账号仓库。")
        before = deepcopy(self._data)
        self._write_auth(self.live_db_path, self._read_auth(self.rollback_db_path))
        self._data["active_profile_id"] = source.profile_id
        self._data["binding_profile_id"] = trans.get("binding_before")
        self._data["transaction"] = None
        try:
            self.save()
        except Exception:
            self._data = before
            raise
        try:
            data_file(self.rollback_db_path).remove(missing_ok=True)
        except OSError:
            pass

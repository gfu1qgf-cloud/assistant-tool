"""Safe local credential profiles for the Codex account-switcher plugin.

Only ``auth.json`` is switched.  The live ``.codex`` directory, including
sessions, project state, and configuration, stays in place at all times.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORE_VERSION = 3
MAX_AUTH_FILE_BYTES = 10 * 1024 * 1024


class AccountSwitcherError(RuntimeError):
    """Raised before a credential operation that cannot be completed safely."""


@dataclass(frozen=True)
class AccountProfile:
    profile_id: str
    name: str
    credential_dir: str
    created_at: str
    last_used_at: str | None = None

    @property
    def home_path(self) -> Path:
        return Path(self.credential_dir)

    @property
    def codex_home(self) -> str:
        """Compatibility name used by standalone switcher profile records."""
        return self.credential_dir

    def to_dict(self) -> dict[str, Any]:
        # Keep the field name used by the standalone switcher so its existing
        # local account profiles remain usable after migration into this plugin.
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "codex_home": self.credential_dir,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AccountProfile":
        return cls(
            profile_id=str(raw["profile_id"]),
            name=str(raw["name"]),
            credential_dir=str(raw["codex_home"]),
            created_at=str(raw["created_at"]),
            last_used_at=(
                str(raw["last_used_at"])
                if raw.get("last_used_at")
                else None
            ),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_live_home() -> Path:
    override = os.environ.get("CODEX_DESKTOP_SWITCHER_LIVE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def default_store_root() -> Path:
    # This is deliberately compatible with the previous standalone tool.
    override = os.environ.get("CODEX_DESKTOP_SWITCHER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex-account-switcher").resolve()


def is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt" or not path.exists():
        return False
    try:
        import ctypes

        invalid_attributes = 0xFFFFFFFF
        reparse_point = 0x0400
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != invalid_attributes and bool(attributes & reparse_point)
    except (AttributeError, OSError):
        return False


class AccountProfileStore:
    """Owns account archives and atomically swaps the live auth cache."""

    def __init__(
        self,
        root: Path | None = None,
        live_home: Path | None = None,
    ) -> None:
        self.root = (root or default_store_root()).expanduser().resolve()
        self.live_home = (live_home or default_live_home()).expanduser().resolve()
        if self.root == self.live_home or self.root in self.live_home.parents:
            raise AccountSwitcherError("账号凭据仓库不能放在 .codex 目录里面。")
        if self.live_home in self.root.parents:
            raise AccountSwitcherError(".codex 目录不能包含账号凭据仓库。")

        self.credentials_root = self.root / "credentials"
        self.archive_root = self.root / "archive"
        self.rollback_root = self.root / "switch-backup"
        self.index_path = self.root / "desktop-profiles.json"
        self.legacy_profiles_root = self.root / "profiles"
        self.migration_performed = False
        self.migration_backup_path: Path | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self.credentials_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.rollback_root.mkdir(parents=True, exist_ok=True)
        self.live_home.parent.mkdir(parents=True, exist_ok=True)
        self._tighten_directory(self.root)

        self._data = self._load()
        version = int(self._data.get("version") or 2)
        if version == 2:
            self._recover_v2_interrupted_switch()
            self._migrate_v2_to_v3()
        elif version != STORE_VERSION:
            raise AccountSwitcherError(
                f"不支持的账号档案版本：{version}。程序没有修改任何账号文件。"
            )
        self._recover_interrupted_switch()

    @property
    def live_auth_path(self) -> Path:
        return self.live_home / "auth.json"

    @property
    def rollback_auth_path(self) -> Path:
        return self.rollback_root / "auth.json"

    @staticmethod
    def _tighten_directory(path: Path) -> None:
        if os.name != "nt" and path.exists():
            path.chmod(stat.S_IRWXU)

    @staticmethod
    def _tighten_file(path: Path) -> None:
        if os.name != "nt" and path.exists():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "active_profile_id": None,
            "profiles": [],
            "transaction": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.index_path.exists():
            data = self._empty_data()
            self._atomic_write_index(data)
            return data
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            backup = self.index_path.with_name(
                "desktop-profiles.broken-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + ".json"
            )
            try:
                shutil.copy2(self.index_path, backup)
                self._tighten_file(backup)
            except OSError:
                backup = None
            suffix = (
                f"原文件已保留为 {backup.name}。" if backup is not None else ""
            )
            raise AccountSwitcherError(
                "账号档案索引无法读取；为避免影响登录缓存，插件没有继续操作。" + suffix
            ) from error
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise AccountSwitcherError("账号档案索引格式无效，插件没有继续操作。")
        data.setdefault("active_profile_id", None)
        data.setdefault("transaction", None)
        return data

    def _atomic_write_index(self, data: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="desktop-profiles-", suffix=".tmp", dir=self.root
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.index_path)
            self._tighten_file(self.index_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _save(self) -> None:
        self._atomic_write_index(self._data)

    def save(self) -> None:
        self._save()

    def profiles(self) -> list[AccountProfile]:
        profiles: list[AccountProfile] = []
        for item in self._data["profiles"]:
            try:
                profiles.append(AccountProfile.from_dict(item))
            except (KeyError, TypeError, ValueError) as error:
                raise AccountSwitcherError("账号档案含有无效记录，已停止操作。") from error
        return profiles

    def profile(self, profile_id: str | None) -> AccountProfile | None:
        if not profile_id:
            return None
        return next(
            (item for item in self.profiles() if item.profile_id == profile_id),
            None,
        )

    # Retain the standalone application's public store names so the dialog and
    # any user scripts built around it keep working after migration.
    get_profile = profile

    @property
    def active_profile_id(self) -> str | None:
        value = self._data.get("active_profile_id")
        return str(value) if value else None

    @property
    def active_profile(self) -> AccountProfile | None:
        return self.profile(self.active_profile_id)

    @staticmethod
    def profile_auth_path(profile: AccountProfile) -> Path:
        return profile.home_path / "auth.json"

    def effective_auth_path(self, profile: AccountProfile) -> Path:
        if profile.profile_id == self.active_profile_id:
            return self.live_auth_path
        return self.profile_auth_path(profile)

    def _is_safe_credential_dir(self, profile: AccountProfile) -> bool:
        try:
            credential_dir = profile.home_path
            return (
                credential_dir.is_dir()
                and not is_reparse_or_symlink(credential_dir)
                and os.path.normcase(str(credential_dir.resolve().parent))
                == os.path.normcase(str(self.credentials_root.resolve()))
            )
        except OSError:
            return False

    @staticmethod
    def _is_valid_auth_file(path: Path) -> bool:
        try:
            if is_reparse_or_symlink(path) or not path.is_file():
                return False
            return 2 < path.stat().st_size <= MAX_AUTH_FILE_BYTES
        except OSError:
            return False

    def has_auth(self, profile: AccountProfile) -> bool:
        if (
            profile.profile_id != self.active_profile_id
            and not self._is_safe_credential_dir(profile)
        ):
            return False
        return self._is_valid_auth_file(self.effective_auth_path(profile))

    has_file_auth = has_auth

    def update_profile_tokens(
        self, profile_id: str, new_tokens: dict[str, Any]
    ) -> bool:
        """Persist a refreshed token in the stored profile and live cache."""
        from .quota import atomic_update_auth_tokens

        profile = self.profile(profile_id)
        if not profile:
            return False
        profile_path = self.profile_auth_path(profile)
        updated = atomic_update_auth_tokens(profile_path, new_tokens)
        if profile.profile_id == self.active_profile_id and self.live_auth_path.is_file():
            live_updated = atomic_update_auth_tokens(self.live_auth_path, new_tokens)
            if not profile_path.is_file() and self._is_valid_auth_file(self.live_auth_path):
                try:
                    self._atomic_copy_auth(self.live_auth_path, profile_path)
                    updated = True
                except (OSError, AccountSwitcherError):
                    pass
            updated = updated or live_updated
        return updated

    def _atomic_copy_auth(self, source: Path, destination: Path) -> None:
        if not self._is_valid_auth_file(source):
            raise AccountSwitcherError("登录缓存不存在、过大或不是普通文件。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._tighten_directory(destination.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="auth-", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            self._tighten_file(temporary_path)
            os.replace(temporary_path, destination)
            self._tighten_file(destination)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = " ".join(str(name or "").strip().split())
        if not value:
            raise ValueError("账号名称不能为空。")
        if len(value) > 40:
            raise ValueError("账号名称最多 40 个字符。")
        return value

    def _validate_name(self, name: str, except_id: str | None = None) -> str:
        value = self._normalize_name(name)
        if any(
            profile.profile_id != except_id and profile.name.casefold() == value.casefold()
            for profile in self.profiles()
        ):
            raise ValueError("这个账号名称已经存在。")
        return value

    def _new_profile(self, name: str) -> AccountProfile:
        profile_id = uuid.uuid4().hex
        return AccountProfile(
            profile_id=profile_id,
            name=self._validate_name(name),
            credential_dir=str((self.credentials_root / profile_id).resolve()),
            created_at=utc_now(),
        )

    def _ensure_file_credential_store(self) -> None:
        """Request file-backed auth only when the user creates the first profile."""
        path = self.live_home / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = original.splitlines()
        first_table = next(
            (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
            len(lines),
        )
        replacement = 'cli_auth_credentials_store = "file"'
        changed = False
        for index in range(first_table):
            if lines[index].strip().lower().startswith("cli_auth_credentials_store"):
                lines[index] = replacement
                changed = True
                break
        if not changed:
            lines.insert(first_table, replacement)
        updated = "\n".join(lines).rstrip() + "\n"
        if updated == original:
            return
        backup = path.with_name("config.before-account-switcher.toml")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
            self._tighten_file(backup)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="config-", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            self._tighten_file(path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def adopt_current(self, name: str) -> AccountProfile:
        """Save the live account as the first profile without moving sessions."""
        if self.profiles() or self.active_profile_id:
            raise AccountSwitcherError("已有账号档案，不能再次导入当前账号。")
        if self.live_home.exists() and (
            not self.live_home.is_dir() or is_reparse_or_symlink(self.live_home)
        ):
            raise AccountSwitcherError("当前 .codex 不是可安全使用的普通文件夹。")
        self.live_home.mkdir(parents=True, exist_ok=True)
        self._ensure_file_credential_store()
        profile = self._new_profile(name)
        profile.home_path.mkdir(parents=True, exist_ok=False)
        self._tighten_directory(profile.home_path)
        if self._is_valid_auth_file(self.live_auth_path):
            self._atomic_copy_auth(self.live_auth_path, self.profile_auth_path(profile))
        self._data["profiles"].append(profile.to_dict())
        self._data["active_profile_id"] = profile.profile_id
        self.save()
        return profile

    def add_profile(self, name: str) -> AccountProfile:
        if not self.active_profile_id:
            raise AccountSwitcherError("请先保存当前登录账号。")
        profile = self._new_profile(name)
        profile.home_path.mkdir(parents=True, exist_ok=False)
        self._tighten_directory(profile.home_path)
        self._data["profiles"].append(profile.to_dict())
        self.save()
        return profile

    add_empty_profile = add_profile

    def rename_profile(self, profile_id: str, name: str) -> AccountProfile:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到账号档案。")
        updated = AccountProfile(
            profile_id=profile.profile_id,
            name=self._validate_name(name, profile.profile_id),
            credential_dir=profile.credential_dir,
            created_at=profile.created_at,
            last_used_at=profile.last_used_at,
        )
        self._replace_profile(updated)
        self.save()
        return updated

    def _replace_profile(self, profile: AccountProfile) -> None:
        for index, item in enumerate(self._data["profiles"]):
            if item.get("profile_id") == profile.profile_id:
                self._data["profiles"][index] = profile.to_dict()
                return
        raise KeyError("找不到账号档案。")

    def _mark_used(self, profile_id: str, *, save: bool = True) -> AccountProfile:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到账号档案。")
        updated = AccountProfile(
            profile_id=profile.profile_id,
            name=profile.name,
            credential_dir=profile.credential_dir,
            created_at=profile.created_at,
            last_used_at=utc_now(),
        )
        self._replace_profile(updated)
        if save:
            self.save()
        return updated

    def next_ready_profile(self) -> AccountProfile | None:
        ready = [profile for profile in self.profiles() if self.has_auth(profile)]
        if not ready:
            return None
        for index, profile in enumerate(ready):
            if profile.profile_id == self.active_profile_id:
                return ready[(index + 1) % len(ready)]
        return ready[0]

    def switch_to(self, target_id: str, *, allow_unbound: bool = False) -> AccountProfile:
        """Atomically replace only the live auth file and retain a rollback copy."""
        current = self.active_profile
        target = self.profile(target_id)
        if not target:
            raise AccountSwitcherError("找不到目标账号档案。")
        if not current:
            raise AccountSwitcherError("没有当前账号。请先保存当前登录账号。")
        if target.profile_id == current.profile_id:
            return self._mark_used(target.profile_id)
        if not self.live_home.is_dir() or is_reparse_or_symlink(self.live_home):
            raise AccountSwitcherError("当前 .codex 不是可安全使用的普通文件夹。")
        if not self._is_safe_credential_dir(current):
            raise AccountSwitcherError("当前账号的凭据目录异常，未执行切换。")
        if not self._is_safe_credential_dir(target):
            raise AccountSwitcherError("目标账号的凭据目录异常，未执行切换。")

        target_auth = self.profile_auth_path(target)
        target_ready = self._is_valid_auth_file(target_auth)
        if not target_ready and not allow_unbound:
            raise AccountSwitcherError("目标账号尚未完成首次登录。")

        # Tokens can refresh while Codex is running, so persist the latest one
        # immediately before replacing the live cache.
        if self._is_valid_auth_file(self.live_auth_path):
            self._atomic_copy_auth(self.live_auth_path, self.profile_auth_path(current))

        had_live_auth = self._is_valid_auth_file(self.live_auth_path)
        if had_live_auth:
            self._atomic_copy_auth(self.live_auth_path, self.rollback_auth_path)
        elif self.rollback_auth_path.exists():
            self.rollback_auth_path.unlink()

        transaction = {
            "from": current.profile_id,
            "to": target.profile_id,
            "had_live_auth": had_live_auth,
            "started_at": utc_now(),
        }
        self._data["transaction"] = transaction
        self.save()
        cleanup_backup = False
        try:
            if target_ready:
                self._atomic_copy_auth(target_auth, self.live_auth_path)
            elif self.live_auth_path.exists():
                self.live_auth_path.unlink()
            self._data["active_profile_id"] = target.profile_id
            updated = self._mark_used(target.profile_id, save=False)
            self._data["transaction"] = None
            self.save()
            cleanup_backup = True
            return updated
        except (OSError, AccountSwitcherError) as error:
            restore_error = self._restore_previous_auth(had_live_auth)
            self._data["active_profile_id"] = current.profile_id
            index_error = None
            if restore_error is None:
                self._data["transaction"] = None
                try:
                    self.save()
                    cleanup_backup = True
                except OSError as save_error:
                    # Keep the transaction and rollback file so the next start
                    # can still restore safely, matching the standalone tool.
                    self._data["transaction"] = transaction
                    index_error = str(save_error)
            message = f"登录缓存切换失败：{error}"
            if restore_error:
                message += f"；自动恢复也失败：{restore_error}"
            if index_error:
                message += f"；恢复后的索引暂时无法写入：{index_error}"
            raise AccountSwitcherError(message) from error
        finally:
            if cleanup_backup and self.rollback_auth_path.exists():
                self.rollback_auth_path.unlink()

    def _restore_previous_auth(self, had_live_auth: bool) -> str | None:
        try:
            if had_live_auth:
                self._atomic_copy_auth(self.rollback_auth_path, self.live_auth_path)
            elif self.live_auth_path.exists():
                self.live_auth_path.unlink()
            return None
        except (OSError, AccountSwitcherError) as error:
            return str(error)

    def _recover_interrupted_switch(self) -> None:
        transaction = self._data.get("transaction")
        if not isinstance(transaction, dict):
            return
        source = self.profile(str(transaction.get("from") or ""))
        if not source:
            raise AccountSwitcherError("发现未完成切换，但原账号不存在；已停止操作。")
        restore_error = self._restore_previous_auth(bool(transaction.get("had_live_auth")))
        if restore_error:
            raise AccountSwitcherError(f"恢复上次登录缓存失败：{restore_error}")
        self._data["active_profile_id"] = source.profile_id
        self._data["transaction"] = None
        self.save()
        if self.rollback_auth_path.exists():
            self.rollback_auth_path.unlink()

    def _recover_v2_interrupted_switch(self) -> None:
        """Stabilize an old whole-directory switch before the v3 migration."""
        transaction = self._data.get("transaction")
        if not isinstance(transaction, dict):
            return
        by_id = {
            str(item.get("profile_id")): item
            for item in self._data.get("profiles", [])
        }
        source = by_id.get(str(transaction.get("from") or ""))
        target = by_id.get(str(transaction.get("to") or ""))
        if not source or not target:
            raise AccountSwitcherError("旧版切换记录不完整，程序没有继续升级。")
        source_home = Path(str(source["codex_home"]))
        target_home = Path(str(target["codex_home"]))
        live_exists = self.live_home.is_dir()
        source_exists = source_home.is_dir()
        target_exists = target_home.is_dir()
        try:
            if live_exists and source_exists and not target_exists:
                self._data["active_profile_id"] = str(target["profile_id"])
            elif not live_exists and source_exists and target_exists:
                os.replace(target_home, self.live_home)
                self._data["active_profile_id"] = str(target["profile_id"])
            elif live_exists and not source_exists and target_exists:
                self._data["active_profile_id"] = str(source["profile_id"])
            elif not live_exists and source_exists and not target_exists:
                os.replace(source_home, self.live_home)
                self._data["active_profile_id"] = str(source["profile_id"])
            else:
                raise AccountSwitcherError(
                    "无法判断旧版中断状态。为保护本地对话，程序没有继续升级。"
                )
        except OSError as error:
            raise AccountSwitcherError(f"恢复旧版目录切换失败：{error}") from error
        self._data["transaction"] = None
        self.save()

    def _migrate_v2_to_v3(self) -> None:
        """Extract v2 auth caches while retaining its complete profiles as backup."""
        if not self.live_home.is_dir() or is_reparse_or_symlink(self.live_home):
            raise AccountSwitcherError(
                "升级前找不到当前 .codex。请先用旧版切回有本地对话的账号。"
            )
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.root / f"desktop-profiles-v2-backup-{timestamp}.json"
        shutil.copy2(self.index_path, backup)
        self._tighten_file(backup)
        active_id = str(self._data.get("active_profile_id") or "")
        migrated: list[dict[str, Any]] = []
        for item in self._data.get("profiles", []):
            profile_id = str(item["profile_id"])
            credential_dir = (self.credentials_root / profile_id).resolve()
            credential_dir.mkdir(parents=True, exist_ok=True)
            self._tighten_directory(credential_dir)
            old_home = Path(str(item["codex_home"]))
            source_auth = (
                self.live_auth_path if profile_id == active_id else old_home / "auth.json"
            )
            if self._is_valid_auth_file(source_auth):
                self._atomic_copy_auth(source_auth, credential_dir / "auth.json")
            migrated.append(
                AccountProfile(
                    profile_id=profile_id,
                    name=str(item["name"]),
                    credential_dir=str(credential_dir),
                    created_at=str(item["created_at"]),
                    last_used_at=(
                        str(item["last_used_at"])
                        if item.get("last_used_at")
                        else None
                    ),
                ).to_dict()
            )
        self._data = {
            "version": STORE_VERSION,
            "active_profile_id": active_id or None,
            "profiles": migrated,
            "transaction": None,
            "legacy_v2_full_profiles_retained": True,
            "migrated_at": utc_now(),
        }
        self.save()
        self.migration_performed = True
        self.migration_backup_path = backup

    def archive_profile(self, profile_id: str) -> Path:
        profile = self.profile(profile_id)
        if not profile:
            raise KeyError("找不到账号档案。")
        if profile.profile_id == self.active_profile_id:
            raise AccountSwitcherError("不能移除当前账号。请先切换到其他账号。")
        if not self._is_safe_credential_dir(profile):
            raise AccountSwitcherError("账号凭据目录异常，未移动任何文件。")
        destination = self.archive_root / (
            "credentials-"
            f"{profile.profile_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.move(str(profile.home_path), str(destination))
        self._data["profiles"] = [
            item for item in self._data["profiles"]
            if item.get("profile_id") != profile.profile_id
        ]
        self.save()
        return destination

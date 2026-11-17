"""Bridge local projects across Codex's account-scoped renderer snapshots.

Codex 26.1103 hashes JSON.stringify([userId, accountId]) for snapshot owners.
Only known local project fields are shared; account atoms and remote/cloud
projects retain Codex's normal account isolation. No database is rewritten.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from typing import Any


ACCOUNT_STATE = "electron-account-renderer-state-v1"
ATOM_STATE = "electron-persisted-atom-state"
GLOBAL_STATE_FILE = ".codex-global-state.json"
MAX_STATE_BYTES = 64 * 1024 * 1024


def auth_owner(auth: dict[str, Any]) -> str:
    tokens = auth.get("tokens", {})
    if isinstance(tokens, dict):
        for key in ("id_token", "access_token"):
            try:
                payload = tokens[key].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                claims = claims["https://api.openai.com/auth"]
                user = claims.get("chatgpt_user_id") or claims.get("user_id")
                account = tokens.get("account_id") or claims.get("chatgpt_account_id")
                if isinstance(user, str) and user and isinstance(account, str) and account:
                    identity = json.dumps([user, account], ensure_ascii=False, separators=(",", ":"))
                    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, UnicodeError):
                continue
    raise ValueError("无法识别目标账号的项目状态归属，请重新登录该账号后再切换。")


def _local_project(key: str, value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("id") == key
        and not key.startswith("g-p-")
        and not any(value.get(k) for k in (
            "chatGptBacking", "chatGptBackingCreation", "retiredChatGptBacking",
        ))
        and isinstance(value.get("rootPaths"), list)
        and bool(value["rootPaths"])
        and all(isinstance(p, str) and p for p in value["rootPaths"])
    )


def share_local_projects(state: dict[str, Any], auth: dict[str, Any], host_key: str) -> dict[str, Any]:
    """Prepare the target snapshot without changing the active snapshot/owner.

    Start with the destination, then recover local records from older snapshots,
    and finally prefer the currently active account's latest local records.
    Existing destination records, including cloud/remote fields, are retained.
    """
    if ACCOUNT_STATE not in state:  # Older Codex already shares these fields.
        return state
    scoped = state[ACCOUNT_STATE]
    if (not isinstance(scoped, dict) or not isinstance(scoped.get("inactive"), dict)
            or not (scoped.get("owner") is None or isinstance(scoped.get("owner"), str))):
        raise ValueError("Codex 账号项目状态格式已变化，未修改登录缓存。")
    owner = auth_owner(auth)
    inactive = scoped["inactive"]
    for snapshot in inactive.values():
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("globals"), dict)
                or not isinstance(snapshot.get("atoms"), dict)):
            raise ValueError("Codex 账号项目快照无效，未修改登录缓存。")
    target = deepcopy(inactive.get(owner, {"atoms": {}, "globals": {}}))
    # A same-identity profile does not need a second, inactive owner snapshot.
    if scoped.get("owner") == owner:
        return state
    sources = [v["globals"] for k, v in inactive.items() if k != owner]
    if scoped.get("owner") is not None:
        sources.append(state)
    destination = target["globals"]
    local_ids: set[str] = set()
    for source in [destination] + sources:
        projects = source.get("local-projects", {})
        if isinstance(projects, dict):
            local_ids.update(k for k, v in projects.items() if _local_project(k, v))
    if not local_ids:
        return state

    def merge_map(key: str, entries: dict[str, Any]) -> None:
        if entries:
            existing = destination.get(key, {})
            if not isinstance(existing, dict):
                raise ValueError(f"Codex 项目字段 {key} 格式无效。")
            destination[key] = {**existing, **deepcopy(entries)}

    def merge_list(key: str, entries: list[Any]) -> None:
        if entries:
            existing = destination.get(key, [])
            if not isinstance(existing, list):
                raise ValueError(f"Codex 项目字段 {key} 格式无效。")
            destination[key] = deepcopy(entries) + [v for v in existing if v not in entries]

    for source in sources:
        projects = source.get("local-projects", {})
        if not isinstance(projects, dict):
            raise ValueError("Codex 本地项目列表格式无效。")
        projects = {k: v for k, v in projects.items() if _local_project(k, v)}
        # A destination cloud association always wins an ID collision.
        existing = destination.get("local-projects", {})
        projects = {k: v for k, v in projects.items()
                    if k not in existing or _local_project(k, existing[k])}
        merge_map("local-projects", projects)
        ids = {k for k, v in destination.get("local-projects", {}).items() if _local_project(k, v)}
        roots = {p for k in ids for p in destination["local-projects"][k]["rootPaths"]}
        assignments = source.get("thread-project-assignments", {})
        memberships = source.get("thread-project-membership-host-ids", {})
        assignments = {k: v for k, v in assignments.items()
                       if isinstance(v, dict) and v.get("projectKind") == "local"
                       and v.get("projectId") in ids and memberships.get(k, "local") == "local"}
        threads = set(assignments)
        orders = source.get("sidebar-project-thread-orders", {})
        for k, v in orders.items():
            if k in ids and isinstance(v, dict):
                threads.update(t for t in v.get("threadIds", []) if memberships.get(t, "local") == "local")
        merge_map("thread-project-assignments", assignments)
        for key in ("project-appearances", "project-files", "project-writable-roots", "sidebar-project-thread-orders"):
            merge_map(key, {k: v for k, v in source.get(key, {}).items() if k in ids})
        for key in ("thread-project-membership-host-ids", "thread-writable-roots", "thread-workspace-root-hints"):
            merge_map(key, {k: v for k, v in source.get(key, {}).items() if k in threads})
        for key in ("project-order", "pinned-project-ids"):
            merge_list(key, [v for v in source.get(key, []) if v in ids])
        merge_list("pinned-thread-ids", [v for v in source.get("pinned-thread-ids", []) if v in threads])
        for key in ("active-workspace-roots", "electron-saved-workspace-roots"):
            merge_list(key, [v for v in source.get(key, []) if v in roots])
        merge_map("electron-workspace-root-labels", {k: v for k, v in source.get("electron-workspace-root-labels", {}).items() if k in roots})
        selected = source.get("selected-project")
        if isinstance(selected, dict) and selected.get("type") == "local" and selected.get("projectId") in ids:
            destination["selected-project"] = deepcopy(selected)
        # Keep the original server IDs to avoid duplicate projects on migration.
        key = "app-server-project-id-by-legacy-project-id-by-host"
        mapping = {k: v for k, v in source.get(key, {}).get(host_key, {}).items() if k in ids}
        if mapping:
            existing_hosts = deepcopy(destination.get(key, {}))
            existing_hosts[host_key] = {**existing_hosts.get(host_key, {}), **mapping}
            destination[key] = existing_hosts
        # Migration flags are intentionally not copied: an account that has not
        # migrated must run its own project/thread migration, using the IDs above.

    if target == inactive.get(owner):
        return state
    result = deepcopy(state)
    result[ACCOUNT_STATE]["inactive"][owner] = target
    return result

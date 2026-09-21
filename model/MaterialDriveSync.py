"""Persistent, one-way Google Drive material folder synchronization.

The history is keyed by Drive file id and remote revision.  A local file that
the user moved or deleted therefore stays consumed and is not downloaded again
until its remote revision changes or the user explicitly forces a download.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from app_paths import APP_ROOT
from model.GoogleDriveDownloader import jfif_content_extension, safe_filename
from model.GoogleDriveHelper import (
    get_remote_file,
    is_retryable_metadata_error,
    load_drive_service,
    retry_sleep_seconds,
)
from model.MaterialSourceDownloader import (
    GOOGLE_FOLDER_MIME,
    GOOGLE_NATIVE_PREFIX,
    GOOGLE_SHORTCUT_MIME,
    NATIVE_EXPORTS,
    parse_material_drive_link,
)


DEFAULT_MATERIAL_SYNC_STATE_FILE = APP_ROOT / "MaterialDriveSyncState.json"
DEFAULT_MATERIAL_SYNC_DIRECTORY = APP_ROOT / "MaterialLibrary" / "网盘同步素材"
MATERIAL_SYNC_CONFIG_KEYS = {
    "enabled": "material_drive_sync_enabled",
    "folder_url": "material_drive_sync_folder_url",
    "local_dir": "material_drive_sync_local_dir",
    "material_id": "material_drive_sync_material_id",
    "interval_minutes": "material_drive_sync_interval_minutes",
}


def normalize_material_sync_settings(config):
    config = config or {}
    try:
        interval = int(config.get(MATERIAL_SYNC_CONFIG_KEYS["interval_minutes"], 5))
    except (TypeError, ValueError):
        interval = 5
    return {
        "enabled": bool(config.get(MATERIAL_SYNC_CONFIG_KEYS["enabled"], False)),
        "folder_url": str(
            config.get(MATERIAL_SYNC_CONFIG_KEYS["folder_url"], "") or ""
        ).strip(),
        "local_dir": str(
            config.get(
                MATERIAL_SYNC_CONFIG_KEYS["local_dir"],
                DEFAULT_MATERIAL_SYNC_DIRECTORY,
            )
            or DEFAULT_MATERIAL_SYNC_DIRECTORY
        ).strip(),
        "material_id": str(
            config.get(MATERIAL_SYNC_CONFIG_KEYS["material_id"], "") or ""
        ).strip(),
        "interval_minutes": max(1, min(1440, interval)),
    }


def update_material_sync_config(config, settings):
    normalized = normalize_material_sync_settings({
        MATERIAL_SYNC_CONFIG_KEYS[key]: value for key, value in settings.items()
    })
    for key, config_key in MATERIAL_SYNC_CONFIG_KEYS.items():
        config[config_key] = normalized[key]
    return config


class MaterialSyncStateStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_MATERIAL_SYNC_STATE_FILE)
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            if not self.path.is_file():
                return {"version": 1, "folder_id": "", "files": {}}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as error:
                raise ValueError(
                    "网盘素材同步历史损坏，已停止自动同步以避免重复下载："
                    f"{self.path}"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(
                    "网盘素材同步历史格式无效，已停止自动同步以避免重复下载："
                    f"{self.path}"
                )
            files = value.get("files", {}) if isinstance(value, dict) else {}
            return {
                "version": 1,
                "folder_id": str(value.get("folder_id", "")),
                "local_root": str(value.get("local_root", "")),
                "last_checked_at": float(value.get("last_checked_at", 0) or 0),
                "files": dict(files) if isinstance(files, dict) else {},
            }

    def save(self, state):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f"{self.path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(str(temporary), str(self.path))
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def _execute_with_retry(factory, max_retries=4):
    for attempt in range(max_retries + 1):
        try:
            return factory().execute()
        except Exception as error:
            if attempt >= max_retries or not is_retryable_metadata_error(error):
                raise
            time.sleep(retry_sleep_seconds(attempt + 1))


def _folder_children(service, folder_id):
    children = []
    page_token = None
    while True:
        response = _execute_with_retry(
            lambda: service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=(
                    "nextPageToken,files(id,name,mimeType,size,modifiedTime,"
                    "md5Checksum,shortcutDetails(targetId,targetMimeType),"
                    "capabilities(canDownload))"
                ),
                pageSize=1000,
                pageToken=page_token,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        children.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return children


def _safe_part(value, fallback="素材"):
    return safe_filename(str(value or fallback), fallback)[0][:180]


def _collect_remote_files(service, folder_id, relative=(), active=None):
    active = set() if active is None else active
    if folder_id in active:
        return []
    active.add(folder_id)
    result = []
    try:
        for raw in _folder_children(service, folder_id):
            item = dict(raw)
            mime_type = str(item.get("mimeType") or "")
            if mime_type == GOOGLE_SHORTCUT_MIME:
                target_id = str(item.get("shortcutDetails", {}).get("targetId") or "")
                if not target_id:
                    continue
                target = get_remote_file(service, target_id)
                target = dict(target)
                target["name"] = item.get("name") or target.get("name")
                item = target
                mime_type = str(item.get("mimeType") or "")
            name = _safe_part(item.get("name") or item.get("id"))
            if mime_type == GOOGLE_FOLDER_MIME:
                result.extend(
                    _collect_remote_files(
                        service,
                        str(item.get("id") or ""),
                        relative + (name,),
                        active,
                    )
                )
                continue
            item["relative_parts"] = relative + (name,)
            result.append(item)
    finally:
        active.discard(folder_id)
    return result


def _download_parts(item):
    mime_type = str(item.get("mimeType") or "")
    parts = list(item.get("relative_parts") or ())
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        export = NATIVE_EXPORTS.get(mime_type)
        if export is None:
            return None
        _export_mime, extension = export
        if parts and not parts[-1].casefold().endswith(extension):
            parts[-1] += extension
    return parts


def _download_request(service, item):
    mime_type = str(item.get("mimeType") or "")
    parts = _download_parts(item)
    if parts is None:
        return None, list(item.get("relative_parts") or ())
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        export_mime, _extension = NATIVE_EXPORTS[mime_type]
        request = service.files().export_media(
            fileId=item["id"],
            mimeType=export_mime,
        )
    else:
        if item.get("capabilities", {}).get("canDownload") is False:
            return None, parts
        request = service.files().get_media(
            fileId=item["id"],
            supportsAllDrives=True,
        )
    return request, parts


def _signature(item):
    return "|".join((
        str(item.get("md5Checksum") or ""),
        str(item.get("modifiedTime") or ""),
        str(item.get("size") or ""),
        str(item.get("mimeType") or ""),
        "/".join(item.get("relative_parts") or ()),
    ))


def _download_item(service, item, local_root, progress_callback=None):
    from googleapiclient.http import MediaIoBaseDownload

    request, parts = _download_request(service, item)
    if request is None or not parts:
        return None
    parent = Path(local_root).joinpath(*parts[:-1])
    parent.mkdir(parents=True, exist_ok=True)
    requested_target = parent / parts[-1]
    part = requested_target.with_name(requested_target.name + ".part")
    if progress_callback:
        progress_callback(f"正在同步：{'/'.join(parts)}")
    try:
        with part.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=2 * 1024 * 1024)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=4)
        target = requested_target
        if requested_target.suffix.casefold() == ".jfif":
            target = requested_target.with_suffix(jfif_content_extension(part))
        os.replace(str(part), str(target))
        if target != requested_target and requested_target.exists():
            try:
                requested_target.unlink()
            except OSError:
                pass
        return target
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def sync_material_drive_folder(
    settings,
    state_store=None,
    force_ids=None,
    adopt_existing=False,
    service_factory=load_drive_service,
    progress_callback=None,
):
    settings = normalize_material_sync_settings({
        MATERIAL_SYNC_CONFIG_KEYS[key]: value for key, value in settings.items()
    })
    link = parse_material_drive_link(settings["folder_url"])
    if not link.is_folder:
        raise ValueError("持续同步来源必须是 Google Drive 文件夹链接。")
    local_root = Path(settings["local_dir"])
    local_root.mkdir(parents=True, exist_ok=True)
    resolved_local_root = str(local_root.resolve())
    state_store = state_store or MaterialSyncStateStore()
    state = state_store.load()
    # A different source folder gets an independent history.  Keeping stale
    # entries would make coincidentally identical ids/statuses misleading.
    if (
        state.get("folder_id") != link.file_id
        or os.path.normcase(str(state.get("local_root") or ""))
        != os.path.normcase(resolved_local_root)
    ):
        state = {
            "version": 1,
            "folder_id": link.file_id,
            "local_root": resolved_local_root,
            "files": {},
        }
    service = service_factory()
    remote_files = _collect_remote_files(service, link.file_id)
    # Drive permits two different ids with the same name in one folder.  Give
    # every member of such a collision a stable id suffix instead of silently
    # overwriting whichever one happened to be listed first.
    path_groups = {}
    for item in remote_files:
        key = "/".join(item.get("relative_parts") or ()).casefold()
        path_groups.setdefault(key, []).append(item)
    for group in path_groups.values():
        if len(group) < 2:
            continue
        for item in group:
            parts = list(item.get("relative_parts") or ())
            if not parts:
                continue
            path = Path(parts[-1])
            parts[-1] = (
                f"{path.stem} [{str(item.get('id') or '')[:8]}]{path.suffix}"
            )
            item["relative_parts"] = tuple(parts)
    force_ids = {str(value) for value in (force_ids or ())}
    current_ids = set()
    downloaded = []
    skipped = 0
    errors = []
    now = time.time()

    for item in remote_files:
        file_id = str(item.get("id") or "")
        if not file_id:
            continue
        current_ids.add(file_id)
        signature = _signature(item)
        previous = dict(state["files"].get(file_id) or {})
        if adopt_existing and not previous and file_id not in force_ids:
            parts = _download_parts(item)
            existing = local_root.joinpath(*parts) if parts else None
            if existing is not None and existing.suffix.casefold() == ".jfif":
                candidates = (
                    existing,
                    existing.with_suffix(".jpg"),
                    existing.with_suffix(".jpeg"),
                    existing.with_suffix(".png"),
                )
                existing = next((path for path in candidates if path.is_file()), existing)
            if existing is not None and existing.is_file():
                previous = {
                    "id": file_id,
                    "local_path": str(existing.resolve()),
                    "local_status": "present",
                    "remote_signature": signature,
                    "adopted_at": now,
                }
        should_download = (
            file_id in force_ids
            or not previous
            or previous.get("remote_signature") != signature
        )
        previous.update({
            "id": file_id,
            "name": str(item.get("name") or ""),
            "last_seen_signature": signature,
            "modified_time": str(item.get("modifiedTime") or ""),
            "size": str(item.get("size") or ""),
            "remote_present": True,
            "last_seen_at": now,
        })
        if not should_download:
            local_path = Path(str(previous.get("local_path") or ""))
            previous["local_status"] = "present" if local_path.is_file() else "consumed"
            state["files"][file_id] = previous
            skipped += 1
            continue
        try:
            target = _download_item(
                service,
                item,
                local_root,
                progress_callback=progress_callback,
            )
            if target is None:
                previous["local_status"] = "unsupported"
                previous["remote_signature"] = signature
                skipped += 1
            else:
                previous["local_path"] = str(target.resolve())
                previous["local_status"] = "present"
                previous["downloaded_at"] = now
                previous["remote_signature"] = signature
                previous.pop("last_error", None)
                downloaded.append(str(target))
        except Exception as error:
            previous["local_status"] = "error"
            previous["last_error"] = f"{type(error).__name__}: {error}"
            errors.append(f"{previous['name']}：{error}")
        state["files"][file_id] = previous

    for file_id, record in state["files"].items():
        if file_id not in current_ids:
            record["remote_present"] = False
            record["local_status"] = "remote_removed"
    state["last_checked_at"] = now
    state_store.save(state)
    return {
        "downloaded": downloaded,
        "downloaded_count": len(downloaded),
        "skipped_count": skipped,
        "errors": errors,
        "state": state,
        "local_dir": str(local_root.resolve()),
    }

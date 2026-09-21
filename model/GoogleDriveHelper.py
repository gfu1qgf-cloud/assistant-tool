import hashlib
import mimetypes
import random
import re
import socket
import ssl
import time
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app_paths import APP_ROOT

CLIENT_SECRET_FILE = APP_ROOT / "GoogleDriveCredentials.json"
TOKEN_FILE = APP_ROOT / "GoogleDriveToken.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_MAX_RETRIES = 8
METADATA_MAX_RETRIES = 5


def folder_name(task_date: date) -> str:
    return f"{task_date.month:02d}{task_date.day:02d}"


def drive_folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def collect_person_folder_links(records: Iterable[Dict], review_folder_name: str) -> Dict[str, str]:
    result = {}
    review_folder_name = str(review_folder_name or "review").strip().casefold()

    for record in records:
        prefix_text = str(record.get("remote_prefix") or "").strip()
        if prefix_text in {"", "."}:
            continue
        prefix_parts = [
            part for part in re.split(r"[\\/]+", prefix_text) if part
        ]
        if not prefix_parts or prefix_parts[0].casefold() == review_folder_name:
            continue

        folder_id = str(
            record.get("remote_prefix_root_folder_id")
            or record.get("remote_prefix_folder_id")
            or ""
        ).strip()
        if not folder_id:
            continue

        person_name = prefix_parts[0]
        result.setdefault(person_name, drive_folder_link(folder_id))

    return result


def print_person_folder_links(records: Iterable[Dict], review_folder_name: str) -> Dict[str, str]:
    links = collect_person_folder_links(records, review_folder_name)
    if not links:
        return links

    print("\n各人员 Google Drive 文件夹链接（可直接分发）：")
    print("=" * 70)
    for person_name, link in links.items():
        print(f"{person_name}：{link}")
    print("=" * 70)
    return links


def extract_drive_folder_id(value: str) -> str:
    text = value.strip()
    if not text:
        return ""

    folder_match = re.search(r"/folders/([a-zA-Z0-9_-]+)", text)
    if folder_match:
        return folder_match.group(1)

    id_match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", text)
    if id_match:
        return id_match.group(1)

    return text


def read_drive_parent_folder_id(configured_value: str = "") -> str:
    configured = extract_drive_folder_id(configured_value)
    if configured:
        return configured

    user_input = input("请输入 Google Drive 目标文件夹 ID 或文件夹链接：").strip()
    folder_id = extract_drive_folder_id(user_input)
    if not folder_id:
        raise ValueError("Google Drive 目标文件夹不能为空")

    return folder_id


def load_drive_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        missing_name = getattr(exc, "name", "Google Drive API 依赖")
        raise SystemExit(
            "缺少 Google Drive API 依赖。请先在你的 Python 环境安装：\n"
            "pip install google-api-python-client google-auth google-auth-oauthlib\n"
            f"当前缺少：{missing_name}"
        )

    credentials = None
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not CLIENT_SECRET_FILE.exists():
                raise SystemExit(
                    f"没找到授权文件：{CLIENT_SECRET_FILE}\n"
                    "请在 Google Cloud Console 创建 OAuth 桌面客户端，"
                    "下载 JSON 后改名为 GoogleDriveCredentials.json，放到程序根目录。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=credentials)


def escape_query_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _is_rate_limit_403(exc: Exception) -> bool:
    try:
        content = getattr(exc, "content", b"")
        if isinstance(content, (bytes, bytearray)):
            content_str = content.decode("utf-8", errors="ignore")
        else:
            content_str = str(content or "")

        resp = getattr(exc, "resp", None)
        reason_str = str(getattr(resp, "reason", ""))
        err_str = str(exc)

        combined = f"{err_str} {content_str} {reason_str}".lower()
        normalized = combined.replace(" ", "").replace("_", "").replace("-", "")
        if "ratelimitexceeded" in normalized or "userratelimitexceeded" in normalized:
            return True
        if "ratelimit" in normalized or "userratelimit" in normalized:
            return True

        if "{" in content_str:
            import json
            try:
                data = json.loads(content_str)
                if isinstance(data, dict):
                    err_obj = data.get("error", {})
                    if isinstance(err_obj, dict):
                        for err in err_obj.get("errors", []):
                            if isinstance(err, dict):
                                r = str(err.get("reason", "")).lower()
                                if r in {"ratelimitexceeded", "userratelimitexceeded"}:
                                    return True
                                m = str(err.get("message", "")).lower()
                                if "rate limit" in m or "quota" in m:
                                    return True
                        msg = str(err_obj.get("message", "")).lower()
                        if "rate limit" in msg or "quota" in msg:
                            return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def is_retryable_upload_error(exc: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except Exception:
        HttpError = None

    status = None
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            status = int(getattr(resp, "status", None))
        except (ValueError, TypeError):
            pass

    if status is None and HttpError is not None and isinstance(exc, HttpError):
        try:
            status = int(getattr(exc.resp, "status", None))
        except (ValueError, TypeError):
            pass

    if status is not None:
        if status in {429, 500, 502, 503, 504}:
            return True
        if status == 403 and _is_rate_limit_403(exc):
            return True
        return False

    retryable_types = (
        ssl.SSLError,
        socket.timeout,
        TimeoutError,
        ConnectionError,
        OSError,
    )
    return isinstance(exc, retryable_types)


is_retryable_metadata_error = is_retryable_upload_error


def retry_sleep_seconds(retry_count: int) -> float:
    base = min(60, 2 ** retry_count)
    return base + random.uniform(0.0, 1.5)


def list_remote_children(
    service,
    parent_id: str,
    name: str,
    max_retries: int = METADATA_MAX_RETRIES,
) -> List[Dict]:
    safe_name = escape_query_text(name)
    query = f"'{parent_id}' in parents and name = '{safe_name}' and trashed = false"
    for attempt in range(max_retries + 1):
        try:
            results = service.files().list(
                q=query,
                spaces="drive",
                fields="files(id,name,mimeType,md5Checksum,size,modifiedTime,webViewLink,webContentLink)",
                pageSize=20,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            return results.get("files", [])
        except Exception as exc:
            if not is_retryable_upload_error(exc) or attempt >= max_retries:
                raise
            wait_seconds = retry_sleep_seconds(attempt + 1)
            time.sleep(wait_seconds)


def get_remote_file(
    service,
    file_id: str,
    max_retries: int = METADATA_MAX_RETRIES,
) -> Dict:
    for attempt in range(max_retries + 1):
        try:
            return service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,md5Checksum,size,modifiedTime,webViewLink,webContentLink,trashed",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            if not is_retryable_upload_error(exc) or attempt >= max_retries:
                raise
            wait_seconds = retry_sleep_seconds(attempt + 1)
            time.sleep(wait_seconds)


def get_or_create_remote_folder(
    service,
    parent_id: str,
    folder_name_text: str,
    max_retries: int = METADATA_MAX_RETRIES,
) -> str:
    matches = list_remote_children(service, parent_id, folder_name_text, max_retries=max_retries)
    for item in matches:
        if item.get("mimeType") == GOOGLE_FOLDER_MIME:
            return item["id"]

    metadata = {
        "name": folder_name_text,
        "mimeType": GOOGLE_FOLDER_MIME,
        "parents": [parent_id],
    }

    for attempt in range(max_retries + 1):
        if attempt > 0:
            matches = list_remote_children(service, parent_id, folder_name_text, max_retries=max_retries)
            for item in matches:
                if item.get("mimeType") == GOOGLE_FOLDER_MIME:
                    return item["id"]

        try:
            folder = service.files().create(
                body=metadata,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            print(f"已创建云端文件夹: {folder_name_text}")
            return folder["id"]
        except Exception as exc:
            if not is_retryable_upload_error(exc) or attempt >= max_retries:
                raise
            wait_seconds = retry_sleep_seconds(attempt + 1)
            time.sleep(wait_seconds)


def get_or_create_remote_folder_path(service, parent_id: str, relative_dir: Path) -> str:
    folder_id, _root_folder_id = get_or_create_remote_folder_path_with_root(
        service,
        parent_id,
        relative_dir,
    )
    return folder_id


def get_or_create_remote_folder_path_with_root(
    service,
    parent_id: str,
    relative_dir: Path,
) -> Tuple[str, str]:
    folder_id = parent_id
    root_folder_id = ""
    for part in relative_dir.parts:
        if part in {"", "."}:
            continue
        folder_id = get_or_create_remote_folder(service, folder_id, part)
        if not root_folder_id:
            root_folder_id = folder_id
    return folder_id, root_folder_id


def file_md5(file_path: Path) -> str:
    digest = hashlib.md5()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_google_native_file(file_info: Dict) -> bool:
    mime_type = file_info.get("mimeType", "")
    return mime_type.startswith("application/vnd.google-apps.")




def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size_bytes}B"




def execute_resumable_upload(request, local_file: Path, action_text: str) -> Dict:
    file_size = local_file.stat().st_size if local_file.exists() else 0
    print(f"{action_text}: {local_file.name}（{format_file_size(file_size)}）")

    response = None
    last_reported = -1
    retry_count = 0

    while response is None:
        try:
            status, response = request.next_chunk()
            retry_count = 0
        except Exception as exc:
            if not is_retryable_upload_error(exc) or retry_count >= UPLOAD_MAX_RETRIES:
                print(f"  上传失败，已超过重试次数：{type(exc).__name__}: {exc}")
                raise

            retry_count += 1
            wait_seconds = retry_sleep_seconds(retry_count)
            print(f"  上传连接中断，{wait_seconds:.1f}s 后重试 {retry_count}/{UPLOAD_MAX_RETRIES}：{type(exc).__name__}")
            time.sleep(wait_seconds)
            continue

        if status:
            percent = int(status.progress() * 100)
            if percent >= last_reported + 5 or percent >= 99:
                print(f"  进度：{percent}%")
                last_reported = percent

    print("  进度：100%")
    return response

def upload_new_file(service, local_file: Path, parent_id: str) -> Dict:
    from googleapiclient.http import MediaFileUpload

    mime_type, _ = mimetypes.guess_type(str(local_file))
    media = MediaFileUpload(
        str(local_file),
        mimetype=mime_type,
        chunksize=UPLOAD_CHUNK_SIZE,
        resumable=True,
    )
    metadata = {"name": local_file.name, "parents": [parent_id]}
    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,mimeType,md5Checksum,size,modifiedTime,webViewLink,webContentLink",
        supportsAllDrives=True,
    )
    created = execute_resumable_upload(request, local_file, "开始上传")
    print(f"已上传: {local_file} -> {created.get('id')}")
    return created


def update_existing_file(service, local_file: Path, remote_file_id: str) -> Dict:
    from googleapiclient.http import MediaFileUpload

    mime_type, _ = mimetypes.guess_type(str(local_file))
    media = MediaFileUpload(
        str(local_file),
        mimetype=mime_type,
        chunksize=UPLOAD_CHUNK_SIZE,
        resumable=True,
    )
    request = service.files().update(
        fileId=remote_file_id,
        media_body=media,
        fields="id,name,mimeType,md5Checksum,size,modifiedTime,webViewLink,webContentLink",
        supportsAllDrives=True,
    )
    updated = execute_resumable_upload(request, local_file, "开始覆盖")
    print(f"已覆盖云端文件: {local_file} -> {updated.get('id')}")
    return updated


def sync_file(
    service,
    local_file: Path,
    parent_id: str,
    preferred_file_id: str = "",
) -> Optional[Dict]:
    matches = list_remote_children(service, parent_id, local_file.name)
    remote_files = [item for item in matches if item.get("mimeType") != GOOGLE_FOLDER_MIME]

    reused_previous_batch = False
    preferred_file_id = str(preferred_file_id or "").strip()
    if not remote_files and preferred_file_id:
        try:
            previous_file = get_remote_file(service, preferred_file_id)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in {403, 404}:
                raise
            print(
                "历史网盘文件已不可访问，改为在本批次新建："
                f"{local_file.name} -> {preferred_file_id}"
            )
        else:
            if (
                not previous_file.get("trashed")
                and previous_file.get("mimeType") != GOOGLE_FOLDER_MIME
                and not is_google_native_file(previous_file)
            ):
                remote_files = [previous_file]
                reused_previous_batch = True
                print(
                    "发现跨批次同名成品，沿用原链接覆盖："
                    f"{local_file.name} -> {preferred_file_id}"
                )

    if not remote_files:
        created = upload_new_file(service, local_file, parent_id)
        created["action"] = "uploaded"
        created["local_file"] = str(local_file)
        return created

    normal_files = [item for item in remote_files if not is_google_native_file(item)]
    if not normal_files:
        print(f"跳过：云端同名对象是 Google 原生文档，不能按普通文件覆盖: {local_file.name}")
        return None

    remote_file = normal_files[0]
    if len(normal_files) > 1:
        print(f"注意：云端有多个同名普通文件，只检查并更新第一个: {local_file.name}")

    local_md5 = file_md5(local_file)
    remote_md5 = remote_file.get("md5Checksum")

    if remote_md5 and remote_md5 == local_md5:
        print(f"跳过一致文件: {local_file}")
        remote_file["action"] = (
            "skipped_same_previous_batch"
            if reused_previous_batch
            else "skipped_same"
        )
        remote_file["local_file"] = str(local_file)
        return remote_file

    updated = update_existing_file(service, local_file, remote_file["id"])
    updated["action"] = (
        "updated_previous_batch" if reused_previous_batch else "updated"
    )
    updated["local_file"] = str(local_file)
    return updated

def sync_directory_contents(service, local_dir: Path, remote_parent_id: str) -> None:
    for item in sorted(local_dir.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
        if item.is_dir():
            remote_folder_id = get_or_create_remote_folder(service, remote_parent_id, item.name)
            sync_directory_contents(service, item, remote_folder_id)
        elif item.is_file():
            try:
                sync_file(service, item, remote_parent_id)
            except Exception as exc:
                print(f"上传失败，跳过当前文件：{item.name} -> {type(exc).__name__}: {exc}")


def upload_local_dirs_to_drive_date_folder(
    service,
    local_dirs: Iterable[Path],
    parent_folder_id: str,
    drive_date: Optional[date] = None,
) -> None:
    upload_date = drive_date or date.today()
    remote_folder_name = folder_name(upload_date)
    remote_folder_id = get_or_create_remote_folder(service, parent_folder_id, remote_folder_name)

    print(f"\nGoogle Drive 上传日期文件夹：{remote_folder_name}（{upload_date:%Y-%m-%d}）")
    for local_dir in local_dirs:
        local_dir = Path(local_dir)
        if not local_dir.exists():
            print(f"本地目录不存在，跳过：{local_dir}")
            continue

        print(f"\n开始上传本地目录：{local_dir}")
        sync_directory_contents(service, local_dir, remote_folder_id)
        print(f"上传完成：{local_dir}")


def upload_changed_files_to_drive_date_folder(
    service,
    changed_file_batches: Iterable[Tuple[Path, Iterable[Path]]],
    parent_folder_id: str,
    drive_date: Optional[date] = None,
) -> None:
    upload_date = drive_date or date.today()
    remote_folder_name = folder_name(upload_date)
    remote_folder_id = get_or_create_remote_folder(service, parent_folder_id, remote_folder_name)

    total = 0
    print(f"\nGoogle Drive 上传日期文件夹：{remote_folder_name}（{upload_date:%Y-%m-%d}）")
    for root_dir, changed_files in changed_file_batches:
        root_dir = Path(root_dir)
        for changed_file in changed_files:
            changed_file = Path(changed_file)
            if not changed_file.exists():
                print(f"本地文件不存在，跳过：{changed_file}")
                continue

            try:
                relative_path = changed_file.relative_to(root_dir)
            except ValueError:
                relative_path = Path(changed_file.name)

            target_parent_id = get_or_create_remote_folder_path(service, remote_folder_id, relative_path.parent)
            try:
                sync_file(service, changed_file, target_parent_id)
            except Exception as exc:
                print(f"上传失败，跳过当前文件：{changed_file.name} -> {type(exc).__name__}: {exc}")
            total += 1

    if total == 0:
        print("没有本次新增/更新的文件需要上传。")
    else:
        print(f"本次检查上传文件数：{total}")

def upload_routed_changed_files_to_drive_batch(
    service,
    routed_file_batches: Iterable[Tuple[Path, Iterable[Path], Path]],
    parent_folder_id: str,
    batch_date: date,
    batch_slot: str,
    preferred_file_ids: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    date_folder_id = get_or_create_remote_folder(service, parent_folder_id, folder_name(batch_date))
    slot_folder_id = get_or_create_remote_folder(service, date_folder_id, batch_slot)

    total = 0
    synced_files = []
    print(f"\nGoogle Drive 上传批次：{folder_name(batch_date)}/{batch_slot}（{batch_date:%Y-%m-%d}）")

    for root_dir, changed_files, remote_prefix in routed_file_batches:
        root_dir = Path(root_dir)
        remote_prefix = Path(remote_prefix)
        remote_prefix_folder_id = slot_folder_id
        remote_prefix_root_folder_id = ""
        if str(remote_prefix) not in {"", "."}:
            (
                remote_prefix_folder_id,
                remote_prefix_root_folder_id,
            ) = get_or_create_remote_folder_path_with_root(
                service,
                slot_folder_id,
                remote_prefix,
            )

        for changed_file in changed_files:
            changed_file = Path(changed_file)
            if not changed_file.exists():
                print(f"本地文件不存在，跳过：{changed_file}")
                continue

            try:
                local_relative_path = changed_file.relative_to(root_dir)
            except ValueError:
                local_relative_path = Path(changed_file.name)

            relative_path = local_relative_path
            if str(remote_prefix) not in {"", "."}:
                relative_path = remote_prefix / relative_path

            target_parent_id = get_or_create_remote_folder_path(
                service,
                remote_prefix_folder_id,
                local_relative_path.parent,
            )
            try:
                synced = sync_file(
                    service,
                    changed_file,
                    target_parent_id,
                    preferred_file_id=(preferred_file_ids or {}).get(
                        changed_file.name,
                        "",
                    ),
                )
            except Exception as exc:
                print(f"上传失败，跳过当前文件：{changed_file.name} -> {type(exc).__name__}: {exc}")
                total += 1
                continue

            if synced:
                synced["root_dir"] = str(root_dir)
                synced["relative_path"] = str(relative_path)
                synced["remote_prefix"] = str(remote_prefix)
                synced["remote_prefix_folder_id"] = remote_prefix_folder_id
                synced["remote_prefix_root_folder_id"] = remote_prefix_root_folder_id
                synced_files.append(synced)
            total += 1

    if total == 0:
        print("没有本次新增/更新的文件需要上传。")
    else:
        print(f"本次检查上传文件数：{total}")

    return synced_files
def upload_local_dirs_to_drive_batch(
    service,
    local_dirs: Iterable[Path],
    parent_folder_id: str,
    batch_date: date,
    batch_slot: str,
) -> None:
    date_folder_id = get_or_create_remote_folder(service, parent_folder_id, folder_name(batch_date))
    slot_folder_id = get_or_create_remote_folder(service, date_folder_id, batch_slot)

    print(f"\nGoogle Drive 整目录上传批次：{folder_name(batch_date)}/{batch_slot}（{batch_date:%Y-%m-%d}）")
    for local_dir in local_dirs:
        local_dir = Path(local_dir)
        if not local_dir.exists():
            print(f"本地目录不存在，跳过：{local_dir}")
            continue

        print(f"\n开始上传本地目录：{local_dir}")
        sync_directory_contents(service, local_dir, slot_folder_id)
        print(f"上传完成：{local_dir}")



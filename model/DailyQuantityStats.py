"""Reconcile uploaded video counts with an existing daily Google Sheet.

The upload ledger is the source of video/date identity.  Local task ODS files
are read afresh on every run so a missing or corrected category can be fixed
without uploading the video again.  Only the three period input cells are
owned here; quotas and SUM formulas are never written.
"""

import json
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from app_paths import APP_ROOT
from model.GoogleSheetsHelper import (
    column_to_letter,
    extract_spreadsheet_id,
    load_sheets_service,
    sheet_range,
)
from model.OdsHelper import ReadTaskOds2
from model.TaskTableSchema import load_task_table_schema
from model.VideoUploadHistory import (
    VIDEO_SUFFIXES,
    all_video_upload_records,
    normalize_video_identity,
)


STATE_FILE = APP_ROOT / "DailyQuantityStats.json"
_LOCK = threading.RLock()
_PERIOD_LABELS = {
    "01": ("12点", "12:00"),
    "02": ("18点", "18:00"),
    "03": ("24点", "24:00"),
}


def _key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _date(value):
    if isinstance(value, (int, float)):
        try:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
        except (ValueError, OverflowError):
            return ""
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def _value(rows, row, col):
    if row < 0 or row >= len(rows) or col < 0 or col >= len(rows[row]):
        return ""
    return rows[row][col]


def _number(value):
    if value in ("", None):
        return 0
    if isinstance(value, bool) or isinstance(value, str) and value.startswith("="):
        raise ValueError("目标格不是手填数字格")
    number = float(str(value).replace(",", ""))
    if not number.is_integer() or number < 0:
        raise ValueError("目标格不是非负整数")
    return int(number)


def _project_ods(root, record, table_name):
    local_file = Path(str(record.get("local_file") or ""))
    try:
        relative = local_file.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) < 3:
        return None
    return root / relative.parts[0] / table_name


def collect_assignments(config, root, records=None, allowed_dates=None):
    """Return one first-delivery assignment per logical finished video."""
    root = Path(root).resolve()
    table_name = str(config.get("task_table_file_name") or "任务登记表格.ods").strip()
    creator = str(config.get("task_submission_creator") or "").strip()
    warnings = []
    if not creator:
        raise ValueError("请先在程序设置 → 整理任务结果填写任务制作人")
    records = all_video_upload_records(config) if records is None else records
    first = {}
    for record in records:
        if record.get("source") != "upload":
            continue
        ods = _project_ods(root, record, table_name)
        if ods is None:
            continue
        day = _date(record.get("batch_date"))
        slot = str(record.get("batch_slot") or "").zfill(2)
        logical = str(record.get("logical_key") or "").strip()
        if not day or slot not in _PERIOD_LABELS or not logical:
            continue
        if allowed_dates is not None and day not in allowed_dates:
            continue
        identity = f"{ods.parent.resolve()}|{logical}"
        order = (day, slot, str(record.get("recorded_at") or ""))
        if identity not in first or order < first[identity][0]:
            first[identity] = (order, record, ods)

    tasks_by_ods = {}
    groups = defaultdict(list)
    for identity, ((day, slot, _), record, ods) in first.items():
        if ods not in tasks_by_ods:
            if not ods.is_file():
                tasks_by_ods[ods] = None
            else:
                try:
                    tasks, report = ReadTaskOds2(
                        ods, schema=load_task_table_schema(), return_report=True
                    )
                    tasks_by_ods[ods] = tasks
                    absent = [label for field, label in (
                        ("daily_stat_sheet", "每日统计分页"),
                        ("daily_stat_category", "每日统计类别"),
                    ) if not report["matched_headers"].get(field)]
                    if absent:
                        warnings.append(
                            f"{ods}：缺少列 {'、'.join(absent)}；"
                            "请在本地任务表末尾新增列后再刷新"
                        )
                except Exception as exc:
                    tasks_by_ods[ods] = None
                    warnings.append(f"{ods.parent.name}：任务表读取失败：{exc}")
        tasks = tasks_by_ods[ods]
        label = str(record.get("file_name") or identity.rsplit("|", 1)[-1])
        if tasks is None:
            warnings.append(f"{label}：找不到可读取的本地任务表 {ods}")
            continue
        info = record.get("task") or {}
        task_id = _key(info.get("id"))
        row = info.get("row")
        candidates = [task for task in tasks if task_id and _key(task.task_id) == task_id]
        if row is not None:
            narrowed = [task for task in candidates if str(task.source_row) == str(row)]
            if narrowed:
                candidates = narrowed
        if len(candidates) != 1:
            warnings.append(f"{label}：本地任务编号匹配到 {len(candidates)} 行，未计数")
            continue
        task = candidates[0]
        sheet = task.daily_stat_sheet
        category = task.daily_stat_category
        if not sheet or not category:
            warnings.append(f"{label}：每日统计分页/类别未填，暂不计数；填好后点刷新")
            continue
        groups[(sheet, day, slot, category, creator)].append({
            "identity": identity,
            "file_name": label,
            "event_id": str(record.get("event_id") or ""),
            "drive_file_id": str(record.get("drive_file_id") or ""),
        })
    return groups, warnings


def _find_cell(rows, day, slot, creator, category):
    if len(rows) < 2:
        raise ValueError("缺少日期/时段表头")
    dates = [(col, _date(raw)) for col, raw in enumerate(rows[0]) if _date(raw)]
    columns = []
    for index, (start, date_text) in enumerate(dates):
        if date_text != day:
            continue
        end = dates[index + 1][0] if index + 1 < len(dates) else len(rows[1])
        labels = _PERIOD_LABELS[slot]
        columns = [col for col in range(start, end)
                   if any(label in str(_value(rows, 1, col)) for label in labels)]
        break
    if len(columns) != 1:
        raise ValueError(f"{day} / {slot} 时段列匹配到 {len(columns)} 列")

    people = [row for row in range(2, len(rows))
              if _key(_value(rows, row, 1)) == _key(creator)]
    if len(people) != 1:
        raise ValueError(f"制作人 {creator} 匹配到 {len(people)} 个区块")
    start = people[0] + 1
    end = next((row for row in range(start, len(rows))
                if str(_value(rows, row, 0)).strip()
                or str(_value(rows, row, 1)).strip()), len(rows))
    matches = [row for row in range(start, end)
               if _key(_value(rows, row, 2)) == _key(category)]
    if len(matches) != 1:
        raise ValueError(f"类别 {category} 在本人区块匹配到 {len(matches)} 行")
    row, col = matches[0], columns[0]
    if str(_value(rows, row, col)).startswith("="):
        raise ValueError("目标格有公式，不会覆盖")
    return row, col


def _category_options(snapshots, creator):
    """Read only category labels inside the configured creator's block."""
    result = {}
    for sheet, rows in snapshots.items():
        people = [row for row in range(2, len(rows))
                  if _key(_value(rows, row, 1)) == _key(creator)]
        if len(people) != 1:
            continue
        start = people[0] + 1
        end = next((row for row in range(start, len(rows))
                    if str(_value(rows, row, 0)).strip()
                    or str(_value(rows, row, 1)).strip()), len(rows))
        categories = sorted({str(_value(rows, row, 2)).strip()
                             for row in range(start, end)
                             if str(_value(rows, row, 2)).strip()})
        if categories:
            result[sheet] = categories
    return result


def _load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save_state(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _scope_key(config, root):
    url = str(config.get("daily_quantity_sheet_url") or "").strip()
    if not url:
        raise ValueError("请先在程序设置 → 整理任务结果填写每日数量表格链接")
    return f"{extract_spreadsheet_id(url)}|{Path(root).resolve()}"


def external_video_records(config, root, state_path=None):
    """Return copies of folder-imported videos for the editor."""
    with _LOCK:
        scope = _load_state(state_path or STATE_FILE).get(_scope_key(config, root), {})
        return [dict(item) for item in scope.get("external_videos", [])]


def external_video_sources(config, root, state_path=None):
    """Return saved folder links so a previously imported source is easy to rescan."""
    with _LOCK:
        scope = _load_state(state_path or STATE_FILE).get(_scope_key(config, root), {})
        return [dict(item) for item in scope.get("external_folders", [])]


def scan_daily_drive_date(
    config, root, batch_date, service=None, state_path=None, records=None
):
    """Reconcile a delivery day against the videos physically in Drive."""
    from model.GoogleDriveHelper import (
        GOOGLE_FOLDER_MIME,
        extract_drive_folder_id,
        load_drive_service,
    )
    from model.MaterialDriveSync import _collect_remote_files, _folder_children

    day = _date(batch_date)
    if not day:
        raise ValueError("请选择有效的交付日期。")
    parent_id = extract_drive_folder_id(
        str(config.get("drive_parent_folder_id") or "").strip()
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", parent_id):
        raise ValueError("请先在程序设置 → 整理任务结果填写网盘父目录链接。")
    service = service or load_drive_service()
    folder_name = date.fromisoformat(day).strftime("%m%d")
    date_folders = [item for item in _folder_children(service, parent_id)
                    if item.get("name") == folder_name
                    and item.get("mimeType") == GOOGLE_FOLDER_MIME]
    if len(date_folders) != 1:
        raise ValueError(
            f"网盘父目录下找到 {len(date_folders)} 个名为 {folder_name} 的日期文件夹；"
            "为避免统计错目录，本次没有修改记录。"
        )
    date_folder_id = str(date_folders[0]["id"])
    remote = []
    for child in _folder_children(service, date_folder_id):
        name = str(child.get("name") or "")
        mime = str(child.get("mimeType") or "")
        if mime == GOOGLE_FOLDER_MIME:
            slot = name if name in _PERIOD_LABELS else ""
            for file_item in _collect_remote_files(
                service, str(child.get("id") or ""), relative=(name,)
            ):
                remote.append((file_item, slot))
        else:
            item = dict(child)
            item["relative_parts"] = (name,)
            remote.append((item, ""))
    remote = [(item, slot) for item, slot in remote
              if str(item.get("mimeType") or "").startswith("video/")
              or Path(str(item.get("name") or "")).suffix.casefold() in VIDEO_SUFFIXES]
    if not remote:
        raise ValueError(f"{folder_name} 日期目录中没有视频；未覆盖已有数量记录。")
    remote_ids = {str(item.get("id") or "") for item, _ in remote}

    # The existing ODS remains a convenient classification hint, not the
    # authority on which files physically exist in a scanned date folder.
    records = all_video_upload_records(config) if records is None else records
    scanned_names = {normalize_video_identity(item.get("name")) for item, _ in remote}
    relevant_records = [item for item in records
                        if normalize_video_identity(item.get("file_name")) in scanned_names]
    known_groups, hint_warnings = collect_assignments(
        config, root, relevant_records
    )
    by_drive_id = {}
    by_name = defaultdict(set)
    prior_names = set()
    for (sheet, delivery_day, _slot, category, _creator), videos in known_groups.items():
        for video in videos:
            classification = (sheet, category)
            drive_id = str(video.get("drive_file_id") or "")
            if drive_id:
                by_drive_id.setdefault(drive_id, set()).add(classification)
            name_key = normalize_video_identity(video.get("file_name"))
            by_name[name_key].add(classification)
            if delivery_day < day:
                prior_names.add(name_key)

    state_path = Path(state_path or STATE_FILE)
    with _LOCK:
        state = _load_state(state_path)
        scope_key = _scope_key(config, root)
        scope = dict(state.get(scope_key, {}))
        videos = [dict(item) for item in scope.get("external_videos", [])]
        by_id = {str(item.get("drive_file_id")): item for item in videos
                 if item.get("drive_file_id")}
        seen_ids = set()
        duplicates = defaultdict(list)
        for item, _slot in remote:
            checksum = str(item.get("md5Checksum") or "")
            size = str(item.get("size") or "")
            if checksum and size:
                duplicates[(checksum, size)].append(str(item.get("id") or ""))
        duplicate_ids = {file_id for group in duplicates.values() if len(set(group)) > 1
                         for file_id in group}
        added = replaced = pending = review = 0
        for item, slot in remote:
            drive_id = str(item.get("id") or "").strip()
            if not drive_id or drive_id in seen_ids:
                continue
            seen_ids.add(drive_id)
            relative = "/".join(item.get("relative_parts") or (item.get("name") or drive_id,))
            is_revision = normalize_video_identity(item.get("name")) in prior_names
            is_review = any(_key(part) == _key(config.get("review_folder_name") or "review")
                            for part in relative.split("/"))
            entry = by_id.get(drive_id)
            if entry is None:
                entry = next((old for old in videos
                              if old.get("daily_scan_date") == day
                              and _key(old.get("relative_path")) == _key(relative)
                              and old.get("drive_file_id") not in remote_ids), None)
                if entry is not None:
                    replaced += 1
            if entry is None:
                entry = {
                    "id": uuid.uuid4().hex,
                    "folder_id": date_folder_id,
                    "first_seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "included": bool(slot and not is_review and not is_revision),
                    "sheet": "", "category": "",
                }
                videos.append(entry)
                added += 1
            elif (is_review or not slot) and entry.get("daily_scan_date") != day:
                entry["included"] = False
            if not entry.get("sheet") or not entry.get("category"):
                possible = by_drive_id.get(drive_id) or by_name.get(
                    normalize_video_identity(item.get("name")), set()
                )
                if len(possible) == 1:
                    entry["sheet"], entry["category"] = next(iter(possible))
            if not entry.get("sheet") or not entry.get("category"):
                pending += 1
            if is_review:
                review += 1
            entry.update({
                "drive_file_id": drive_id,
                "drive_link": f"https://drive.google.com/file/d/{drive_id}/view",
                "folder_name": folder_name,
                "file_name": str(item.get("name") or ""),
                "relative_path": relative,
                "batch_date": day,
                "batch_slot": slot,
                "daily_scan_date": day,
                "missing_from_daily": False,
                "outside_daily_scan": False,
                "possible_duplicate": drive_id in duplicate_ids,
                "possible_revision": is_revision,
                "review_path": is_review,
            })
            by_id[drive_id] = entry
        for entry in videos:
            if entry.get("daily_scan_date") == day and entry.get("drive_file_id") not in seen_ids:
                entry["missing_from_daily"] = True
            if entry.get("batch_date") == day and entry.get("daily_scan_date") != day:
                entry["outside_daily_scan"] = True
        scans = dict(scope.get("daily_scans", {}))
        scans[day] = {
            "folder_id": date_folder_id,
            "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "video_count": len(seen_ids),
        }
        scope["daily_scans"] = scans
        scope["external_videos"] = videos
        state[scope_key] = scope
        _save_state(state_path, state)
        return {
            "date": day, "folder_link": f"https://drive.google.com/drive/folders/{date_folder_id}",
            "found": len(seen_ids), "added": added, "replaced": replaced,
            "pending": pending,
            "review": review, "possible_duplicates": len(duplicate_ids),
            "possible_revisions": sum(1 for entry in videos
                                      if entry.get("daily_scan_date") == day
                                      and not entry.get("missing_from_daily")
                                      and entry.get("possible_revision")),
            "records": [dict(entry) for entry in videos if entry.get("batch_date") == day],
            "warnings": hint_warnings,
        }


def scan_external_video_folder(
    config, root, folder_link, batch_date, batch_slot, service=None, state_path=None
):
    """Snapshot successful videos in a Drive folder; never delete old deliveries."""
    from model.GoogleDriveHelper import (
        GOOGLE_FOLDER_MIME,
        extract_drive_folder_id,
        load_drive_service,
    )
    from model.MaterialDriveSync import _collect_remote_files, _execute_with_retry

    folder_id = extract_drive_folder_id(str(folder_link or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", folder_id):
        raise ValueError("请粘贴有效的 Google Drive 文件夹链接。")
    day = _date(batch_date)
    slot = str(batch_slot or "").zfill(2)
    if not day or slot not in _PERIOD_LABELS:
        raise ValueError("请选择有效的交付日期和时段。")
    service = service or load_drive_service()
    folder = _execute_with_retry(lambda: service.files().get(
        fileId=folder_id, fields="id,name,mimeType,trashed", supportsAllDrives=True
    ))
    if folder.get("mimeType") != GOOGLE_FOLDER_MIME or folder.get("trashed"):
        raise ValueError("链接指向的不是可用的 Google Drive 文件夹。")
    remote = [item for item in _collect_remote_files(service, folder_id)
              if str(item.get("mimeType") or "").startswith("video/")
              or Path(str(item.get("name") or "")).suffix.casefold() in VIDEO_SUFFIXES]
    remote_ids = {str(item.get("id") or "") for item in remote}
    state_path = Path(state_path or STATE_FILE)
    with _LOCK:
        state = _load_state(state_path)
        scope_key = _scope_key(config, root)
        scope = dict(state.get(scope_key, {}))
        videos = [dict(item) for item in scope.get("external_videos", [])]
        by_id = {(str(item.get("folder_id")), str(item.get("drive_file_id"))): item
                 for item in videos
                 if item.get("drive_file_id")}
        added = replaced = 0
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for item in remote:
            drive_id = str(item.get("id") or "").strip()
            if not drive_id:
                continue
            relative = "/".join(item.get("relative_parts") or (item.get("name") or drive_id,))
            entry = by_id.get((folder_id, drive_id))
            if entry is None:
                # A same-name replacement gets the old delivery identity only
                # when its previous Drive file is no longer in the folder.
                entry = next((old for old in videos
                              if old.get("folder_id") == folder_id
                              and _key(old.get("relative_path")) == _key(relative)
                              and old.get("drive_file_id") not in remote_ids), None)
                if entry is not None:
                    replaced += 1
                else:
                    entry = {
                        "id": uuid.uuid4().hex,
                        "folder_id": folder_id,
                        "batch_date": day,
                        "batch_slot": slot,
                        "sheet": "",
                        "category": "",
                        "included": True,
                        "first_seen_at": now,
                    }
                    videos.append(entry)
                    added += 1
            entry.update({
                "drive_file_id": drive_id,
                "drive_link": f"https://drive.google.com/file/d/{drive_id}/view",
                "folder_name": str(folder.get("name") or folder_id),
                "file_name": str(item.get("name") or ""),
                "relative_path": relative,
                "missing_from_folder": False,
            })
            by_id[(folder_id, drive_id)] = entry
        for entry in videos:
            if entry.get("folder_id") == folder_id and entry.get("drive_file_id") not in remote_ids:
                entry["missing_from_folder"] = True
        scope["external_videos"] = videos
        folders = [dict(item) for item in scope.get("external_folders", [])]
        saved_folder = next((item for item in folders if item.get("id") == folder_id), None)
        if saved_folder is None:
            saved_folder = {"id": folder_id}
            folders.append(saved_folder)
        saved_folder.update({
            "name": str(folder.get("name") or folder_id),
            "link": f"https://drive.google.com/drive/folders/{folder_id}",
            "last_scan_at": now,
        })
        scope["external_folders"] = folders
        state[scope_key] = scope
        _save_state(state_path, state)
        return {"found": len(remote), "added": added, "replaced": replaced,
                "records": [dict(item) for item in videos], "sources": folders}


def update_external_video_records(config, root, edits, state_path=None):
    """Apply explicit classification/date/include edits by stable local ID."""
    state_path = Path(state_path or STATE_FILE)
    with _LOCK:
        state = _load_state(state_path)
        scope_key = _scope_key(config, root)
        scope = dict(state.get(scope_key, {}))
        videos = [dict(item) for item in scope.get("external_videos", [])]
        pending = {str(item.get("id")): item for item in edits}
        known = {str(item.get("id")) for item in videos}
        if set(pending) - known:
            raise ValueError("补录记录已经变化，请重新打开后再保存。")
        for item in videos:
            edit = pending.get(str(item.get("id")))
            if edit is None:
                continue
            day = _date(edit.get("batch_date"))
            raw_slot = str(edit.get("batch_slot") or "").strip()
            slot = raw_slot.zfill(2) if raw_slot and raw_slot != "00" else ""
            included = bool(edit.get("included", True))
            if not day:
                raise ValueError(f"{item.get('file_name')}：交付日期无效，请填写 YYYY-MM-DD")
            if included and slot not in _PERIOD_LABELS:
                raise ValueError(
                    f"{item.get('file_name')}：已勾选计数，但时段为空或无效；"
                    "请填写 01/02/03，或取消计数。"
                )
            if item.get("daily_scan_date"):
                if day != item["daily_scan_date"] or (
                    item.get("batch_slot") in _PERIOD_LABELS
                    and slot != item["batch_slot"]
                ):
                    raise ValueError(
                        f"{item.get('file_name')}：日期和时段由网盘目录确定，"
                        "请修改分类或重新扫描正确的日期目录。"
                    )
            item.update({
                "batch_date": day,
                "batch_slot": slot,
                "sheet": str(edit.get("sheet") or "").strip(),
                "category": str(edit.get("category") or "").strip(),
                "included": included,
            })
        scope["external_videos"] = videos
        state[scope_key] = scope
        _save_state(state_path, state)
        return len(pending)


def _external_assignments(scope, creator, local_groups):
    groups = defaultdict(list)
    warnings = []
    counted_ids = {str(video.get("drive_file_id") or "") for videos in local_groups.values()
                   for video in videos}
    seen_ids = set()
    scanned_days = set(scope.get("daily_scans", {}))
    for item in scope.get("external_videos", []):
        if not item.get("included", True):
            continue
        file_id = str(item.get("drive_file_id") or "")
        if not file_id or file_id in counted_ids or file_id in seen_ids:
            continue
        label = str(item.get("file_name") or file_id)
        day = _date(item.get("batch_date"))
        if day in scanned_days and (
            item.get("daily_scan_date") != day or item.get("missing_from_daily")
        ):
            continue
        sheet = str(item.get("sheet") or "").strip()
        category = str(item.get("category") or "").strip()
        if not sheet or not category:
            warnings.append(f"流程外视频 {label}：统计分页/类别待填写")
            continue
        slot = str(item.get("batch_slot") or "").zfill(2)
        if not day or slot not in _PERIOD_LABELS:
            warnings.append(f"流程外视频 {label}：交付日期/时段无效")
            continue
        seen_ids.add(file_id)
        groups[(sheet, day, slot, category, creator)].append({
            "identity": str(item.get("id") or file_id),
            "file_name": label,
            "drive_file_id": file_id,
            "source": "external_folder",
        })
    return groups, warnings


def reconcile_daily_quantity(
    config, root, service=None, records=None, state_path=None, dry_run=False
):
    """Apply verified absolute counts; never guess over a nonempty manual cell."""
    scope = _scope_key(config, root)
    spreadsheet_id = scope.split("|", 1)[0]
    state_path = Path(state_path or STATE_FILE)
    with _LOCK:
        state = _load_state(state_path)
        previous = state.get(scope, {})
        old_cells = previous.get("cells", {}) if isinstance(previous, dict) else {}
        service = service or load_sheets_service(config, "task_submission_sheet")
        metadata = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties(rowCount,columnCount)))",
        ).execute()
        sizes = {
            item["properties"]["title"]: item["properties"].get("gridProperties", {})
            for item in metadata.get("sheets", [])
        }
        names = sorted(sizes)
        ranges = [sheet_range(name, "A1:{}{}".format(
            column_to_letter(sizes[name].get("columnCount", 1)),
            sizes[name].get("rowCount", 1),
        )) for name in names]
        response = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            valueRenderOption="FORMULA",
        ).execute() if ranges else {"valueRanges": []}
        snapshots = {
            name: item.get("values", [])
            for name, item in zip(names, response.get("valueRanges", []))
        }
        category_options = _category_options(
            snapshots, str(config.get("task_submission_creator") or "").strip()
        )
        allowed_dates = {
            parsed
            for rows in snapshots.values()
            for raw in (rows[0] if rows else [])
            if (parsed := _date(raw))
        }
        records = all_video_upload_records(config) if records is None else records
        groups, warnings = collect_assignments(
            config, root, records, allowed_dates=allowed_dates
        )
        scanned_days = set(previous.get("daily_scans", {}))
        scanned_ids = {
            str(item.get("drive_file_id") or "")
            for item in previous.get("external_videos", [])
            if item.get("daily_scan_date") in scanned_days
            and not item.get("missing_from_daily")
        }
        remaining_groups = {}
        for key, videos in groups.items():
            if key[1] in scanned_days:
                continue
            kept = [video for video in videos
                    if str(video.get("drive_file_id") or "") not in scanned_ids]
            if kept:
                remaining_groups[key] = kept
        groups = remaining_groups
        external_groups, external_warnings = _external_assignments(
            previous, str(config.get("task_submission_creator") or "").strip(), groups
        )
        for key, videos in external_groups.items():
            groups.setdefault(key, []).extend(videos)
        warnings.extend(external_warnings)
        desired = {
            json.dumps(key, ensure_ascii=False): videos for key, videos in groups.items()
        }
        relevant = set(desired) | set(old_cells)
        if not relevant:
            return {"updated": [], "warnings": warnings, "counted": 0,
                    "category_options": category_options}

        updates = []
        pending = []
        next_cells = dict(old_cells)
        for key in sorted(relevant):
            sheet, day, slot, category, creator = json.loads(key)
            if sheet not in snapshots:
                warnings.append(f"统计分页不存在：{sheet}")
                continue
            try:
                row, col = _find_cell(snapshots[sheet], day, slot, creator, category)
                raw = _value(snapshots[sheet], row, col)
                current = _number(raw)
            except (ValueError, TypeError) as exc:
                warnings.append(f"{sheet} / {day} / {category}：{exc}")
                continue
            prior = old_cells.get(key)
            desired_count = len(desired.get(key, []))
            if prior is None and current != 0 and current == desired_count:
                next_cells[key] = {"base": 0, "written": current,
                                   "videos": desired.get(key, [])}
                continue
            if prior is None and current != 0:
                warnings.append(f"{sheet} / {day} / {category}：已有手填数字 {current}，未覆盖；请先核对")
                continue
            if prior is not None and current != prior.get("written"):
                warnings.append(f"{sheet} / {day} / {category}：数字已被外部修改，未覆盖")
                continue
            base = int(prior.get("base", 0)) if prior else 0
            target = base + desired_count
            next_entry = {"base": base, "written": target,
                          "videos": desired.get(key, [])}
            if target != current:
                a1 = sheet_range(sheet, f"{column_to_letter(col + 1)}{row + 1}")
                updates.append({"range": a1, "values": [[target]]})
                pending.append((key, next_entry, a1, target))
            else:
                next_cells[key] = next_entry
        if updates and not dry_run:
            result = service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            changed = result.get("totalUpdatedCells")
            if changed is not None and changed != len(updates):
                raise RuntimeError(
                    f"Google 表格仅确认更新 {changed}/{len(updates)} 格；"
                    "请先核对线上数字，再重新刷新"
                )
        for key, entry, _, _ in pending:
            next_cells[key] = entry
        if not dry_run:
            state[scope] = {**previous,
                            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "cells": next_cells}
            _save_state(state_path, state)
        return {"updated": [{"range": a1, "count": number}
                            for _, _, a1, number in pending],
                "warnings": warnings,
                "counted": sum(len(v) for v in desired.values()),
                "category_options": category_options}

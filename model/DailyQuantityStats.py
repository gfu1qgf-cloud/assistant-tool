"""Reconcile uploaded video counts with an existing daily Google Sheet.

The upload ledger is the source of video/date identity.  Local task ODS files
are read afresh on every run so a missing or corrected category can be fixed
without uploading the video again.  Only the three period input cells are
owned here; quotas and SUM formulas are never written.
"""

import copy
import json
import logging
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from app_paths import APP_ROOT
from model.DailyQuantityCategories import CategoryStore
from model.GoogleSheetsHelper import (
    column_to_letter,
    extract_spreadsheet_id,
    load_sheets_service,
    sheet_range,
)
from model.OdsHelper import ReadTaskOds2
from model.ReviewSubmissionHistory import canonical_review_link, read_review_history
from model.TaskTableSchema import load_task_table_schema
from model.VideoUploadHistory import (
    VIDEO_SUFFIXES,
    all_video_upload_records,
    normalize_video_identity,
)
from model.DailyQuantityDedup import (
    deduplicate_video_groups, video_content_fields, video_content_key,
)


def _prepare_video_content(groups, records, cache, drive_service=None, verify=False):
    """Recover legacy fingerprints; verify unresolved items before live counting.

    History and versioned metadata cache avoid fetching the same hashes on every
    refresh. Only metadata is requested, never video content. Verification fails
    closed before any Sheets write rather than silently allowing an overcount.
    """
    historical = {}
    for record in sorted(records, key=lambda item: str(item.get("recorded_at") or "")):
        file_id = str(record.get("drive_file_id") or "")
        if file_id and video_content_key(record):
            historical[file_id] = record
    result = {}
    service = drive_service
    for key, videos in groups.items():
        prepared = []
        for original in videos:
            video = dict(original)
            file_id = str(video.get("drive_file_id") or "")
            version = str(video.get("duration_version") or "")
            cache_key = json.dumps([file_id, version])
            if not video_content_key(video):
                source = cache.get(cache_key) or historical.get(file_id) or {}
                if video_content_key(source):
                    video.update(video_content_fields(source))
            if verify and not video_content_key(video):
                label = str(video.get("file_name") or "未知视频")
                if not file_id:
                    raise ValueError(f"{label}：缺少网盘文件 ID，无法核实重复内容；本次未写入数量。")
                try:
                    if service is None:
                        from model.GoogleDriveHelper import load_drive_service
                        service = load_drive_service()
                    from model.MaterialDriveSync import _execute_with_retry
                    metadata = _execute_with_retry(lambda: service.files().get(
                        fileId=file_id, fields="md5Checksum,size,trashed", supportsAllDrives=True))
                    if metadata.get("trashed") or not video_content_key(metadata):
                        raise ValueError("文件不可用或缺少内容哈希")
                    video.update(video_content_fields(metadata))
                except (Exception, SystemExit) as exc:
                    # Do not print credential-bearing API URLs or response bodies.
                    raise RuntimeError(f"{label}：重复内容核验失败（{type(exc).__name__}）；"
                                       "为避免虚报，本次未写入数量。请检查网盘访问后重试。") from None
            if file_id and video_content_key(video):
                cache[cache_key] = video_content_fields(video)
            prepared.append(video)
        result[key] = prepared
    return result


STATE_FILE = APP_ROOT / "DailyQuantityStats.json"
_LOCK = threading.RLock()
_PERIOD_LABELS = {
    "01": ("12点", "12:00"),
    "02": ("18点", "18:00"),
    "03": ("24点", "24:00"),
}


def effective_batch_slot(value):
    """Missing period means the last period; malformed explicit values stay invalid."""
    raw = str(value or "").strip()
    return "03" if raw in {"", "00"} else raw.zfill(2)


def _key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _valid_video_duration(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = int(value)
        if isinstance(value, float) and value != duration:
            return None
        return duration if duration > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _duration_fields(record, cache=None):
    """Keep the video version beside its duration, independently of delivery date."""
    metadata = record.get("videoMediaMetadata") or {}
    duration = _valid_video_duration(record.get("duration_millis"))
    if duration is None and isinstance(metadata, dict):
        duration = _valid_video_duration(metadata.get("durationMillis"))
    version = str(record.get("duration_version") or "")
    modified = record.get("drive_modified_at") or record.get("modifiedTime")
    checksum = record.get("md5") or record.get("md5Checksum")
    if not version and (modified or checksum):
        version = json.dumps([modified, checksum, record.get("size")], ensure_ascii=False)
    fields = {
        **video_content_fields(record),
        "duration_millis": duration,
        "duration_drive_file_id": str(record.get("duration_drive_file_id")
                                      or record.get("drive_file_id") or record.get("id") or ""),
        "duration_version": version,
    }
    if duration is None and fields["duration_drive_file_id"] and version and isinstance(cache, dict):
        key = json.dumps([fields["duration_drive_file_id"], version])
        fields["duration_millis"] = _valid_video_duration(cache.get(key))
    return fields


def _adjust_fl_oral_counts(groups, category_options=None, duration_cache=None,
                           drive_service=None, read_remote=False):
    """Promote only FL short oral counts; never rewrite local ODS classifications."""
    from model.TaskSubmissionHelper import ORAL_SHORT_MAX_DURATION_MILLIS

    cache = duration_cache if isinstance(duration_cache, dict) else {}
    result, warnings, adjustments = defaultdict(list), [], []
    used_cache, run_cache = {}, {}
    remote_failed = False
    for key, videos in groups.items():
        sheet, day, slot, category, creator = key
        if _key(category) != "fl短口播":
            result[key].extend(videos)
            continue
        desired_label = re.sub(r"短\s*口\s*播", "长口播", category)
        labels = (category_options or {}).get(sheet, [])
        long_labels = [label for label in labels if _key(label) == "fl长口播"]
        long_label = long_labels[0] if len(long_labels) == 1 else desired_label
        for video in videos:
            source = _duration_fields(video)
            file_id = source["duration_drive_file_id"]
            version = source["duration_version"]
            cache_key = json.dumps([file_id, version]) if file_id and version else ""
            duration = source["duration_millis"]
            if duration is None and cache_key:
                duration = _valid_video_duration(cache.get(cache_key))
            if duration is None and file_id in run_cache:
                duration = run_cache[file_id]
            if duration is None and read_remote and file_id and file_id not in run_cache and not remote_failed:
                try:
                    from model.GoogleDriveHelper import load_drive_service
                    from model.MaterialDriveSync import _execute_with_retry
                    if drive_service is None:
                        drive_service = load_drive_service()
                    metadata = _execute_with_retry(lambda: drive_service.files().get(
                        fileId=file_id, fields="id,videoMediaMetadata(durationMillis)",
                        supportsAllDrives=True,
                    ))
                    duration = _duration_fields(metadata)["duration_millis"]
                    run_cache[file_id] = duration
                except Exception as error:
                    logging.getLogger("assistant_tool").exception("每日数量：网盘视频时长读取失败")
                    # Avoid retrying the same broken credentials/network for every video.
                    status = getattr(getattr(error, "resp", None), "status", None)
                    remote_failed = status not in {403, 404, 410}
                    run_cache[file_id] = None
            if duration is not None and cache_key:
                used_cache[cache_key] = duration
            target = category
            if duration is not None and duration > ORAL_SHORT_MAX_DURATION_MILLIS:
                if category_options is not None and len(long_labels) != 1:
                    warnings.append(f"{video.get('file_name') or file_id}：超过 60 秒，但统计分页 {sheet} "
                                    f"中的 FL 长口播匹配到 {len(long_labels)} 行；暂按原类别计数，请核对类别")
                else:
                    target = long_label
                    adjustments.append({"file_name": video.get("file_name", file_id),
                                        "date": day, "from": category, "to": target,
                                        "duration_millis": duration})
            elif duration is None and read_remote:
                warnings.append(f"{video.get('file_name') or file_id}：口播时长未能确认，"
                                f"暂按原类别 {category} 计数；请重新扫描网盘目录后刷新核对")
            result[(sheet, day, slot, target, creator)].append(video)
    cache.clear()
    cache.update(dict(list(used_cache.items())[-5000:]))
    return result, warnings, adjustments


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
    # Windows may report the same TEMP directory with an 8.3 short name in
    # upload history while ``root.resolve()`` expands it to the long name.
    local_file = Path(str(record.get("local_file") or "")).resolve()
    try:
        relative = local_file.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) < 3:
        return None
    return root / relative.parts[0] / table_name


def _review_upload(record, folder_name):
    review_name = _key(folder_name or "review")
    for field in ("remote_prefix", "relative_path"):
        parts = [part for part in re.split(r"[\\/]+", str(record.get(field) or "")) if part]
        if parts:
            return _key(parts[0]) == review_name
    return False


def _review_index(history=None):
    """Index the original and rework Drive IDs under the same review result."""
    items = (history if history is not None else read_review_history()).get("items", {})
    statuses = {}
    for key, item in items.items():
        if not str(key).startswith("google:"):
            continue
        status = str(item.get("status") or "pending")
        statuses[str(key).removeprefix("google:")] = status
        rework = canonical_review_link(item.get("rework_link"))
        if rework.startswith("google:"):
            statuses[rework.removeprefix("google:")] = status
    return statuses


def collect_assignments(config, root, records=None, allowed_dates=None,
                        include_unapproved_reviews=False):
    """Return one first-delivery assignment per logical finished video."""
    root = Path(root).resolve()
    table_name = str(config.get("task_table_file_name") or "任务登记表格.ods").strip()
    creator = str(config.get("task_submission_creator") or "").strip()
    warnings = []
    if not creator:
        raise ValueError("请先在程序设置 → 整理任务结果填写任务制作人")
    records = all_video_upload_records(config) if records is None else records
    review_history = read_review_history()
    review_statuses = _review_index(review_history)
    first, latest = {}, {}
    for record in records:
        if record.get("source") != "upload":
            continue
        ods = _project_ods(root, record, table_name)
        if ods is None:
            continue
        day = _date(record.get("batch_date"))
        slot = effective_batch_slot(record.get("batch_slot"))
        logical = str(record.get("logical_key") or "").strip()
        if not day or slot not in _PERIOD_LABELS or not logical:
            continue
        identity = f"{ods.parent.resolve()}|{logical}"
        order = (day, slot, str(record.get("recorded_at") or ""))
        if identity not in latest or order >= latest[identity][0]:
            latest[identity] = (order, record)
        if allowed_dates is not None and day not in allowed_dates:
            continue
        if identity not in first or order < first[identity][0]:
            first[identity] = (order, record, ods)

    tasks_by_ods = {}
    groups = defaultdict(list)
    first_ids = {str(record.get("drive_file_id") or "")
                 for _, record, _ in first.values()}
    rework_to_original = {}
    for key, item in review_history.get("items", {}).items():
        rework = canonical_review_link(item.get("rework_link"))
        if str(key).startswith("google:") and rework.startswith("google:"):
            rework_to_original[rework.removeprefix("google:")] = (
                str(key).removeprefix("google:")
            )
    for identity, ((day, slot, _), record, ods) in first.items():
        file_id = str(record.get("drive_file_id") or "")
        if rework_to_original.get(file_id) in first_ids:
            continue
        if (not include_unapproved_reviews
                and (_review_upload(record, config.get("review_folder_name"))
                     or file_id in review_statuses)
                and review_statuses.get(file_id) != "passed"):
            continue
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
            **_duration_fields(latest[identity][1]),
            **video_content_fields(record),
            "identity": identity,
            "file_name": label,
            "event_id": str(record.get("event_id") or ""),
            "drive_file_id": str(record.get("drive_file_id") or ""),
        })
    return groups, warnings


def _find_cell(rows, day, slot, creator, category, label_rows=None):
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

    labels = label_rows if label_rows is not None else rows
    matches = [row for row in _creator_category_rows(labels, creator)
               if _key(_value(labels, row, 2)) == _key(category)]
    if len(matches) != 1:
        raise ValueError(f"类别 {category} 在本人区块匹配到 {len(matches)} 行")
    row, col = matches[0], columns[0]
    if str(_value(rows, row, col)).startswith("="):
        raise ValueError("目标格有公式，不会覆盖")
    return row, col


def _creator_category_rows(rows, creator):
    """Match explicit owners first; A may repeat on every category row.

    Blank B is inherited only within the older blank-A continuation layout.
    Header/quota rows are excluded without skipping a person's first category.
    """
    wanted = _key(creator)
    if not wanted:
        return []
    active, result = False, []
    headers = {_key(text) for text in ("全时间", "尽本分时间", "类别", "统计类别",
                                       "合计", "总计", "一天总数", "定额")}
    for row in range(2, len(rows)):
        group = str(_value(rows, row, 0)).strip()
        owner = str(_value(rows, row, 1)).strip()
        category = str(_value(rows, row, 2)).strip()
        if owner:
            active = _key(owner) == wanted
        elif group:
            # An unlabeled new section must not inherit someone else's owner.
            active = False
        if not active or not category or _key(category) in headers:
            continue
        try:
            float(category)
        except ValueError:
            result.append(row)
    return result


def _category_options(snapshots, creator):
    """Read only category labels inside the configured creator's block."""
    result = {}
    for sheet, rows in snapshots.items():
        categories = sorted({str(_value(rows, row, 2)).strip()
                             for row in _creator_category_rows(rows, creator)
                             if str(_value(rows, row, 2)).strip()})
        if categories:
            result[sheet] = categories
    return result


def read_daily_quantity_categories(config, service=None):
    """Compatibility API: choices are local even if a Sheets service is passed."""
    return CategoryStore(config).load()[0]


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


def _sync_progress_summary(previous):
    """Describe committed local cell records, never scan dates or proposed writes.

    The latest recorded date is not a promise that earlier dates are complete.
    Keep unresolved-warning metadata beside it, without changing any counts.
    """
    previous = previous if isinstance(previous, dict) else {}
    days = defaultdict(int)
    cells = previous.get("cells", {})
    for key, entry in (cells.items() if isinstance(cells, dict) else ()):
        try:
            identity = json.loads(key)
            day = _date(identity[1]) if isinstance(identity, list) and len(identity) == 5 else ""
        except (ValueError, TypeError, IndexError):
            continue
        if (day and day <= date.today().isoformat() and isinstance(entry, dict)
                and "written" in entry):
            days[day] += 1
    dates = sorted(days)
    warnings = previous.get("last_refresh_warning_count")
    if not isinstance(warnings, int) or isinstance(warnings, bool) or warnings < 0:
        warnings = None
    return {"latest_date": dates[-1] if dates else "", "first_date": dates[0] if dates else "",
            "dates": [{"date": day, "cells": days[day]} for day in dates],
            "updated_at": str(previous.get("updated_at") or ""), "warning_count": warnings}


def daily_quantity_sync_progress(config, root, state_path=None):
    """Read the current spreadsheet/project's sync progress without network access."""
    scope = _scope_key(config, root)
    with _LOCK:
        return _sync_progress_summary(_load_state(state_path or STATE_FILE).get(scope, {}))


def _reject_future_date(day, today=None):
    if day > (today or date.today()).isoformat():
        raise ValueError(f"交付日期 {day} 在未来，请核对年份；未扫描或修改已有记录。")


def repair_future_scan_dates(config, root, corrections, state_path=None, dry_run=False):
    """Explicit, scoped repair. Never infer years or rewrite existing ownership cells."""
    today = date.today().isoformat()
    for old, new in corrections.items():
        if (_date(old) != old or _date(new) != new or old <= today or new > today
                or old[5:] != new[5:]):
            raise ValueError("只允许将已确认的未来扫描日期改为同月日的过去日期。")
    path = Path(state_path or STATE_FILE)
    with _LOCK:
        # A repair must fail closed on malformed state, not replace it with {}.
        state = json.loads(path.read_text(encoding="utf-8"))
        scope_key = _scope_key(config, root)
        original = state.get(scope_key)
        if not isinstance(original, dict):
            raise ValueError("没有找到当前项目和数量表的统计记录。")
        scope = copy.deepcopy(original)
        if any(json.loads(key)[1] in corrections for key in scope.get("cells", {})):
            raise ValueError("错误年份已有写入归属记录，需要先人工核对，未改动。")
        scans = scope.setdefault("daily_scans", {})
        merged = 0
        for old, new in corrections.items():
            if old not in scans:
                continue
            old_scan = scans[old]
            existing = scans.get(new)
            if existing and existing.get("folder_id") != old_scan.get("folder_id"):
                raise ValueError(f"{new} 的扫描文件夹不一致，未合并或覆盖。")
            if not existing or str(old_scan.get("scanned_at", "")) > str(existing.get("scanned_at", "")):
                scans[new] = old_scan
            del scans[old]
            merged += 1
        changed = 0
        for item in scope.get("external_videos", []):
            touched = False
            for field in ("batch_date", "daily_scan_date"):
                if item.get(field) in corrections:
                    item[field] = corrections[item[field]]
                    touched = True
            changed += int(touched)
        if not dry_run and (changed or merged):
            state[scope_key] = scope
            _save_state(path, state)
        return {"changed_records": changed, "merged_scan_dates": merged,
                "corrections": dict(corrections)}


def _included(item):
    """Honor explicit exclusions, but migrate the old review-folder default."""
    if item.get("included", True):
        return True
    if item.get("manual_included") or item.get("possible_revision"):
        return False
    # Older daily scans excluded files solely because no period folder existed.
    # Recover those defaults, but never override a user's explicit exclusion.
    return bool((item.get("review_path")
                 and effective_batch_slot(item.get("batch_slot")) in _PERIOD_LABELS)
                or (item.get("daily_scan_date")
                    and str(item.get("batch_slot") or "").strip() in {"", "00"}))


def _with_review_status(items):
    statuses = _review_index()
    for item in items:
        if item.get("review_path"):
            file_id = str(item.get("drive_file_id") or "")
            item["review_status"] = statuses.get(file_id, "untracked")
    return items


def external_video_records(config, root, state_path=None):
    """Return copies of folder-imported videos for the editor."""
    with _LOCK:
        scope = _load_state(state_path or STATE_FILE).get(_scope_key(config, root), {})
        return _with_review_status([dict(item, **_duration_fields(item, scope.get("duration_cache")),
                                        batch_slot=effective_batch_slot(item.get("batch_slot")),
                                        included=_included(item))
                                    for item in scope.get("external_videos", [])])


def external_video_sources(config, root, state_path=None):
    """Return saved folder links so a previously imported source is easy to rescan."""
    with _LOCK:
        scope = _load_state(state_path or STATE_FILE).get(_scope_key(config, root), {})
        return [dict(item) for item in scope.get("external_folders", [])]


def pending_review_quantity_records(config, root, state_path=None, records=None):
    """List review-linked uploads in this project still waiting to be counted."""
    root = Path(root).resolve()
    table_name = str(config.get("task_table_file_name") or "任务登记表格.ods").strip()
    statuses = _review_index()
    with _LOCK:
        scope = _load_state(state_path or STATE_FILE).get(_scope_key(config, root), {})
        external = [dict(item) for item in scope.get("external_videos", [])]
    review_names = {
        normalize_video_identity(item.get("file_name")): statuses.get(
            str(item.get("drive_file_id") or ""), "untracked"
        ) for item in external if item.get("review_path")
    }
    result = {}
    for item in external:
        file_id = str(item.get("drive_file_id") or "")
        name = str(item.get("file_name") or "")
        if not file_id or not (item.get("review_path") or file_id in statuses
                               or normalize_video_identity(name) in review_names):
            continue
        status = statuses.get(file_id, review_names.get(normalize_video_identity(name),
                                                        "untracked"))
        if status == "passed":
            continue
        result[file_id] = {
            "drive_file_id": file_id, "file_name": name,
            "drive_link": str(item.get("drive_link") or ""),
            "batch_date": str(item.get("batch_date") or ""),
            "batch_slot": effective_batch_slot(item.get("batch_slot")),
            "status": status,
        }
    uploads = all_video_upload_records(config) if records is None else records
    for record in uploads:
        if record.get("source") != "upload" or _project_ods(root, record, table_name) is None:
            continue
        file_id = str(record.get("drive_file_id") or "")
        if not file_id or not (_review_upload(record, config.get("review_folder_name"))
                               or file_id in statuses):
            continue
        status = statuses.get(file_id, "untracked")
        if status == "passed" or file_id in result:
            continue
        result[file_id] = {
            "drive_file_id": file_id,
            "file_name": str(record.get("file_name") or ""),
            "drive_link": str(record.get("drive_link") or ""),
            "batch_date": str(record.get("batch_date") or ""),
            "batch_slot": effective_batch_slot(record.get("batch_slot")),
            "status": status,
        }
    return sorted(result.values(), key=lambda item: (
        item["batch_date"], item["batch_slot"], item["file_name"]
    ), reverse=True)


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
    _reject_future_date(day)
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
        config, root, relevant_records, include_unapproved_reviews=True
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
        for item, detected_slot in remote:
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
            was_auto_excluded_review = False
            if entry is None:
                entry = {
                    "id": uuid.uuid4().hex,
                    "folder_id": date_folder_id,
                    "first_seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "included": not is_revision,
                    "sheet": "", "category": "",
                }
                videos.append(entry)
                added += 1
            else:
                was_auto_excluded_review = bool(
                    entry.get("review_path") and not entry.get("included", True)
                    and not entry.get("manual_included")
                )
            was_included = _included(entry)
            manual_slot = str(entry.get("manual_batch_slot") or "")
            slot = detected_slot or (manual_slot if manual_slot in _PERIOD_LABELS else "03")
            if detected_slot:
                entry.pop("manual_batch_slot", None)
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
                **_duration_fields(item),
                "drive_file_id": drive_id,
                "drive_link": f"https://drive.google.com/file/d/{drive_id}/view",
                "folder_name": folder_name,
                "file_name": str(item.get("name") or ""),
                "relative_path": relative,
                "batch_date": day,
                "batch_slot": slot,
                "detected_batch_slot": detected_slot,
                "daily_scan_date": day,
                "missing_from_daily": False,
                "outside_daily_scan": False,
                "possible_duplicate": drive_id in duplicate_ids,
                "possible_revision": is_revision,
                "review_path": is_review,
            })
            if (was_included and not is_revision) or (is_review and _included(entry)) or (
                was_auto_excluded_review and slot in _PERIOD_LABELS and not is_revision
            ):
                entry["included"] = True
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
            "records": _with_review_status([dict(entry, **_duration_fields(entry, scope.get("duration_cache"))) for entry in videos
                                            if entry.get("batch_date") == day]),
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
    slot = effective_batch_slot(batch_slot)
    if not day or slot not in _PERIOD_LABELS:
        raise ValueError("请选择有效的交付日期和时段。")
    _reject_future_date(day)
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
                **_duration_fields(item),
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
                "records": [dict(item, **_duration_fields(item, scope.get("duration_cache")))
                            for item in videos], "sources": folders}


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
            slot = effective_batch_slot(edit.get("batch_slot"))
            included = bool(edit.get("included", True))
            if not day:
                raise ValueError(f"{item.get('file_name')}：交付日期无效，请填写 YYYY-MM-DD")
            _reject_future_date(day)
            if included and slot not in _PERIOD_LABELS:
                raise ValueError(
                    f"{item.get('file_name')}：已勾选计数，但时段无效；"
                    "请填写 01/02/03；留空默认 03，或取消计数。"
                )
            if item.get("daily_scan_date"):
                detected_slot = item.get(
                    "detected_batch_slot",
                    "" if item.get("manual_batch_slot") else item.get("batch_slot"),
                )
                if day != item["daily_scan_date"] or (
                    detected_slot in _PERIOD_LABELS and slot != detected_slot
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
                "manual_included": True,
            })
            if item.get("daily_scan_date") and detected_slot not in _PERIOD_LABELS:
                if slot in _PERIOD_LABELS:
                    item["manual_batch_slot"] = slot
                else:
                    item.pop("manual_batch_slot", None)
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
    review_statuses = _review_index()
    review_names = {
        normalize_video_identity(item.get("file_name")): review_statuses.get(
            str(item.get("drive_file_id") or ""), "untracked"
        )
        for item in scope.get("external_videos", []) if item.get("review_path")
    }

    def review_passed(item):
        file_id = str(item.get("drive_file_id") or "")
        name = normalize_video_identity(item.get("file_name"))
        if item.get("review_path") or file_id in review_statuses or name in review_names:
            return review_statuses.get(file_id, review_names.get(name)) == "passed"
        return True

    def eligible(item):
        day = _date(item.get("batch_date"))
        return (bool(_included(item) and review_passed(item)
                     and item.get("sheet") and item.get("category"))
                and effective_batch_slot(item.get("batch_slot")) in _PERIOD_LABELS
                and not item.get("missing_from_daily")
                and not item.get("outside_daily_scan")
                and (day not in scanned_days or item.get("daily_scan_date") == day))

    eligible_review_ids = {str(item.get("drive_file_id") or "")
                           for item in scope.get("external_videos", [])
                           if item.get("review_path") and eligible(item)}
    rework_original_by_id = {}
    for key, item in read_review_history().get("items", {}).items():
        rework_key = canonical_review_link(item.get("rework_link"))
        if (str(key).startswith("google:") and rework_key.startswith("google:")
                and rework_key != key):
            rework_original_by_id[rework_key.removeprefix("google:")] = (
                str(key).removeprefix("google:")
            )
    active_review_names = {
        normalize_video_identity(item.get("file_name"))
        for item in scope.get("external_videos", [])
        if item.get("review_path") and eligible(item)
    }
    for item in scope.get("external_videos", []):
        if not _included(item):
            continue
        if not review_passed(item):
            continue
        if item.get("missing_from_daily") or item.get("outside_daily_scan"):
            continue
        file_id = str(item.get("drive_file_id") or "")
        if (not item.get("review_path")
                and (normalize_video_identity(item.get("file_name")) in active_review_names
                     or rework_original_by_id.get(file_id) in eligible_review_ids)):
            continue
        if not file_id or file_id in counted_ids or file_id in seen_ids:
            continue
        label = str(item.get("file_name") or file_id)
        day = _date(item.get("batch_date"))
        if day and day > date.today().isoformat():
            warnings.append(f"流程外视频 {label}：交付日期 {day} 在未来，请核对年份；未计数")
            continue
        if day in scanned_days and (
            item.get("daily_scan_date") != day or item.get("missing_from_daily")
        ):
            continue
        sheet = str(item.get("sheet") or "").strip()
        category = str(item.get("category") or "").strip()
        if not sheet or not category:
            warnings.append(f"流程外视频 {label}：统计分页/类别待填写")
            continue
        slot = effective_batch_slot(item.get("batch_slot"))
        if not day or slot not in _PERIOD_LABELS:
            warnings.append(f"流程外视频 {label}：交付日期/时段无效")
            continue
        seen_ids.add(file_id)
        groups[(sheet, day, slot, category, creator)].append({
            **_duration_fields(item),
            "identity": str(item.get("id") or file_id),
            "file_name": label,
            "drive_file_id": file_id,
            "source": "external_folder",
        })
    return groups, warnings


def _daily_count_rows(groups):
    """Summarize classified videos independently of whether a cell needs rewriting."""
    rows = {}
    for (sheet, day, slot, category, _creator), videos in groups.items():
        row = rows.setdefault((day, sheet, category), {
            "date": day, "sheet": sheet, "category": category,
            "01": 0, "02": 0, "03": 0, "total": 0,
        })
        count = len(videos)
        row[slot] += count
        row["total"] += count
    return [rows[key] for key in sorted(rows)]


def preview_external_day(records, day):
    """Show a local inventory preview without claiming Google Sheet sync succeeded."""
    day = _date(day)
    items = [item for item in records if _date(item.get("batch_date")) == day]
    missing_slot = missing_category = 0
    for item in items:
        slot = effective_batch_slot(item.get("batch_slot"))
        sheet = str(item.get("sheet") or "").strip()
        category = str(item.get("category") or "").strip()
        if slot not in _PERIOD_LABELS:
            missing_slot += 1
        if not sheet or not category:
            missing_category += 1
    groups, _warnings = _external_assignments({"external_videos": records}, "", {})
    groups, excluded = deduplicate_video_groups(groups)
    groups = {key: videos for key, videos in groups.items() if key[1] == day}
    groups, _, _ = _adjust_fl_oral_counts(groups)
    daily_counts = _daily_count_rows(groups)
    counted = sum(row["total"] for row in daily_counts)
    return {"date": day, "total_files": len(items), "counted": counted,
            "not_counted": len(items) - counted,
            "missing_slot": missing_slot, "missing_category": missing_category,
            "daily_counts": daily_counts,
            "duplicate_exclusions": [item for item in excluded if item["date"] == day]}


def reconcile_daily_quantity(
    config, root, service=None, records=None, state_path=None, dry_run=False,
    drive_service=None, only_sheet=None, only_dates=None, verify_content=False,
):
    """Apply verified absolute counts; never guess over a nonempty manual cell."""
    scope = _scope_key(config, root)
    spreadsheet_id = scope.split("|", 1)[0]
    state_path = Path(state_path or STATE_FILE)
    selected_dates = None if only_dates is None else set(only_dates)
    if selected_dates is not None and any(_date(day) != day for day in selected_dates):
        raise ValueError("定向补填日期必须为 YYYY-MM-DD。")
    def selected(key):
        return (only_sheet is None or key[0] == only_sheet) and (
            selected_dates is None or key[1] in selected_dates)
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
        if only_sheet is not None and only_sheet not in sizes:
            raise ValueError("指定的统计分页不存在；未填写。")
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
        # Names in column B may be formulas.  Match against their displayed
        # values, while retaining FORMULA snapshots for safe numeric writes.
        label_ranges = [sheet_range(name, "A1:C{}".format(
            sizes[name].get("rowCount", 1))) for name in names]
        label_response = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=label_ranges,
            valueRenderOption="FORMATTED_VALUE",
        ).execute() if label_ranges else {"valueRanges": []}
        label_snapshots = {
            name: item.get("values", [])
            for name, item in zip(names, label_response.get("valueRanges", []))
        }
        category_options = _category_options(
            label_snapshots, str(config.get("task_submission_creator") or "").strip()
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
        content_cache = dict(previous.get("content_cache", {}))
        groups = _prepare_video_content(groups, records, content_cache,
                                        drive_service, verify=verify_content)
        groups, duplicate_exclusions = deduplicate_video_groups(groups)
        groups = {key: videos for key, videos in groups.items() if selected(key)}
        duplicate_exclusions = [item for item in duplicate_exclusions if selected((
            item["sheet"], item["date"], item["slot"], item["category"], ""))]
        duration_cache = dict(previous.get("duration_cache", {}))
        groups, duration_warnings, adjustments = _adjust_fl_oral_counts(
            groups, category_options, duration_cache, drive_service, read_remote=True
        )
        warnings.extend(duration_warnings)
        daily_counts = _daily_count_rows(groups)
        counted = sum(row["total"] for row in daily_counts)
        desired = {
            json.dumps(key, ensure_ascii=False): videos for key, videos in groups.items()
        }
        relevant = {key for key in set(desired) | set(old_cells) if selected(json.loads(key))}
        if not relevant:
            next_state = {**previous, "last_refresh_warning_count": len(warnings),
                          "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            if not dry_run:
                state[scope] = next_state
                _save_state(state_path, state)
            return {"updated": [], "warnings": warnings, "counted": 0,
                    "duplicate_exclusions": duplicate_exclusions,
                    "automatic_classifications": adjustments,
                    "category_options": category_options, "daily_counts": daily_counts,
                    "sync_progress": _sync_progress_summary(previous if dry_run else next_state)}

        updates = []
        pending = []
        next_cells = dict(old_cells)
        for key in sorted(relevant):
            sheet, day, slot, category, creator = json.loads(key)
            if sheet not in snapshots:
                warnings.append(f"统计分页不存在：{sheet}")
                continue
            try:
                row, col = _find_cell(snapshots[sheet], day, slot, creator,
                                      category, label_snapshots.get(sheet))
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
                            "content_cache": content_cache,
                            "duration_cache": duration_cache,
                            "last_refresh_warning_count": len(warnings),
                            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "cells": next_cells}
            _save_state(state_path, state)
        return {"updated": [{"range": a1, "count": number}
                            for _, _, a1, number in pending],
                "warnings": warnings,
                "duplicate_exclusions": duplicate_exclusions,
                "automatic_classifications": adjustments,
                "counted": counted,
                "daily_counts": daily_counts,
                "category_options": category_options,
                "sync_progress": _sync_progress_summary(previous if dry_run else state[scope])}

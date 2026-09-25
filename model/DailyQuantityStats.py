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
from model.VideoUploadHistory import all_video_upload_records


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


def reconcile_daily_quantity(
    config, root, service=None, records=None, state_path=None, dry_run=False
):
    """Apply verified absolute counts; never guess over a nonempty manual cell."""
    url = str(config.get("daily_quantity_sheet_url") or "").strip()
    if not url:
        raise ValueError("请先在程序设置 → 整理任务结果填写每日数量表格链接")
    spreadsheet_id = extract_spreadsheet_id(url)
    state_path = Path(state_path or STATE_FILE)
    with _LOCK:
        state = _load_state(state_path)
        scope = f"{spreadsheet_id}|{Path(root).resolve()}"
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
        allowed_dates = {
            parsed
            for rows in snapshots.values()
            for raw in (rows[0] if rows else [])
            if (parsed := _date(raw))
        }
        groups, warnings = collect_assignments(
            config, root, records, allowed_dates=allowed_dates
        )
        desired = {
            json.dumps(key, ensure_ascii=False): videos for key, videos in groups.items()
        }
        relevant = set(desired) | set(old_cells)
        if not relevant:
            return {"updated": [], "warnings": warnings, "counted": 0}

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
            state[scope] = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "cells": next_cells}
            _save_state(state_path, state)
        return {"updated": [{"range": a1, "count": number}
                            for _, _, a1, number in pending],
                "warnings": warnings,
                "counted": sum(len(v) for v in desired.values())}

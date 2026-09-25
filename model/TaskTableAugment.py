"""Add optional daily-statistics headers to a newly copied ODS task table."""

import os
import uuid
from pathlib import Path

from odf import teletype
from odf.opendocument import load
from odf.table import Table, TableCell, TableRow
from odf.text import P

from model.OdsHelper import _find_header, _select_sheet


DAILY_STAT_HEADERS = ("每日统计分页", "每日统计类别")


def _header_cells(row):
    column = 0
    for cell in row.childNodes:
        if cell.qname[1] not in {"table-cell", "covered-table-cell"}:
            continue
        repeat = max(1, int(cell.getAttribute("numbercolumnsrepeated") or 1))
        yield column, repeat, cell, teletype.extractText(cell).strip()
        column += repeat


def _replace_blank_at(row, column, text):
    for start, repeat, cell, value in _header_cells(row):
        if not start <= column < start + repeat:
            continue
        if value or cell.qname[1] != "table-cell":
            raise ValueError("统计表头目标列已有内容，停止修改")
        before = column - start
        after = repeat - before - 1
        style = cell.getAttribute("stylename")
        if repeat == 1:
            cell.addElement(P(text=text))
            return
        new_cell = TableCell(stylename=style) if style else TableCell()
        new_cell.addElement(P(text=text))
        if before:
            cell.setAttribute("numbercolumnsrepeated", before)
            row.insertBefore(new_cell, cell.nextSibling)
        else:
            row.insertBefore(new_cell, cell)
            cell.setAttribute("numbercolumnsrepeated", after)
        if before and after:
            tail = TableCell(stylename=style) if style else TableCell()
            tail.setAttribute("numbercolumnsrepeated", after)
            row.insertBefore(tail, new_cell.nextSibling)
        return
    raise ValueError("任务表格没有可追加统计表头的空列")


def add_daily_stat_headers_to_new_copy(path, schema=None):
    """Modify a newly copied ODS only; leave the source template untouched."""
    target = Path(path)
    doc = load(str(target))
    if schema is None:
        sheets = doc.spreadsheet.getElementsByType(Table)
        if not sheets:
            raise ValueError("任务表格没有工作表")
        sheet = sheets[0]
    else:
        sheet, _name = _select_sheet(doc, schema)
    rows = sheet.getElementsByType(TableRow)
    if not rows:
        raise ValueError("任务表格没有表头行")
    header = rows[_find_header(rows, schema)[0]] if schema is not None else rows[0]
    present = {value for _start, _repeat, _cell, value in _header_cells(header) if value}
    missing = [name for name in DAILY_STAT_HEADERS if name not in present]
    if not missing:
        return False
    last_used = max(
        (start + repeat for start, repeat, _cell, value in _header_cells(header) if value),
        default=0,
    )
    for offset, name in enumerate(missing):
        _replace_blank_at(header, last_used + offset, name)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.ods")
    try:
        doc.save(str(temporary), addsuffix=False)
        verified = load(str(temporary))
        verified_sheet = _select_sheet(verified, schema)[0] if schema is not None else verified.spreadsheet.getElementsByType(Table)[0]
        verified_rows = verified_sheet.getElementsByType(TableRow)
        verified_headers = {
            value for _start, _repeat, _cell, value in _header_cells(
                verified_rows[_find_header(verified_rows, schema)[0]] if schema is not None else verified_rows[0]
            ) if value
        }
        if not set(DAILY_STAT_HEADERS).issubset(verified_headers):
            raise ValueError("保存后未能验证统计表头")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True

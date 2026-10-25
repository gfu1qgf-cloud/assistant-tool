"""Two-node editor model; compiles to the existing task-table schema only."""
import copy
from model.TaskTableSchema import (
    FIELD_ORDER, SUBMISSION_FIELD_ORDER, normalize_task_table_schema,
)

# Generic vocabulary only: no customer sheet names, aliases or private labels.
DISPLAY_NAMES = {
    'task_id': '任务编号', 'admin': '负责人', 'creator': '制作人', 'task_name': '任务名称',
    'task_type': '任务类型', 'submission_task_type': '提交类型', 'task_date': '任务日期',
    'task_reference_link': '参考链接', 'task_audio_text': '配音文案', 'task_audio_type': '配音类型',
    'review_required': '审核要求', 'subcategory': '子类别', 'daily_stat_sheet': '每日统计分页',
    'daily_stat_category': '每日统计类别', 'task_audio_voice_id': '语音ID',
    'task_requirements': '任务需求', 'task_notes': '备注', 'task_video_prompt': '视频生成提示词',
    'task_resolve_requirement': '达芬奇需求', 'requester': '需求人', 'chinese': '中文文案',
    'completed_at': '完成日期', 'review_status': '审核状态', 'product_link': '成品链接', 'video_type': '视频类型',
}


def display_name(section, key):
    label = str(section['labels'].get(key, key))
    return DISPLAY_NAMES.get(key, key) if label == key else label


def saved_display_names(schema):
    """On explicit save, publish generic UI translations to existing labels."""
    result = normalize_task_table_schema(schema)
    for section, keys in ((result, FIELD_ORDER), (result['submission_sheet'], SUBMISSION_FIELD_ORDER)):
        for key in keys:
            section['labels'][key] = display_name(section, key)
    return result


class SchemaMapping:
    def __init__(self, schema, submission=False):
        self.schema = normalize_task_table_schema(schema)
        self.submission = bool(submission)
        self.fields = SUBMISSION_FIELD_ORDER if submission else FIELD_ORDER
        self.sources = list(dict.fromkeys(alias for field in self.fields
                           for alias in self.section['fields'][field]['aliases']))

    @property
    def section(self):
        return self.schema['submission_sheet'] if self.submission else self.schema

    def aliases(self, field):
        if field not in self.fields:
            raise ValueError('未知程序字段。')
        return list(self.section['fields'][field]['aliases'])

    def add_source(self, name):
        name = str(name).strip()
        if not name or any(c in name for c in '\r\n') or len(name) > 200:
            raise ValueError('列名须为1–200个字符，不能含换行。')
        if name not in self.sources:
            self.sources.append(name)
        return name

    def rename_source(self, old, new):
        if old not in self.sources:
            raise ValueError('列名不存在。')
        new = str(new).strip()
        if new != old and new in self.sources:
            raise ValueError('新列名已经存在，请使用现有端口连线。')
        self.add_source(new)
        if new == old:
            return
        self.sources.remove(new)
        self.sources[self.sources.index(old)] = new
        for field in self.fields:
            self.section['fields'][field]['aliases'] = [new if value == old else value
                                                       for value in self.aliases(field)]

    def remove_source(self, name):
        if name not in self.sources:
            raise ValueError('列名不存在。')
        self.sources.remove(name)
        for field in self.fields:
            self.section['fields'][field]['aliases'] = [v for v in self.aliases(field) if v != name]

    def set_aliases(self, field, aliases):
        self.aliases(field)  # Validate before changing anything.
        values = list(aliases)
        if any(value not in self.sources for value in values) or len(set(values)) != len(values):
            raise ValueError('连线包含不存在或重复的列名。')
        self.section['fields'][field]['aliases'] = values

    def connect(self, name, field):
        values = self.aliases(field)
        if name not in self.sources:
            raise ValueError('列名不存在。')
        if name in values:
            raise ValueError('此列已经连接到该字段。')
        self.set_aliases(field, values + [name])

    def compile(self):
        # Preserve default templates, labels, scan options and the other mapping.
        return normalize_task_table_schema(copy.deepcopy(self.schema))


def read_columns(payload, run):
    """Read headers only, not task rows; keep parsing outside the GUI thread."""
    from pathlib import Path
    from zipfile import ZipFile
    from model.OdsHelper import load, TableRow, _select_sheet, _find_header
    path = Path(payload['path'])
    run.check_canceled()
    stat = path.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    if stamp[0] > 32 * 1024 * 1024:
        raise ValueError('表格超过32MiB，请使用较小的任务表。')
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError('表格解压内容超过128MiB，未继续解析。')
    run.report('读取表头', 10)
    schema = normalize_task_table_schema(payload['schema'])
    document = load(str(path))
    run.check_canceled()
    sheet, sheet_name = _select_sheet(document, schema)
    row, headers, _ = _find_header(sheet.getElementsByType(TableRow), schema)
    current = path.stat()
    if (current.st_size, current.st_mtime_ns) != stamp:
        raise ValueError('读取期间表格发生变化，请重新读取。')
    run.check_canceled()
    return {'headers': [name for name in headers if name], 'sheet_name': sheet_name, 'header_row': row + 1}

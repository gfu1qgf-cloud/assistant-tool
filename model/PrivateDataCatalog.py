"""Explicit private-data inventory. Code/models/tasks/material media stay put."""
from dataclasses import dataclass
import json
import os
import stat as stat_module
from pathlib import Path

PRIVATE_DIRECTORIES = (
    'PluginData', 'AIModelClient', 'BatchTextVideo', 'CookingAssistant', 'config',
    'NodeWorkflows', 'SmartImageSearch', 'SmartMusicSearch', 'SmartVideoSearch',
    'SmartImageAssignment', 'MaterialOrganizer', 'VideoPromptAssistant',
    'VideoUploadHistoryArchives', 'logs', '人脸数据库',
)
# These settings point at private records, not user-selected media/tasks.
PRIVATE_KEYS = frozenset({
    'inventory_state_file', 'daily_tasks_file', 'daily_quantity_stats_file',
    'daily_quantity_categories_file', 'delivery_todo_file', 'task_result_pending_file',
    'video_upload_history_file', 'video_upload_archive_dir', 'task_submission_audit_file',
    'review_submission_history_file', 'task_reference_cache_file', 'material_sync_state_file',
    'google_sheet_monitor_state_file', 'google_sheets_credentials_file', 'google_sheets_token_file',
    'google_drive_credentials_file', 'google_drive_token_file', 'video_prompt_cases_file',
    'review_sheet_credentials_file', 'review_sheet_token_file',
    'task_submission_sheet_credentials_file', 'task_submission_sheet_token_file',
    'sheet_monitor_credentials_file', 'sheet_monitor_token_file', 'task_submission_log_file',
})


def no_reparse(path, boundary):
    """Reject all symlinks/junctions, including ancestors, not just final file."""
    if '..' in Path(path).parts or '..' in Path(boundary).parts:
        raise ValueError('私有数据路径不能包含上级目录跳转。')
    path, boundary = Path(os.path.abspath(path)), Path(os.path.abspath(boundary))
    try: path.relative_to(boundary)
    except ValueError: raise ValueError('私有数据路径超出已批准边界。') from None
    for item in (path, *path.parents):
        try: stat = item.lstat()
        except FileNotFoundError: stat = None
        if stat and (stat_module.S_ISLNK(stat.st_mode) or getattr(stat, 'st_file_attributes', 0) & 0x400):
            raise ValueError('私有数据目录不能使用 Windows 重解析点。')
        if item == boundary: break
    # abspath removes lexical dot segments; every component through the
    # boundary was lstat'ed and rejects every reparse type. realpath/resolve
    # would repeat filesystem walks without adding a non-racy guarantee.
    return path


@dataclass(frozen=True)
class DataLocation:
    source: str
    relative: str


def catalog(program_root):
    root = Path(program_root).absolute()
    records = []
    for path in root.iterdir():
        name = path.name
        if path.is_file() and (name.endswith(('.json', '.jsonl', '.sqlite3', '.db'))
                or '.json.bak' in name) and name != 'config.example.json':
            records.append(DataLocation(str(no_reparse(path, root)), name))
        elif name in PRIVATE_DIRECTORIES:
            records.append(DataLocation(str(no_reparse(path, root)), name))
    legacy = root / 'model/task_result_organizer/defaults.json'
    if legacy.exists():
        records.append(DataLocation(str(no_reparse(legacy, root)), 'LegacyData/task_result_defaults.json'))
    config_path = root / 'config.json'
    if config_path.exists():
        no_reparse(config_path, root)
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict): raise ValueError('原配置损坏，不能迁移或创建空配置。')
        import hashlib
        for key in PRIVATE_KEYS:
            raw = config.get(key)
            if not isinstance(raw, str) or not raw.strip(): continue
            path = Path(raw).expanduser()
            if not path.is_absolute(): path = root / path
            if any(path == Path(record.source) or Path(record.source) in path.parents for record in records): continue
            # Register configured locations, even missing ones, so future writes
            # cannot fall back to a former plaintext custom directory.
            no_reparse(path, Path(path.anchor))
            relative = ('ExternalData/' + hashlib.sha256(str(path).casefold().encode()).hexdigest()[:20]
                        + '/' + path.name)
            records.append(DataLocation(str(path.absolute()), relative))
    # Inventory avatars are application-created copies, unlike the large
    # original material library. Register only these individual private files.
    inventory = next((Path(item.source) for item in records if item.relative=='InventoryState.json'), None)
    if inventory and inventory.exists():
        import hashlib
        state=json.loads(inventory.read_text(encoding='utf-8-sig'))
        for person in state.get('people',[]):
            raw=person.get('avatar_path')
            if not raw:continue
            path=Path(raw).absolute()
            if not path.is_file():continue
            if any(path==Path(item.source) or Path(item.source) in path.parents for item in records):continue
            no_reparse(path,Path(path.anchor))
            person_id=hashlib.sha256(str(person['id']).encode()).hexdigest()[:20]
            records.append(DataLocation(str(path),'PluginData/inventory/avatars/'+person_id+'/'+path.name))
    # Private Codex profile archive is distinct from the external .codex client.
    codex = Path(os.environ.get('CODEX_DESKTOP_SWITCHER_DATA_DIR') or Path.home()/'.codex-account-switcher')
    if codex.exists():
        no_reparse(codex, Path(codex.anchor))
        records.append(DataLocation(str(codex.absolute()), 'CodexAccountProfiles'))
    return records


def enumerate_files(locations):
    """Stable bounded inventory; do not follow junctions or SQLite sidecars."""
    files, size = [], 0
    for location in locations:
        source = Path(location.source)
        if not source.exists(): continue
        candidates = [source] if source.is_file() else source.rglob('*')
        for path in candidates:
            no_reparse(path, source if source.is_dir() else source.parent)
            if not path.is_file() or path.name.endswith(('-wal', '-shm', '-journal')): continue
            relative = Path(location.relative) / path.relative_to(source) if source.is_dir() else Path(location.relative)
            size += path.stat().st_size
            files.append((path, relative))
            if len(files) > 250000: raise ValueError('私有文件超过迁移上限，请先检查数据目录，未移动原文件。')
    return files, size

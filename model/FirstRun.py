"""First-use decisions and exclusive config creation, before private services."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

DEFAULT_DISABLED_PLUGINS = (
    'codex_account_switcher', 'antigravity_account_switcher', 'waste_reminder',
    'ai_model_client', 'document_formatter', 'video_prompt_assistant',
)


@dataclass(frozen=True)
class FirstUseDecision:
    mode: str = 'existing'
    initial_config: object = None
    created: bool = False


def first_use_state(program_root):
    root = Path(program_root)
    # Never inspect the former plaintext config/data when VC is already bound.
    if (root / 'storage_bootstrap.json').exists():
        return 'bound'
    if (root / 'config.json').exists():
        return 'existing'
    from model.PrivateDataCatalog import PRIVATE_DIRECTORIES
    # Detect old data by names only. Do not parse credentials/inventory simply
    # to decide whether the user needs recovery (corrupt data needs it too).
    for entry in root.iterdir():
        name = entry.name
        if name.casefold() in {'build_info.json', 'config.example.json'}:
            continue
        if name in PRIVATE_DIRECTORIES or name.endswith(('.json', '.jsonl', '.sqlite3', '.db')) or '.json.bak' in name:
            return 'recovery'
    if (root / 'model/task_result_organizer/defaults.json').exists():
        return 'recovery'
    return 'fresh'


def read_config_object(path):
    file = Path(path)
    if file.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('配置文件超过8MiB，请确认选择的是 config.json。')
    value = json.loads(file.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('配置文件最外层必须是对象，未创建或覆盖任何配置。')
    return value


def initial_settings(program_root, *, task_path='', offline=False, model='base'):
    root = Path(program_root)
    template = root / 'config.example.json'
    if not template.is_file():
        raise ValueError('缺少 config.example.json。请恢复完整程序包后重试，不要只复制 EXE。')
    value = read_config_object(template)
    task_path = str(task_path).strip()
    if task_path and not Path(task_path).is_dir():
        raise ValueError('任务目录不存在，请选择已有文件夹，或暂时留空。')
    if model not in {'base', 'small', 'medium', 'large-v3'}:
        raise ValueError('所选字幕模型无效。')
    value['task_path'] = task_path
    value['offline_mode'] = bool(offline)
    enabled = value.get('plugin_enabled', {})
    if not isinstance(enabled, dict):
        enabled = {}
    value['plugin_enabled'] = dict(enabled)
    for plugin_id in DEFAULT_DISABLED_PLUGINS:
        value['plugin_enabled'].setdefault(plugin_id, False)
    settings = value.get('smart_video_editor', {})
    if not isinstance(settings, dict):
        settings = {}
    value['smart_video_editor'] = dict(settings, whisper_model_size=model)
    return value


def create_normal_config(program_root, config, *, restoring=False):
    root = Path(program_root).resolve()
    state = first_use_state(root)
    if state in {'bound', 'existing'}:
        raise ValueError('已经存在配置或密盘绑定，未覆盖；请重新启动程序。')
    if state == 'recovery' and not restoring:
        raise ValueError('发现已有私有数据但主配置缺失，请先恢复备份，不能创建空配置顶替。')
    if not isinstance(config, dict):
        raise ValueError('配置必须是对象。')
    contents = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    target = root / 'config.json'
    descriptor, temporary = tempfile.mkstemp(prefix='.first-use-', suffix='.tmp', dir=root)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if (root / 'storage_bootstrap.json').exists():
            raise ValueError('填写期间出现密盘绑定，未创建普通配置，请重新启动。')
        if os.name == 'nt':
            # Windows rename fails if the destination already exists.
            os.rename(temporary, target)
        else:
            # POSIX rename overwrites; publish with exclusive link semantics.
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target

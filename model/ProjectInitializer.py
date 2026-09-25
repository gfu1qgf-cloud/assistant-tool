"""Safe creation of a task project directory from a workbook template."""

import os
import shutil
import uuid
from pathlib import Path

from model.TaskTableAugment import add_daily_stat_headers_to_new_copy


def initialize_project_directory(
    project_dir, table_file_name, template_candidates, *,
    add_daily_stat_headers=False, task_schema=None,
):
    project_dir = Path(project_dir)
    table_name = str(table_file_name or "").strip()
    if not table_name or Path(table_name).name != table_name:
        raise ValueError("任务表格文件名无效。")

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "result").mkdir(parents=True, exist_ok=True)
    destination = project_dir / table_name
    if destination.exists():
        return {
            "project_dir": project_dir,
            "table_path": destination,
            "copied": False,
            "template_path": None,
        }

    template = next(
        (Path(candidate) for candidate in template_candidates if Path(candidate).is_file()),
        None,
    )
    if template is None:
        return {
            "project_dir": project_dir,
            "table_path": destination,
            "copied": False,
            "template_path": None,
            "missing_template": True,
        }

    temporary = destination.with_name(
        f"{destination.name}.{uuid.uuid4().hex}.copying"
    )
    try:
        shutil.copy2(str(template), str(temporary))
        if add_daily_stat_headers:
            add_daily_stat_headers_to_new_copy(temporary, schema=task_schema)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return {
        "project_dir": project_dir,
        "table_path": destination,
        "copied": True,
        "template_path": template,
    }


from model.AppDataStorage import data_directory
import re
from datetime import date
from pathlib import Path
from string import Formatter
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from model.TaskResultFileGuard import (
    FileStillWritingError, copy_if_ready_and_updated, is_copy_temporary,
)

try:
    from model.OdsHelper import ReadTaskOds
except ImportError as exc:
    raise SystemExit(
        "整理模块依赖缺失，通常需要安装 odfpy：\n"
        "pip install odfpy\n"
        f"当前缺少：{getattr(exc, 'name', exc)}"
    )


class ExportResult(NamedTuple):
    output_dir: Path
    updated_files: List[Path]


class _TemplateValues(dict):
    def __missing__(self, key):
        return ""


DEFAULT_OUTPUT_FILENAME_MAX_LENGTH = 50
MIN_OUTPUT_FILENAME_MAX_LENGTH = 12


def folder_name(task_date: date) -> str:
    return f"{task_date.month:02d}{task_date.day:02d}"


def render_template(template: str, task, task_date: date, index=None, *, task_name=None) -> str:
    values = _TemplateValues(
        year=task_date.year,
        month=task_date.month,
        day=task_date.day,
        mm=f"{task_date.month:02d}",
        dd=f"{task_date.day:02d}",
        index="" if index is None else index,
        task_id=task.task_id,
        admin=task.admin,
        creator=task.creator,
        task_date=task.task_date,
        task_name=task.task_name if task_name is None else task_name,
        task_type=task.task_type,
    )
    return str(template or "").format_map(values)


def copy_if_updated(src: Path, dst: Path) -> bool:
    return copy_if_ready_and_updated(src, dst)


def _copy_output(src, dst, destination_callback=None, deferred_file_callback=None):
    try:
        updated = copy_if_updated(src, dst)
    except FileStillWritingError as error:
        print(f"[整理暂缓] {src}：{error}；下次整理自动重试，不计为失败。")
        if deferred_file_callback is not None:
            deferred_file_callback(Path(src), Path(dst), str(error))
        return False
    if destination_callback is not None:
        destination_callback(Path(dst))
    return updated


def copytree_if_updated(
    src_dir: Path,
    dst_dir: Path,
    destination_callback: Optional[Callable[[Path], None]] = None,
    deferred_file_callback=None,
) -> List[Path]:
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if not src_dir.exists():
        return []

    updated_files = []
    from model.MediaStorage import generated_media_path
    for src_file in src_dir.rglob("*"):
        if generated_media_path(src_file):
            continue
        if src_file.is_file() and not is_copy_temporary(src_file):
            dst_file = dst_dir / src_file.relative_to(src_dir)
            if _copy_output(src_file, dst_file, destination_callback, deferred_file_callback):
                updated_files.append(dst_file)
    return updated_files


def first_existing_file(task_dir: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        file_path = task_dir / name
        if file_path.is_file():
            return file_path

    # Older profiles enumerate reels1..reels10.  Extend only an explicitly
    # configured numbered family, keeping the configured exact-name priority.
    families = set()
    for name in names:
        path = Path(name)
        match = re.fullmatch(r"(.+?)(\d+)", path.stem)
        if match and any(
            Path(other).stem == match.group(1) and Path(other).suffix.lower() == path.suffix.lower()
            for other in names
        ):
            families.add((match.group(1), path.suffix.lower()))
    if task_dir.is_dir() and families:
        numbered = []
        for path in task_dir.iterdir():
            if not path.is_file():
                continue
            for stem, suffix in families:
                if path.suffix.lower() != suffix:
                    continue
                match = re.fullmatch(re.escape(stem) + r"(\d+)", path.stem)
                if match:
                    numbered.append((int(match.group(1)), path))
        if numbered:
            return max(numbered, key=lambda entry: entry[0])[1]
    return None


def output_filename_max_length(config: Optional[Dict] = None) -> int:
    """Return 0 for unlimited, otherwise a safe total filename length."""
    try:
        value = int(
            (config or {}).get(
                "task_output_filename_max_length",
                DEFAULT_OUTPUT_FILENAME_MAX_LENGTH,
            )
        )
    except (TypeError, ValueError):
        value = DEFAULT_OUTPUT_FILENAME_MAX_LENGTH
    if value <= 0:
        return 0
    return max(MIN_OUTPUT_FILENAME_MAX_LENGTH, min(255, value))


def limit_output_filename(file_name: str, max_length: int) -> str:
    """Shorten only the last path component and preserve its extension."""
    value = str(file_name or "")
    if not value or max_length <= 0:
        return value
    path = Path(value)
    name = path.name
    if len(name) <= max_length:
        return value
    suffix = path.suffix
    stem_length = max(1, max_length - len(suffix))
    limited_name = f"{path.stem[:stem_length]}{suffix}"
    parent = path.parent
    return limited_name if str(parent) in {"", "."} else str(parent / limited_name)


def _output_template(config, profile=None):
    profile = profile if isinstance(profile, dict) else {}
    return (
        profile.get("output_name_template")
        or config.get("task_output_name_template")
        or "{task_id}-{task_name}.mp4"
    )


def legacy_output_name(
    task, task_date: date, config: Dict, profile=None, index=None
) -> str:
    return render_template(_output_template(config, profile), task, task_date, index=index)


def output_name(task, task_date: date, config: Dict, profile=None, index=None) -> str:
    original = legacy_output_name(task, task_date, config, profile, index)
    maximum = output_filename_max_length(config)
    if index is None:
        return limit_output_filename(original, maximum)

    template = _output_template(config, profile)
    has_index = any(field == 'index' for _, field, _, _ in Formatter().parse(template))

    def numbered(title):
        name = render_template(template, task, task_date, index=index, task_name=title)
        if not has_index:
            path = Path(name)
            name = str(path.with_name(f'{path.stem}-{index}{path.suffix}'))
        return name

    title = str(task.task_name)
    name = numbered(title)
    if maximum <= 0 or len(Path(name).name) <= maximum:
        return name
    # Trim the title *inside* the template, not the rendered tail: task ID,
    # formatted index and extension must survive, wherever the index appears.
    if len(Path(numbered('')).name) > maximum:
        raise ValueError(f'成品命名模板的固定内容超过 {maximum} 字符，无法安全保留视频序号；'
                         '请增加最大文件名长度或缩短命名模板。')
    low, high = 0, len(title)
    while low < high:
        middle = (low + high + 1) // 2
        if len(Path(numbered(title[:middle])).name) <= maximum:
            low = middle
        else:
            high = middle - 1
    return numbered(title[:low])


def compatible_output_path(directory: Path, current_name: str, legacy_name: str) -> Path:
    """Keep using an existing pre-limit file so old work is not re-exported."""
    directory = Path(directory)
    current_path = directory / current_name
    legacy_path = directory / legacy_name
    if (
        current_path != legacy_path
        and not current_path.exists()
        and legacy_path.exists()
    ):
        return legacy_path
    return current_path


def export_task(
    task,
    task_date: date,
    root_dir: Path,
    output_dir: Path,
    output_dir_wsp: Path,
    profile: Dict,
    config: Dict,
    wsp_export: bool,
    exported_file_callback: Optional[Callable[[Path, Any], None]] = None,
    deferred_file_callback=None,
) -> List[Path]:
    def remember_output(file_path: Path) -> None:
        if exported_file_callback is not None:
            exported_file_callback(Path(file_path), task)

    mode = str(profile.get("mode") or "single").strip().lower()
    task_dir_name = render_template(profile.get("task_dir", ""), task, task_date)
    if not task_dir_name:
        return []
    task_dir = root_dir / task_dir_name / task.task_id

    if mode == "single":
        candidate_names = [
            render_template(name, task, task_date)
            for name in profile.get("candidate_names", [])
            if str(name).strip()
        ]
        result_file = first_existing_file(task_dir, candidate_names)
        updated = []
        if result_file:
            legacy_name = legacy_output_name(task, task_date, config, profile)
            current_name = output_name(task, task_date, config, profile)
            dst = compatible_output_path(output_dir, current_name, legacy_name)
            if _copy_output(result_file, dst, remember_output, deferred_file_callback):
                updated.append(dst)

        wsp_name = str(profile.get("wsp_candidate") or "").strip()
        if wsp_export and wsp_name:
            wsp_file = task_dir / render_template(wsp_name, task, task_date)
            if wsp_file.exists():
                legacy_name = legacy_output_name(task, task_date, config, profile)
                current_name = output_name(task, task_date, config, profile)
                dst = compatible_output_path(
                    output_dir_wsp, current_name, legacy_name
                )
                if _copy_output(wsp_file, dst, remember_output, deferred_file_callback):
                    updated.append(dst)
        return updated

    if mode == "all_videos":
        if not task_dir.exists():
            return []
        updated = []
        from model.MediaStorage import generated_media_path
        videos = sorted((path for path in task_dir.rglob('*.mp4') if not generated_media_path(path)),
                        key=lambda path: str(path.relative_to(task_dir)).casefold())
        planned, destinations = [], {}
        for index, video_file in enumerate(videos, start=1):
            legacy_name = legacy_output_name(
                task,
                task_date,
                config,
                profile,
                index=index,
            )
            current_name = output_name(
                task, task_date, config, profile, index=index
            )
            dst = compatible_output_path(output_dir, current_name, legacy_name)
            identity = str(dst.absolute()).casefold()
            if identity in destinations:
                raise ValueError('不同视频的成品名称冲突，未开始复制：'
                                 f'{destinations[identity].name} / {video_file.name} → {dst.name}。'
                                 '请在命名模板中保留有效的 {index} 序号。')
            destinations[identity] = video_file
            planned.append((video_file, dst))
        for video_file, dst in planned:
            if _copy_output(video_file, dst, remember_output, deferred_file_callback):
                updated.append(dst)
        return updated

    if mode == "copy_tree":
        if not task_dir.exists():
            return []
        directory_template = (
            profile.get("output_directory_template")
            or config.get("task_output_directory_template")
            or "{task_id}-{task_name}"
        )
        directory_name = render_template(directory_template, task, task_date)
        return copytree_if_updated(
            task_dir,
            output_dir / directory_name,
            destination_callback=remember_output,
            deferred_file_callback=deferred_file_callback,
        )

    print(f"未知导出模式，已跳过：{mode}")
    return []


def export_one_date(
    task_date: date,
    base_dir=None,
    wsp_export: bool = False,
    config: Optional[Dict] = None,
    exported_file_callback: Optional[Callable[[Path, Any], None]] = None,
    deferred_file_callback=None,
) -> Optional[ExportResult]:
    config = dict(config or {})
    if base_dir is None:
        base_dir = config.get("task_path")
    if not base_dir:
        raise ValueError("未配置 task_path")

    root_dir = Path(base_dir) / folder_name(task_date)
    output_dir = root_dir / str(config.get("task_result_output_dir") or "result")
    output_dir_wsp = root_dir / str(
        config.get("task_result_wsp_output_dir") or "result-wsp"
    )
    table_file_name = str(config.get("task_table_file_name") or "tasks.ods")
    doc_path = root_dir / table_file_name
    profiles = config.get("task_export_profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("task_export_profiles 必须是对象")

    print(f"\n开始整理：{task_date:%Y-%m-%d}")
    print(f"任务目录：{root_dir}")
    if not doc_path.exists():
        print(f"没找到登记表，跳过：{doc_path}")
        return None

    data_directory(output_dir).ensure(parents=True, exist_ok=True)
    data_directory(output_dir_wsp).ensure(parents=True, exist_ok=True)
    updated_files = []
    for task in ReadTaskOds(doc_path):
        if (
            getattr(task, "review_required_text", "")
            and getattr(task, "review_required", None) is None
        ):
            print(
                "无法识别是否需要审核，按未设置处理："
                f"第 {getattr(task, 'source_row', '?')} 行 -> "
                f"{task.review_required_text}"
            )
        profile = profiles.get(task.task_type)
        if not isinstance(profile, dict):
            print(f"未配置该任务类型的导出规则，已跳过：{task.task_type}")
            continue
        task_updated_files = export_task(
            task,
            task_date,
            root_dir,
            output_dir,
            output_dir_wsp,
            profile,
            config,
            wsp_export,
            exported_file_callback=exported_file_callback,
            deferred_file_callback=deferred_file_callback,
        )
        updated_files.extend(task_updated_files)

    print(f"整理完成：{output_dir}")
    print(f"本次新增/更新文件数：{len(updated_files)}")
    return ExportResult(output_dir=output_dir, updated_files=updated_files)

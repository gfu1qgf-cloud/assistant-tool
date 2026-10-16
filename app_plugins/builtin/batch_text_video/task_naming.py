"""Names consumed by task delivery, kept inside each bound task directory."""
from datetime import date
from functools import lru_cache
from pathlib import Path
import re

_STANDARD = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日-(reels|口播)(\d*)$", re.I)


def naming_rules(config):
    profiles = config.get("task_export_profiles", {}) if isinstance(config, dict) else {}
    return {str(kind): [str(value) for value in profile.get("candidate_names", []) if str(value).strip()]
            for kind, profile in profiles.items() if isinstance(profile, dict)
            and profile.get("mode", "single") == "single"
            and isinstance(profile.get("candidate_names"), list)} if isinstance(profiles, dict) else {}


def is_task_video(path):
    return bool(_STANDARD.fullmatch(Path(path).stem))


@lru_cache(maxsize=128)
def _existing_year(project, kind, mmdd, fallback_year):
    folder = Path(project) / kind
    if folder.is_dir():
        children = sorted((path for path in folder.iterdir() if path.is_dir() and not path.is_symlink()), key=lambda path: path.name)
        # Do not recursively scan every media file. Existing final names in the
        # first few task folders provide the project year for legacy queues.
        for child in children[:24]:
            for path in child.glob("*.mp4"):
                match = _STANDARD.fullmatch(path.stem)
                if match and f"{int(match[2]):02d}{int(match[3]):02d}" == mmdd:
                    return int(match[1])
    return fallback_year


def task_date(job):
    explicit = None
    raw = job.get("task_date", "")
    if isinstance(raw, str):
        match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw.strip())
        if match:
            try:
                explicit = date(*(int(value) for value in match.groups()))
            except ValueError:
                pass
    folder = Path(str(job.get("task_dir") or ""))
    if not job.get("task_dir"):
        return explicit
    # Host task layout is <MMDD>/<task type>/<task id>. Do not mistake a
    # numeric task id or any arbitrary ancestor for the date.
    project = folder.parent.parent
    if not re.fullmatch(r"\d{4}", project.name):
        return explicit
    month, day = int(project.name[:2]), int(project.name[2:])
    kind = str(job.get("task_type") or folder.parent.name)
    year = job.get("task_year")
    if type(year) is not int or not 2000 <= year <= 2100:
        parent_year = int(project.parent.name) if re.fullmatch(r"\d{4}", project.parent.name) else 0
        year = explicit.year if explicit else parent_year if 2000 <= parent_year <= 2100 else _existing_year(str(project), kind, project.name, date.today().year)
    try:
        # Delivery uses the loaded project date. Its folder wins over a stale
        # month/day in spreadsheet metadata; the loaded UI can provide the year.
        return date(year, month, day)
    except ValueError:
        return None


def legacy_auto_name(job):
    name = str(job.get("name") or "")
    return bool(job.get("task_dir") and re.fullmatch(r".+_文字版(?:_\d+)?", name))


def output_stem(job, rules=None):
    name = str(job.get("name") or "")
    if job.get("task_filename_auto") is False or not (job.get("task_filename_auto") is True or legacy_auto_name(job)):
        return name
    day = task_date(job)
    if day is None:
        return name
    kind = str(job.get("task_type") or Path(job["task_dir"]).parent.name)
    templates = (rules or {}).get(kind, [])
    if templates:
        values = {"year": day.year, "month": day.month, "day": day.day,
                  "mm": f"{day.month:02d}", "dd": f"{day.day:02d}",
                  "task_id": str(job.get("task_id") or Path(job["task_dir"]).name),
                  "task_type": kind, "task_date": job.get("task_date", "")}
        try:
            filename = templates[0].format_map(values)
        except (KeyError, ValueError):
            raise ValueError("整理任务的候选文件名包含无法填充的字段，请检查该任务类型的导出规则。") from None
    elif kind in {"reels", "口播"}:
        filename = f"{day.year}年{day.month}月{day.day}日-{kind}.mp4"
    else:
        return name
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or not filename.lower().endswith(".mp4"):
        raise ValueError("整理任务的候选文件名不是安全的 MP4 文件名。")
    stem = filename[:-4]
    # Pair variants retain their separate output; numbered families are already
    # understood by task delivery. Never use a task id as the reels suffix.
    variant = re.search(r"_文字版_(\d+)$", name)
    standard = _STANDARD.fullmatch(name)
    numbered = re.fullmatch(r"(.+)_(\d+)", name)
    number = variant[1] if variant else standard[5] if standard else numbered[2] if numbered and _STANDARD.fullmatch(numbered[1]) else ""
    if number and _STANDARD.fullmatch(stem):
        stem = re.sub(r"\d+$", "", stem) + number
    return stem


def refresh_task_names(state):
    rules = state.get("settings", {}).get("task_naming_rules", {})
    for job in state.get("jobs", []) + state.get("copy_pool", []):
        stem = output_stem(job, rules)
        if stem and stem != job.get("name") and (day := task_date(job)) is not None:
            job.update(name=stem, task_filename_auto=True, task_date=day.isoformat())
        elif job.get("task_filename_auto") is True and (day := task_date(job)) is not None:
            job["task_date"] = day.isoformat()

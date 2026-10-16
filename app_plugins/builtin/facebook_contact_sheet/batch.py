"""Sequential, resumable batch generation for task reference sheets."""
from model.AppDataStorage import data_io
from model.MediaStorage import media_temporary

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path

from .download import DownloadCanceled, download_facebook_video
from .engine import AnalysisCanceled, add_overview_frames, detect_cuts, render_contact_sheets
from .task_cache import cache_directory, cached_sheets, reference_key, remember_sheets
from model.BackgroundTasks import BackgroundTaskCanceled


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchJob:
    label: str
    url: str
    task_dir: str

    @property
    def folder(self):
        return cache_directory(self.task_dir, self.url)


def unique_jobs(targets):
    """Include every Facebook link of every selected task, without duplicate work."""
    from .task_cache import extract_facebook_references

    jobs = []
    seen = set()
    for target in targets:
        for url in extract_facebook_references(getattr(target["task"], "task_reference_link", "")):
            key = (str(Path(target["target_dir"]).resolve()).casefold(), reference_key(url))
            if key not in seen:
                seen.add(key)
                jobs.append(BatchJob(str(target["label"]), url, str(target["target_dir"])))
    return jobs


def run_reference_job(payload, context):
    """One independently managed task; commit its cache even if the view closed."""
    job, settings = payload["job"], payload["settings"]
    existing = cached_sheets(job.folder, job.url)
    if existing:
        context.report("已缓存，跳过下载", 99)
        return {"paths": [str(path) for path in existing], "cached": True}
    try:
        with media_temporary(job.folder, prefix=".facebook-download-") as temporary:
            context.report("下载中", None)
            video, title = download_facebook_video(
                job.url, Path(temporary) / 'download', browser=settings["browser"], profile=settings["profile"],
                progress=lambda done, total: context.report("下载中", int(done / total * 35) if total else None),
                canceled=context.is_canceled,
            )
            context.report("检测镜头中", 35)
            duration, cuts = detect_cuts(
                video, sensitivity=settings["sensitivity"], min_scene_seconds=settings["min_scene_seconds"],
                progress=lambda percent: context.report("检测镜头中", 35 + int(percent * .4)),
                canceled=context.is_canceled,
            )
            cuts = add_overview_frames(cuts, duration, settings["max_interval"])
            identifier = hashlib.sha256(job.url.encode("utf-8")).hexdigest()[:8]
            context.report("生成大图中", 75)
            sheets = render_contact_sheets(
                video, duration, cuts, job.folder, title=f"{title}_{identifier}",
                progress=lambda percent: context.report("生成大图中", 75 + int(percent * .24)),
                canceled=context.is_canceled,
            )
            context.check_canceled()
            remember_sheets(job.folder, job.url, sheets)
            return {"paths": [str(path) for path in sheets], "cached": False}
    except (DownloadCanceled, AnalysisCanceled) as error:
        raise BackgroundTaskCanceled("操作已取消。") from error


def run_batch(jobs, *, browser="", profile="", sensitivity=5, min_scene_seconds=0.8,
              max_interval=15, progress=None, canceled=None):
    """Return per-job outcomes; one bad URL never aborts the rest of the queue."""
    canceled = canceled or (lambda: False)
    progress = progress or (lambda _index, _status: None)
    outcomes = []
    settings = dict(browser=browser, profile=profile, sensitivity=sensitivity,
                    min_scene_seconds=min_scene_seconds, max_interval=max_interval)
    for index, job in enumerate(jobs):
        if canceled(): break
        class Context:
            def is_canceled(self): return canceled()
            def check_canceled(self):
                if canceled(): raise BackgroundTaskCanceled('操作已取消。')
            def report(self, message, _percent=None): progress(index, message)
        try:
            result = run_reference_job({'job':job,'settings':settings}, Context())
            status = '已缓存，跳过' if result['cached'] else f"完成（{len(result['paths'])} 张）"
        except (BackgroundTaskCanceled, DownloadCanceled, AnalysisCanceled):
            break
        except Exception as exc:
            logger.exception('任务 %s 的 Facebook 参考大图生成失败', job.label)
            status = f'失败：{type(exc).__name__}: {exc}'
        outcomes.append(status)
        progress(index, status)
    return outcomes

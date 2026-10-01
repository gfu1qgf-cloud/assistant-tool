"""Sequential, resumable batch generation for task reference sheets."""

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import tempfile

from .download import DownloadCanceled, download_facebook_video
from .engine import AnalysisCanceled, add_overview_frames, detect_cuts, render_contact_sheets
from .task_cache import cache_directory, cached_sheets, reference_key, remember_sheets


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


def run_batch(jobs, *, browser="", profile="", sensitivity=5, min_scene_seconds=0.8,
              max_interval=15, progress=None, canceled=None):
    """Return per-job outcomes; one bad URL never aborts the rest of the queue."""
    canceled = canceled or (lambda: False)
    progress = progress or (lambda _index, _status: None)
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="facebook_contact_sheet_batch_") as temporary:
        for index, job in enumerate(jobs):
            if canceled():
                break
            try:
                if cached_sheets(job.folder, job.url):
                    status = "已缓存，跳过"
                else:
                    progress(index, "下载中…")
                    video, title = download_facebook_video(
                        job.url, Path(temporary) / str(index), browser=browser, profile=profile,
                        canceled=canceled,
                    )
                    if canceled():
                        break
                    progress(index, "检测镜头中…")
                    duration, cuts = detect_cuts(
                        video, sensitivity=sensitivity, min_scene_seconds=min_scene_seconds,
                        canceled=canceled,
                    )
                    cuts = add_overview_frames(cuts, duration, max_interval)
                    if canceled():
                        break
                    progress(index, "生成大图中…")
                    identifier = hashlib.sha256(job.url.encode("utf-8")).hexdigest()[:8]
                    sheets = render_contact_sheets(
                        video, duration, cuts, job.folder, title=f"{title}_{identifier}", canceled=canceled,
                    )
                    if canceled():
                        break
                    remember_sheets(job.folder, job.url, sheets)
                    status = f"完成（{len(sheets)} 张）"
            except (DownloadCanceled, AnalysisCanceled):
                break
            except Exception as exc:
                logger.exception("任务 %s 的 Facebook 参考大图生成失败", job.label)
                status = f"失败：{type(exc).__name__}: {exc}"
            outcomes.append(status)
            progress(index, status)
    return outcomes

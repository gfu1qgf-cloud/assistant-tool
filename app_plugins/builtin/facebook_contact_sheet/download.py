"""Fetch an accessible Facebook video into a private temporary directory."""

from pathlib import Path
from urllib.parse import urlsplit


class DownloadCanceled(Exception):
    pass


def is_facebook_video_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and (
            host in {"fb.watch", "fb.com", "facebook.com"}
            or host.endswith(".facebook.com")
            or host.endswith(".fb.watch")
            or host.endswith(".fb.com")
        )
    )


def download_facebook_video(url, destination, *, browser="", profile="", progress=None, canceled=None):
    """Download one video only; login cookies stay in memory and are never persisted here."""
    if not is_facebook_video_url(url):
        raise ValueError("请输入有效的 Facebook 视频或 Reels 链接。")
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("缺少 yt-dlp 依赖；请安装更新后的 requirements.txt。") from exc

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    def hook(data):
        if canceled and canceled():
            raise DownloadCanceled("已取消获取视频。")
        if progress and data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            progress(downloaded, total)

    options = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": str(destination / "source.%(ext)s"),
        "noplaylist": True,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "skip_unavailable_fragments": False,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if browser:
        if browser not in {"chrome", "edge"}:
            raise ValueError("只支持 Chrome 或 Edge 的登录状态。")
        options["cookiesfrombrowser"] = (browser, profile or None, None, None)

    if canceled and canceled():
        raise DownloadCanceled("已取消获取视频。")
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(str(url).strip(), download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(
            "这个 Facebook 链接暂时无法读取：可能不支持、已失效，或需要选择浏览器登录状态。"
        ) from exc
    if canceled and canceled():
        raise DownloadCanceled("已取消获取视频。")
    files = sorted(
        (path for path in destination.glob("source.*") if path.is_file()
         and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".flv"}
         and path.stat().st_size > 0),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    if not files:
        raise RuntimeError("链接解析完成，但没有获得可读取的视频文件。")
    return files[0], str((info or {}).get("title") or "Facebook 视频")

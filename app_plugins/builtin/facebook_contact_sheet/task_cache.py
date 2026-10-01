"""Task-local cache for Facebook reference contact sheets."""

import hashlib
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .download import is_facebook_video_url


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？）】"
_CACHE_FOLDER_NAME = "Facebook参考大图"


def extract_facebook_references(value):
    """ODS rich-text fields contain both visible text and hyperlink targets."""
    found = []
    seen = set()
    for match in _URL.finditer(html.unescape(str(value or ""))):
        url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not is_facebook_video_url(url):
            continue
        key = reference_key(url)
        if key not in seen:
            found.append(url)
            seen.add(key)
    return found


def reference_key(url):
    if not is_facebook_video_url(url):
        raise ValueError("不是支持的 Facebook 链接。")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    match = re.search(r"/(?:reel|videos?)/(\d+)(?:/|$)", path, re.IGNORECASE)
    query = parse_qs(parsed.query)
    video_id = match.group(1) if match else next(
        (item for key in ("v", "video_id", "story_fbid") for item in query.get(key, ()) if item.isdigit()),
        "",
    )
    if video_id:
        identity = "facebook-video:" + video_id
    elif host == "fb.watch" or host.endswith(".fb.watch"):
        identity = "fb.watch:" + path.casefold()
    else:
        filtered = "&".join(
            f"{key}={item}" for key in sorted(query)
            if key not in {"fbclid", "ref", "mibextid"} for item in query[key]
        )
        identity = "facebook-url:" + host + path.casefold() + ("?" + filtered if filtered else "")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def cache_directory(task_dir, url):
    return Path(task_dir) / _CACHE_FOLDER_NAME / reference_key(url)


def cached_sheets(folder, url):
    folder = Path(folder)
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or manifest.get("key") != reference_key(url):
            return []
        names = manifest.get("files")
        if not isinstance(names, list) or not names:
            return []
        paths = []
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(".jpg"):
                return []
            path = folder / name
            if not path.is_file() or path.stat().st_size == 0:
                return []
            paths.append(path)
        return paths
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def remember_sheets(folder, url, sheets):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for sheet in sheets:
        path = Path(sheet)
        if path.parent.resolve() != folder.resolve() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("分镜大图不在当前任务的缓存目录中。")
        names.append(path.name)
    if not names:
        raise ValueError("没有可缓存的分镜大图。")
    payload = {"version": 1, "key": reference_key(url), "files": names}
    temporary = folder / (".manifest-" + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, folder / "manifest.json")
    finally:
        if temporary.exists():
            temporary.unlink()

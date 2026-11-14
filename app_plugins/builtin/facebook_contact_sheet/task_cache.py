"""Task-local cache for Facebook reference contact sheets."""
from model.AppDataStorage import data_directory, data_file, data_io

import hashlib
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .download import is_facebook_video_url
from .engine import ANALYSIS_VERSION


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
    reference_key(url)  # Validate the declared source without nesting output folders.
    return Path(task_dir) / _CACHE_FOLDER_NAME


def manifest_path(folder,url,separate=False):
    return Path(folder)/f'FB_{reference_key(url)}_{"frames" if separate else "sheet"}.json'


def cached_sheets(folder, url, *, separate=None):
    folder = Path(folder)
    modes=(False,True) if separate is None else (bool(separate),)
    manifests=[(manifest_path(folder,url,mode),folder,2,mode) for mode in modes]
    if separate is None:
        def modified(item):
            try:return item[0].stat().st_mtime_ns
            except OSError:return 0
        manifests.sort(key=modified,reverse=True)
    if separate is not True:
        manifests.extend((parent/'manifest.json',parent,1,False) for parent in (folder/reference_key(url),folder))
    for manifest,parent,version,mode in manifests:
        paths=_cached_manifest(manifest,parent,url,version,mode)
        if paths:return paths
    return []


def _cached_manifest(manifest_path,folder,url,version,mode):
    try:
        manifest = json.loads(data_file(manifest_path).read_text(encoding="utf-8"))
        if manifest.get("version") != version or manifest.get("key") != reference_key(url):
            return []
        if version==2 and manifest.get('separate') is not mode:return []
        if manifest.get("analysis_version") != ANALYSIS_VERSION:
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


def remember_sheets(folder, url, sheets, *, separate=False):
    folder = Path(folder)
    data_directory(folder).ensure(parents=True, exist_ok=True)
    names = []
    for sheet in sheets:
        path = Path(sheet)
        if path.parent.resolve() != folder.resolve() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("分镜大图不在当前任务的缓存目录中。")
        names.append(path.name)
    if not names:
        raise ValueError("没有可缓存的分镜大图。")
    payload = {"version": 2, "analysis_version": ANALYSIS_VERSION,
               "key": reference_key(url), "files": names, "separate":bool(separate)}
    temporary = folder / (".manifest-" + uuid4().hex + ".tmp")
    try:
        data_file(temporary).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        data_io.replace(temporary, manifest_path(folder,url,separate))
    finally:
        if temporary.exists():
            data_file(temporary).remove()

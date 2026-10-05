"""Deduplicate completed videos by Drive identity or verified binary content.

Names are deliberately not identities: independent videos may share a name.
No network calls or file writes are performed here.
"""
import json
import re
from collections import defaultdict


def video_content_key(video):
    if "content_md5" in video or "content_size" in video:
        checksum, size = video.get("content_md5"), video.get("content_size")
    else:
        checksum = video.get("md5") or video.get("md5Checksum")
        size = video.get("size")
        if not checksum or not size:
            try:
                version = json.loads(video.get("duration_version") or "null")
            except (TypeError, ValueError):
                version = None
            if isinstance(version, list) and len(version) == 3:
                _, checksum, size = version
    checksum, size = str(checksum or "").strip().lower(), str(size or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", checksum) or not re.fullmatch(r"[0-9]+", size):
        return None
    size = int(size)
    return (checksum, str(size)) if size > 0 else None


def video_content_fields(video):
    key = video_content_key(video)
    # Clear stale fingerprints when a folder entry is replaced by a new file
    # without metadata. An old checksum must never label the new video.
    return {"content_md5": key[0] if key else "", "content_size": key[1] if key else ""}


def deduplicate_video_groups(groups):
    """Keep earliest eligible delivery, globally across dates/categories.

    Explicit inclusion cannot bypass content deduplication. Unknown content is
    not guessed from filenames, sizes alone, or duration. Output does not mutate
    the caller's records. Creator remains part of the deduplication scope.
    """
    candidates = [(key, video) for key, videos in groups.items() for video in videos]
    candidates.sort(key=lambda pair: (
        pair[0][1], pair[0][2], pair[1].get("source") == "external_folder",
        str(pair[1].get("drive_file_id") or pair[1].get("identity") or ""),
        str(pair[0]),
    ))
    # Connected components also cover an updated Drive file whose old and new
    # versions both have separately uploaded copies. A single-pass greedy
    # filter would miss this bridge depending on input order.
    parents = list(range(len(candidates)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    seen = {}
    for index, (key, video) in enumerate(candidates):
        creator = key[4]
        file_id = str(video.get("drive_file_id") or "").strip()
        content = video_content_key(video)
        tokens = ([(creator, "id", file_id)] if file_id else []) + (
            [(creator, "content", content)] if content else [])
        for token in tokens:
            if token in seen:
                left, right = find(index), find(seen[token])
                parents[max(left, right)] = min(left, right)
            else:
                seen[token] = index
    result, excluded, retained_by_root = defaultdict(list), [], {}
    for index, (key, video) in enumerate(candidates):
        root = find(index)
        retained = retained_by_root.get(root)
        if retained is None:
            retained_by_root[root] = (key, video)
            result[key].append(video)
        else:
            retained_key, retained_video = retained
            file_id = str(video.get("drive_file_id") or "").strip()
            excluded.append({
                "file_name": str(video.get("file_name") or file_id),
                "drive_file_id": file_id,
                "date": key[1], "slot": key[2], "sheet": key[0], "category": key[3],
                "retained_file_name": str(retained_video.get("file_name") or ""),
                "retained_drive_file_id": str(retained_video.get("drive_file_id") or ""),
                "retained_date": retained_key[1], "retained_slot": retained_key[2],
                "retained_sheet": retained_key[0], "retained_category": retained_key[3],
                "reason": "相同网盘文件" if file_id and file_id == retained_video.get("drive_file_id")
                          else "文件大小及内容 MD5 相同" if video_content_key(video) == video_content_key(retained_video)
                          else "关联同一网盘文件的内容副本",
            })
    return dict(result), excluded

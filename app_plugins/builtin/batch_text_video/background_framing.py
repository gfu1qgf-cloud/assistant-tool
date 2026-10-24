"""One background framing policy for the editor, playback and export.

Only stable, opposing black edge strips are removed. Source media is never
rewritten; crop coordinates are applied before canvas fitting.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import threading

from model.AppDataStorage import data_directory, data_file

_LOCK = threading.RLock()
_MEMORY = {}
_VERSION = "stable-black-edges-v1"


def preview_size(width, height, max_width=540, max_height=960):
    scale = min(1, max_width / width, max_height / height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def framing_filter(width, height, settings):
    prefix = ""
    crop = settings.get("_background_crop") if settings.get("fit") == "cover" else None
    if crop:
        prefix = "crop=" + ":".join(str(value) for value in crop) + ","
    if settings["fit"] == "cover":
        return prefix + f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"


def stable_crop(samples, width, height):
    """Use the outer envelope, never a single frame's dark-scene bounds."""
    if not samples or any(not crop or len(crop) != 4 for crop in samples):
        return None
    if any(w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > width or y+h > height
           for w, h, x, y in samples):
        return None
    left = min(crop[2] for crop in samples)
    top = min(crop[3] for crop in samples)
    right = max(crop[2]+crop[0] for crop in samples)
    bottom = max(crop[3]+crop[1] for crop in samples)
    # Reject one-sided shadows and unstable framing. Keep at least half of each
    # axis, and require black borders on BOTH sides of a cropped axis.
    def axis(start, end, full):
        a, b = start, full-end
        if min(a, b) >= max(2, full*.005) and abs(a-b) <= max(4, full*.02) and end-start >= full*.5:
            return start//2*2, math.ceil(end/2)*2
        return 0, full
    left, right = axis(left, right, width)
    top, bottom = axis(top, bottom, height)
    result = (right-left, bottom-top, left, top)
    return result if result != (width, height, 0, 0) else None


def detect_background_crop(source, media, ffmpeg, directory=None, cancel=None):
    from .engine import _run, output_size
    width, height = output_size({"aspect": "source"}, media)
    info = Path(source).stat()
    signature = [_VERSION, str(Path(source).resolve()), info.st_size, info.st_mtime_ns, width, height, ffmpeg]
    key = hashlib.sha256(json.dumps(signature).encode("utf-8")).hexdigest()
    cache = Path(directory)/"background-framing-cache" if directory else None
    path = cache/(key+".json") if cache else None
    with _LOCK:
        if key in _MEMORY:
            return _MEMORY[key]
        if path and path.is_file():
            try:
                value = data_file(path).read_json()
                crop = value.get("crop")
                if value.get("signature") == signature and (crop is None or
                        all(isinstance(n, int) and not isinstance(n, bool) for n in crop) and
                        stable_crop([crop], width, height) == tuple(crop)):
                    _MEMORY[key] = tuple(crop) if crop else None
                    return _MEMORY[key]
            except (OSError, ValueError, TypeError):
                pass  # A damaged disposable cache is not authoritative data.
    duration = media["duration"]
    moments = sorted({min(.1, duration*.1), duration*.2, duration*.5, duration*.8, max(0, duration-.3)})
    samples = []
    for moment in moments:
        log = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-ss", f"{moment:.6f}",
                    # Limited-range YUV encodes black at 16, not 0. The normal
                    # cropdetect limit of 24 includes black without dark grays.
                    "-i", str(source), "-an", "-vf", "cropdetect=limit=0.0941176:round=2:reset=0:skip=0",
                    "-frames:v", "8", "-f", "null", "-"], cancel, timeout=30)
        values = [tuple(map(int, match)) for match in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", log)]
        # Cropdetect accumulates the largest visible bounds within each sample.
        samples.append(values[-1] if values else None)
        if stable_crop(samples, width, height) is None:
            result = None
            break  # Unpadded media needs just one short check, not five scans.
    else:
        result = stable_crop(samples, width, height)
    with _LOCK:
        _MEMORY[key] = result
        while len(_MEMORY) > 256:
            _MEMORY.pop(next(iter(_MEMORY)))
        if cache:
            data_directory(cache).ensure(parents=True, exist_ok=True)
            data_file(path).write_json({"signature": signature, "crop": result})
            files = sorted(cache.glob("*.json"), key=lambda file: file.stat().st_mtime, reverse=True)
            for old in files[256:]:
                data_file(old).remove()
    return result


def prepare_background_settings(source, media, settings, ffmpeg, directory=None, cancel=None):
    result = dict(settings)
    result.pop("_background_crop", None)
    if not media.get("still_image") and settings["fit"] == "cover" and settings.get("auto_crop_black_bars", True):
        result["_background_crop"] = detect_background_crop(source, media, ffmpeg, directory, cancel)
    return result

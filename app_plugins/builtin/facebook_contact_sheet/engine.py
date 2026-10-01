"""Shot-change analysis and contact-sheet rendering for short/medium videos."""

from dataclasses import dataclass
from pathlib import Path
import math
import re
import statistics


class AnalysisCanceled(Exception):
    pass


@dataclass(frozen=True)
class Cut:
    seconds: float
    kind: str
    strength: float = 0.0


def format_timestamp(seconds):
    tenths = max(0, int(round(seconds * 10)))
    whole, fraction = divmod(tenths, 10)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}.{fraction}"


def _descriptor(frame):
    import cv2
    import numpy as np

    small = cv2.resize(frame, (48, 27), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)
    return small, hist, float(np.mean(hsv[:, :, 2])) / 255.0


def _difference(a, b):
    import cv2
    import numpy as np

    pixel = float(np.mean(cv2.absdiff(a[0], b[0]))) / 255.0
    color = float(cv2.compareHist(a[1], b[1], cv2.HISTCMP_BHATTACHARYYA))
    # Coarse histogram bins jump even during a smooth dissolve; keep the
    # spatial pixel difference dominant so a gradual blend is not a hard cut.
    return 0.85 * pixel + 0.15 * min(1.0, color)


def classify_cuts(samples, *, sensitivity=5, min_scene_seconds=0.8):
    """Find hard cuts and short, bounded runs of change (cross-dissolves)."""
    if len(samples) < 2:
        return []
    sensitivity = max(1, min(10, int(sensitivity)))
    hard_limit = 0.32 - 0.014 * (sensitivity - 1)
    soft_limit = 0.068 - 0.0035 * (sensitivity - 1)
    scores = [_difference(samples[i - 1][1], samples[i][1]) for i in range(1, len(samples))]
    candidates = []
    hard_indexes = set()
    for index, score in enumerate(scores):
        baseline = statistics.median(scores[max(0, index - 12):max(0, index - 2)] or [0.025])
        if score >= hard_limit and score >= max(0.08, baseline * 2.35):
            hard_indexes.add(index)
            candidates.append(Cut(samples[index + 1][0], "硬切", score))

    index = 0
    while index < len(scores):
        if index in hard_indexes:
            index += 1
            continue
        baseline = statistics.median(scores[max(0, index - 12):max(0, index - 2)] or [0.02])
        if scores[index] < max(soft_limit, baseline * 1.65):
            index += 1
            continue
        start = index
        while index < len(scores) and index not in hard_indexes and scores[index] >= soft_limit:
            index += 1
        end = index
        if not 2 <= end - start <= 10:
            continue
        run_mean = statistics.mean(scores[start:end])
        before = scores[max(0, start - 4):start]
        after = scores[end:min(len(scores), end + 4)]
        surrounding = statistics.mean((before or [0.0]) + (after or [0.0]))
        if surrounding > run_mean * 0.72:
            continue
        accumulated = _difference(samples[start][1], samples[end][1])
        if accumulated >= max(0.19, hard_limit * 0.85):
            midpoint = (start + end) // 2
            candidates.append(Cut(samples[midpoint + 1][0], "叠化/渐变", accumulated))

    # A fade through black may span longer than the short dissolve window.
    for index in range(1, len(samples) - 1):
        if samples[index - 1][1][2] < 0.07 and samples[index][1][2] >= 0.18:
            candidates.append(Cut(samples[index][0], "黑场切换", 0.5))

    candidates.sort(key=lambda cut: cut.seconds)
    merged = []
    for cut in candidates:
        if cut.seconds < min_scene_seconds:
            continue
        if merged and cut.seconds - merged[-1].seconds < min_scene_seconds:
            if cut.strength > merged[-1].strength:
                merged[-1] = cut
        else:
            merged.append(cut)
    return merged


def detect_cuts(video_path, *, sensitivity=5, min_scene_seconds=0.8, progress=None, canceled=None):
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError("无法解码视频；请确认视频格式可由 OpenCV 读取。")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if not math.isfinite(fps) or fps <= 0 or frames <= 0:
            raise ValueError("无法读取视频帧率或时长。")
        duration = frames / fps
        stride = max(1, int(round(fps / 5)))
        samples = []
        frame_index = 0
        while capture.grab():
            if canceled and canceled():
                raise AnalysisCanceled("已取消镜头检测。")
            if frame_index % stride == 0:
                ok, frame = capture.retrieve()
                if ok:
                    samples.append((frame_index / fps, _descriptor(frame)))
            frame_index += 1
            if progress and frame_index % max(stride * 20, 1) == 0:
                progress(min(100, int(frame_index / max(frames, 1) * 100)))
        if not samples:
            raise ValueError("视频没有可读取的画面。")
        cuts = classify_cuts(
            samples, sensitivity=sensitivity, min_scene_seconds=min_scene_seconds
        )
        return duration, cuts
    finally:
        capture.release()


def add_overview_frames(cuts, duration, max_interval=15):
    """Give long single shots a few extra frames without mislabeling them as cuts."""
    if max_interval <= 0:
        return sorted(cuts, key=lambda cut: cut.seconds)
    boundaries = [0.0] + [cut.seconds for cut in sorted(cuts, key=lambda cut: cut.seconds)] + [duration]
    additions = []
    for start, end in zip(boundaries, boundaries[1:]):
        segments = math.ceil((end - start) / max_interval)
        for number in range(1, segments):
            additions.append(Cut(start + (end - start) * number / segments, "定时补帧", 0.0))
    return sorted([*cuts, *additions], key=lambda cut: cut.seconds)


def _font(size):
    from PIL import ImageFont

    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _representative_frame(capture, start, end):
    import cv2

    span = max(0.01, end - start)
    best = None
    best_quality = -1.0
    for fraction in (0.35, 0.55, 0.72):
        timestamp = min(end - 0.02, start + span * fraction)
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
        ok, frame = capture.read()
        if not ok:
            continue
        scale = min(1.0, 640 / max(frame.shape[0], frame.shape[1]))
        preview = cv2.resize(
            frame,
            (max(1, round(frame.shape[1] * scale)), max(1, round(frame.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        grey = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
        quality = float(cv2.Laplacian(grey, cv2.CV_64F).var())
        if float(grey.mean()) < 12:
            quality *= 0.1
        if quality > best_quality:
            best = frame
            best_quality = quality
    return best


def render_contact_sheets(video_path, duration, cuts, output_dir, *, title="视频", progress=None, canceled=None):
    """Render all shots, splitting only when one image would become unwieldy."""
    import cv2
    from PIL import Image, ImageDraw, ImageOps

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    boundaries = [0.0] + [cut.seconds for cut in cuts if 0 < cut.seconds < duration] + [duration]
    boundaries = sorted(set(boundaries))
    shot_count = len(boundaries) - 1
    if shot_count < 1:
        raise ValueError("视频时长不足，无法生成分镜图。")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError("无法重新读取视频画面。")
    portrait = capture.get(cv2.CAP_PROP_FRAME_HEIGHT) > capture.get(cv2.CAP_PROP_FRAME_WIDTH)
    columns, cell_width, cell_height = 4, 310, 465 if portrait else 255
    margin, label_height, per_page = 14, 36, 80 if portrait else 120
    font = _font(16)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(title)).strip(" ._")[:58] or "视频"
    paths = []
    try:
        for page_start in range(0, shot_count, per_page):
            page_end = min(shot_count, page_start + per_page)
            rows = math.ceil((page_end - page_start) / columns)
            sheet = Image.new("RGB", (margin + columns * (cell_width + margin), margin + rows * (cell_height + margin)), "#161b22")
            draw = ImageDraw.Draw(sheet)
            for shot_index in range(page_start, page_end):
                if canceled and canceled():
                    raise AnalysisCanceled("已取消生成分镜图。")
                start, end = boundaries[shot_index:shot_index + 2]
                frame = _representative_frame(capture, start, end)
                col = (shot_index - page_start) % columns
                row = (shot_index - page_start) // columns
                x = margin + col * (cell_width + margin)
                y = margin + row * (cell_height + margin)
                draw.rectangle((x, y, x + cell_width, y + cell_height), fill="#242b34")
                if frame is not None:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = Image.fromarray(rgb)
                    image = ImageOps.contain(image, (cell_width, cell_height - label_height), Image.Resampling.LANCZOS)
                    sheet.paste(image, (x + (cell_width - image.width) // 2, y + (cell_height - label_height - image.height) // 2))
                label = f"#{shot_index + 1:03d}  {format_timestamp(start)}–{format_timestamp(end)}"
                draw.text((x + 8, y + cell_height - label_height + 8), label, font=font, fill="#f5f7fa")
                if progress:
                    progress(int((shot_index + 1) / shot_count * 100))
            suffix = f"_{len(paths) + 1:02d}" if shot_count > per_page else ""
            path = output_dir / f"{stem}_分镜速览{suffix}.jpg"
            sheet.save(path, "JPEG", quality=90, optimize=True)
            paths.append(path)
    finally:
        capture.release()
    return paths

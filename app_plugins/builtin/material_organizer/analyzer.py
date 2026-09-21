from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np


VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts"
}
MATERIAL_ORGANIZER_CONFIG_KEY = "material_organizer_settings"
ANALYZER_VERSION = 1

DEFAULT_SETTINGS = {
    "sample_fps": 2.5,
    "analysis_width": 480,
    "maximum_samples": 240,
    "static_translation_percent": 0.18,
    "motion_translation_percent": 0.42,
    "zoom_threshold_percent": 0.28,
    "rotation_threshold_degrees": 0.18,
    "subject_motion_percent": 0.65,
    "minimum_confidence": 0.48,
    "thumbnail_width": 320,
    "thumbnail_height": 180,
}


def _float(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, min(maximum, value))


def _integer(value, default, minimum, maximum):
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def normalize_material_organizer_settings(value=None):
    source = value if isinstance(value, dict) else {}
    return {
        "sample_fps": _float(source.get("sample_fps"), 2.5, 0.25, 15.0),
        "analysis_width": _integer(source.get("analysis_width"), 480, 240, 1280),
        "maximum_samples": _integer(source.get("maximum_samples"), 240, 20, 3000),
        "static_translation_percent": _float(
            source.get("static_translation_percent"), 0.18, 0.01, 5.0
        ),
        "motion_translation_percent": _float(
            source.get("motion_translation_percent"), 0.42, 0.02, 10.0
        ),
        "zoom_threshold_percent": _float(
            source.get("zoom_threshold_percent"), 0.28, 0.01, 5.0
        ),
        "rotation_threshold_degrees": _float(
            source.get("rotation_threshold_degrees"), 0.18, 0.01, 10.0
        ),
        "subject_motion_percent": _float(
            source.get("subject_motion_percent"), 0.65, 0.02, 10.0
        ),
        "minimum_confidence": _float(
            source.get("minimum_confidence"), 0.48, 0.10, 0.95
        ),
        "thumbnail_width": _integer(
            source.get("thumbnail_width"), 320, 120, 640
        ),
        "thumbnail_height": _integer(
            source.get("thumbnail_height"), 180, 80, 640
        ),
    }


def discover_videos(paths):
    result = []
    seen = set()
    for value in paths or ():
        path = Path(value)
        candidates = path.rglob("*") if path.is_dir() else (path,)
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            key = os.path.normcase(str(candidate.resolve()))
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate.resolve())
    return sorted(result, key=lambda item: os.path.normcase(str(item)))


def analysis_signature(path, settings):
    path = Path(path)
    stat = path.stat()
    payload = {
        "version": ANALYZER_VERSION,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "settings": normalize_material_organizer_settings(settings),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _resize_for_analysis(frame, target_width):
    height, width = frame.shape[:2]
    if width <= target_width:
        return frame
    scale = target_width / float(width)
    return cv2.resize(
        frame,
        (target_width, max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def _thumbnail_score(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < 12.0 or brightness > 245.0:
        return -1.0
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    contrast = float(np.std(gray))
    return sharpness + contrast * 2.0 - abs(brightness - 125.0) * 0.15


def _letterbox(frame, width, height):
    source_height, source_width = frame.shape[:2]
    scale = min(width / source_width, height / source_height)
    target_width = max(1, int(round(source_width * scale)))
    target_height = max(1, int(round(source_height * scale)))
    resized = cv2.resize(
        frame,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    canvas = np.full((height, width, 3), 24, dtype=np.uint8)
    left = (width - target_width) // 2
    top = (height - target_height) // 2
    canvas[top:top + target_height, left:left + target_width] = resized
    return canvas


def _error_thumbnail(width, height, message="VIDEO ERROR"):
    canvas = np.full((height, width, 3), (38, 38, 42), dtype=np.uint8)
    cv2.rectangle(canvas, (4, 4), (width - 5, height - 5), (70, 70, 190), 3)
    font_scale = max(0.45, min(0.9, width / 420.0))
    text_size = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)[0]
    cv2.putText(
        canvas,
        message,
        ((width - text_size[0]) // 2, (height + text_size[1]) // 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        (220, 220, 240),
        1,
        cv2.LINE_AA,
    )
    return canvas


def thumbnail_cache_path(path, thumbnail_root):
    digest = hashlib.sha256(
        os.path.normcase(str(Path(path).resolve())).encode("utf-8", "replace")
    ).hexdigest()[:24]
    return Path(thumbnail_root) / f"{digest}.jpg"


def _write_jpeg(path, image, quality=88):
    """Write a JPEG without OpenCV's Windows Unicode-path limitation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(
        ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
    )
    if not ok:
        raise OSError(f"无法编码缩略图：{path}")
    try:
        path.write_bytes(encoded.tobytes())
    except OSError as error:
        raise OSError(f"无法写入缩略图：{path}") from error
    if not path.is_file() or path.stat().st_size <= 0:
        raise OSError(f"缩略图写入后为空：{path}")


def error_analysis_result(path, thumbnail_root, settings=None, message="视频无法读取"):
    settings = normalize_material_organizer_settings(settings)
    thumbnail_path = thumbnail_cache_path(path, thumbnail_root)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    image = _error_thumbnail(
        settings["thumbnail_width"], settings["thumbnail_height"]
    )
    _write_jpeg(thumbnail_path, image)
    try:
        signature = analysis_signature(path, settings)
    except OSError:
        signature = ""
    return {
        "motion_key": "damaged",
        "confidence": 1.0,
        "status": "error",
        "thumbnail_path": str(thumbnail_path),
        "analysis_signature": signature,
        "analysis": {"reason": str(message or "视频无法读取")},
    }


def generate_thumbnail(path, thumbnail_path, settings):
    settings = normalize_material_organizer_settings(settings)
    width = settings["thumbnail_width"]
    height = settings["thumbnail_height"]
    capture = cv2.VideoCapture(str(path))
    best = None
    best_score = -1.0
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fractions = (0.08, 0.22, 0.40, 0.58, 0.76)
        for fraction in fractions:
            if frame_count > 1:
                capture.set(cv2.CAP_PROP_POS_FRAMES, int((frame_count - 1) * fraction))
            ok, frame = capture.read()
            if not ok or frame is None or frame.size == 0:
                continue
            score = _thumbnail_score(frame)
            if score > best_score:
                best, best_score = frame, score
    finally:
        capture.release()
    if best is None:
        best = _error_thumbnail(width, height)
        success = False
    else:
        best = _letterbox(best, width, height)
        success = True
    thumbnail_path = Path(thumbnail_path)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jpeg(thumbnail_path, best)
    return success


def _histogram_correlation(left, right):
    left_hist = cv2.calcHist([left], [0], None, [32], [0, 256])
    right_hist = cv2.calcHist([right], [0], None, [32], [0, 256])
    cv2.normalize(left_hist, left_hist)
    cv2.normalize(right_hist, right_hist)
    return float(cv2.compareHist(left_hist, right_hist, cv2.HISTCMP_CORREL))


def _frame_motion(previous_gray, current_gray):
    points = cv2.goodFeaturesToTrack(
        previous_gray,
        maxCorners=360,
        qualityLevel=0.012,
        minDistance=7,
        blockSize=7,
    )
    if points is None or len(points) < 12:
        return None
    tracked, status, _errors = cv2.calcOpticalFlowPyrLK(
        previous_gray,
        current_gray,
        points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(
            cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
            30,
            0.01,
        ),
    )
    if tracked is None or status is None:
        return None
    mask = status.reshape(-1).astype(bool)
    old = points.reshape(-1, 2)[mask]
    new = tracked.reshape(-1, 2)[mask]
    if len(old) < 10:
        return None
    transform, inliers = cv2.estimateAffinePartial2D(
        old,
        new,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.5,
        maxIters=1200,
        confidence=0.99,
        refineIters=10,
    )
    if transform is None:
        return None
    inlier_mask = (
        inliers.reshape(-1).astype(bool)
        if inliers is not None else np.ones(len(old), dtype=bool)
    )
    inlier_ratio = float(np.mean(inlier_mask)) if len(inlier_mask) else 0.0
    a, b = float(transform[0, 0]), float(transform[1, 0])
    scale = math.sqrt(a * a + b * b)
    rotation = math.degrees(math.atan2(b, a))
    width = max(1.0, float(previous_gray.shape[1]))
    height = max(1.0, float(previous_gray.shape[0]))
    dx = float(transform[0, 2]) / width * 100.0
    dy = float(transform[1, 2]) / height * 100.0
    zoom = (scale - 1.0) * 100.0
    predicted = cv2.transform(old.reshape(1, -1, 2), transform).reshape(-1, 2)
    residual = np.linalg.norm(new - predicted, axis=1)
    residual_percent = float(np.percentile(residual, 75)) / math.hypot(width, height) * 100.0
    flow = np.linalg.norm(new - old, axis=1)
    flow_percent = float(np.median(flow)) / math.hypot(width, height) * 100.0
    return {
        "dx": dx,
        "dy": dy,
        "zoom": zoom,
        "rotation": rotation,
        "residual": residual_percent,
        "flow": flow_percent,
        "inlier_ratio": inlier_ratio,
    }


def _median(values):
    return float(np.median(values)) if values else 0.0


def _sign_consistency(values, threshold):
    active = [value for value in values if abs(value) >= threshold]
    if not active:
        return 0.0
    positive = sum(value > 0 for value in active)
    return max(positive, len(active) - positive) / len(active)


def classify_motion_measurements(measurements, settings=None):
    """Classify normalized frame-pair measurements into a virtual category."""
    settings = normalize_material_organizer_settings(settings)
    samples = [dict(value) for value in measurements or () if value]
    if len(samples) < 3:
        return "uncertain", 0.20, {"reason": "有效画面特征不足", "samples": len(samples)}

    dx_values = [float(item.get("dx") or 0.0) for item in samples]
    dy_values = [float(item.get("dy") or 0.0) for item in samples]
    zoom_values = [float(item.get("zoom") or 0.0) for item in samples]
    rotation_values = [float(item.get("rotation") or 0.0) for item in samples]
    residual_values = [float(item.get("residual") or 0.0) for item in samples]
    flow_values = [float(item.get("flow") or 0.0) for item in samples]
    inlier_values = [float(item.get("inlier_ratio") or 0.0) for item in samples]

    dx = _median(dx_values)
    dy = _median(dy_values)
    zoom = _median(zoom_values)
    rotation = _median(rotation_values)
    abs_translation = math.hypot(dx, dy)
    residual = _median(residual_values)
    flow = _median(flow_values)
    inlier_ratio = _median(inlier_values)
    translation_threshold = settings["motion_translation_percent"]
    static_threshold = settings["static_translation_percent"]
    zoom_threshold = settings["zoom_threshold_percent"]
    rotation_threshold = settings["rotation_threshold_degrees"]

    candidates = []
    dx_consistency = _sign_consistency(dx_values, translation_threshold * 0.55)
    dy_consistency = _sign_consistency(dy_values, translation_threshold * 0.55)
    zoom_consistency = _sign_consistency(zoom_values, zoom_threshold * 0.55)
    rotation_consistency = _sign_consistency(rotation_values, rotation_threshold * 0.55)

    if abs(dx) >= translation_threshold and abs(dx) >= abs(dy) * 1.25:
        # Image movement is opposite the physical camera pan direction.
        candidates.append(("pan_left" if dx > 0 else "pan_right", abs(dx), dx_consistency))
    if abs(dy) >= translation_threshold and abs(dy) >= abs(dx) * 1.25:
        candidates.append(("tilt_up" if dy > 0 else "tilt_down", abs(dy), dy_consistency))
    if abs(zoom) >= zoom_threshold:
        candidates.append(("push_in" if zoom > 0 else "pull_out", abs(zoom), zoom_consistency))
    if abs(rotation) >= rotation_threshold:
        candidates.append(("rotate", abs(rotation), rotation_consistency))

    direction_changes = sum(
        1
        for values, threshold in (
            (dx_values, translation_threshold),
            (dy_values, translation_threshold),
            (zoom_values, zoom_threshold),
        )
        if any(value > threshold for value in values)
        and any(value < -threshold for value in values)
    )
    if len(candidates) >= 2 or direction_changes >= 1:
        strength = max((item[1] for item in candidates), default=abs_translation)
        confidence = min(0.96, 0.52 + strength / max(1.0, translation_threshold * 8))
        key = "mixed"
        reason = "检测到两种以上持续运镜或明显方向变化"
    elif candidates:
        key, strength, consistency = max(candidates, key=lambda item: item[1])
        confidence = min(0.98, 0.45 + consistency * 0.35 + min(0.18, strength / 8.0))
        reason = "全局背景运动方向稳定"
    elif (
        abs_translation <= static_threshold
        and abs(zoom) <= zoom_threshold * 0.70
        and abs(rotation) <= rotation_threshold * 0.70
    ):
        if residual >= settings["subject_motion_percent"] or flow >= settings["subject_motion_percent"]:
            key = "subject_motion"
            confidence = min(0.92, 0.50 + max(residual, flow) / 8.0)
            reason = "镜头基本稳定，但局部画面持续运动"
        else:
            key = "static"
            confidence = min(0.98, 0.72 + max(0.0, inlier_ratio - 0.5) * 0.4)
            reason = "全局平移、缩放和旋转均低于静态阈值"
    else:
        irregularity = 1.0 - max(dx_consistency, dy_consistency, zoom_consistency)
        if abs_translation >= static_threshold or flow >= static_threshold:
            key = "handheld"
            confidence = min(0.88, 0.46 + irregularity * 0.30 + flow / 12.0)
            reason = "存在运动但方向不稳定，接近手持晃动"
        else:
            key = "uncertain"
            confidence = 0.35
            reason = "运动幅度处于分类阈值之间"

    if confidence < settings["minimum_confidence"]:
        key = "uncertain"
        reason = "分析置信度不足，需要人工确认"
    metrics = {
        "reason": reason,
        "samples": len(samples),
        "median_dx_percent": round(dx, 4),
        "median_dy_percent": round(dy, 4),
        "median_zoom_percent": round(zoom, 4),
        "median_rotation_degrees": round(rotation, 4),
        "median_residual_percent": round(residual, 4),
        "median_flow_percent": round(flow, 4),
        "median_inlier_ratio": round(inlier_ratio, 4),
    }
    return key, round(float(confidence), 4), metrics


def analyze_video(path, thumbnail_root, settings=None, should_stop=None):
    path = Path(path).resolve()
    settings = normalize_material_organizer_settings(settings)
    should_stop = should_stop or (lambda: False)
    signature = analysis_signature(path, settings)
    thumbnail_path = thumbnail_cache_path(path, thumbnail_root)
    try:
        thumbnail_ok = generate_thumbnail(path, thumbnail_path, settings)
    except Exception as error:
        fallback = error_analysis_result(
            path,
            thumbnail_root,
            settings,
            f"缩略图生成失败：{type(error).__name__}: {error}",
        )
        fallback["analysis_signature"] = signature
        return fallback

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        result = error_analysis_result(
            path, thumbnail_root, settings, "OpenCV 无法打开视频"
        )
        result["analysis_signature"] = signature
        return result
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = frame_count / fps if fps > 0 else 0.0
        interval = max(1, int(round(max(fps, 1.0) / settings["sample_fps"])))
        maximum = settings["maximum_samples"]
        previous_gray = None
        measurements = []
        sampled = 0
        frame_index = 0
        while sampled < maximum:
            if should_stop():
                raise InterruptedError("用户取消素材分析")
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if frame_index % interval:
                frame_index += 1
                continue
            frame_index += 1
            sampled += 1
            frame = _resize_for_analysis(frame, settings["analysis_width"])
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if previous_gray is not None:
                if _histogram_correlation(previous_gray, gray) >= 0.42:
                    motion = _frame_motion(previous_gray, gray)
                    if motion is not None:
                        measurements.append(motion)
            previous_gray = gray
    finally:
        capture.release()

    if not thumbnail_ok and not measurements:
        motion_key, confidence = "damaged", 1.0
        metrics = {"reason": "视频无法解码出有效画面", "samples": 0}
        status = "error"
    else:
        motion_key, confidence, metrics = classify_motion_measurements(
            measurements, settings
        )
        status = "ready"
    return {
        "motion_key": motion_key,
        "confidence": confidence,
        "status": status,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "thumbnail_path": str(thumbnail_path),
        "analysis_signature": signature,
        "analysis": metrics,
    }

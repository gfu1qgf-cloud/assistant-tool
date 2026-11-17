"""Frame-based B-roll at known edit joins; interview audio is never replaced."""
from dataclasses import dataclass
import math
from pathlib import Path

ENABLED = "seam_cover_enabled"
SOURCE = "seam_cover_source"
DURATION = "seam_cover_duration_ms"
DEFAULTS = {ENABLED: False, SOURCE: "", DURATION: 1500}


def normalize_options(settings):
    try:
        duration = float(settings.get(DURATION, 1500))
        if not math.isfinite(duration):
            raise ValueError("non-finite duration")
        duration = max(500, min(5000, int(round(duration))))
    except (TypeError, ValueError, OverflowError):
        duration = 1500
    return {ENABLED: bool(settings.get(ENABLED, False)),
            SOURCE: str(settings.get(SOURCE) or "").strip(), DURATION: duration}


def is_active(settings):
    return bool(settings.get(ENABLED)) and settings.get("export_mode", "joined") in ("joined", "video_only")


def validate_source(settings):
    source = str(settings.get(SOURCE) or "").strip()
    if not source or not Path(source).is_file():
        raise ValueError("请为“接缝补画面”选择一个存在的视频文件，或关闭此功能。")
    return source


@dataclass(frozen=True)
class CoverWindow:
    start: int
    end: int
    source_start: int


def plan_windows(frame_counts, fps, duration_ms):
    """Half-open output-frame intervals. Overlapping joins share one cover shot."""
    if len(frame_counts) < 2:
        return ()
    total = sum(frame_counts)
    length = max(1, int(round(duration_ms * fps / 1000)))
    before = length // 2
    seam = 0
    spans = []
    for count in frame_counts[:-1]:
        seam += count
        start, end = max(0, seam - before), min(total, seam + length - before)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    cursor = 0
    result = []
    for start, end in spans:
        result.append(CoverWindow(start, end, cursor))
        cursor += end - start
    return tuple(result)


@dataclass(frozen=True)
class CoverPlan:
    path: str
    source_frames: int
    fps: float
    windows: tuple

    def pieces(self, start, end):
        for window in self.windows:
            left, right = max(start, window.start), min(end, window.end)
            if right > left:
                yield CoverWindow(left - start, right - start,
                                  (window.source_start + left - window.start) % self.source_frames)


def prepare_cover(settings, frame_counts, width, height, fps, directory, ffmpeg,
                  run_process, progress, cancelled):
    if not is_active(settings) or len(frame_counts) < 2:
        return None
    if cancelled():
        raise InterruptedError("用户已请求停止智能剪辑导出")
    source = validate_source(settings)
    windows = plan_windows(frame_counts, fps, settings.get(DURATION, 1500))
    # Reuse the application's video-aware probe. In particular, an audio file
    # renamed to .mp4 must fail instead of producing an invisible cover shot.
    from model.node_workflow.render import probe, resolve_tools
    _, ffprobe = resolve_tools(ffmpeg)
    probe(source, ffprobe)
    if cancelled():
        raise InterruptedError("用户已请求停止智能剪辑导出")
    cover = Path(directory) / "seam_cover.mp4"
    used_frames = sum(window.end - window.start for window in windows)
    progress("[智能剪辑] 准备接缝补画面：保留访谈原声，补画面视频静音、顺序取用并循环。")
    # A video-only, fixed-rate source gives looping an unambiguous duration,
    # even if the selected file has a longer audio track or variable frame rate.
    # Decode only the prefix actually needed by this edit, not an entire long asset.
    run_process([
        ffmpeg, "-y", "-i", source, "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", (f"fps={fps:.3f}:start_time=0,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"),
        "-frames:v", str(used_frames), "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-bf", "0", str(cover),
    ], progress, cancelled)
    info = probe(str(cover), ffprobe)
    frames = max(1, int(round(info["duration"] * fps)))
    progress(f"[智能剪辑] 接缝补画面：{len(frame_counts)-1} 个接缝，"
             f"合并为 {len(windows)} 处覆盖；每处约 {settings.get(DURATION, 1500)/1000:g} 秒。")
    return CoverPlan(str(cover), frames, fps, windows)


def segment_video_args(plan, start, frame_count, video_filter):
    """Additional video inputs + mapping. Caller maps ONLY input 0's audio."""
    pieces = list(plan.pieces(start, start + frame_count)) if plan else []
    if not pieces:
        return [], ["-map", "0:v:0", "-vf", video_filter]
    inputs, filters = [], [f"[0:v:0]{video_filter}[base0]"]
    for index, piece in enumerate(pieces, 1):
        offset = piece.source_start / plan.fps
        duration = (piece.end - piece.start) / plan.fps
        inputs += ["-stream_loop", "-1", "-ss", f"{offset:.9f}",
                   "-t", f"{duration + 2/plan.fps:.9f}", "-i", plan.path]
        left, right = piece.start / plan.fps, piece.end / plan.fps
        filters += [
            f"[{index}:v:0]trim=end_frame={piece.end-piece.start},"
            f"setpts=PTS-STARTPTS+{left:.9f}/TB[cover{index}]",
            f"[base{index-1}][cover{index}]overlay=x=0:y=0:"
            f"eof_action=pass:repeatlast=0:"
            f"enable='gte(t,{left-0.000001:.9f})*lt(t,{right-0.000001:.9f})'[base{index}]",
        ]
    return inputs, ["-filter_complex", ";".join(filters), "-map", f"[base{len(pieces)}]"]

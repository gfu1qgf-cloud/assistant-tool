"""Bounded video adaptation; crossfade loops reuse one short cached cycle."""
from model.AppDataStorage import data_directory, data_file, data_io
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


def source_fps(media):
    try:
        fps = Fraction(media["video"].get("avg_frame_rate") or media["video"].get("r_frame_rate") or "30")
        if 1 <= fps <= 120:
            return fps
    except (ValueError, ZeroDivisionError):
        pass
    return Fraction(30)


def normalize_duration(settings):
    settings.setdefault("unvoiced_duration_mode", "source")
    settings.setdefault("unvoiced_duration_seconds", 15.0)
    if settings["unvoiced_duration_mode"] not in {"source", "fixed"}:
        raise ValueError("无人声时的成品时长模式无效。")
    value = settings["unvoiced_duration_seconds"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .1 <= value <= 86400:
        raise ValueError("固定成品时长须为0.1秒至24小时。")


def target_duration(source_duration, settings, voice_duration=None):
    normalize_duration(settings)
    if voice_duration is not None:
        return voice_duration  # Never silently cut narration to the fixed target.
    return settings["unvoiced_duration_seconds"] if settings["unvoiced_duration_mode"] == "fixed" else source_duration


def adaptation_plan(source_duration, target_duration, settings, fps=30):
    fps = float(fps)
    strategy = settings["duration_strategy"]
    if strategy not in {"loop", "slow", "hybrid"}:
        raise ValueError("未知的背景时长策略。")
    speed = 1.0
    if target_duration > source_duration and strategy != "loop":
        speed = source_duration / target_duration
        if strategy == "hybrid":
            speed = max(float(settings["min_speed"]), speed)
        elif speed < 0.05:
            raise ValueError("减速超过20倍，请选择减速＋循环，避免极端卡顿。")
    if not 0.05 <= speed <= 1 or not math.isfinite(speed):
        raise ValueError("视频播放速度无效。")
    clip_duration = math.ceil(source_duration / speed * fps - 1e-6) / fps
    repeating = target_duration > clip_duration + 0.5 / fps
    fade = 0.0
    if repeating and settings["loop_transition"] == "fade":
        fade = min(float(settings["transition_seconds"]), clip_duration / 3)
        fade = math.floor(fade * fps + 1e-6) / fps
    if settings["loop_transition"] not in {"none", "fade"}:
        raise ValueError("未知的循环转场。")
    if settings["slow_interpolation"] not in {"repeat", "blend", "motion"}:
        raise ValueError("未知的减速补帧方式。")
    return {"speed": speed, "clip_duration": clip_duration, "fade": fade,
            "repeating": repeating, "loops": max(0, math.ceil(target_duration / (source_duration / speed)) - 1)}


def base_filter(width, height, settings, plan, fps):
    from .background_framing import framing_filter
    fit = framing_filter(width, height, settings)
    rate = f"{fps.numerator}/{fps.denominator}"
    method = settings["slow_interpolation"]
    retiming = f"setpts=(PTS-STARTPTS)/{plan['speed']:.12f},{fit},setsar=1,fps={rate}"
    if plan["speed"] < 0.99999 and method != "repeat":
        # minterpolate needs neighboring frames at BOTH ends. Pad at source fps,
        # interpolate, then remove precisely that prefix; don't lose the first frames.
        padding = 3/(float(fps)*plan["speed"])
        retiming = (f"{fit},setsar=1,fps={rate},tpad=start=3:stop=3:start_mode=clone:stop_mode=clone,"
                    f"setpts=(PTS-STARTPTS)/{plan['speed']:.12f},"
                    f"minterpolate=fps={rate}:mi_mode={'mci' if method == 'motion' else 'blend'},"
                    f"trim=start={padding:.9f},setpts=PTS-STARTPTS")
    # Cover only final-frame rounding, after the interpolation context is handled.
    color = settings.get('_source_color_filter', '')
    return (f"{color + ',' if color else ''}{retiming},format=yuv420p,settb=AVTB,tpad=stop_mode=clone:stop_duration={3/float(fps):.9f},"
            f"trim=duration={plan['clip_duration']:.9f},setpts=PTS-STARTPTS")


def prepare_cycle(source, directory, ffmpeg, ffprobe, width, height, settings, plan, fps, cancel, report):
    from .engine import _run, filter_file_option, probe
    info = Path(source).stat()
    values = ["cycle-v4-sdr709", str(Path(source).resolve()), info.st_size, info.st_mtime_ns,
              width, height, settings["fit"], settings["slow_interpolation"],
              settings.get("_background_crop"), settings.get('_source_color_filter', ''),
              {key: plan[key] for key in ("speed", "clip_duration", "fade")}, str(fps)]
    key = hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()
    cache = Path(directory)/"video-cache"
    data_directory(cache).ensure(parents=True, exist_ok=True)
    target = cache/(key + ".mp4")
    from model.MediaCacheLeases import media_cache_build
    try:
        with media_cache_build(target, cancel):
            return _prepare_cycle_cached(source, cache, target, ffmpeg, ffprobe, width, height, settings, plan, fps, cancel, report)
    except InterruptedError as error:
        from .engine import Cancelled
        raise Cancelled(str(error)) from error


def _prepare_cycle_cached(source, cache, target, ffmpeg, ffprobe, width, height, settings, plan, fps, cancel, report):
    from .engine import _run, filter_file_option, probe
    if target.is_file():
        try:
            duration = probe(target, ffprobe)["duration"]
            if abs(duration - (plan["clip_duration"] - plan["fade"])) < 2/float(fps):
                os.utime(target, None)
                return target, duration
        except (ValueError, OSError):
            pass
    report("准备可复用的叠化循环；不会为每次重复创建一个大型视频。")
    fade, end = plan["fade"], plan["clip_duration"]
    from model.MediaColor import sdr_output_filter, sdr_output_args
    graph = [f"[0:v]{base_filter(width,height,settings,plan,fps)},split=3[head][tail][middle]",
             f"[head]trim=start=0:end={fade:.9f},setpts=PTS-STARTPTS[h]",
             f"[tail]trim=start={end-fade:.9f}:end={end:.9f},setpts=PTS-STARTPTS[t]",
             f"[t][h]xfade=transition=fade:duration={fade:.9f}:offset=0,trim=duration={fade:.9f}[join]",
             f"[middle]trim=start={fade:.9f}:end={end-fade:.9f},setpts=PTS-STARTPTS[m]",
             f"[join][m]concat=n=2:v=1:a=0,{sdr_output_filter()}[out]"]
    with data_io.TemporaryDirectory(dir=cache, prefix="cycle-") as folder:
        script, output = Path(folder)/"cycle.filter", Path(folder)/"cycle.mp4"
        data_file(script).write_text(";\n".join(graph), "utf-8")
        _run([ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
              filter_file_option(ffmpeg), str(script), "-map", "[out]", "-an", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *sdr_output_args(), str(output)], cancel,
             timeout=max(600, end*60))
        duration = probe(output, ffprobe)["duration"]
        if abs(duration - (end-fade)) > 2/float(fps):
            raise RuntimeError("叠化循环时长异常，未继续导出。")
        data_io.replace(output, target)
    # Cleanup runs when the export releases its media-cache lease.
    return target, duration

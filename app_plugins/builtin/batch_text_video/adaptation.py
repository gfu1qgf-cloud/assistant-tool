"""Bounded video adaptation; crossfade loops reuse one short cached cycle."""
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
    if settings["fit"] == "cover":
        fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    else:
        fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
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
    return (f"{retiming},format=yuv420p,settb=AVTB,tpad=stop_mode=clone:stop_duration={3/float(fps):.9f},"
            f"trim=duration={plan['clip_duration']:.9f},setpts=PTS-STARTPTS")


def prepare_cycle(source, directory, ffmpeg, ffprobe, width, height, settings, plan, fps, cancel, report):
    from .engine import _run, filter_file_option, probe
    info = Path(source).stat()
    values = ["cycle-v2", str(Path(source).resolve()), info.st_size, info.st_mtime_ns,
              width, height, settings["fit"], settings["slow_interpolation"],
              {key: plan[key] for key in ("speed", "clip_duration", "fade")}, str(fps)]
    key = hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()
    cache = Path(directory)/"video-cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache/(key + ".mp4")
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
    graph = [f"[0:v]{base_filter(width,height,settings,plan,fps)},split=3[head][tail][middle]",
             f"[head]trim=start=0:end={fade:.9f},setpts=PTS-STARTPTS[h]",
             f"[tail]trim=start={end-fade:.9f}:end={end:.9f},setpts=PTS-STARTPTS[t]",
             f"[t][h]xfade=transition=fade:duration={fade:.9f}:offset=0,trim=duration={fade:.9f}[join]",
             f"[middle]trim=start={fade:.9f}:end={end-fade:.9f},setpts=PTS-STARTPTS[m]",
             "[join][m]concat=n=2:v=1:a=0,format=yuv420p[out]"]
    with tempfile.TemporaryDirectory(dir=cache, prefix="cycle-") as folder:
        script, output = Path(folder)/"cycle.filter", Path(folder)/"cycle.mp4"
        script.write_text(";\n".join(graph), "utf-8")
        _run([ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
              filter_file_option(ffmpeg), str(script), "-map", "[out]", "-an", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(output)], cancel,
             timeout=max(600, end*60))
        duration = probe(output, ffprobe)["duration"]
        if abs(duration - (end-fade)) > 2/float(fps):
            raise RuntimeError("叠化循环时长异常，未继续导出。")
        os.replace(output, target)
    files = sorted(cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for old in files:
        if total <= 512 * 1024**2:
            break
        if old != target:
            total -= old.stat().st_size
            old.unlink()
    return target, duration

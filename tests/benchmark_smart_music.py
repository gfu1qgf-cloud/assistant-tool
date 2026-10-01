"""Opt-in real-model comparison; no source audio changes or library index writes.

Run: python -m tests.benchmark_smart_music --output C:/path/comparison.json
The report contains candidates for listening, not an invented accuracy percentage.
"""

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np

from app_plugins.builtin.smart_music_search.audio import decode_segment, media_duration, resolve_tools
from app_plugins.builtin.smart_music_search.encoder import MusicEncoder
from app_plugins.builtin.smart_music_search.index import MusicIndex, discover_music


def main():
    parser = argparse.ArgumentParser(description="本地音乐模型对照测试（不会修改原歌曲）")
    parser.add_argument("--library", default="D:/2.配乐库")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--models", nargs="+", choices=("general", "unfused"), default=["general", "unfused"])
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates = discover_music(args.library)
    # Include a known reference and a deterministic spread through the library.
    selected = [path for path in candidates if "PYZ-懊悔自责" in Path(path).name][:1]
    # Names only select a varied TEST corpus, never score search results.
    for label in ("恐怖", "惊悚", "悬疑", "紧张", "懊悔", "悲伤", "希望", "积极", "欢快", "温暖", "庄严", "激励"):
        selected.extend([path for path in candidates if label in Path(path).stem and path not in selected][:2])
    for position in np.linspace(0, max(0, len(candidates)-1), min(len(candidates), max(1, args.limit)), dtype=int):
        path = candidates[position]
        if path not in selected:
            selected.append(path)
    selected = selected[:max(1, min(100, args.limit))]
    ffmpeg, ffprobe = resolve_tools(args.ffmpeg)
    report = {"note": "同一组音乐的模型对照。候选由音频评分产生；未经人工试听，不表示精度已提高。",
              "library_files": len(candidates), "models": {}, "failed_files": []}
    excerpts = {}
    for path in selected:
        try:
            duration = media_duration(path, ffprobe)
            if duration < 10:
                continue
            # Bound test runtime; production full coverage still indexes all normal songs.
            starts = np.linspace(0, duration-10, min(8, max(2, int(duration/10))), dtype=float)
            excerpts[path] = (duration, [(float(start), decode_segment(path, start, ffmpeg)) for start in starts])
        except Exception as error:
            report["failed_files"].append({"path": path, "error": str(error)})
    if not excerpts:
        raise RuntimeError("没有可测试的音乐")
    for model_key in args.models:
        encoder = MusicEncoder(model_key, "full")
        began = time.perf_counter()
        encoder._load_music()
        loaded = time.perf_counter() - began
        index = MusicIndex.for_encoder(encoder, output.parent / "music-benchmark-index")
        with index._connect() as db:
            db.execute("DELETE FROM tracks")  # Only this script's separate test database.
        audio_times = []
        for number, (path, (duration, samples)) in enumerate(excerpts.items(), 1):
            vectors = []
            for start, sample in samples:
                began = time.perf_counter()
                vector = encoder.audio(sample)
                audio_times.append(time.perf_counter() - began)
                vectors.append((path, start, vector.astype(np.float16).tobytes()))
            stat = Path(path).stat()
            with index._connect() as db:
                db.execute("DELETE FROM segments WHERE path=?", (path,))
                db.execute("INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?)",
                           (path, stat.st_size, stat.st_mtime_ns, duration, encoder.model_id, "", time.time()))
                db.executemany("INSERT INTO segments VALUES (?,?,?)", vectors)
            print(f"{model_key}: {number}/{len(excerpts)} 首完成", flush=True)
        queries = {}
        for query in ("恐怖", "懊悔", "懊悔自责", "希望"):
            began = time.perf_counter()
            vector, description = encoder.text(query)
            text_time = time.perf_counter() - began
            began = time.perf_counter()
            rows = index.search(encoder.model_id, vector, limit=5, stable=True)
            ranking_time = time.perf_counter() - began
            began = time.perf_counter()
            encoder.text(query)
            cached_time = time.perf_counter() - began
            queries[query] = {"text_seconds": round(text_time, 4), "ranking_seconds": round(ranking_time, 4),
                              "cached_text_seconds": round(cached_time, 6), "description": description,
                              "top_candidates": rows}
        report["models"][model_key] = {"model_id": encoder.model_id, "load_seconds": round(loaded, 3),
            "tracks": len(excerpts), "segments": len(audio_times),
            "audio_seconds_per_segment": round(float(np.median(audio_times)), 4), "queries": queries}
        del encoder
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("对照测试完成：" + str(output), flush=True)


if __name__ == "__main__":
    main()

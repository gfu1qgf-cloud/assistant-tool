"""Opt-in local model comparison; source pictures and production index are read-only.

python -m tests.benchmark_smart_image --source-index C:/path/index.sqlite3 --output C:/path/report.json
The contact sheets permit visual comparison; scores are not accuracy percentages.
"""

import argparse
import gc
import json
import os
import sqlite3
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app_plugins.builtin.smart_image_search.encoder import (
    ChineseImageEncoder, MODEL_IDS, combine_search_vectors,
)
from app_plugins.builtin.smart_image_search.index import ImageSearchIndex


QUERIES = ("耶稣向人伸出手", "玛丽亚面带微笑", "十字架", "洪水中的城市",
           "燃烧的房屋", "伤心流泪的人")


def contact_sheet(report, output):
    width, height = 170, 235
    keys = list(report["models"])
    sheet = Image.new("RGB", (width * 12, height * len(QUERIES) + 45), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 16)
    except OSError:
        font = ImageFont.load_default()
    for model_number, key in enumerate(keys):
        draw.text((model_number * width * 4 + 8, 8), key, fill="black", font=font)
        for query_number, query in enumerate(QUERIES):
            matches = report["models"][key]["queries"][query]["top_candidates"][:4]
            y = 45 + query_number * height
            draw.text((model_number * width * 4 + 5, y), query, fill="black", font=font)
            for column, row in enumerate(matches):
                x = (model_number * 4 + column) * width
                with Image.open(row["path"]) as source:
                    thumb = ImageOps.exif_transpose(source).convert("RGB")
                    thumb.thumbnail((width - 6, height - 35))
                    sheet.paste(thumb, (x + (width - thumb.width) // 2, y + 28))
                    thumb.close()
    sheet.save(output)


def main():
    parser = argparse.ArgumentParser(description="中文搜图真实模型对照（不修改原图或现有索引）")
    parser.add_argument("--source-index", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=180)
    parser.add_argument("--models", nargs="+", choices=list(MODEL_IDS), default=list(MODEL_IDS))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    test_root = output.parent / "image-benchmark-index"
    source_index = Path(args.source_index).resolve()
    if test_root == source_index.parent:
        raise ValueError("测试目录不能与生产索引目录相同")
    with sqlite3.connect(source_index.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT path,source_kind,source_name,vector FROM images WHERE vector IS NOT NULL ORDER BY path"
        ) if Path(row["path"]).is_file()]
    if not rows:
        raise ValueError("现有索引里没有可用图片")
    base = ChineseImageEncoder()
    base.prepare()
    matrix = np.stack([np.frombuffer(row["vector"], np.float16).astype(np.float32) for row in rows])
    chosen = set()
    # Include existing text candidates and a deterministic broad spread; the
    # identical corpus is used by every model. No filenames influence ranking.
    for query in QUERIES:
        chosen.update(int(i) for i in np.argsort(-(matrix @ base.text(query)))[:10])
    limit = max(len(chosen), min(500, max(1, args.limit)))
    rng = np.random.default_rng(20261001)
    for i in rng.permutation(len(rows)):
        if len(chosen) >= min(limit, len(rows)):
            break
        chosen.add(int(i))
    selected = [rows[i] for i in sorted(chosen)]
    del base, matrix
    gc.collect()
    groups = [{"source_kind": "folder", "name": "测试样本", "images": selected}]
    report = {"note": "同一批真实图片的检索候选与速度对照；未经用户标注，不表示精度百分比。",
              "existing_images": len(rows), "test_images": len(selected), "models": {}}
    for model_key in args.models:
        encoder = ChineseImageEncoder(model_key)
        start = time.perf_counter()
        encoder.prepare()
        load_time = time.perf_counter() - start
        index = ImageSearchIndex.for_encoder(encoder, test_root)
        start = time.perf_counter()
        result = index.sync(groups, encoder, batch_size=2,
            progress=lambda done, total, _message: print(f"{model_key}: {done}/{total}", flush=True)
            if done % 20 == 0 or done == total else None)
        build_time = time.perf_counter() - start
        queries = {}
        vectors = []
        for query in QUERIES:
            start = time.perf_counter()
            vector = encoder.text(query)
            vectors.append(vector)
            condition_time = time.perf_counter() - start
            start = time.perf_counter()
            matches = index.search(encoder.model_id, vector, limit=10)
            ranking_time = time.perf_counter() - start
            start = time.perf_counter()
            encoder.text(query)
            cache_time = time.perf_counter() - start
            queries[query] = {"condition_seconds": round(condition_time, 4),
                "ranking_seconds": round(ranking_time, 4), "cached_seconds": round(cache_time, 6),
                "top_candidates": matches}
        similarity = np.stack(vectors) @ np.stack(vectors).T
        off_diagonal = similarity[np.triu_indices(len(vectors), 1)]
        # Check a true reference search and one combined search, not just text.
        reference = selected[len(selected)//2]["path"]
        start = time.perf_counter()
        image_vector = encoder.image(reference)
        image_time = time.perf_counter() - start
        image_matches = index.search(encoder.model_id, image_vector, limit=5)
        combined = index.search(encoder.model_id,
            combine_search_vectors(image_vector, vectors[0], .35), limit=5)
        import torch
        report["models"][model_key] = {"model_id": encoder.model_id, "device": encoder._runtime[-1],
            "cpu_threads": torch.get_num_threads(), "load_seconds": round(load_time, 3),
            "build_seconds": round(build_time, 3), "build_result": result,
            "image_condition_seconds": round(image_time, 4), "reference": reference,
            "reference_candidates": image_matches, "combined_candidates": combined,
            "max_distinct_text_cosine": round(float(off_diagonal.max()), 5), "queries": queries}
        print(f"{model_key}: 完成，建库 {build_time:.2f}s，参考图 {image_time:.2f}s", flush=True)
        del encoder, index
        gc.collect()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    contact_sheet(report, output.with_suffix(".png"))
    print("对照完成：" + str(output), flush=True)


if __name__ == "__main__":
    main()

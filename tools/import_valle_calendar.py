"""Offline, reproducible extraction of the official 2026 raster calendar.

Developer tool only: requires pypdf and Pillow, not plugin/runtime dependencies.
No weekday recurrence is used. Each coloured date cell is decoded independently.
"""
import calendar
from collections import Counter
from datetime import date
import hashlib
import io
import json
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "app_plugins/builtin/waste_reminder/data"


def lines(image, axis):
    width, height = image.size
    positions = []
    for n in range(height if axis == "h" else width):
        samples = (image.getpixel((x, n)) for x in range(width)) if axis == "h" else (
            image.getpixel((n, y)) for y in range(24, height - 2))
        values = list(samples)
        if sum(max(p) < (65 if axis == "h" else 110) for p in values) / len(values) > (0.92 if axis == "h" else 0.86):
            positions.append(n)
    groups = []
    for n in positions:
        if not groups or n > groups[-1][-1] + 1:
            groups.append([n])
        else:
            groups[-1].append(n)
    return [round(sum(g) / len(g)) for g in groups]


def extract_images():
    reader = PdfReader(DATA / "sources/valle_lomellina_2026.pdf")
    images = [Image.open(io.BytesIO(x.data)).convert("RGB")
              for x in reader.pages[0].images if x.image.width > 1200]
    return images


PALETTE = {
    "umido": (191, 143, 0), "carta": (248, 203, 173),
    "plastica": (255, 217, 102), "secco": (191, 191, 191),
    "pannolini": (189, 215, 238), "vetro": (0, 176, 80),
    "verde": (146, 208, 80),
}


def decode(im, rect, tolerance=45):
    pixels = list(im.crop(rect).getdata())
    counts = Counter()
    for p in pixels:
        key, distance = min(((key, sum((a-b)**2 for a, b in zip(p, rgb)))
                             for key, rgb in PALETTE.items()), key=lambda x: x[1])
        if distance < tolerance ** 2:
            counts[key] += 1
    result = [key for key in PALETTE if counts[key] / len(pixels) > 0.12]
    if not result and sum(min(p) > 225 for p in pixels) / len(pixels) < 0.45:
        raise ValueError(f"Unrecognised cell: {rect}, {counts}")
    return result, dict(counts)


def read_table(im, first_month, tolerance=45):
    hs, vs = lines(im, "h"), lines(im, "v")
    if len(hs) != 33 or len(vs) != 19:
        raise ValueError(f"Unexpected grid: {len(hs)} horizontal/{len(vs)} vertical")
    entries, evidence = {}, {}
    for column in range(6):
        month = first_month + column
        for day in range(1, calendar.monthrange(2026, month)[1] + 1):
            rect = (vs[column*3+2]+3, hs[day]+3,
                    vs[column*3+3]-3, hs[day+1]-2)
            key = date(2026, month, day).isoformat()
            entries[key], counts = decode(im, rect, tolerance)
            evidence[key] = {"rect": rect, "pixels": counts}
    return entries, evidence


def generate():
    dates, evidence = {}, {}
    for im, month in zip(extract_images(), (1, 7)):
        values, proof = read_table(im, month)
        dates.update(values)
        evidence.update(proof)
    # Independently decode every date from the Comune's separately downloaded JPG.
    poster = Image.open(DATA / "sources/comune_calendar_2026.jpg").convert("RGB")
    other = {}
    for rect, month in (((40, 410, 1719, 1112), 1), ((38, 1140, 1722, 1847), 7)):
        values, _ = read_table(poster.crop(rect), month, 55)
        other.update(values)
    differences = [day for day in dates if set(dates[day]) != set(other[day])]
    if differences:
        raise ValueError(f"PDF/JPG disagreement: {differences}")
    if len(dates) != 365:
        raise ValueError("Incomplete year")
    kinds = {
        "carta": {"it": "Carta e Cartone", "zh": "纸和纸板", "emoji": "📦"},
        "umido": {"it": "Umido / Organico", "zh": "厨余垃圾", "emoji": "🍎"},
        "plastica": {"it": "Plastica", "zh": "塑料包装", "emoji": "🧴"},
        "vetro": {"it": "Vetro e Lattine", "zh": "玻璃和金属罐", "emoji": "🍾"},
        "secco": {"it": "Secco / Indifferenziato", "zh": "其他不可回收垃圾", "emoji": "🗑️"},
        "verde": {"it": "Verde", "zh": "园林垃圾", "emoji": "🌿"},
        "pannolini": {"it": "Pannolini", "zh": "尿布专项收运（请确认适用性）", "emoji": "👶",
                      "optional": True},
    }
    sources = [
        {"title": "TeknoService 官方年度日历（含分类说明）", "file": "sources/valle_lomellina_2026.pdf",
         "url": "https://teknoserviceitalia.b-cdn.net/wp-content/uploads/2025/12/Calendario-raccolta-rifiuti-2026-VALLE-LOMELLINA.pdf"},
        {"title": "Comune 官方日历图片", "file": "sources/comune_calendar_2026.jpg",
         "url": "https://www.comune.vallelomellina.pv.it/it-it/download/calendario-414824-4-1505-62c17669ba75c826672a734dda3ac1ef"},
        {"title": "Comune 官方分类说明图片", "file": "sources/comune_instructions_2026.jpg",
         "url": "https://www.comune.vallelomellina.pv.it/it-it/download/istruzioni-414825-4-1505-297d28c27a2a1199bee8c97a406836a2"},
        {"title": "2026 年 7 月起禁止黑袋：新通知优先于旧图例", "file": "sources/notice_black_bags_2026.pdf",
         "url": "https://www.teknoserviceitalia.com/wp-content/uploads/2026/05/Avviso-sacchi-neri-Valle-lomellina-1.pdf"},
        {"title": "TeknoService 当地服务网页", "file": "sources/teknoservice_local_page.html",
         "url": "https://www.teknoserviceitalia.com/lombardia/pavia/valle-lomellina/"},
    ]
    for source in sources:
        source["sha256"] = hashlib.sha256((DATA/source["file"]).read_bytes()).hexdigest()
    document = {"schema_version": 1, "commune": "Valle Lomellina (PV)", "year": 2026,
                "timezone": "Europe/Rome", "provider": "TeknoService",
                "verified_on": "2026-10-01", "exposure": {"from": "22:00", "until": "06:00"},
                "types": kinds, "sources": sources, "dates": dates,
                "guidance": json.loads((DATA/"guidance_2026.json").read_text(encoding="utf-8")),
                "extraction": {"method": "PDF raster-cell colour decoding, all 365 dates cross-checked against official Comune JPG",
                               "dates_cross_checked": 365, "disagreements": 0}}
    (DATA/"schedule_2026.json").write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    report = {"palette": PALETTE, "counts": dict(Counter(x for v in dates.values() for x in v)),
              "dates": evidence, "pdf_jpg_disagreements": differences}
    (DATA/"extraction_report_2026.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("Validated", len(dates), "dates; collections:", report["counts"])


if __name__ == "__main__":
    generate()

"""TeknoService's two six-month colour grids -> explicit dated collections.

Strict adapter, not a universal PDF guesser. Unknown layouts fail closed.
"""
import calendar
from collections import Counter
from datetime import date
import io
import re
import json
from pathlib import Path

PALETTE = {
    "umido": (191, 143, 0), "carta": (248, 203, 173),
    "plastica": (255, 217, 102), "secco": (191, 191, 191),
    "pannolini": (189, 215, 238), "vetro": (0, 176, 80),
    "verde": (146, 208, 80),
}
TYPES = {
    "carta": {"it": "Carta e Cartone", "zh": "纸和纸板", "emoji": "📦"},
    "umido": {"it": "Umido / Organico", "zh": "厨余垃圾", "emoji": "🍎"},
    "plastica": {"it": "Plastica", "zh": "塑料包装", "emoji": "🧴"},
    "vetro": {"it": "Vetro e Lattine", "zh": "玻璃和金属罐", "emoji": "🍾"},
    "secco": {"it": "Secco / Indifferenziato", "zh": "其他不可回收垃圾", "emoji": "🗑️"},
    "verde": {"it": "Verde", "zh": "园林垃圾", "emoji": "🌿"},
    "pannolini": {"it": "Pannolini", "zh": "尿布专项（请确认适用性）", "emoji": "👶", "optional": True},
}


class ParseError(ValueError):
    pass


def grid_lines(im, axis):
    import numpy as np
    pixels = np.asarray(im.convert("RGB"))
    black = pixels.max(axis=2) < (65 if axis == "h" else 110)
    scores = black.mean(axis=1) if axis == "h" else black[24:-2].mean(axis=0)
    positions = np.flatnonzero(scores > (0.92 if axis == "h" else 0.86)).tolist()
    groups = []
    for n in positions:
        if not groups or n > groups[-1][-1] + 1:
            groups.append([n])
        else:
            groups[-1].append(n)
    return [round(sum(g) / len(g)) for g in groups]


def read_table(im, year, first_month, tolerance=45):
    import numpy as np
    hs, vs = grid_lines(im, "h"), grid_lines(im, "v")
    if len(hs) != 33 or len(vs) != 19:
        raise ParseError("日历布局不是可识别的六个月、31 日网格；不能可靠导入。")
    if max(np.diff(hs)) > min(np.diff(hs)) * 1.25:
        raise ParseError("日期行高不一致，需检查新版日历结构。")
    palette = np.asarray(list(PALETTE.values()), dtype=np.int32)
    templates_path = Path(__file__).parent/"data/weekday_templates.json"
    templates = json.loads(templates_path.read_text(encoding="utf-8")) if templates_path.exists() else None
    if templates is None:
        raise ParseError("缺少星期校验模板，不能可靠判断月份顺序。")
    dates = {}
    for column in range(6):
        month = first_month + column
        for day in range(1, 32):
            rect = (vs[column*3+2]+3, hs[day]+3, vs[column*3+3]-3, hs[day+1]-2)
            pixels = np.asarray(im.crop(rect).convert("RGB"), dtype=np.int32).reshape(-1, 3)
            distances = ((pixels[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2)
            nearest = distances.argmin(axis=1)
            matched = distances.min(axis=1) < tolerance ** 2
            counts = np.bincount(nearest[matched], minlength=len(palette)) / len(pixels)
            values = [key for i, key in enumerate(PALETTE) if counts[i] > 0.12]
            white = (pixels.min(axis=1) > 225).mean()
            coloured = sum(counts[i] for i, _ in enumerate(PALETTE) if counts[i] > 0.12)
            if not values and white < 0.45 or values and coloured < 0.40:
                raise ParseError(f"{year}-{month:02d}-{day:02d} 颜色无法可靠识别，停止导入。")
            if day > calendar.monthrange(year, month)[1]:
                if values:
                    raise ParseError("月份天数不一致，可能选错年份或月份顺序。")
                continue
            weekday_rect = (vs[column*3+1]+3, hs[day]+3, vs[column*3+2]-3, hs[day+1]-2)
            glyph = weekday_glyph(im.crop(weekday_rect))
            scores = {int(key): min(float(np.not_equal(glyph, np.asarray(sample)).mean())
                                    for sample in samples) for key, samples in templates.items()}
            best = min(scores, key=scores.get)
            expected_weekday = date(year, month, day).weekday()
            if best != expected_weekday or scores[best] > 0.28:
                raise ParseError(f"{year}-{month:02d}-{day:02d} 的星期与年份不匹配；月份顺序/日历版式可能变化，停止导入。")
            dates[date(year, month, day).isoformat()] = values
    return dates


def weekday_glyph(image):
    import numpy as np
    from PIL import Image
    mask = np.asarray(image.convert("RGB")).min(axis=2) < 128
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ParseError("星期单元格为空。")
    tight = Image.fromarray((mask[ys.min():ys.max()+1, xs.min():xs.max()+1]*255).astype("uint8"))
    return np.asarray(tight.resize((40, 16), Image.Resampling.NEAREST)) > 128


def parse_pdf(pdf_bytes, year, commune="Valle Lomellina"):
    try:
        from pypdf import PdfReader
        from PIL import Image
    except ImportError as error:
        raise ParseError("官网解析需要 pypdf；请安装更新后的 requirements.txt。") from error
    if len(pdf_bytes) > 25 * 1024 * 1024 or not pdf_bytes.startswith(b"%PDF"):
        raise ParseError("不是有效 PDF，或文件超过 25 MB。")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not 1 <= len(reader.pages) <= 8:
        raise ParseError("PDF 页数异常，不能当成年度日历。")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    collapsed = re.sub(r"\s+", "", text).casefold()
    if str(year) not in collapsed or re.sub(r"[^a-z]", "", commune.casefold()) not in re.sub(r"[^a-z]", "", text.casefold()):
        raise ParseError("PDF 内的年份或 Comune 与目标不符，不导入。")
    if "calendario" not in collapsed or "22.00" not in collapsed or "06.00" not in collapsed:
        raise ParseError("无法验证年度日历标题或投放时间；需新的解析适配。")
    legend = re.sub(r"[^a-z0-9]", "", text.casefold())
    if any(name not in legend for name in ("cartaecartone", "vetroelattine", "umidoorganico", "seccoindifferenziato")):
        raise ParseError("分类图例有变化，不能沿用旧颜色与垃圾名称的对应关系。")
    candidates = []
    # Order within the first page is the publisher's Jan-Jun, Jul-Dec order.
    for embedded in reader.pages[0].images:
        if embedded.image.width < 600 or embedded.image.height < 250:
            continue
        im = Image.open(io.BytesIO(embedded.data)).convert("RGB")
        if len(grid_lines(im, "h")) == 33 and len(grid_lines(im, "v")) == 19:
            candidates.append(im)
    if len(candidates) != 2:
        raise ParseError("未找到两个完整的半年日历网格；官网可能改版，旧数据未改动。")
    dates = {}
    for im, month in zip(candidates, (1, 7)):
        dates.update(read_table(im, year, month))
    expected = 366 if calendar.isleap(year) else 365
    if len(dates) != expected:
        raise ParseError("年度日期不完整。")
    totals = Counter(x for values in dates.values() for x in values)
    if any(not 1 <= totals.get(key, 0) <= 200 for key in TYPES):
        raise ParseError("类别计数异常，不能确认解析结果。")
    return {"schema_version": 1, "commune": commune + " (PV)", "year": year,
            "timezone": "Europe/Rome", "provider": "TeknoService",
            "types": TYPES, "dates": dates, "sources": [],
            "exposure": {"from": "22:00", "until": "06:00"},
            "extraction": {"method": "official PDF coloured date cells, strict grid/year/Comune validation",
                           "days": expected, "collection_counts": dict(totals)}}

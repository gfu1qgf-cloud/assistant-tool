"""Generate tiny raster weekday-recognition templates from saved official data."""
from datetime import date
import calendar
import importlib.util
import io
import json
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("waste_parser", ROOT/"app_plugins/builtin/waste_reminder/parser.py")
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)
DATA = ROOT/"app_plugins/builtin/waste_reminder/data"


def main():
    images = [Image.open(io.BytesIO(x.data)).convert("RGB")
              for x in PdfReader(DATA/"sources/valle_lomellina_2026.pdf").pages[0].images if x.image.width > 1200]
    samples = {i: [] for i in range(7)}
    for im, month in zip(images, (1, 7)):
        hs, vs = parser.grid_lines(im, "h"), parser.grid_lines(im, "v")
        for column in range(6):
            for day in range(1, calendar.monthrange(2026, month+column)[1]+1):
                rect = (vs[column*3+1]+3, hs[day]+3, vs[column*3+2]-3, hs[day+1]-2)
                sample = parser.weekday_glyph(im.crop(rect)).astype(int).tolist()
                values = samples[date(2026, month+column, day).weekday()]
                if sample not in values:
                    values.append(sample)
    (DATA/"weekday_templates.json").write_text(json.dumps(samples, separators=(",", ":"))+"\n", encoding="utf-8")
    print("Generated weekday verification templates")


if __name__ == "__main__":
    main()

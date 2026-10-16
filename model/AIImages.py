"""Shared, bounded local image normalization for chat and workflow requests."""
import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps
from model.AppDataStorage import data_file

MAX_SOURCE_BYTES = 32 * 1024 * 1024


def normalize_image(source):
    if isinstance(source, bytes):
        raw = source
    else:
        path = Path(source)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("图片不存在或超过32MB：" + path.name)
        raw = data_file(path).read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("图片超过32MB，请先缩小。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as original:
            if original.width * original.height > 40000000:
                raise ValueError("图片超过4000万像素，请先缩小。")
            original.seek(0)
            image = ImageOps.exif_transpose(original)
            image = image.convert("RGBA" if image.mode in {"RGBA", "LA"} or "transparency" in image.info else "RGB")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            suffix, mime = ("png", "image/png") if image.mode == "RGBA" else ("jpg", "image/jpeg")
            for _ in range(3):
                output = io.BytesIO()
                image.save(output, format="PNG" if suffix == "png" else "JPEG",
                           **({} if suffix == "png" else {"quality": 92}))
                normalized = output.getvalue()
                if len(normalized) <= 3 * 1024 * 1024:
                    break
                image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)
            if len(normalized) > 3 * 1024 * 1024:
                raise ValueError("图片压缩后仍过大，请先缩小。")
    return normalized, suffix, mime, image.width, image.height

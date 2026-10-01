"""Lazy Chinese image/text encoder. No model is loaded during app startup."""

import os
from model.ModelFeatures import feature_tensor
from collections import OrderedDict
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

MODEL_REVISION = "f4a64596bbcf9a2a94591b74b9dc39b2e4e77e3e"
MODEL_SPECS = {
    "base": {
        "repository": "OFA-Sys/chinese-clip-vit-base-patch16",
        "revision": MODEL_REVISION,
        "label": "中文 CLIP Base（原版）", "download": "约 753 MB",
    },
    "large": {
        "repository": "OFA-Sys/chinese-clip-vit-large-patch14",
        "revision": "e84ea94303a56200300792613c510894a4c6f9f5",
        "label": "中文 CLIP Large", "download": "约 1.63 GB",
    },
    "large336": {
        "repository": "OFA-Sys/chinese-clip-vit-large-patch14-336px",
        "revision": "7543eecd94b8736b8ccc315153553905f31e6749",
        "label": "中文 CLIP Large／336（精细）", "download": "约 1.63 GB",
    },
}
# All revisions pin SafeTensors conversions in the original model repositories.
MODEL_IDS = {key: spec["repository"] for key, spec in MODEL_SPECS.items()}


def combine_search_vectors(image_vector, text_vector=None, text_weight=0.35):
    """Weighted semantic preference, not a hard filter or image-edit instruction."""
    import numpy as np

    def normalized(value):
        vector = np.asarray(value, dtype=np.float32).reshape(-1)
        length = float(np.linalg.norm(vector))
        if not np.all(np.isfinite(vector)) or not np.isfinite(length) or length <= 0:
            raise ValueError("搜索条件包含无效特征")
        return vector / length

    image = normalized(image_vector)
    if text_vector is None:
        return image
    text = normalized(text_vector)
    if image.shape != text.shape:
        raise ValueError("图片与文字特征不属于同一个模型")
    weight = float(text_weight)
    if not np.isfinite(weight) or not 0 <= weight <= 1:
        raise ValueError("文字侧重必须在 0% 到 100% 之间")
    return normalized((1 - weight) * image + weight * text)


class ModelLoadError(RuntimeError):
    pass


class ChineseImageEncoder:
    def __init__(self, model_key="base"):
        if model_key not in MODEL_IDS:
            raise ValueError("不支持的中文搜图模型")
        self.model_key = model_key
        self.repository_id = MODEL_IDS[model_key]
        self.revision = MODEL_SPECS[model_key]["revision"]
        self.model_id = f"{self.repository_id}@{self.revision}"
        self._runtime = None
        self._query_cache = OrderedDict()

    @property
    def is_loaded(self):
        return self._runtime is not None

    def prepare(self):
        """Load once so the UI can report model loading separately from encoding."""
        self._load()

    def model_is_cached(self):
        try:
            from huggingface_hub import try_to_load_from_cache
            cached = try_to_load_from_cache(
                self.repository_id, "model.safetensors",
                revision=self.revision,
            )
            return isinstance(cached, str) and Path(cached).is_file()
        except Exception:
            return False

    def _load(self):
        if self._runtime is None:
            try:
                import torch
                from PIL import Image, ImageOps
                from transformers import ChineseCLIPModel, ChineseCLIPProcessor
            except Exception as error:
                raise ModelLoadError(f"中文搜图模型依赖无法加载：{error}") from error
            try:
                processor = ChineseCLIPProcessor.from_pretrained(
                    self.repository_id, revision=self.revision,
                    use_fast=False,
                )
                model = ChineseCLIPModel.from_pretrained(
                    self.repository_id, revision=self.revision,
                    use_safetensors=True,
                ).eval()
            except Exception as error:
                raise ModelLoadError(
                    f"中文模型 {self.model_id} 加载失败。首次使用需要下载模型；"
                    f"请检查网络或模型缓存。原始错误：{error}"
                ) from error
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = model.to(device)
            self._runtime = (torch, Image, ImageOps, processor, model, device)
        return self._runtime

    @staticmethod
    def _normalize(features):
        import numpy as np
        vector = features.detach().cpu().numpy().astype(np.float32).reshape(-1)
        length = float(np.linalg.norm(vector))
        if not np.all(np.isfinite(vector)) or not np.isfinite(length) or length <= 0:
            raise ValueError("模型返回了无效特征")
        return vector / length

    def images(self, paths):
        torch, Image, ImageOps, processor, model, device = self._load()
        images = []
        try:
            for path in paths:
                with Image.open(path) as original:
                    images.append(ImageOps.exif_transpose(original).convert("RGB"))
            inputs = processor(images=images, return_tensors="pt")
            with torch.inference_mode():
                features = feature_tensor(model.get_image_features(
                    pixel_values=inputs.pixel_values.to(device)
                ))
            return [self._normalize(row) for row in features]
        finally:
            for image in images:
                image.close()

    def text(self, query):
        query = str(query).strip()
        if not query:
            raise ValueError("请输入搜索描述")
        key = ("text", query)
        cached = self._cached(key)
        if cached is not None:
            return cached
        torch, _Image, _ImageOps, processor, model, device = self._load()
        inputs = processor(text=[query], return_tensors="pt", padding=True,
                           truncation=True,
                           max_length=model.config.text_config.max_position_embeddings).to(device)
        with torch.inference_mode():
            # Transformers 4.57 constructs this text model without a pooler,
            # so get_text_features() projects None. Match ChineseCLIPModel.forward:
            # project the final CLS token into the shared image/text space.
            hidden = model.text_model(**inputs).last_hidden_state[:, 0, :]
            features = model.text_projection(hidden)
        return self._remember(key, self._normalize(features[0]))

    def image(self, path):
        path = Path(path).resolve()
        stat = path.stat()
        key = ("image", os.path.normcase(str(path)), stat.st_size, stat.st_mtime_ns)
        cached = self._cached(key)
        if cached is not None:
            return cached
        return self._remember(key, self.images([path])[0])

    def _cached(self, key):
        value = self._query_cache.get(key)
        if value is not None:
            self._query_cache.move_to_end(key)
            return value.copy()
        return None

    def _remember(self, key, vector):
        self._query_cache[key] = vector.copy()
        self._query_cache.move_to_end(key)
        while len(self._query_cache) > 64:
            self._query_cache.popitem(last=False)
        return vector.copy()

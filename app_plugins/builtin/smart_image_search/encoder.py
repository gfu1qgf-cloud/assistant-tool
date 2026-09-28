"""Lazy Chinese image/text encoder. No model is loaded during app startup."""

import os
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

MODEL_IDS = {
    "base": "OFA-Sys/chinese-clip-vit-base-patch16",
}
MODEL_REVISION = "f4a64596bbcf9a2a94591b74b9dc39b2e4e77e3e"


class ModelLoadError(RuntimeError):
    pass


class ChineseImageEncoder:
    def __init__(self, model_key="base"):
        if model_key not in MODEL_IDS:
            raise ValueError("不支持的中文搜图模型")
        self.model_key = model_key
        self.repository_id = MODEL_IDS[model_key]
        self.model_id = f"{self.repository_id}@{MODEL_REVISION}"
        self._runtime = None

    def model_is_cached(self):
        try:
            from huggingface_hub import try_to_load_from_cache
            cached = try_to_load_from_cache(
                self.repository_id, "model.safetensors",
                revision=MODEL_REVISION,
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
                    self.repository_id, revision=MODEL_REVISION,
                    use_fast=False,
                )
                model = ChineseCLIPModel.from_pretrained(
                    self.repository_id, revision=MODEL_REVISION,
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
        if not length:
            raise ValueError("模型返回了空特征")
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
                features = model.get_image_features(
                    pixel_values=inputs.pixel_values.to(device)
                )
            return [self._normalize(row) for row in features]
        finally:
            for image in images:
                image.close()

    def text(self, query):
        torch, _Image, _ImageOps, processor, model, device = self._load()
        inputs = processor(text=[str(query)], return_tensors="pt", padding=True).to(device)
        with torch.inference_mode():
            # Transformers 4.57 constructs this text model without a pooler,
            # so get_text_features() projects None. Match ChineseCLIPModel.forward:
            # project the final CLS token into the shared image/text space.
            hidden = model.text_model(**inputs).last_hidden_state[:, 0, :]
            features = model.text_projection(hidden)
        return self._normalize(features[0])

    def image(self, path):
        return self.images([path])[0]

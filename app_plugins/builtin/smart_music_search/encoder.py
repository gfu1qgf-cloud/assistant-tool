"""Pinned, local CLAP music embedding and Chinese-to-English translation."""

import os
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

import numpy as np

MUSIC_MODEL = "laion/larger_clap_music"
MUSIC_REVISION = "4189a948e5e2aaa5df3e69da506e54513e977191"  # SafeTensors conversion.
TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-zh-en"
TRANSLATION_REVISION = "fd5d2c871cd77baae0ec4beb338a3f3eee806af0"  # SafeTensors conversion.
MODEL_ID = f"{MUSIC_MODEL}@{MUSIC_REVISION}:10s:v1"


def _normal(vector):
    vector = vector.detach().cpu().numpy().astype(np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        raise ValueError("模型返回了空特征")
    return vector / norm


class MusicEncoder:
    def __init__(self):
        self.model_id = MODEL_ID
        self._music = None
        self._translation = None

    @staticmethod
    def _cached(repository, revision):
        try:
            from huggingface_hub import try_to_load_from_cache
            result = try_to_load_from_cache(repository, "model.safetensors", revision=revision)
            return isinstance(result, str) and Path(result).is_file()
        except Exception:
            return False

    def music_is_cached(self):
        return self._cached(MUSIC_MODEL, MUSIC_REVISION)

    def translation_is_cached(self):
        return self._cached(TRANSLATION_MODEL, TRANSLATION_REVISION)

    def _load_music(self):
        if self._music is None:
            import torch
            from transformers import ClapModel, ClapProcessor
            processor = ClapProcessor.from_pretrained(MUSIC_MODEL, revision=MUSIC_REVISION)
            model = ClapModel.from_pretrained(
                MUSIC_MODEL, revision=MUSIC_REVISION, use_safetensors=True
            ).eval().to("cpu")
            self._music = torch, processor, model
        return self._music

    def _load_translation(self):
        if self._translation is None:
            import torch
            from transformers import MarianMTModel, MarianTokenizer
            tokenizer = MarianTokenizer.from_pretrained(
                TRANSLATION_MODEL, revision=TRANSLATION_REVISION
            )
            model = MarianMTModel.from_pretrained(
                TRANSLATION_MODEL, revision=TRANSLATION_REVISION,
                use_safetensors=True,
            ).eval().to("cpu")
            self._translation = torch, tokenizer, model
        return self._translation

    def translate(self, query):
        query = str(query).strip()
        if not any("\u3400" <= char <= "\u9fff" for char in query):
            return query
        torch, tokenizer, model = self._load_translation()
        tokens = tokenizer([query], return_tensors="pt", truncation=True, max_length=256)
        with torch.inference_mode():
            output = model.generate(**tokens, max_new_tokens=128)
        return tokenizer.batch_decode(output, skip_special_tokens=True)[0].strip()

    def audio(self, samples):
        torch, processor, model = self._load_music()
        inputs = processor(audios=[samples], sampling_rate=48000, return_tensors="pt")
        with torch.inference_mode():
            return _normal(model.get_audio_features(**inputs)[0])

    def text(self, query):
        english = self.translate(query)
        torch, processor, model = self._load_music()
        # This checkpoint has a very large common "generic music" component:
        # unrelated captions otherwise produce almost identical unit vectors.
        # Contrast the requested description with a neutral music caption.
        inputs = processor(text=[english, "music"], return_tensors="pt", padding=True)
        with torch.inference_mode():
            features = model.get_text_features(**inputs)
        vector = _normal(features[0]) - _normal(features[1])
        length = float(np.linalg.norm(vector))
        if length < 1e-5:
            raise ValueError("搜索描述过于笼统，请补充情绪、乐器或场景。")
        vector /= length
        return vector, english

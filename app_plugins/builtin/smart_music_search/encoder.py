"""Pinned, local CLAP music embedding and Chinese-to-English translation."""

import os
from model.ModelFeatures import feature_tensor
from collections import OrderedDict
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

import numpy as np

MUSIC_MODEL = "laion/larger_clap_general"
MUSIC_REVISION = "16c8cc3159a3c8a31e8ff5ef1f66b0d9ab3667db"  # SafeTensors conversion.
TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-zh-en"
TRANSLATION_REVISION = "fd5d2c871cd77baae0ec4beb338a3f3eee806af0"  # SafeTensors conversion.
MODEL_ID = f"{MUSIC_MODEL}@{MUSIC_REVISION}:10s:v2"
MODEL_SPECS = {
    "general": {
        "label": "大型 CLAP（推荐）", "repository": MUSIC_MODEL,
        "revision": MUSIC_REVISION,
    },
    "unfused": {
        "label": "CLAP Unfused（对照模型）", "repository": "laion/clap-htsat-unfused",
        "revision": "79b58ed25fc00386262a2bea4b19fd21dc4310a0",
    },
}
# larger_clap_music was tested and rejected: its text tower collapses unrelated
# descriptions to cosine similarity ~0.999 (MTEB issue #5069). Do not re-add it
# just because its model card advertises a music-specialized checkpoint.
COVERAGE_LABELS = {
    "full": "精细覆盖（推荐）",
    "balanced": "均衡覆盖",
    "legacy": "旧版抽样（对照）",
}

# The translation checkpoint mistakes the isolated Chinese mood “恐怖” for
# “horrible” (bad quality), rather than “horror” (the requested soundtrack).
# Disambiguate the query only; ranking always uses the decoded audio vectors.
MOOD_QUERIES = {
    "恐怖": "ominous horror soundtrack with eerie suspense and frightening tension",
    "恐怖音乐": "ominous horror soundtrack with eerie suspense and frightening tension",
    "惊悚": "ominous suspenseful thriller soundtrack",
    "惊悚音乐": "ominous suspenseful thriller soundtrack",
    "懊悔": "sad reflective instrumental music with a feeling of regret and longing",
    "懊悔自责": "deeply sorrowful remorseful instrumental music, heavy sadness and emotional pain",
    "悲伤": "sad melancholic instrumental music",
    "忧郁": "melancholic reflective instrumental music",
}

# These are acoustic descriptions, not guesses based on the library filenames.
# Keeping the phrases visible in the UI makes the Chinese presets predictable.
MOOD_TAGS = {
    "懊悔": "sad reflective regretful",
    "懊悔自责": "deeply sorrowful remorseful emotional pain",
    "哀伤": "sad melancholic",
    "忧郁": "melancholic reflective",
    "恐怖": "ominous eerie",
    "紧张": "tense suspenseful",
    "孤独": "lonely wistful",
    "庄严": "solemn reverent",
    "温暖": "warm gentle",
    "希望": "hopeful uplifting",
    "激昂": "dramatic energetic",
    "平静": "calm peaceful",
    "欢快": "joyful upbeat",
    "神秘": "mysterious atmospheric",
}
SOUND_TAGS = {
    "钢琴": "piano",
    "弦乐": "strings",
    "管弦乐": "orchestral",
    "合唱": "choir",
    "吉他": "guitar",
    "电子": "electronic",
}
for _label, _description in MOOD_TAGS.items():
    MOOD_QUERIES.setdefault(_label, f"{_description} instrumental music")
for _label, _description in SOUND_TAGS.items():
    MOOD_QUERIES.setdefault(_label, f"{_description} music")


def build_music_prompt(query, moods=(), sounds=(), translate=None):
    """Compose selected acoustic qualities without translating the preset labels."""
    query = str(query or "").strip()
    moods = tuple(moods)
    sounds = tuple(sounds)
    parts = []
    if query and query not in moods and query not in sounds:
        parts.append((translate(query) if translate else query).strip().rstrip("."))
    parts.extend(MOOD_TAGS[label] for label in moods if label in MOOD_TAGS)
    parts.extend(SOUND_TAGS[label] for label in sounds if label in SOUND_TAGS)
    if moods or sounds:
        parts.append("music" if sounds else "instrumental music")
    return " ".join(part for part in parts if part).strip()


def _normal(vector):
    vector = vector.detach().cpu().numpy().astype(np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(vector).all() or not np.isfinite(norm) or norm == 0:
        raise ValueError("模型返回了空特征")
    return vector / norm


class MusicEncoder:
    def __init__(self, model_key="general", coverage="full"):
        self.model_key = model_key if model_key in MODEL_SPECS else "general"
        self.coverage = coverage if coverage in COVERAGE_LABELS else "full"
        self.spec = MODEL_SPECS[self.model_key]
        self.model_id = (
            MODEL_ID if self.model_key == "general" and self.coverage == "legacy"
            else f"{self.spec['repository']}@{self.spec['revision']}:10s:{self.coverage}:v3"
        )
        self._music = None
        self._translation = None
        self._text_cache = OrderedDict()

    @property
    def label(self):
        return self.spec["label"]

    def segment_starts(self, duration):
        from .audio import segment_starts
        return segment_starts(duration, self.coverage)

    @staticmethod
    def _cached(repository, revision):
        try:
            from huggingface_hub import try_to_load_from_cache
            result = try_to_load_from_cache(repository, "model.safetensors", revision=revision)
            return isinstance(result, str) and Path(result).is_file()
        except Exception:
            return False

    def music_is_cached(self):
        return self._cached(self.spec["repository"], self.spec["revision"])

    def translation_is_cached(self):
        return self._cached(TRANSLATION_MODEL, TRANSLATION_REVISION)

    @staticmethod
    def needs_translation(query):
        query = str(query).strip()
        return query not in MOOD_QUERIES and any(
            "\u3400" <= char <= "\u9fff" for char in query
        )

    def _load_music(self):
        if self._music is None:
            import torch
            from transformers import ClapModel, ClapProcessor
            processor = ClapProcessor.from_pretrained(
                self.spec["repository"], revision=self.spec["revision"]
            )
            model = ClapModel.from_pretrained(
                self.spec["repository"], revision=self.spec["revision"], use_safetensors=True
            ).eval().to("cpu")
            with torch.inference_mode():
                features = feature_tensor(model.get_text_features(**processor(
                    text=["a heavy metal guitar solo", "a slow sad piano ballad", "a female opera singer"],
                    return_tensors="pt", padding=True, truncation=True,
                )))
            self._validate_text_features(features)
            self._music = torch, processor, model
        return self._music

    @staticmethod
    def _validate_text_features(features):
        vectors = np.stack([_normal(vector) for vector in features])
        similarities = vectors @ vectors.T
        pairs = similarities[np.triu_indices(len(vectors), 1)]
        if len(pairs) < 3 or float(np.min(pairs)) > 0.98:
            raise RuntimeError("音乐模型文本编码异常：不同描述得到几乎相同的特征，已停止检索。请切换模型，避免产生误导结果。")
        return float(np.mean(pairs))

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
        if query in MOOD_QUERIES:
            return MOOD_QUERIES[query]
        if not self.needs_translation(query):
            return query
        torch, tokenizer, model = self._load_translation()
        tokens = tokenizer([query], return_tensors="pt", truncation=True, max_length=256)
        with torch.inference_mode():
            output = model.generate(**tokens, max_new_tokens=128)
        return tokenizer.batch_decode(output, skip_special_tokens=True)[0].strip()

    def audio(self, samples):
        torch, processor, model = self._load_music()
        inputs = processor(audio=[samples], sampling_rate=48000, return_tensors="pt")
        with torch.inference_mode():
            return _normal(feature_tensor(model.get_audio_features(**inputs))[0])

    def text(self, query):
        key = str(query).strip()
        if key in self._text_cache:
            self._text_cache.move_to_end(key)
            vector, english = self._text_cache[key]
            return vector.copy(), english
        english = self.translate(query)
        torch, processor, model = self._load_music()
        prompts = [english]
        if self.coverage != "legacy":
            prompts.extend((f"Music described as: {english}",
                            f"The sound and mood of this music: {english}"))
        inputs = processor(text=prompts, return_tensors="pt", padding=True,
                           truncation=True, max_length=128)
        with torch.inference_mode():
            features = feature_tensor(model.get_text_features(**inputs))
        vectors = np.stack([_normal(vector) for vector in features])
        vector = vectors.mean(axis=0)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm < 1e-8:
            raise ValueError("模型返回了无效文本特征")
        vector = (vector / norm).astype(np.float32)
        self._text_cache[key] = (vector.copy(), english)
        if len(self._text_cache) > 128:
            self._text_cache.popitem(last=False)
        return vector, english

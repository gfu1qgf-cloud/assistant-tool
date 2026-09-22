from __future__ import annotations

import copy
import os
import shutil
import threading
from pathlib import Path


# Transformers otherwise probes the broken legacy TensorFlow installation on
# this workstation while importing CLIP.  This plugin only uses PyTorch.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")


IMAGE_CLASSIFIER_CONFIG_KEY = "image_classifier_settings"
PENDING_CATEGORY = "99_待人工确认"
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif",
}
MODEL_CHOICES = {
    "large": "openai/clip-vit-large-patch14",
    "base": "openai/clip-vit-base-patch32",
}
PROMPT_TEMPLATES = (
    "a clear photo of {}.",
    "a realistic scene of {}.",
    "a cinematic video frame of {}.",
    "a classic painting of {}.",
    "a digital artwork depicting {}.",
    "an illustration of {}.",
)


DEFAULT_CATEGORY_TREE = {
    "01_宗教与信仰": {
        "01_耶稣_受难与十字架": [
            "Jesus carrying a wooden cross",
            "Jesus crucified on the cross",
            "Jesus wearing a crown of thorns with Roman soldiers",
            "a wounded shirtless Jesus wearing a crown of thorns during the Passion",
            "Jesus after being scourged with wounds and Roman soldiers behind him",
        ],
        "02_耶稣_复活与升天": [
            "Jesus walking out of an empty stone tomb",
            "the resurrection of Jesus Christ",
            "Jesus ascending into heaven among clouds",
            "the risen Jesus standing on a mountain among bright clouds with open arms",
            "the resurrected Christ in white and red robes surrounded by heavenly clouds",
        ],
        "03_耶稣_传道与教导": [
            "Jesus preaching to a crowd",
            "Jesus teaching his disciples",
            "Jesus speaking to people beside the Sea of Galilee",
        ],
        "04_耶稣_带领与牧养": [
            "Jesus leading people along a road",
            "Jesus as a shepherd leading sheep",
            "Jesus protecting and comforting a person",
        ],
        "05_耶稣_神迹与生平": [
            "Jesus walking on water",
            "Jesus being baptized in a river",
            "Jesus riding a donkey into Jerusalem",
            "Jesus riding a white horse in heaven",
            "baby Jesus in a manger nativity scene",
            "Jesus healing a sick person",
        ],
        "06_耶稣_肖像与雕塑": [
            "a close portrait of Jesus Christ",
            "a stone statue of Jesus Christ",
            "a sculpture of the Sacred Heart of Jesus",
        ],
        "07_圣母与朝圣": [
            "the Virgin Mary holding baby Jesus",
            "a statue of the Virgin Mary",
            "pilgrims praying at the Lourdes grotto",
            "pilgrims gathered outside the Lourdes basilica sanctuary",
            "pilgrims holding lit candles in prayer",
        ],
        "08_祈祷与敬拜": [
            "a person praying with folded hands",
            "a group of Christians worshiping together",
            "people kneeling in prayer inside a church",
        ],
        "09_教堂与宗教仪式": [
            "a large Catholic church interior",
            "the exterior of a Christian church",
            "a Catholic mass ceremony at an altar",
            "a priest celebrating mass",
        ],
        "10_圣经与宗教物件": [
            "an open Bible with a wooden cross",
            "a rosary and Christian cross",
            "a glowing Christian cross in the sky",
        ],
        "11_天堂与地狱寓意": [
            "two roads splitting toward heaven and hell",
            "people choosing between a bright heavenly path and a dark hell path",
            "a golden staircase rising to heaven beside black stairs descending into fire",
            "the gates of heaven contrasted with flames and the gates of hell",
        ],
        "12_天使与天堂意象": [
            "an angel with wings in heaven",
            "golden gates of heaven among clouds",
            "a person walking toward heavenly light",
        ],
    },
    "02_自然灾害": {
        "01_冰雹与极端天气": [
            "a severe hailstorm hitting a street",
            "giant hailstones covering the ground",
            "a violent thunderstorm over a city",
        ],
        "02_洪水与海啸": [
            "a severe flood disaster in a town",
            "people escaping from rising flood water",
            "a huge tsunami wave approaching a city",
        ],
        "03_地震与坍塌": [
            "an earthquake destroying buildings",
            "collapsed buildings and earthquake rubble",
            "a massive landslide falling on a town",
        ],
        "04_火山与熔岩": [
            "a volcanic eruption with ash and lava",
            "molten lava flowing from a volcano",
        ],
        "05_龙卷风与飓风": [
            "a tornado touching down near houses",
            "a hurricane with violent wind and rain",
        ],
        "06_山火与干旱": [
            "a large wildfire burning a forest",
            "a drought with dry cracked earth",
        ],
        "07_暴雪与雪崩": [
            "a heavy blizzard covering a town",
            "a large snow avalanche on a mountain",
        ],
    },
    "03_自然风景与生态": {
        "01_海岸与海洋风光": [
            "a beautiful ocean coast landscape",
            "waves hitting a rocky coastline",
            "a tropical beach at sunset",
        ],
        "02_山川森林与草原": [
            "a majestic mountain landscape",
            "a green forest with sunlight",
            "a vast green grassland landscape",
            "a river flowing through a valley",
        ],
        "03_瀑布湖泊与河流": [
            "a large waterfall in nature",
            "a calm mountain lake",
            "a clear river flowing through rocks",
        ],
        "04_天空云海与日落": [
            "dramatic clouds in the sky",
            "a colorful sunset over the horizon",
            "sun rays shining through clouds",
        ],
        "05_海洋生物": [
            "an underwater coral reef",
            "jellyfish swimming underwater",
            "fish swimming in the ocean",
            "a whale or dolphin in the sea",
        ],
        "06_动物与鸟类": [
            "a wild animal in nature",
            "birds flying in the sky",
            "a farm animal in a field",
        ],
        "07_花草与微距生态": [
            "colorful flowers blooming",
            "macro photography of dew drops on grass",
            "a butterfly or bee on a flower",
        ],
    },
    "04_城市与人文": {
        "01_城市街景": [
            "a busy city street intersection",
            "a modern city skyline",
            "people walking on an urban street",
        ],
        "02_乡村与农业": [
            "a rural village landscape",
            "a farmer working in a field",
            "people harvesting crops",
        ],
        "03_人物肖像": [
            "a close portrait of a person",
            "a person looking at the camera",
            "an elderly person portrait",
        ],
        "04_家庭与儿童": [
            "a happy family spending time together",
            "a mother holding a child",
            "children playing together",
        ],
        "05_人群与活动": [
            "a large crowd of people at an event",
            "people celebrating at a festival",
            "a public gathering or demonstration",
        ],
        "06_工作与生活": [
            "a person working at a desk",
            "people cooking in a kitchen",
            "daily life activities at home",
        ],
        "07_交通工具": [
            "cars driving on a road",
            "a train or railway",
            "an airplane in flight",
            "a boat or ship on the water",
        ],
        "08_建筑与室内": [
            "the exterior of a modern building",
            "the interior of a house or room",
            "an ancient historic building",
        ],
    },
    "05_视觉与杂项": {
        "01_文字海报与截图": [
            "a poster dominated by text and typography",
            "a screenshot of a website or mobile application",
            "a quote card with words on a background",
        ],
        "02_食物与餐饮": [
            "a plate of food or a meal",
            "fruit and vegetables",
            "a drink in a cup or glass",
        ],
        "03_商品与日用品": [
            "a product photographed on a plain background",
            "household objects and everyday tools",
            "clothing or fashion accessories",
        ],
        "04_抽象背景与纹理": [
            "an abstract background or texture",
            "a decorative pattern",
            "a blurred colorful background",
        ],
        "05_空白与损坏画面": [
            "a blank black or white image",
            "a corrupted image with visual noise",
            "a nearly empty background",
        ],
    },
}


DEFAULT_SETTINGS = {
    "model": "large",
    "device": "auto",
    "batch_size": 12,
    "minimum_similarity": 0.20,
    "minimum_margin": 0.020,
    "parent_weight": 0.15,
    "recursive": True,
    "operation": "copy",
    "last_sources": [],
    "last_output_dir": "",
    "categories": DEFAULT_CATEGORY_TREE,
}


def _float(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, min(maximum, value))


def _integer(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _safe_name(value):
    name = str(value or "").strip()
    for character in '<>:"/\\|?*':
        name = name.replace(character, "_")
    return name.rstrip(". ")


def normalize_category_tree(value=None):
    source = value if isinstance(value, dict) else DEFAULT_CATEGORY_TREE
    result = {}
    for raw_main, raw_children in source.items():
        main = _safe_name(raw_main)
        if not main or not isinstance(raw_children, dict):
            continue
        children = {}
        for raw_sub, raw_descriptions in raw_children.items():
            sub = _safe_name(raw_sub)
            if not sub:
                continue
            descriptions = []
            seen = set()
            values = (
                raw_descriptions if isinstance(raw_descriptions, (list, tuple))
                else [raw_descriptions]
            )
            for raw_description in values:
                description = str(raw_description or "").strip()
                key = description.casefold()
                if description and key not in seen:
                    seen.add(key)
                    descriptions.append(description)
            if descriptions:
                children[sub] = descriptions
        if children:
            result[main] = children
    return result or copy.deepcopy(DEFAULT_CATEGORY_TREE)


def normalize_image_classifier_settings(value=None):
    source = value if isinstance(value, dict) else {}
    model = str(source.get("model") or "large")
    if model not in MODEL_CHOICES:
        model = "large"
    device = str(source.get("device") or "auto")
    if device not in {"auto", "cpu", "cuda"}:
        device = "auto"
    operation = str(source.get("operation") or "copy")
    if operation not in {"copy", "move"}:
        operation = "copy"
    sources = []
    for value in source.get("last_sources", []) or []:
        path = str(value or "").strip()
        if path and path not in sources:
            sources.append(path)
    return {
        "model": model,
        "device": device,
        "batch_size": _integer(source.get("batch_size"), 12, 1, 64),
        "minimum_similarity": _float(
            source.get("minimum_similarity"), 0.20, -1.0, 1.0
        ),
        "minimum_margin": _float(
            source.get("minimum_margin"), 0.020, 0.0, 0.5
        ),
        "parent_weight": _float(
            source.get("parent_weight"), 0.15, 0.0, 0.5
        ),
        "recursive": bool(source.get("recursive", True)),
        "operation": operation,
        "last_sources": sources[-20:],
        "last_output_dir": str(source.get("last_output_dir") or ""),
        "categories": normalize_category_tree(source.get("categories")),
    }


def flatten_categories(tree):
    result = []
    for main, children in normalize_category_tree(tree).items():
        for sub, descriptions in children.items():
            result.append({
                "path": f"{main}/{sub}",
                "parent": main,
                "name": sub,
                "descriptions": list(descriptions),
            })
    return result


def discover_images(paths, output_dir="", recursive=True):
    output = None
    if output_dir:
        try:
            output = Path(output_dir).resolve()
        except OSError:
            output = None
    result = []
    seen = set()
    for raw_path in paths or ():
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            source_root = path.resolve()
        except OSError:
            source_root = path
        # Only exclude an output tree that is nested *inside* this input
        # directory.  If the user explicitly selects a folder below an output
        # or library root, excluding every file would incorrectly report that
        # the folder contains no supported images.
        exclude_nested_output = bool(
            output is not None
            and path.is_dir()
            and source_root != output
            and source_root in output.parents
        )
        candidates = (
            path.rglob("*") if path.is_dir() and recursive
            else path.glob("*") if path.is_dir()
            else (path,)
        )
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file() or resolved.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if exclude_nested_output and (
                resolved == output or output in resolved.parents
            ):
                continue
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                result.append(resolved)
    return sorted(result, key=lambda item: os.path.normcase(str(item)))


def choose_category(category_paths, scores, minimum_similarity, minimum_margin):
    if not category_paths or not scores:
        raise ValueError("没有可用的图片分类")
    ranked = sorted(
        zip(category_paths, scores), key=lambda item: float(item[1]), reverse=True
    )
    best_path, best_score = ranked[0]
    second_score = float(ranked[1][1]) if len(ranked) > 1 else -1.0
    best_score = float(best_score)
    margin = best_score - second_score
    if best_score < float(minimum_similarity):
        status = "low_confidence"
        reason = "相似度过低"
    elif margin < float(minimum_margin):
        status = "ambiguous"
        reason = "前两类过于接近"
    else:
        status = "ready"
        reason = "可自动分类"
    return {
        "suggested_category": best_path,
        "assigned_category": best_path if status == "ready" else PENDING_CATEGORY,
        "similarity": best_score,
        "margin": margin,
        "status": status,
        "reason": reason,
    }


_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


class ImageClassifierEngine:
    def __init__(self, settings, progress=None, cancelled=None):
        self.settings = normalize_image_classifier_settings(settings)
        self.progress = progress or (lambda _current, _total, _message: None)
        self.cancelled = cancelled or (lambda: False)

    def _load_runtime(self):
        self.progress(0, 0, "正在加载 CLIP 模型；首次使用可能需要下载模型…")
        try:
            import warnings
            import torch
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message="Failed to load image Python extension.*"
                )
                warnings.filterwarnings(
                    "ignore", message="The torchvision.*namespaces are still Beta.*"
                )
                from PIL import Image, ImageOps
                from transformers import CLIPModel, CLIPProcessor
        except Exception as error:
            raise RuntimeError(
                "无法加载图片分类依赖。请安装 transformers、Pillow，并确认 "
                f"PyTorch 可用。原始错误：{error}"
            ) from error

        requested = self.settings["device"]
        if requested == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("设置要求使用 CUDA，但当前电脑没有可用的 CUDA 显卡。")
        device = (
            "cuda" if requested in {"auto", "cuda"} and torch.cuda.is_available()
            else "cpu"
        )
        model_id = MODEL_CHOICES[self.settings["model"]]
        cache_key = (model_id, device)
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(cache_key)
            if cached is None:
                processor = CLIPProcessor.from_pretrained(
                    model_id, use_fast=False
                )
                model = CLIPModel.from_pretrained(
                    model_id, use_safetensors=True
                ).to(device)
                model.eval()
                cached = (torch, Image, ImageOps, model, processor, device)
                _MODEL_CACHE[cache_key] = cached
        return cached

    def _category_features(self, torch, model, processor, device, categories):
        feature_rows = []
        description_indices = []
        description_categories = []
        prompts = []
        for category_index, category in enumerate(categories):
            for description in category["descriptions"]:
                description_index = len(description_categories)
                description_categories.append(category_index)
                for template in PROMPT_TEMPLATES:
                    prompts.append(template.format(description))
                    description_indices.append(description_index)

        text_batch_size = 64
        with torch.inference_mode():
            for offset in range(0, len(prompts), text_batch_size):
                if self.cancelled():
                    return None
                batch = prompts[offset:offset + text_batch_size]
                inputs = processor(
                    text=batch, return_tensors="pt", padding=True
                ).to(device)
                features = model.get_text_features(**inputs)
                features = features / features.norm(p=2, dim=-1, keepdim=True)
                feature_rows.append(features)
        all_features = torch.cat(feature_rows, dim=0)
        description_vectors = []
        for description_index in range(len(description_categories)):
            indices = [
                index for index, owner in enumerate(description_indices)
                if owner == description_index
            ]
            vector = all_features[indices].mean(dim=0)
            description_vectors.append(vector / vector.norm(p=2, dim=-1))
        description_features = torch.stack(description_vectors)
        category_vectors = []
        for category_index in range(len(categories)):
            indices = [
                index for index, owner in enumerate(description_categories)
                if owner == category_index
            ]
            vector = description_features[indices].mean(dim=0)
            category_vectors.append(vector / vector.norm(p=2, dim=-1))
        category_features = torch.stack(category_vectors)

        parent_vectors = {}
        for parent in {category["parent"] for category in categories}:
            indices = [
                index for index, category in enumerate(categories)
                if category["parent"] == parent
            ]
            vector = category_features[indices].mean(dim=0)
            parent_vectors[parent] = vector / vector.norm(p=2, dim=-1)
        parent_features = torch.stack([
            parent_vectors[category["parent"]] for category in categories
        ])
        return (
            description_features,
            description_categories,
            category_features,
            parent_features,
        )

    def classify(self, paths):
        categories = flatten_categories(self.settings["categories"])
        if not categories:
            raise ValueError("分类设置中没有包含描述词的子类别。")
        images = [Path(path) for path in paths]
        torch, Image, ImageOps, model, processor, device = self._load_runtime()
        self.progress(0, len(images), f"模型已加载，正在 {device.upper()} 上分析…")
        features = self._category_features(
            torch, model, processor, device, categories
        )
        if features is None:
            return []
        (
            description_features,
            description_categories,
            category_features,
            parent_features,
        ) = features
        category_paths = [category["path"] for category in categories]
        results = []
        batch_size = self.settings["batch_size"]
        parent_weight = self.settings["parent_weight"]

        for offset in range(0, len(images), batch_size):
            if self.cancelled():
                break
            batch_paths = images[offset:offset + batch_size]
            opened = []
            valid_paths = []
            for path in batch_paths:
                try:
                    with Image.open(path) as source:
                        image = ImageOps.exif_transpose(source).convert("RGB")
                        opened.append(image.copy())
                    valid_paths.append(path)
                except Exception as error:
                    results.append({
                        "source": str(path),
                        "suggested_category": "",
                        "assigned_category": "",
                        "similarity": 0.0,
                        "margin": 0.0,
                        "status": "error",
                        "reason": f"读取失败：{error}",
                    })
            if opened:
                try:
                    pixel_values = processor(
                        images=opened, return_tensors="pt"
                    ).pixel_values.to(device)
                    with torch.inference_mode():
                        image_features = model.get_image_features(pixel_values)
                        image_features = image_features / image_features.norm(
                            p=2, dim=-1, keepdim=True
                        )
                        description_scores = image_features @ description_features.T
                        detail_scores = torch.stack([
                            description_scores[:, [
                                index for index, owner in enumerate(
                                    description_categories
                                ) if owner == category_index
                            ]].max(dim=1).values
                            for category_index in range(len(categories))
                        ], dim=1)
                        sub_scores = image_features @ category_features.T
                        parent_scores = image_features @ parent_features.T
                        broad_scores = (
                            sub_scores * (1.0 - parent_weight)
                            + parent_scores * parent_weight
                        )
                        # The strongest concrete description preserves visual
                        # detail (cross, tomb, flood), while the averaged leaf
                        # and parent prototypes suppress lucky one-prompt hits.
                        scores = (
                            detail_scores * 0.60 + broad_scores * 0.40
                        ).detach().cpu().tolist()
                    for path, row in zip(valid_paths, scores):
                        decision = choose_category(
                            category_paths,
                            row,
                            self.settings["minimum_similarity"],
                            self.settings["minimum_margin"],
                        )
                        decision["source"] = str(path)
                        results.append(decision)
                finally:
                    for image in opened:
                        image.close()
            current = min(len(images), offset + len(batch_paths))
            self.progress(current, len(images), f"已分析 {current}/{len(images)} 张")
        return results


def unique_destination(path):
    path = Path(path)
    if not path.exists():
        return path
    for index in range(1, 100000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为重名文件生成新名称：{path.name}")


def apply_classification_results(
    results, output_dir, operation="copy", progress=None, cancelled=None
):
    progress = progress or (lambda _current, _total, _message: None)
    cancelled = cancelled or (lambda: False)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    completed = []
    failed = []
    candidates = [
        result for result in results
        if result.get("status") not in {"error", "completed"}
        and result.get("assigned_category")
    ]
    for index, result in enumerate(candidates, 1):
        if cancelled():
            break
        source = Path(result["source"])
        category_parts = [
            _safe_name(part)
            for part in str(result["assigned_category"]).replace("\\", "/").split("/")
            if _safe_name(part)
        ]
        try:
            if not source.is_file():
                raise FileNotFoundError("源图片已不存在")
            target_dir = output.joinpath(*category_parts)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = unique_destination(target_dir / source.name)
            if operation == "move":
                shutil.move(str(source), str(target))
            else:
                shutil.copy2(source, target)
            completed.append({**result, "target": str(target)})
        except Exception as error:
            failed.append({**result, "error": str(error)})
        progress(index, len(candidates), f"已整理 {index}/{len(candidates)} 张")
    return {"completed": completed, "failed": failed}

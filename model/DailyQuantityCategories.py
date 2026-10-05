"""Local, user-owned quantity categories. Never infer choices from Sheets."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from app_paths import APP_ROOT

# One-time seed recovered from the local template, not refreshed online.
DEFAULT_CATEGORIES = {
    "（口播视频组）": [
        "FL 短口播", "FL 长口播", "FL免费模型刷 长口播", "Heygen 短口播",
        "Heygen 长口播", "flow+heygen 口播", "AI上下合拍（有成品视频）",
        "新人上下合拍", "实拍自媒体剪辑", "简单钩子+口播", "复杂钩子+口播",
    ],
    "（祷告词）": [
        "常用声音新生成【实拍素材、加人物图片/动态】\n官方套模板视频【只需要生图，其他套模板】",
        "常用声音新生成【AI生图自己跑动画】", "图转动画【AI生图自己跑动画】",
        "套模板视频【没有人声朗诵，有配乐 5-10秒】",
    ],
    "（效果视频组）": [
        "常规短reels", "常规长reels", "Reels简单钩子", "Reels复杂钩子",
        "短故事（20镜头）", "短故事（30镜头）", "长故事（50镜头）", "新时代",
    ],
    "（动画组）": [
        "常规动画（主耶稣类、灾难类、游行类、圣经故事）150",
        "图转动画（风景类、祷告词背景）200", "故事片段6s以下",
        "故事片段6s-9s", "故事片段10s-15s", "开场钩子（惊喜开场）",
    ],
}
_LOCK = threading.RLock()


def _key(value):
    return "".join(value.split()).casefold()


def validate_categories(categories):
    if not isinstance(categories, dict) or len(categories) > 100:
        raise ValueError("类别文件应包含分页与类别列表，最多 100 个分页。")
    result, seen = {}, set()
    for sheet, labels in categories.items():
        if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > 200:
            raise ValueError("统计分页名称不能为空，且不能超过 200 个字符。")
        sheet = sheet.strip()
        if _key(sheet) in seen:
            raise ValueError("统计分页名称重复：" + sheet)
        seen.add(_key(sheet))
        if not isinstance(labels, list) or len(labels) > 500:
            raise ValueError("每个分页必须是类别列表，最多 500 个类别。")
        normalized, keys = [], set()
        for label in labels:
            if not isinstance(label, str) or not label.strip() or len(label) > 2000:
                raise ValueError("类别名称不能为空，且不能超过 2000 个字符。")
            label = label.strip()
            if _key(label) in keys:
                raise ValueError("同一分页内类别重复：" + " ".join(label.split()))
            keys.add(_key(label))
            normalized.append(label)
        result[sheet] = normalized
    return result


class CategoryStore:
    def __init__(self, config=None):
        self.path = Path((config or {}).get("daily_quantity_categories_file")
                         or APP_ROOT / "DailyQuantityCategories.json")

    def load(self):
        """Return editable choices and a revision; explicit empty stays empty."""
        with _LOCK:
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                return copy.deepcopy(DEFAULT_CATEGORIES), None
            try:
                document = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(document, dict) or document.get("version") != 1:
                    raise ValueError("不支持的类别文件版本")
                categories = validate_categories(document.get("categories"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"本地类别文件损坏，未清空或覆盖。请核对 {self.path.name}"
                                 f"（之前的版本保存在 .bak 中）：{exc}") from None
            return categories, hashlib.sha256(raw).hexdigest()

    def save(self, categories, expected_revision):
        categories = validate_categories(categories)
        with _LOCK:
            _, revision = self.load()
            if revision != expected_revision:
                raise ValueError("类别文件已被其他窗口或程序修改，请取消后重新打开再编辑。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                                      dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "categories": categories}, handle,
                              ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self.load()[1]

    def initialize(self):
        categories, revision = self.load()
        if revision is None:
            self.save(categories, None)
        return categories

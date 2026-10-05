"""Private plugin data. No credentials and no writes to the host configuration."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from app_paths import APP_ROOT
from .layers import DEFAULT_LAYERS, normalize_layers
from .effects import blur_strength

SCHEMES = {"static_text": "静态文字版"}
DEFAULTS = {
    "scheme": "static_text", "aspect": "portrait", "fit": "contain",
    "geometry_units": "percent", "show_layer_bounds": True,
    "background_tint_color":"#000000","background_tint_strength":0,
    "background_blur":0,
    "vertical": "center", "title_align": "center", "body_align": "left",
    "font": "Segoe UI", "title_min": 36, "title_max": 64,
    "body_min": 26, "body_max": 42, "title_color": "#d3e55a",
    "body_color": "#ffffff", "margin_x": 8, "margin_top": 8,
    "margin_bottom": 18, "line_spacing": 1.15, "gap": 22,
    "darkness": 14, "music_volume": 70, "keep_audio": False,
    "skip_silence": True, "silence_db": -50, "silence_seconds": 0.6,
    "fade_seconds": 0.5, "overwrite": False, "output_dir": "", "ffmpeg_path": "",
    "use_voice": False, "duration_strategy": "loop", "loop_transition": "fade",
    "transition_seconds": 0.4, "min_speed": 0.5, "slow_interpolation": "repeat",
    "voice_volume": 100, "voice_music_volume": 25,
    "voice_eq": True, "eq_frequency": 1500, "eq_width": 2.0, "eq_gain": -9,
    "voice_duck": True, "duck_threshold_db": -30, "duck_ratio": 6,
    "duck_attack_ms": 20, "duck_release_ms": 350,
    "layers": DEFAULT_LAYERS,
}
_LOCK = threading.RLock()


def fresh_state():
    return {"version": 1, "revision": 0, "settings": copy.deepcopy(DEFAULTS),
            "jobs": [], "music": [], "next_music_id": "", "backgrounds": [],
            "copy_pool": [], "image_pool": [], "active_profile": "default",
            "profiles": [{"id": "default", "name": "默认配置", "data": None}]}


def file_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def add_paths(items, paths, music=False):
    known = {file_identity(item["path"]) for item in items}
    added = 0
    for path in paths:
        path = Path(path).resolve()
        identity = file_identity(path)
        if not path.is_file() or identity in known:
            continue
        item = {"id": uuid.uuid4().hex, "path": str(path)}
        item.update({"cursor": 0.0, "last_used": "", "audio_key": ""} if music else {
            "title": "", "body": "", "name": path.stem + "_文字版",
            "status": "待生成", "output": "", "error": ""})
        items.append(item)
        known.add(identity)
        added += 1
    return added


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or APP_ROOT / "BatchTextVideo")
        self.path = self.directory / "state.json"

    def load(self):
        with _LOCK:
            if not self.path.exists():
                return fresh_state()
            state = json.loads(self.path.read_text("utf-8"))
            if state.get("version") != 1 or not isinstance(state.get("jobs"), list) or not isinstance(state.get("music"), list):
                raise ValueError("批量文案视频记录格式异常，未清空或覆盖；请检查 state.json。")
            state["settings"] = {**copy.deepcopy(DEFAULTS), **state.get("settings", {})}
            state["settings"]["background_blur"] = blur_strength(state["settings"])
            state["settings"]["layers"] = normalize_layers(state["settings"].get("layers"))
            state.setdefault("backgrounds", [])
            from .pools import migrate_pools
            migrate_pools(state)
            if "profiles" not in state:
                state.update(active_profile="default", profiles=[{"id": "default", "name": "默认配置", "data": None}])
            if (not isinstance(state["profiles"], list) or not state["profiles"] or
                    len({item["id"] for item in state["profiles"]}) != len(state["profiles"]) or
                    not any(item["id"] == state.get("active_profile") for item in state["profiles"])):
                raise ValueError("配置列表格式异常，未清空或覆盖原记录。")
            return state

    def save(self, state, expected_revision):
        with _LOCK:
            current = self.load()
            if current["revision"] != expected_revision:
                raise ValueError("插件记录已被其他窗口或程序修改，请重新打开后再操作。")
            state = copy.deepcopy(state)
            state["settings"]["background_blur"] = blur_strength(state["settings"])
            state["settings"]["layers"] = normalize_layers(state["settings"].get("layers"))
            from .pools import migrate_pools, snapshot
            migrate_pools(state)
            state.setdefault("active_profile", "default")
            state.setdefault("profiles", [{"id": "default", "name": "默认配置", "data": None}])
            profile = next(item for item in state["profiles"] if item["id"] == state["active_profile"])
            profile["data"] = snapshot(state)
            state["revision"] = current["revision"] + 1
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(state, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return state

    def select_profile(self, state, identifier):
        from .pools import snapshot
        state = copy.deepcopy(state)
        current = next(item for item in state["profiles"] if item["id"] == state["active_profile"])
        current["data"] = snapshot(state)
        selected = next(item for item in state["profiles"] if item["id"] == identifier)
        if not selected.get("data"):
            raise ValueError("配置没有内容，原配置未更改。")
        state.update(copy.deepcopy(selected["data"]), active_profile=identifier)
        state["settings"] = {**copy.deepcopy(DEFAULTS), **state["settings"]}
        return self.save(state, state["revision"])

    def create_profile(self, state, name, clone=False):
        from .pools import snapshot
        name = str(name).strip()
        if not name or len(name) > 80 or any(item["name"] == name for item in state["profiles"]):
            raise ValueError("配置名称不能为空、重复或超过80字。")
        state = copy.deepcopy(state)
        data = snapshot(state) if clone else snapshot(fresh_state())
        identifier = uuid.uuid4().hex
        state["profiles"].append({"id": identifier, "name": name, "data": data})
        return self.select_profile(state, identifier)

    def completed(self, state, job_id, output, music_plan, sequence_plan=None):
        """Advance the rotation/playhead only after a verified output exists."""
        if not Path(output).is_file():
            raise FileNotFoundError("成品尚未保存，不能推进音乐进度。")
        state = copy.deepcopy(state)
        if sequence_plan:
            from .components import commit_sequences
            state["settings"]["layers"] = commit_sequences(state["settings"]["layers"],sequence_plan)
        job = next(item for item in state["jobs"] if item["id"] == job_id)
        job.update(status="已完成", output=str(output), error="")
        if music_plan:
            music = next(item for item in state["music"] if item["id"] == music_plan["id"])
            music.update(cursor=music_plan["cursor"], audio_key=music_plan["audio_key"],
                         last_used=datetime.now().astimezone().isoformat(timespec="seconds"))
            index = state["music"].index(music)
            state["next_music_id"] = state["music"][(index + 1) % len(state["music"])]["id"]
        return self.save(state, state["revision"])

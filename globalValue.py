import json
import os
import threading
from pathlib import Path

from faster_whisper import WhisperModel

from app_paths import APP_ROOT


class WhisperModelRestartRequired(RuntimeError):
    """A different Whisper model is already resident in this process."""


class GlobalValue:
    def __init__(self):
        self.__whisper_model = None
        self.__whisper_model_name = ""
        self.__whisper_model_error = ""
        self.__whisper_model_lock = threading.RLock()

    def videoSortingStationPath(self):
        config_path = APP_ROOT / "config.json"
        if not config_path.exists():
            return ""
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return ""
        return str(config.get("video_sorting_station_path") or "").strip()

    @staticmethod
    def _normalize_whisper_model_name(value):
        value = str(value or "base").strip().lower()
        return value if value in {"base", "small", "medium", "large-v3"} else "base"

    def _configured_whisper_model_name(self):
        config_path = APP_ROOT / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return "base"
        smart_settings = config.get("smart_video_editor")
        if not isinstance(smart_settings, dict):
            return "base"
        return self._normalize_whisper_model_name(
            smart_settings.get("whisper_model_size")
        )

    @staticmethod
    def _cached_large_v3_snapshot():
        """Reuse either official or already-installed CT2 large-v3 files."""
        hf_home = Path(
            os.environ.get("HF_HOME")
            or (Path.home() / ".cache" / "huggingface")
        )
        hub = hf_home / "hub"
        repositories = (
            "models--Systran--faster-whisper-large-v3",
            "models--LukeJacob2023--whisper-large-v3-ct2",
        )
        for repository in repositories:
            root = hub / repository
            candidates = []
            try:
                revision = (root / "refs" / "main").read_text(
                    encoding="utf-8"
                ).strip()
                if revision:
                    candidates.append(root / "snapshots" / revision)
            except (FileNotFoundError, OSError, ValueError):
                pass
            snapshots = root / "snapshots"
            try:
                candidates.extend(
                    path for path in snapshots.iterdir() if path.is_dir()
                )
            except (FileNotFoundError, OSError):
                pass
            for candidate in candidates:
                if all((candidate / name).is_file() for name in (
                    "model.bin", "config.json", "tokenizer.json"
                )):
                    return str(candidate)
        return "large-v3"

    def get_whisper_model(self, model_name=None):
        requested = self._normalize_whisper_model_name(
            model_name or self._configured_whisper_model_name()
        )
        with self.__whisper_model_lock:
            if self.__whisper_model is not None:
                if requested == self.__whisper_model_name or model_name is None:
                    return self.__whisper_model
                # CTranslate2 can keep allocations and worker references alive
                # after Python drops the old object.  Never load a second model
                # into the same process; the saved preference takes effect next
                # launch instead of risking a native crash.
                raise WhisperModelRestartRequired(
                    f"当前已加载 {self.__whisper_model_name}，已选择 {requested}。"
                    "请保存设置并重启程序后使用新模型。"
                )
            source = self._model_source(requested)
            try:
                replacement = WhisperModel(
                    source,
                    device="cpu",
                    compute_type="int8",
                )
            except Exception as error:
                self.__whisper_model_error = (
                    f"{requested} 加载失败：{type(error).__name__}: {error}"
                )
                raise RuntimeError(self.__whisper_model_error) from error

            self.__whisper_model = replacement
            self.__whisper_model_name = requested
            self.__whisper_model_error = ""
            return replacement

    def _model_source(self, model_name):
        return (
            self._cached_large_v3_snapshot()
            if model_name == "large-v3" else model_name
        )

    def whisper_model_name(self):
        return self.__whisper_model_name or self._configured_whisper_model_name()

    def loaded_whisper_model_name(self):
        """Return the actual model in memory, not the saved next-launch choice."""
        with self.__whisper_model_lock:
            return self.__whisper_model_name


globalValue = GlobalValue()

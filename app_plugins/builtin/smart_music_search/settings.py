from pathlib import Path

from qt_compat import QtWidgets
from .encoder import COVERAGE_LABELS, MODEL_SPECS

CONFIG_KEY = "smart_music_search"
DEFAULT_LIBRARY = r"D:\2.配乐库"


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    try:
        limit = int(value.get("result_limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return {
        "library_root": str(value.get("library_root") or DEFAULT_LIBRARY).strip(),
        "ffmpeg_path": str(value.get("ffmpeg_path") or "").strip(),
        "result_limit": max(10, min(200, limit)),
        "model_key": value.get("model_key") if value.get("model_key") in MODEL_SPECS else "general",
        "coverage": value.get("coverage") if value.get("coverage") in COVERAGE_LABELS else "full",
        "use_feedback": value.get("use_feedback", True) is not False,
    }


class SmartMusicSearchSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        note = QtWidgets.QLabel(
            "在本机后台建立配乐片段索引；不会移动原曲，也不会在程序启动时加载模型。"
            "首次建索引需要下载音频模型和中文翻译模型，此后只更新变化的文件。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        root_row = QtWidgets.QHBoxLayout()
        self.root = QtWidgets.QLineEdit()
        root_row.addWidget(self.root, 1)
        browse = QtWidgets.QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        root_row.addWidget(browse)
        form.addRow("配乐库", root_row)
        self.ffmpeg = QtWidgets.QLineEdit()
        self.ffmpeg.setPlaceholderText("留空则查找 PATH 中的 ffmpeg；ffprobe 使用同目录程序")
        form.addRow("FFmpeg 路径", self.ffmpeg)
        self.model = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items():
            self.model.addItem(spec["label"], key)
        form.addRow("检索模型", self.model)
        self.coverage = QtWidgets.QComboBox()
        for key, label in COVERAGE_LABELS.items():
            self.coverage.addItem(label, key)
        self.coverage.setToolTip("精细：每约10秒，最多256段；均衡：每约20秒，最多128段。各模式索引独立，旧记录不删除。")
        form.addRow("音乐覆盖", self.coverage)
        self.feedback = QtWidgets.QCheckBox("参考我标记的情绪喜好")
        form.addRow("个人偏好", self.feedback)
        self.limit = QtWidgets.QSpinBox()
        self.limit.setRange(10, 200)
        form.addRow("每批显示结果", self.limit)
        layout.addLayout(form)
        layout.addStretch(1)

    def _browse(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self.widget, "选择配乐库", self.root.text() or str(Path.home())
        )
        if path:
            self.root.setText(path)

    def load_config(self, config):
        value = normalize_settings(config.get(CONFIG_KEY))
        self.root.setText(value["library_root"])
        self.ffmpeg.setText(value["ffmpeg_path"])
        self.limit.setValue(value["result_limit"])
        self.model.setCurrentIndex(self.model.findData(value["model_key"]))
        self.coverage.setCurrentIndex(self.coverage.findData(value["coverage"]))
        self.feedback.setChecked(value["use_feedback"])

    def update_config(self, config):
        config[CONFIG_KEY] = normalize_settings({
            "library_root": self.root.text(),
            "ffmpeg_path": self.ffmpeg.text(),
            "result_limit": self.limit.value(),
            "model_key": self.model.currentData(),
            "coverage": self.coverage.currentData(),
            "use_feedback": self.feedback.isChecked(),
        })
        return config

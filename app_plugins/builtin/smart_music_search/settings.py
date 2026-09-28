from pathlib import Path

from qt_compat import QtWidgets

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

    def update_config(self, config):
        config[CONFIG_KEY] = normalize_settings({
            "library_root": self.root.text(),
            "ffmpeg_path": self.ffmpeg.text(),
            "result_limit": self.limit.value(),
        })
        return config

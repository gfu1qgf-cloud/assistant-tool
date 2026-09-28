import os
from pathlib import Path

from qt_compat import QtWidgets

from .encoder import MODEL_IDS


CONFIG_KEY = "smart_image_search"


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    model = str(value.get("model") or "base")
    if model not in MODEL_IDS:
        model = "base"
    try:
        limit = int(value.get("result_limit", 60))
    except (TypeError, ValueError):
        limit = 60
    roots = []
    seen = set()
    raw_roots = value.get("library_roots", ())
    if not isinstance(raw_roots, (list, tuple)):
        raw_roots = ()
    for raw in raw_roots:
        path = str(raw or "").strip()
        key = os.path.normcase(os.path.abspath(path)) if path else ""
        if key and key not in seen:
            roots.append(path)
            seen.add(key)
    return {"model": model, "result_limit": min(200, max(10, limit)),
            "library_roots": roots}


class SmartImageSearchSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        note = QtWidgets.QLabel(
            "智能搜图在后台为库存图片建立本地索引，首次使用才下载中文模型。"
            "当前电脑使用 CPU 版 PyTorch，因此默认采用中文 CLIP Base。"
            "更换模型版本时需要重新计算图片特征。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        self.model.addItem("中文 CLIP Base（CPU 推荐，约 753 MB）", "base")
        self.result_limit = QtWidgets.QSpinBox()
        self.result_limit.setRange(10, 200)
        form.addRow("检索模型", self.model)
        form.addRow("最多展示", self.result_limit)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel("额外图片库文件夹（递归搜索子目录）"))
        self.roots = QtWidgets.QListWidget()
        self.roots.setMaximumHeight(150)
        layout.addWidget(self.roots)
        root_actions = QtWidgets.QHBoxLayout()
        add_root = QtWidgets.QPushButton("添加文件夹…")
        add_root.clicked.connect(self._add_root)
        remove_root = QtWidgets.QPushButton("移除选中")
        remove_root.clicked.connect(self._remove_root)
        root_actions.addWidget(add_root)
        root_actions.addWidget(remove_root)
        root_actions.addStretch(1)
        layout.addLayout(root_actions)
        layout.addStretch(1)

    def _add_root(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self.widget, "选择图片库根目录")
        if path and path not in self.library_roots():
            self.roots.addItem(path)

    def _remove_root(self):
        for item in self.roots.selectedItems():
            self.roots.takeItem(self.roots.row(item))

    def library_roots(self):
        return [self.roots.item(index).text()
                for index in range(self.roots.count())]

    def load_config(self, config):
        settings = normalize_settings(config.get(CONFIG_KEY))
        self.model.setCurrentIndex(self.model.findData(settings["model"]))
        self.result_limit.setValue(settings["result_limit"])
        self.roots.clear()
        self.roots.addItems(settings["library_roots"])

    def update_config(self, config):
        config[CONFIG_KEY] = {
            "model": self.model.currentData(),
            "result_limit": self.result_limit.value(),
            "library_roots": self.library_roots(),
        }
        return config

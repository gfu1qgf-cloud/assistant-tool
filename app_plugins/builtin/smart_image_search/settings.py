import os
from pathlib import Path

from qt_compat import QtWidgets

from .encoder import MODEL_IDS, MODEL_SPECS


CONFIG_KEY = "smart_image_search"


def normalize_settings(value=None):
    value = value if isinstance(value, dict) else {}
    model = str(value.get("model") or "base")
    if model not in MODEL_IDS:
        model = "base"
    try:
        limit = int(value.get("result_limit", 100))
    except (TypeError, ValueError, OverflowError):
        limit = 100
    try:
        text_weight = int(value.get("text_weight", 35))
    except (TypeError, ValueError, OverflowError):
        text_weight = 35
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
    return {"model": model, "result_limit": min(500, max(20, limit)),
            "library_roots": roots, "text_weight": min(100, max(0, text_weight))}


class SmartImageSearchSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        note = QtWidgets.QLabel(
            "智能搜图在后台为库存图片建立本地索引，首次使用才下载中文模型。"
            "Base 保留原索引；Large 和 336 各自建立独立索引，切回旧模型无需重建。"
            "336 适合精细对照，但建库和参考图分析会更慢；CPU 环境也能运行。"
            "搜索覆盖整个索引，结果分页展示；每批数量不会限制总结果。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items():
            self.model.addItem(f"{spec['label']} · {spec['download']}", key)
        self.result_limit = QtWidgets.QSpinBox()
        self.result_limit.setRange(20, 500)
        self.result_limit.setSingleStep(20)
        self.text_weight = QtWidgets.QSpinBox()
        self.text_weight.setRange(0, 100)
        self.text_weight.setSuffix(" %")
        self.text_weight.setToolTip("图片＋文字搜索的文字侧重；不是严格筛选条件。")
        form.addRow("检索模型", self.model)
        form.addRow("每批展示", self.result_limit)
        form.addRow("组合搜索文字侧重", self.text_weight)
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
        self.text_weight.setValue(settings["text_weight"])
        self.roots.clear()
        self.roots.addItems(settings["library_roots"])

    def update_config(self, config):
        config[CONFIG_KEY] = {
            "model": self.model.currentData(),
            "result_limit": self.result_limit.value(),
            "library_roots": self.library_roots(),
            "text_weight": self.text_weight.value(),
        }
        return config

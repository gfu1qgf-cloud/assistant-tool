"""Main-thread colour/trimap controls shared by component and node inspectors."""
from qt_compat import QtCore, QtGui, QtWidgets
from .chroma_key import DEFAULTS, MASK_SUFFIXES, key_color, normalize_key


class KeyDetails(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer, self.loading = None, False
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        color_row = QtWidgets.QHBoxLayout()
        self.color = QtWidgets.QLineEdit()
        self.color.setMaxLength(7)
        self.color.setPlaceholderText("#00ff00")
        self.color.setValidator(QtGui.QRegularExpressionValidator(
            QtCore.QRegularExpression("#[0-9a-fA-F]{6}"), self))
        self.pick = QtWidgets.QPushButton("选颜色…")
        self.pick.setAutoDefault(False)
        self.pick.setToolTip("选任意基准颜色；会自动切换到自定义颜色。")
        self.pick.clicked.connect(self.pick_color)
        self.color.editingFinished.connect(self.commit_color)
        color_row.addWidget(self.color, 1); color_row.addWidget(self.pick)
        form.addRow("基准颜色", color_row)
        self.mask_group = QtWidgets.QGroupBox("增强辅助遮罩（可选）")
        box = QtWidgets.QVBoxLayout(self.mask_group)
        row = QtWidgets.QHBoxLayout()
        self.mask = QtWidgets.QLineEdit()
        self.mask.setMinimumWidth(0)
        self.mask.setPlaceholderText("不填则自动分析轮廓")
        self.mask.editingFinished.connect(self.commit_mask)
        self.browse = QtWidgets.QPushButton("选择…"); self.clear = QtWidgets.QPushButton("清空")
        for button in (self.browse, self.clear): button.setAutoDefault(False)
        self.browse.clicked.connect(self.choose_mask); self.clear.clicked.connect(self.clear_mask)
        row.addWidget(self.mask, 1); row.addWidget(self.browse); row.addWidget(self.clear)
        box.addLayout(row)
        tip = QtWidgets.QLabel("白色＝保护主体；黑色＝强制去除；灰色128＝跟随自动。\n"
            "支持图片／视频／GIF，透明处不干预；遮罩按素材全幅缩放，动态遮罩从头循环。")
        tip.setWordWrap(True)
        tip.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        box.addWidget(tip)
        form.addRow(self.mask_group)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.note)

    def set_layer(self, layer):
        self.loading = True
        self.layer = dict(layer) if layer else None
        self.setEnabled(bool(layer) and layer.get("key_mode", "none") != "none")
        if layer:
            rgb = key_color(layer)
            value = "#" + "".join(f"{v:02x}" for v in rgb)
            self.color.setText(value)
            self.pick.setStyleSheet(f"border-bottom: 3px solid {value};")
            self.mask.setText(layer.get("key_mask_path", ""))
        enhanced = bool(layer) and layer.get("key_method", "normal") == "enhanced"
        self.mask_group.setVisible(enhanced)
        self.note.setText("增强：自动保护轮廓内封闭的同色区域，去色溢不会染掉被保护的装饰。\n"
            "这是区域算法，不是人物AI；真实孔洞也可能被保留。与背景连通的饰品请用白色遮罩，"
            "真实背景孔洞可用黑色遮罩。" if enhanced else
            "普通：按基准颜色、容差和边缘柔化扣除；绿色／蓝色／黑色仍可一键选预设。")
        self.loading = False

    def commit_color(self):
        if self.loading or not self.layer: return
        value = self.color.text().strip()
        updated = dict(self.layer, key_mode="custom", key_color=value)
        try:
            normalize_key(updated)
        except ValueError:
            self.set_layer(self.layer)
            return
        if key_color(self.layer) == key_color(updated): return
        self.changed.emit({"key_mode": "custom", "key_color": updated["key_color"]})

    def pick_color(self):
        if not self.layer: return
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(*key_color(self.layer)), self, "选择抠像基准颜色")
        if color.isValid():
            self.color.setText(color.name()); self.commit_color()

    def commit_mask(self):
        if self.loading or not self.layer: return
        value = self.mask.text().strip()
        if value == self.layer.get("key_mask_path", ""): return
        if any(c in value for c in ("\x00", "\r", "\n")):
            self.mask.setText(self.layer.get("key_mask_path", "")); return
        self.changed.emit({"key_mask_path": value})

    def choose_mask(self):
        extensions = " ".join("*"+suffix for suffix in sorted(MASK_SUFFIXES))
        value, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择辅助遮罩（只读取，不改原文件）",
            self.mask.text(), "遮罩图片／视频 ("+extensions+")")
        if value:
            self.mask.setText(value); self.commit_mask()

    def clear_mask(self):
        self.mask.clear(); self.commit_mask()

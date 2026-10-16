"""Original-look thumbnails rendered from the exact same RGB functions as LUTs."""
from qt_compat import QtCore, QtGui, QtWidgets
from model.node_workflow.visual_nodes import LOOKS, look_rgb


def thumbnail(style, width=160, height=90):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    for y in range(height):
        for x in range(width):
            # Skin/blue/green ramps and neutral light: visual comparison, not a photo.
            value = x/(width-1)
            base = (value, value*.73, value*.56) if y < height/3 else (value*.5, value*.75, value) if y < height*2/3 else (value,)*3
            color = look_rgb(style, *base)
            image.setPixelColor(x, y, QtGui.QColor.fromRgbF(*color))
    return QtGui.QPixmap.fromImage(image)


class LutEditor(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        self.changed, self.params = changed, dict(params)
        root = QtWidgets.QFormLayout(self)
        self.style = QtWidgets.QComboBox(); self.style.setIconSize(QtCore.QSize(160, 90))
        for key, title, description in LOOKS:
            self.style.addItem(QtGui.QIcon(thumbnail(key)), title, key)
            self.style.setItemData(self.style.count()-1, description, QtCore.Qt.ItemDataRole.ToolTipRole)
        self.style.addItem('自选.cube', 'file'); self.style.setCurrentIndex(max(0, self.style.findData(params['style'])))
        root.addRow('预设预览', self.style)
        row = QtWidgets.QHBoxLayout(); self.path = QtWidgets.QLineEdit(params.get('file', '')); row.addWidget(self.path)
        choose = QtWidgets.QToolButton(); choose.setText('…'); row.addWidget(choose); root.addRow('.cube文件', row)
        self.strength = QtWidgets.QDoubleSpinBox(); self.strength.setRange(0, 1); self.strength.setDecimals(3); self.strength.setSingleStep(.05); self.strength.setValue(params['strength'])
        root.addRow('强度 · 0为原画面', self.strength)
        self.space = QtWidgets.QComboBox(); self.space.addItem('必须有SDR元数据', 'strict'); self.space.addItem('我确认源为SDR/Rec.709', 'assume_sdr')
        self.space.setCurrentIndex(max(0, self.space.findData(params['input_space']))); root.addRow('色彩空间', self.space)
        self.interpolation = QtWidgets.QComboBox()
        for key, title in (('nearest', '最近邻'), ('trilinear', '三线性'), ('tetrahedral', '四面体')): self.interpolation.addItem(title, key)
        self.interpolation.setCurrentIndex(max(0, self.interpolation.findData(params['interpolation']))); root.addRow('插值', self.interpolation)
        note = QtWidgets.QLabel('上方为相同RGB规则生成的预设缩略图，不代表当前视频。\n完整原图／效果比较：将原视频与套用LUT的解算器分别接预览。仅支持SDR；不自动转换未知Log/HDR。')
        note.setWordWrap(True); root.addRow(note)
        for control in (self.style, self.space, self.interpolation): control.currentIndexChanged.connect(self.apply)
        self.strength.valueChanged.connect(self.apply); self.path.textEdited.connect(self.apply); choose.clicked.connect(self.choose)

    def apply(self, *_):
        self.changed(dict(style=self.style.currentData(), file=self.path.text(), strength=self.strength.value(),
                          input_space=self.space.currentData(), interpolation=self.interpolation.currentData()))

    def choose(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择SDR LUT', self.path.text(), '3D LUT (*.cube)')
        if path:
            self.path.setText(path); self.style.setCurrentIndex(self.style.findData('file')); self.apply()

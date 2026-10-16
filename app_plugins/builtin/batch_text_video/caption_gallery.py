"""Local style thumbnails from the existing subtitle renderer; no model/network."""
from qt_compat import QtCore, QtGui, QtWidgets
from .caption_styles import preset_choices, apply_style, is_catalog

def sample(preset, width=440, height=248):
    from .captions import new_caption
    from .layers import normalize_layers
    from .text_components import render_text_box
    layer = apply_style(new_caption(), preset)
    layer.update(text="Božia láska dáva nádej", x=50, y=50, width=94, height=90)
    layer = normalize_layers([layer])[0]
    from .caption_style_render import render_design
    image, _ = render_design(layer, width, height)
    canvas = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QtGui.QColor("#263748"))
    painter = QtGui.QPainter(canvas)
    painter.drawImage(0, 0, image)
    painter.end()
    return canvas

class CaptionGallery(QtWidgets.QDialog):
    def __init__(self, selected="pop", parent=None):
        super().__init__(parent)
        self.setWindowTitle("字幕样式 · 点击预览图选择")
        self.resize(850, 650)
        self.selected = selected
        layout = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索样式名称")
        self.search.textChanged.connect(self.filter)
        layout.addWidget(self.search)
        self.list = QtWidgets.QListWidget()
        self.list.setViewMode(self.list.ViewMode.IconMode)
        self.list.setResizeMode(self.list.ResizeMode.Adjust)
        self.list.setMovement(self.list.Movement.Static)
        self.list.setIconSize(QtCore.QSize(220, 124))
        self.list.setGridSize(QtCore.QSize(246, 164))
        self.list.setSpacing(8)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        layout.addWidget(QtWidgets.QLabel("本机渲染的样式示意图；真实人声时机及动画请用播放预览核对。"))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.pending = []
        for name, key in [("逐词跃动", "pop"), ("逐词出现", "appear")] + preset_choices():
            item = QtWidgets.QListWidgetItem(name)
            item.setSizeHint(QtCore.QSize(246, 164))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            item.setToolTip(name)
            self.list.addItem(item)
            self.pending.append((item, key))
            if key == selected: self.list.setCurrentItem(item)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.load_next)
        self.timer.start(0)
    def load_next(self):
        if not self.pending:
            self.timer.stop(); return
        item, key = self.pending.pop(0)
        try:
            image = sample(key)
            item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
        except Exception as error:
            item.setToolTip(item.text() + "\n样式示意无法生成：" + str(error))
            item.setText(item.text() + "（需调整）")
    def filter(self, text):
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())
    def accept(self):
        item = self.list.currentItem()
        if not item: return
        self.selected = item.data(QtCore.Qt.ItemDataRole.UserRole)
        super().accept()

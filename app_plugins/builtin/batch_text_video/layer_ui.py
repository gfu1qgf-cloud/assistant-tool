"""Shared batch layers, with queued image import and explicit stacking controls."""
import copy
from pathlib import Path
import threading
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from .layers import IMAGE_SUFFIXES, MAX_IMAGES, MAX_TEXT_BOXES, TEXT_KINDS, import_images, normalize_layers, componentize_layers
from .components import MAX_SEQUENCE_ITEMS, new_text_component
from .component_ui import Combo, ContentSourceEditor, TextBoxProperties


class ImageImportWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, paths, directory, parent=None):
        super().__init__(parent)
        self.paths, self.directory = list(paths), directory
        self.cancel = threading.Event()

    def run(self):
        try:
            self.ready.emit(import_images(self.paths, self.directory, self.cancel))
        except Exception:
            self.ready.emit({"layers": [], "errors": [traceback.format_exc()], "cancelled": False})


class Spin(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class LayerPanel(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    busyChanged = QtCore.pyqtSignal(bool)
    message = QtCore.pyqtSignal(str)
    selectionChanged = QtCore.pyqtSignal(str)
    copyRequested = QtCore.pyqtSignal(str)

    def __init__(self, layers, directory, parent=None, settings=None):
        super().__init__(parent)
        self.directory = directory
        self.settings = copy.deepcopy(settings or {})
        self.canvas_size = QtCore.QSize(1920,1080) if self.settings.get("aspect") == "landscape" else QtCore.QSize(1080,1920)
        self._layers = componentize_layers(layers,self.settings)
        self._loading, self.worker = False, None
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("上方图层盖住下方。\n固定内容整批共用；轮换内容从资源池逐视频取用。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(note)
        tools = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("添加图片…")
        menu = QtWidgets.QMenu(self.add_button)
        menu.addAction("添加文本框",self.add_text)
        menu.addAction("添加轮换文本框（文案池）",self.add_rotating_text)
        menu.addAction("添加图片框…",self.choose_images)
        menu.addAction("添加自定义轮换图片…",lambda:self.choose_images(True))
        self.add_button.setText("添加组件…")
        self.add_button.setMenu(menu)
        self.remove_button = QtWidgets.QPushButton("移除")
        self.up_button = QtWidgets.QPushButton("↑")
        self.down_button = QtWidgets.QPushButton("↓")
        self.up_button.setFixedWidth(32)
        self.down_button.setFixedWidth(32)
        self.remove_button.setFixedWidth(52)
        self.up_button.setToolTip("向上：显示在更上层")
        self.down_button.setToolTip("向下：显示在更下层")
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            tools.addWidget(button)
        self.remove_button.clicked.connect(self.remove_image)
        self.up_button.clicked.connect(lambda: self.move_layer(-1))
        self.down_button.clicked.connect(lambda: self.move_layer(1))
        layout.addLayout(tools)
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("batch_video_layers")
        self.list.setMinimumHeight(125)
        self.list.setMaximumHeight(200)
        self.list.setIconSize(QtCore.QSize(28, 28))
        self.list.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.list.currentRowChanged.connect(self.select_layer)
        self.list.itemChanged.connect(self._item_changed)
        self.list.model().rowsMoved.connect(self._reordered)
        layout.addWidget(self.list)
        background = QtWidgets.QLabel("🔒 背景视频（固定最底层，压暗只作用于背景）")
        background.setWordWrap(True)
        background.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(background)
        text_top = QtWidgets.QPushButton("文字置顶")
        text_top.setToolTip("把所有文本框、标题和正文移到图片上方，避免图片遮住文案。")
        text_top.clicked.connect(self.text_to_top)
        layout.addWidget(text_top)
        common = QtWidgets.QFormLayout()
        common.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        common.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("图层名称")
        self.name.editingFinished.connect(self._name_changed)
        common.addRow("图层名", self.name)
        self.opacity = self._spin(0, 100)
        self.opacity.valueChanged.connect(self._property_changed)
        common.addRow("不透明度", self.opacity)
        layout.addLayout(common)
        self.geometry = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.geometry)
        form.setContentsMargins(0,0,0,0)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.units = Combo()
        self.units.addItem("百分比 %","percent")
        self.units.addItem("像素 px","pixels")
        self.units.setCurrentIndex(max(0,self.units.findData(self.settings.get("geometry_units","percent"))))
        self.units.setToolTip("像素按当前导出画布计算。原点在左上角，位置表示框中心；切换单位不改变布局。更换画布尺寸时仍按原比例适配。")
        self.units.currentIndexChanged.connect(self._units_changed)
        form.addRow("位置单位",self.units)
        self.canvas_label = QtWidgets.QLabel()
        self.canvas_label.setWordWrap(True)
        self.canvas_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.canvas_label)
        self.properties = {}
        for key, title, low in (("x", "横向中心", 0), ("y", "纵向中心", 0),
                                ("width", "框宽度", 1), ("height", "框高度", 1)):
            spin = self._spin(low, 100)
            spin.valueChanged.connect(lambda _value,key=key:self._geometry_changed(key))
            self.properties[key] = spin
            form.addRow(title,spin)
        self.content_editor = ContentSourceEditor()
        self.content_editor.changed.connect(self._content_changed)
        self.content_editor.addImages.connect(self.append_sequence_images)
        self.content_editor.selectCopy.connect(self.copyRequested)
        self.text_properties = TextBoxProperties()
        self.text_properties.changed.connect(self._text_properties_changed)
        self.property_tabs = QtWidgets.QTabWidget()
        self.property_tabs.addTab(self.text_properties,"排版")
        self.property_tabs.addTab(self.geometry,"位置")
        self.property_tabs.addTab(self.content_editor,"内容")
        layout.addWidget(self.property_tabs)
        self.hint = QtWidgets.QLabel("直接在预览中拖动框内移动，拖边或角缩放；Esc取消本次拖动。位置单位可切换，对齐在框内生效。图片保持原比例。")
        self.hint.setWordWrap(True)
        self.hint.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.hint)
        layout.addStretch()
        self.rebuild()

    def _spin(self, low, high):
        spin = Spin()
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSuffix(" %")
        return spin

    def values(self):
        return copy.deepcopy(self._layers)

    def set_canvas_size(self,width,height):
        size = QtCore.QSize(int(width),int(height))
        if size != self.canvas_size:
            self.canvas_size = size
            self._refresh_geometry()

    def _refresh_geometry(self):
        loading,self._loading = self._loading,True
        layer = self.current()
        pixels = self.units.currentData() == "pixels"
        self.canvas_label.setText(f"画布：{self.canvas_size.width()} × {self.canvas_size.height()} px；100% = 整个画布。")
        for key,spin in self.properties.items():
            extent = self.canvas_size.width() if key in {"x","width"} else self.canvas_size.height()
            factor = extent/100 if pixels else 1
            spin.setDecimals(2 if pixels else 3)
            spin.setRange((1 if key in {"width","height"} else 0)*factor,100*factor)
            spin.setSingleStep(1 if pixels else .1)
            spin.setSuffix(" px" if pixels else " %")
            if layer:
                spin.setValue(layer[key]*factor)
                spin.setToolTip(f"实际值：{layer[key]*extent/100:.2f} px；{layer[key]:.3f}%")
        self._loading = loading

    def _units_changed(self):
        if not self._loading:
            self._refresh_geometry()
            self.changed.emit()

    def _geometry_changed(self,key):
        layer = self.current()
        if self._loading or not layer:
            return
        extent = self.canvas_size.width() if key in {"x","width"} else self.canvas_size.height()
        value = self.properties[key].value()
        layer[key] = value*100/extent if self.units.currentData() == "pixels" else value
        self.changed.emit()

    def select_layer_id(self,identifier):
        for row in range(self.list.count()):
            if self.list.item(row).data(QtCore.Qt.ItemDataRole.UserRole) == identifier:
                self.list.setCurrentRow(row)
                return

    def set_geometry(self,identifier,values):
        layer = next((item for item in self._layers if item["id"] == identifier),None)
        if not layer:
            return False
        candidate = {**layer,**{key:values[key] for key in ("x","y","width","height")}}
        normalized = normalize_layers([candidate])[0]
        if all(layer[key] == normalized[key] for key in ("x","y","width","height")):
            return False
        layer.update({key:normalized[key] for key in ("x","y","width","height")})
        if self.current() is layer:
            self._refresh_geometry()
        self.changed.emit()
        return True

    def set_layers(self,layers):
        selected = self.current()
        self._layers = componentize_layers(layers,self.settings)
        self.rebuild(selected["id"] if selected else None)

    def add_text(self):
        if sum(layer["kind"] == "text" for layer in self._layers) >= MAX_TEXT_BOXES:
            self.message.emit(f"最多允许{MAX_TEXT_BOXES}个文本框。")
            return
        layer = new_text_component()
        self._layers.append(layer)
        self.rebuild(layer["id"])
        self.changed.emit()

    def add_rotating_text(self):
        if sum(layer["kind"] == "text" for layer in self._layers) >= MAX_TEXT_BOXES:
            self.message.emit(f"最多允许{MAX_TEXT_BOXES}个文本框。")
            return
        layer = new_text_component()
        layer.update(source="pool", name="轮换文本框（文案池）", pool_field="full")
        self._layers.append(layer)
        self.rebuild(layer["id"])
        self.changed.emit()
        self.property_tabs.setCurrentIndex(2)

    def set_fixed_text(self, identifier, value):
        layer = next((item for item in self._layers if item["id"] == identifier), None)
        if layer and layer["kind"] != "image":
            layer.update(source="fixed", text=value)
            self.rebuild(identifier)
            self.changed.emit()

    def current(self):
        item = self.list.currentItem()
        identifier = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        return next((layer for layer in self._layers if layer["id"] == identifier), None)

    def rebuild(self, selected=None):
        layer = self.current()
        selected = selected or (layer["id"] if layer else None)
        self._layers = normalize_layers(self._layers)
        self._loading = True
        self.list.clear()
        for row, layer in enumerate(reversed(self._layers)):
            suffix = " · 轮换" if layer["source"] in {"sequence", "pool"} else ""
            item = QtWidgets.QListWidgetItem(layer["name"]+suffix)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, layer["id"])
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if layer["enabled"] else QtCore.Qt.CheckState.Unchecked)
            if layer["kind"] == "image":
                source = (layer["sequence_items"][layer["sequence_cursor"]]
                          if layer["source"] in {"sequence", "pool"} and layer["sequence_items"] else layer)
                item.setToolTip(source.get("path","") + "\n图片副本；拖动可调整覆盖顺序。")
                if source.get("thumbnail"):
                    item.setIcon(QtGui.QIcon(source["thumbnail"]))
            else:
                item.setToolTip("选中后在“排版、位置、内容”中设置；标题和正文默认读取任务文案。")
            self.list.addItem(item)
            if layer["id"] == selected:
                self.list.setCurrentRow(row)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._loading = False
        self.select_layer(self.list.currentRow())

    def select_layer(self, _row):
        if self._loading:
            return
        layer = self.current()
        self._loading = True
        geometry = bool(layer)
        text = bool(layer and layer["kind"] in TEXT_KINDS)
        self.property_tabs.setEnabled(geometry)
        self.property_tabs.setTabEnabled(0,text)
        if not text and self.property_tabs.currentIndex() == 0:
            self.property_tabs.setCurrentIndex(1)
        elif text and (not self.text_properties.layer or self.text_properties.layer["kind"] == "image"):
            self.property_tabs.setCurrentIndex(0)
        self.remove_button.setEnabled(bool(layer and layer["kind"] in {"image","text"}))
        self.name.setEnabled(bool(layer))
        self.opacity.setEnabled(bool(layer))
        if layer:
            self.name.setText(layer["name"])
            self.opacity.setValue(layer["opacity"])
            if geometry:
                self._refresh_geometry()
        self.content_editor.set_layer(layer)
        self.text_properties.set_layer(layer)
        row = self.list.currentRow()
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.list.count()-1)
        self._loading = False
        self.selectionChanged.emit(layer["id"] if layer else "")

    def _property_changed(self):
        layer = self.current()
        if self._loading or not layer:
            return
        layer["opacity"] = self.opacity.value()
        self.changed.emit()

    def _content_changed(self,updated):
        layer = next((item for item in self._layers if item["id"] == updated["id"]),None)
        if layer and not self._loading:
            for key in ("source","text","sequence_items","sequence_cursor","pool_field"):
                if key in updated:
                    layer[key] = copy.deepcopy(updated[key])
            item = self.list.currentItem()
            if item:
                self._loading = True
                item.setText(layer["name"]+(" · 轮换" if layer["source"] in {"sequence", "pool"} else ""))
                self._loading = False
            self.changed.emit()

    def _text_properties_changed(self,values):
        layer = self.current()
        if layer and layer["kind"] in TEXT_KINDS and not self._loading:
            layer.update(values)
            self.changed.emit()

    def _name_changed(self):
        layer = self.current()
        if not self._loading and layer and self.name.text().strip():
            layer["name"] = self.name.text().strip()[:120]
            self.rebuild(layer["id"])
            self.changed.emit()

    def _item_changed(self, item):
        if self._loading:
            return
        layer = next((value for value in self._layers if value["id"] == item.data(QtCore.Qt.ItemDataRole.UserRole)), None)
        if layer:
            layer["enabled"] = item.checkState() == QtCore.Qt.CheckState.Checked
            self.changed.emit()

    def _reordered(self, *_args):
        if self._loading:
            return
        by_id = {layer["id"]: layer for layer in self._layers}
        self._layers = [by_id[self.list.item(row).data(QtCore.Qt.ItemDataRole.UserRole)]
                        for row in reversed(range(self.list.count()))]
        self.select_layer(self.list.currentRow())
        self.changed.emit()

    def move_layer(self, direction):
        row, target = self.list.currentRow(), self.list.currentRow()+direction
        if not 0 <= target < self.list.count() or row < 0:
            return
        self._loading = True
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)
        self._loading = False
        self._reordered()

    def text_to_top(self):
        selected = self.current()
        images = [layer for layer in self._layers if layer["kind"] == "image"]
        texts = [layer for layer in self._layers if layer["kind"] != "image"]
        self._layers = images + texts
        self.rebuild(selected["id"] if selected else None)
        self.changed.emit()

    def remove_image(self):
        layer = self.current()
        if layer and layer["kind"] in {"image","text"}:
            self._layers.remove(layer)
            self.rebuild()
            self.changed.emit()

    def choose_images(self,as_sequence=False):
        title = "创建图片序列，可多选" if as_sequence else "添加图片框，每张一个，可多选"
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, title, "",
            "图片 (*.png *.jpg *.jpeg *.jfif *.webp *.bmp *.tif *.tiff)")
        if paths:
            self.add_images(paths,as_sequence=as_sequence)

    def append_sequence_images(self,identifier):
        paths,_ = QtWidgets.QFileDialog.getOpenFileNames(self,"追加到图片序列", "",
            "图片 (*.png *.jpg *.jpeg *.jfif *.webp *.bmp *.tif *.tiff)")
        if paths:
            self.add_images(paths,as_sequence=True,target_id=identifier)

    def add_images(self, paths, as_sequence=False, target_id=None):
        if self.worker:
            return False
        remaining = MAX_IMAGES - sum(layer["kind"] == "image" for layer in self._layers)
        if as_sequence:
            target = next((item for item in self._layers if item["id"] == target_id),None)
            if not remaining and not target:
                self.message.emit(f"图片组件已达到{MAX_IMAGES}个，请先移除一些。")
                return False
            remaining = MAX_SEQUENCE_ITEMS-len(target["sequence_items"]) if target else MAX_SEQUENCE_ITEMS
        paths = list(paths)
        if not remaining:
            self.message.emit(f"序列已达到{MAX_SEQUENCE_ITEMS}项，请先移除一些。" if as_sequence else
                              f"图片图层已达到{MAX_IMAGES}个，请先移除一些。")
            return False
        if len(paths) > remaining:
            self.message.emit(f"本次只导入前{remaining}张；"+(
                f"每个序列最多{MAX_SEQUENCE_ITEMS}项。" if as_sequence else f"最多{MAX_IMAGES}个图片图层。"))
            paths = paths[:remaining]
        if not paths:
            return False
        self._result = None
        self._import_as_sequence,self._import_target = as_sequence,target_id
        self.worker = ImageImportWorker(paths, self.directory, self)
        self.worker.ready.connect(self._ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._finished)
        self.busyChanged.emit(True)
        self.worker.start()
        return True

    def _ready(self, result):
        self._result = result

    def _finished(self):
        self.worker.deleteLater()
        self.worker = None
        result = self._result or {"layers": [], "errors": ["图片导入没有返回结果。"]}
        if not result.get("cancelled"):
            # New pictures always start below both text layers, not above the copy.
            first_text = next((index for index, layer in enumerate(self._layers) if layer["kind"] != "image"), len(self._layers))
            imported = result["layers"]
            selected = imported[-1]["id"] if imported else None
            if self._import_as_sequence and imported:
                target = next((item for item in self._layers if item["id"] == self._import_target),None)
                entries = [{key:layer[key] for key in ("id","path","thumbnail","name")} for layer in imported]
                if target:
                    if not target["sequence_items"] and target.get("path"):
                        target["sequence_items"] = [{key:target[key] for key in ("id","path","name")}
                            | {"thumbnail":target.get("thumbnail","")}]
                    target["sequence_items"].extend(entries)
                    target["source"] = "sequence"
                    selected = target["id"]
                elif self._import_target:
                    self.message.emit("目标组件已移除，本次没有追加图片。")
                else:
                    target = {**imported[0],"source":"sequence","sequence_items":entries,
                              "sequence_cursor":0,"name":"图片序列"}
                    self._layers.insert(first_text,target)
                    selected = target["id"]
            else:
                self._layers[first_text:first_text] = imported
            self.rebuild(selected)
        self.busyChanged.emit(False)
        self.changed.emit()
        self.message.emit("已取消图片导入。" if result.get("cancelled") else (
            f"导入{len(result['layers'])}张序列图片，逐视频轮换。" if self._import_as_sequence else
            f"添加了{len(result['layers'])}个图片图层；整批任务共用。"))
        for error in result.get("errors", []):
            self.message.emit(error)

    def cancel_work(self):
        if self.worker:
            self.worker.cancel.set()

    def dragEnterEvent(self, event):
        if not self.worker and event.mimeData().hasUrls() and all(url.isLocalFile() and Path(url.toLocalFile()).suffix.casefold() in IMAGE_SUFFIXES for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths and not self.worker:
            self.add_images(paths)
            event.acceptProposedAction()

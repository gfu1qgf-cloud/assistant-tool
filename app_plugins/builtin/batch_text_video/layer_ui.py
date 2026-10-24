"""Shared batch layers, with queued image import and explicit stacking controls."""
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import copy
from pathlib import Path
import threading
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from .layers import MAX_IMAGES, MAX_TEXT_BOXES, TEXT_KINDS, normalize_layers, componentize_layers
from .components import MAX_SEQUENCE_ITEMS, new_text_component
from .component_ui import Combo
from .layer_drag import LayerList, VisualDropArea, accept_copy, visual_file_paths


class ImageImportWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, paths, directory, parent=None):
        super().__init__(parent)
        self.paths, self.directory = list(paths), directory
        self.cancel = threading.Event()

    def run(self):
        try:
            from .visual_media import import_visuals
            self.ready.emit(import_visuals(self.paths, self.directory, self.cancel))
        except Exception:
            self.ready.emit({"layers": [], "errors": [traceback.format_exc()], "cancelled": False})


class Spin(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class LayerPanel(QtWidgets.QWidget):
    convertRequested = QtCore.pyqtSignal(str)
    changed = QtCore.pyqtSignal()
    busyChanged = QtCore.pyqtSignal(bool)
    message = QtCore.pyqtSignal(str)
    selectionChanged = QtCore.pyqtSignal(str)
    copyRequested = QtCore.pyqtSignal(str)
    imagePoolRequested = QtCore.pyqtSignal(str)
    mattingRequested = QtCore.pyqtSignal(str)

    def _component_menu(self, position):
        item = self.list.itemAt(position)
        if item is None or not self.isEnabled(): return
        self.list.setCurrentItem(item)
        layer = self.current()
        if not layer: return
        menu = QtWidgets.QMenu(self.list)
        text = '编辑节点工作流…' if layer['kind'] == 'node_effect' else '转换为节点效果…'
        action = menu.addAction(text)
        action.setEnabled(self.receivers(self.convertRequested) > 0)
        action.triggered.connect(lambda: self.convertRequested.emit(layer['id']))
        menu.exec(self.list.viewport().mapToGlobal(position))

    def __init__(self, layers, directory, parent=None, settings=None, services=None, inspector_mode="batch"):
        super().__init__(parent)
        self.directory = directory
        self.settings = copy.deepcopy(settings or {})
        from .background_visual import control
        self._background = control(self.settings)
        self._background_selected = False
        self.canvas_size = QtCore.QSize(1920,1080) if self.settings.get("aspect") == "landscape" else QtCore.QSize(1080,1920)
        self._layers = componentize_layers(layers,self.settings)
        self._loading, self.worker = False, None
        self._catalog_hidden = False
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        self.catalog = VisualDropArea(self._can_import_visuals, self.add_images)
        layout = QtWidgets.QVBoxLayout(self.catalog)
        layout.setContentsMargins(0,0,0,0)
        note = QtWidgets.QLabel("上方图层盖住下方。\n固定内容整批共用；轮换内容从资源池逐视频取用。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(note)
        tools = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("添加图片…")
        menu = QtWidgets.QMenu(self.add_button)
        menu.addAction("添加文本框",self.add_text)
        menu.addAction("添加滚动文字框", self.add_scrolling_text)
        menu.addAction("添加动态字幕…",self.add_caption)
        menu.addAction('添加朗读跟随字幕', self.add_follow_caption)
        menu.addAction("四角水印…",self.edit_corner_watermarks)
        menu.addAction("添加图片／视频／GIF…",self.choose_images)
        menu.addAction("添加节点效果…", self.add_node_effect)
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
        self.list = LayerList(self._can_import_visuals, self.add_images)
        from .thumbnails import attach
        attach(self.list)
        self.list.setObjectName("batch_video_layers")
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._component_menu)
        self.list.setMinimumHeight(125)
        self.list.setMaximumHeight(200)
        self.list.setIconSize(QtCore.QSize(28, 28))
        self.list.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self.select_layer)
        self.list.itemChanged.connect(self._item_changed)
        self.list.model().rowsMoved.connect(self._reordered)
        layout.addWidget(self.list)
        self.background_button = QtWidgets.QPushButton("底层背景 · 视觉控件")
        self.background_button.setCheckable(True)
        self.background_button.setToolTip("点击调整底层背景的动画；全局背景池在项目设置中选择。")
        self.background_button.clicked.connect(self.select_background)
        layout.addWidget(self.background_button)
        text_top = QtWidgets.QPushButton("文字置顶")
        text_top.setToolTip("把所有文本框、标题和正文移到图片上方，避免图片遮住文案。")
        text_top.clicked.connect(self.text_to_top)
        layout.addWidget(text_top)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.catalog)
        self.header = QtWidgets.QWidget()
        common = QtWidgets.QFormLayout(self.header)
        common.setContentsMargins(0,0,0,0)
        common.setFieldGrowthPolicy(common.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        common.setRowWrapPolicy(common.RowWrapPolicy.WrapLongRows)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText('控件名称')
        self.name.editingFinished.connect(self._name_changed)
        common.addRow('控件名称', self.name)
        self.opacity = self._spin(0,100)
        self.opacity.valueChanged.connect(self._property_changed)
        common.addRow('不透明度', self.opacity)
        layout.addWidget(self.header)
        self.units = Combo(self)
        self.units.addItem('百分比 %','percent'); self.units.addItem('像素 px','pixels')
        self.units.setCurrentIndex(max(0,self.units.findData(self.settings.get('geometry_units','percent'))))
        self.units.hide()
        self.units.currentIndexChanged.connect(self._units_changed)
        self._pool_state = {'resource_pools':[]}
        from .component_inspector import ComponentInspector
        self.inspector = ComponentInspector(services, mode=inspector_mode)
        self.inspector.mattingRequested.connect(self.mattingRequested)
        self.inspector.edited.connect(self._inspector_edited)
        self.inspector.unitChanged.connect(lambda unit: self.units.setCurrentIndex(self.units.findData(unit)))
        self.inspector.addImages.connect(self.append_sequence_images)
        self.inspector.selectCopy.connect(self.copyRequested)
        self.inspector.selectImagePool.connect(self.imagePoolRequested)
        self.inspector.message.connect(self.message)
        layout.addWidget(self.inspector)
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


    def background_values(self):
        from .background_visual import values
        return values(self._background)

    def set_background(self, settings):
        from .background_visual import control
        self._background = control(settings)
        if self._background_selected: self.select_background()

    def select_background(self):
        if not self.isEnabled(): return
        blocker = QtCore.QSignalBlocker(self.list)
        self.list.setCurrentRow(-1); self.list.clearSelection()
        del blocker
        self._background_selected = True
        self.select_layer(-1)

    def values(self):
        return copy.deepcopy(self._layers)

    def set_canvas_size(self,width,height):
        size = QtCore.QSize(int(width),int(height))
        if size != self.canvas_size:
            self.canvas_size = size
            self._refresh_geometry()
            editor = self.inspector.active
            if editor and hasattr(editor,'watermark_properties'):
                editor.watermark_properties.set_canvas_size(size.width(),size.height())

    def _refresh_geometry(self):
        if self.inspector.active:
            self.inspector.active.sync_geometry(self.current(),self.canvas_size,self.units.currentData())

    def _units_changed(self):
        if not self._loading:
            self._refresh_geometry()
            self.changed.emit()



    def snap_to_edges(self,horizontal=None,vertical=None):
        editor = self.inspector.active
        if editor and editor.geometry_editor:
            editor.geometry_editor.snap_to_edges(horizontal,vertical)

    def select_layer_id(self,identifier):
        if identifier == self._background["id"]:
            self.select_background(); return
        for row in range(self.list.count()):
            if self.list.item(row).data(QtCore.Qt.ItemDataRole.UserRole) == identifier:
                self.list.setCurrentRow(row)
                return

    def set_geometry(self,identifier,values):
        layer = next((item for item in self._layers if item["id"] == identifier),None)
        if not layer or layer["kind"] == "watermark":
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

    def add_caption(self):
        from .captions import new_caption, MAX_CAPTIONS
        if sum(layer["kind"] == "caption" for layer in self._layers) >= MAX_CAPTIONS:
            self.message.emit(f"最多允许{MAX_CAPTIONS}个动态字幕组件。")
            return
        layer = new_caption()
        self._layers.append(layer)
        self.rebuild(layer["id"])
        self.reveal_property('caption')
        self.changed.emit()

    def add_scrolling_text(self):
        if sum(layer["kind"] == "text" for layer in self._layers) >= MAX_TEXT_BOXES:
            self.message.emit(f"最多允许{MAX_TEXT_BOXES}个文本框。")
            return
        from .scrolling_text import new_scrolling_component
        layer = new_scrolling_component()
        self._layers.append(layer)
        self.rebuild(layer["id"])
        self.reveal_property('scroll')
        self.changed.emit()

    def add_follow_caption(self):
        from .captions import MAX_CAPTIONS
        from .caption_follow import new_follow_caption
        if sum(layer['kind']=='caption' for layer in self._layers)>=MAX_CAPTIONS:
            self.message.emit(f'最多允许{MAX_CAPTIONS}个动态字幕组件。');return
        layer=new_follow_caption()
        self._layers.append(layer);self.rebuild(layer['id'])
        self.reveal_property('caption_follow');self.changed.emit()



    def edit_corner_watermarks(self):
        from .watermarks import new_watermark, OBJECT_ID
        existing = next((layer for layer in self._layers if layer["kind"] == "watermark"), None)
        if existing:
            self.select_layer_id(existing["id"])
            self.reveal_property('watermark')
            return
        if any(layer["id"] == OBJECT_ID for layer in self._layers):
            self.message.emit("水印编号被其他图层占用，请先移除冲突图层。")
            return
        layer = new_watermark()
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
        self.reveal_property('source')

    def set_fixed_text(self, identifier, value):
        layer = next((item for item in self._layers if item["id"] == identifier), None)
        if layer and layer["kind"] in TEXT_KINDS:
            layer.update(source="fixed", text=value)
            self.rebuild(identifier)
            self.changed.emit()

    def current(self):
        if self._background_selected: return self._background
        item = self.list.currentItem()
        identifier = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        return next((layer for layer in self._layers if layer["id"] == identifier), None)

    def add_node_effect(self):
        from .node_effects import new_effect
        if sum(layer['kind'] == 'node_effect' for layer in self._layers) >= 8:
            self.message.emit('每个配置最多允许8个节点效果组件。'); return
        layer = new_effect(); self._layers.append(layer)
        self.rebuild(layer['id']); self.reveal_property('node'); self.changed.emit()


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
                from .thumbnails import ROLE, descriptor
                item.setData(ROLE, descriptor(source.get('path'), source.get('thumbnail')))
            elif layer["kind"] == "watermark":
                item.setToolTip("独立四角水印组件；内容、样式和位置在“水印设置”中统一管理。")
            elif layer["kind"] == "caption":
                item.setToolTip("跟随当前任务人声，字号可随机；在右侧“字幕样式、时间与动画”中选择预设和对齐来源。播放预览可核对。")
            elif layer['kind'] == 'node_effect':
                item.setToolTip('节点效果：在此图层位置执行选定工作流，播放预览与导出共用，不重复编码。')
            else:
                item.setToolTip("选中后在右侧属性区设置字体、位置与来源；标题和正文默认读取任务文案。")
            self.list.addItem(item)
            if layer["id"] == selected:
                self.list.setCurrentRow(row)
        if self._background_selected:
            self.list.setCurrentRow(-1); self.list.clearSelection()
        elif self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._loading = False
        self.select_layer(self.list.currentRow())

    def select_layer(self,_row):
        if self._loading: return
        if _row >= 0: self._background_selected = False
        self.background_button.setChecked(self._background_selected)
        layer = self.current()
        self._loading = True
        try:
            self.header.setEnabled(bool(layer))
            self.header.setVisible(not self._catalog_hidden and bool(layer and layer['kind'] not in {'caption', 'background'}))
            self.name.setText(layer['name'] if layer else '')
            self.opacity.setValue(layer['opacity'] if layer else 100)
            self.inspector.show_layer(layer,self.canvas_size,self._pool_state,self.units.currentData())
            kind=layer['kind'] if layer else ''
            self.hint.setText('底层背景固定铺在所有图层下方；背景池在项目设置选择，动画在本控件中调整。' if kind=='background' else
                '效果顺序由图层位置决定；元素的位置和样式在工作流中调整。' if kind=='node_effect' else
                '先选择要调整的角落，再设置来源、样式和内缩；四角文字可以直接填写。' if kind=='watermark' else
                '直接在预览中拖动框内移动，拖边或角缩放；Esc取消本次拖动。位置单位可切换，对齐在框内生效。图片保持原比例。')
            self.remove_button.setEnabled(bool(layer and layer['kind'] in {'image','text','watermark','caption','node_effect'}))
            row = self.list.currentRow()
            self.up_button.setEnabled(row > 0 and not self._background_selected)
            self.down_button.setEnabled(0 <= row < self.list.count()-1 and not self._background_selected)
        finally:
            self._loading = False
        self.selectionChanged.emit(layer['id'] if layer else '')

    def _property_changed(self):
        layer = self.current()
        if not self._loading and layer:
            self._inspector_edited(layer['id'],{'opacity':self.opacity.value()})

    def _inspector_edited(self,identifier,patch):
        if self._loading: return
        layer = self.current()
        # Cached/hidden editors cannot write a stale snapshot into another layer.
        if not layer or layer['id'] != identifier: return
        try:
            if layer['kind'] == 'background':
                from .background_visual import control
                candidate = control({'background_visual': {**layer, **copy.deepcopy(patch)}})
            else:
                candidate = normalize_layers([{**layer,**copy.deepcopy(patch),'id':layer['id'],'kind':layer['kind']}])[0]
        except ValueError as error:
            self.message.emit('控件设置未应用：'+str(error))
            return
        layer.update(candidate)
        self._loading = True
        try:
            self.inspector.show_layer(layer,self.canvas_size,self._pool_state,self.units.currentData())
            item = self.list.currentItem()
            if item:
                item.setText(layer['name']+(' · 轮换' if layer['source'] in {'sequence','pool'} else ''))
                item.setCheckState(QtCore.Qt.CheckState.Checked if layer['enabled'] else QtCore.Qt.CheckState.Unchecked)
        finally:
            self._loading = False
        self.changed.emit()

    @property
    def editor(self):
        return self.inspector.active

    def reveal_property(self,key):
        self.inspector.reveal(key)

    def hide_catalog(self):
        self._catalog_hidden = True
        self.catalog.hide()
        self.header.hide()



    def set_pool_state(self,state):
        self._pool_state = state
        editor = self.inspector.active
        if editor:
            if hasattr(editor,'content_editor'): editor.content_editor.set_state(state)
            if hasattr(editor,'watermark_properties'): editor.watermark_properties.set_pool_state(state)



    @property
    def text_properties(self):
        return getattr(self.editor,"text_properties",None) if self.editor else None

    @property
    def content_editor(self):
        return getattr(self.editor,"content_editor",None) if self.editor else None

    @property
    def caption_properties(self):
        return getattr(self.editor,"caption_properties",None) if self.editor else None

    @property
    def scroll_properties(self):
        return getattr(self.editor,"scroll_properties",None) if self.editor else None

    @property
    def visual_properties(self):
        return getattr(self.editor,"visual_properties",None) if self.editor else None

    @property
    def watermark_properties(self):
        return getattr(self.editor,"watermark_properties",None) if self.editor else None

    @property
    def node_effect_properties(self):
        return getattr(self.editor,"node_effect_properties",None) if self.editor else None

    @property
    def properties(self):
        return getattr(self.editor.geometry_editor,"properties")

    @property
    def anchor_controls(self):
        return getattr(self.editor.geometry_editor,"anchor_controls")

    @property
    def canvas_label(self):
        return getattr(self.editor.geometry_editor,"canvas_label")

    @property
    def snap_button(self):
        return getattr(self.editor.geometry_editor,"snap_button")

    @property
    def geometry_labels(self):
        return getattr(self.editor.geometry_editor,"geometry_labels")

    def _name_changed(self):
        layer = self.current()
        name = self.name.text().strip()[:120]
        if not self._loading and layer and name and name != layer['name']:
            self._inspector_edited(layer['id'], {'name': name})

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
        if self._background_selected: return
        row, target = self.list.currentRow(), self.list.currentRow()+direction
        self.list.move_row(row, target)

    def text_to_top(self):
        selected = self.current()
        images = [layer for layer in self._layers if layer["kind"] == "image"]
        texts = [layer for layer in self._layers if layer["kind"] != "image"]
        self._layers = images + texts
        self.rebuild(selected["id"] if selected else None)
        self.changed.emit()

    def remove_image(self):
        layer = self.current()
        if layer and layer["kind"] in {"image","text","watermark","caption","node_effect"}:
            self._layers.remove(layer)
            self.rebuild()
            self.changed.emit()

    def choose_images(self,as_sequence=False):
        title = "创建图片序列，可多选" if as_sequence else "添加图片框，每张一个，可多选"
        from .visual_media import VISUAL_FILTER
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加图片／视频／GIF", "", VISUAL_FILTER)
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
        connect_thread_finished(self.worker, self, 'worker', self._finished)
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

    def _can_import_visuals(self):
        return self.isEnabled() and self.catalog.isEnabled() and self.worker is None

    def dragEnterEvent(self, event):
        if self._can_import_visuals() and visual_file_paths(event.mimeData()):
            accept_copy(event)
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        paths = visual_file_paths(event.mimeData())
        if paths and self._can_import_visuals() and self.add_images(paths):
            accept_copy(event)
        else:
            event.ignore()

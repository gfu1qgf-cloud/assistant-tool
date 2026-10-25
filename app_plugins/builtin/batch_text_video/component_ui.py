"""Reusable content-source and text-box widgets, separated from the layer host."""
import copy
from pathlib import Path
import uuid
from qt_compat import QtCore, QtGui, QtWidgets
from .components import CONTENT_FIELDS, MAX_SEQUENCE_ITEMS
from .fonts import ensure_bundled_fonts, font_weight_status


class Combo(QtWidgets.QComboBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(self.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Fixed)

    def wheelEvent(self,event):
        event.ignore()


class WeightCombo(Combo):
    """Named presets with an exact, editable OpenType/Qt weight value."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(self.InsertPolicy.NoInsert)
        self.lineEdit().setValidator(QtGui.QIntValidator(1,1000,self))
        self.lineEdit().setPlaceholderText("输入 1–1000")
        self.lineEdit().installEventFilter(self)

    def currentData(self,role=QtCore.Qt.ItemDataRole.UserRole):
        if role != QtCore.Qt.ItemDataRole.UserRole:
            return super().currentData(role)
        text = self.currentText().strip()
        index = self.findText(text,QtCore.Qt.MatchFlag.MatchExactly)
        if index >= 0:
            return self.itemData(index,role)
        if text.isascii() and text.isdecimal() and 1 <= int(text) <= 1000:
            return int(text)
        return None

    def setValue(self,value):
        index = self.findData(value)
        self.setCurrentIndex(index)
        if index < 0:
            self.setEditText(str(value))

    def eventFilter(self,watched,event):
        if watched is self.lineEdit() and event.type() == QtCore.QEvent.Type.KeyPress and event.key() in (
                QtCore.Qt.Key.Key_Return,QtCore.Qt.Key.Key_Enter):
            # Editing a weight must never activate a dialog's default export button.
            event.accept()
            return True
        return super().eventFilter(watched,event)


class Spin(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self,event):
        event.ignore()


class FontCombo(QtWidgets.QFontComboBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        # Font names must not determine the width of the entire scroll page.
        self.setSizeAdjustPolicy(self.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Fixed)

    def wheelEvent(self,event):
        event.ignore()


class SequenceDialog(QtWidgets.QDialog):
    def __init__(self, layer, parent=None):
        super().__init__(parent)
        self.setWindowTitle(layer["name"]+" · 自定义轮换内容")
        self.resize(610,520)
        self.items = copy.deepcopy(layer["sequence_items"])
        self.field = CONTENT_FIELDS[layer["kind"]]
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("每成功生成一个视频，使用下一项；用完循环。可拖动排序。\n多行文字是一条内容，不会按换行拆成多个视频。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QtWidgets.QListWidget()
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.list.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.list.currentRowChanged.connect(self.select_item)
        self.list.model().rowsMoved.connect(self.sync_order)
        layout.addWidget(self.list,1)
        self.edit = QtWidgets.QPlainTextEdit()
        self.edit.setPlaceholderText("当前条目的完整文字，支持换行")
        self.edit.setMaximumHeight(145)
        self.edit.setVisible(self.field == "text")
        self.edit.textChanged.connect(self.edit_item)
        layout.addWidget(self.edit)
        buttons = QtWidgets.QHBoxLayout()
        if self.field == "text":
            add = QtWidgets.QPushButton("新增一条")
            add.clicked.connect(self.add_item)
            buttons.addWidget(add)
        else:
            label = QtWidgets.QLabel("追加图片请使用组件面板中的“追加图片…”")
            label.setWordWrap(True)
            buttons.addWidget(label)
        remove = QtWidgets.QPushButton("移除选中项")
        remove.clicked.connect(self.remove_item)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                         QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.loading = False
        self.rebuild()

    def summary(self,item):
        return (item["text"].replace("\n"," ")[:160] or "（空白文字）") if self.field == "text" else item.get("name") or Path(item[self.field]).name

    def rebuild(self,selected=0):
        self.loading = True
        self.list.clear()
        for item in self.items:
            row = QtWidgets.QListWidgetItem(self.summary(item))
            row.setData(QtCore.Qt.ItemDataRole.UserRole,item["id"])
            row.setToolTip(item[self.field])
            self.list.addItem(row)
        self.loading = False
        if self.items:
            self.list.setCurrentRow(min(selected,len(self.items)-1))
        else:
            self.select_item(-1)

    def select_item(self,row):
        if self.loading:
            return
        self.loading = True
        self.edit.setEnabled(0 <= row < len(self.items))
        self.edit.setPlainText(self.items[row]["text"] if self.field == "text" and 0 <= row < len(self.items) else "")
        self.loading = False

    def edit_item(self):
        row = self.list.currentRow()
        if not self.loading and 0 <= row < len(self.items):
            self.items[row]["text"] = self.edit.toPlainText()
            self.list.item(row).setText(self.summary(self.items[row]))
            self.list.item(row).setToolTip(self.items[row]["text"])

    def sync_order(self,*_args):
        if not self.loading:
            by_id = {item["id"]:item for item in self.items}
            self.items = [by_id[self.list.item(row).data(QtCore.Qt.ItemDataRole.UserRole)] for row in range(self.list.count())]
            self.select_item(self.list.currentRow())

    def add_item(self):
        if len(self.items) >= MAX_SEQUENCE_ITEMS:
            return
        self.items.append({"id":uuid.uuid4().hex,"text":""})
        self.rebuild(len(self.items)-1)
        self.edit.setFocus()

    def remove_item(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.items):
            self.items.pop(row)
            self.rebuild(row)


class ContentSourceEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)
    addImages = QtCore.pyqtSignal(str)
    selectCopy = QtCore.pyqtSignal(str)
    selectImagePool = QtCore.pyqtSignal(str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Maximum)
        self.layer,self.loading = None,False
        self.pool_state = {"resource_pools": []}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.source = Combo()
        self.source.setMinimumWidth(0)
        self.source.setMinimumContentsLength(8)
        self.source.setSizeAdjustPolicy(self.source.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.source.currentIndexChanged.connect(self.source_changed)
        source_row = QtWidgets.QHBoxLayout()
        source_row.addWidget(QtWidgets.QLabel("来源"))
        source_row.addWidget(self.source, 1)
        layout.addLayout(source_row)
        self.file_row = QtWidgets.QWidget()
        file_layout = QtWidgets.QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path = QtWidgets.QLineEdit()
        self.file_path.setReadOnly(True)
        browse = QtWidgets.QPushButton("选择文件…")
        browse.clicked.connect(self.choose_file)
        file_layout.addWidget(self.file_path, 1)
        file_layout.addWidget(browse)
        layout.addWidget(self.file_row)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setPlaceholderText("这个文本框的固定文字，支持多行")
        self.text.setMinimumHeight(90)
        self.text.setMaximumHeight(145)
        self.text.textChanged.connect(self.text_changed)
        layout.addWidget(self.text)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.note)
        row = QtWidgets.QHBoxLayout()
        self.edit_sequence = QtWidgets.QPushButton("编辑自定义轮换…")
        self.edit_sequence.clicked.connect(self.open_sequence)
        self.reset = QtWidgets.QPushButton("从头开始")
        self.reset.clicked.connect(self.reset_cursor)
        row.addWidget(self.edit_sequence)
        row.addWidget(self.reset)
        layout.addLayout(row)
        self.add_images = QtWidgets.QPushButton("追加图片…")
        self.add_images.clicked.connect(lambda:self.addImages.emit(self.layer["id"]) if self.layer else None)
        layout.addWidget(self.add_images)
        self.choose_copy = QtWidgets.QPushButton("从文案池选择固定文字…")
        self.choose_copy.clicked.connect(lambda: self.selectCopy.emit(self.layer["id"]) if self.layer else None)
        layout.addWidget(self.choose_copy)
        self.choose_image_pool = QtWidgets.QPushButton("选择 / 绑定图片池…")
        self.choose_image_pool.setToolTip("选择一个默认或自建图片池；可固定使用其中一张，或逐视频轮换。")
        self.choose_image_pool.clicked.connect(lambda: self.selectImagePool.emit(self.layer["id"]) if self.layer else None)
        layout.addWidget(self.choose_image_pool)
        self.pool_field = Combo()
        for label, field in (("完整文案", "full"), ("仅标题", "title"), ("仅正文", "body")):
            self.pool_field.addItem(label, field)
        self.pool_field.currentIndexChanged.connect(self.pool_field_changed)
        layout.addWidget(self.pool_field)
        self.copy_binding = Combo()
        self.copy_binding.addItem('自动匹配当前任务／文案（推荐）', 'task')
        self.copy_binding.addItem('独立轮换（不跟随任务）', 'rotate')
        self.copy_binding.setToolTip('默认文案池与队列已搭配时，标题和正文读取同一条文案；独立轮换会忽略任务对应关系。')
        self.copy_binding.currentIndexChanged.connect(self.copy_binding_changed)
        layout.addWidget(self.copy_binding)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

    def set_layer(self,layer):
        self.loading = True
        self.layer = copy.deepcopy(layer)
        self.source.clear()
        caption = bool(layer and layer["kind"] == "caption")
        visual = bool(layer and layer["kind"] == "image")
        if caption:
            self.source.addItem("当前任务人声／字幕", "voice")
        if layer and layer["kind"] in {"title","body"}:
            self.source.addItem("使用每个任务的"+("标题" if layer["kind"] == "title" else "正文"),"job")
        choices = ([] if caption else [("当前固定素材" if visual else "固定内容（手填／选择）", "fixed")]) + [("来自文件（整批固定）", "file")]
        if not caption:
            choices.append(("资源池：默认图片池" if visual else "资源池：默认文案池", "pool"))
        allowed = {"subtitle"} if caption else {"image", "video", "visual"} if visual else {"text"}
        choices += [("资源池：" + pool["name"], "pool:" + pool["id"]) for pool in self.pool_state.get("resource_pools", [])
                    if pool["kind"] in allowed]
        if layer and layer.get("source") == "sequence":
            choices.append(("旧配置的自定义轮换（保留）", "sequence"))
        for title,value in choices:
            self.source.addItem(title,value)
        if layer:
            key = "pool:" + layer["pool_id"] if layer["source"] == "pool" and layer.get("pool_id") else layer["source"]
            if self.source.findData(key) < 0:
                self.source.addItem("原来源已不存在（请重选）", key)
            self.source.setCurrentIndex(self.source.findData(key))
            self.text.setPlainText(layer.get("text",""))
            self.file_path.setText(layer.get("source_file") or layer.get("caption_file" if caption else "path", ""))
            self.pool_field.setCurrentIndex(max(0, self.pool_field.findData(layer.get("pool_field", "full"))))
            self.copy_binding.setCurrentIndex(max(0, self.copy_binding.findData(layer.get('copy_binding', 'task'))))
        self.loading = False
        self.show_mode()

    def show_mode(self):
        layer = self.layer
        if not layer:
            self.setEnabled(False)
            return
        self.setEnabled(True)
        sequence = layer["source"] in {"sequence", "pool"}
        text = layer["kind"] not in {"image", "caption"}
        self.file_row.setVisible(layer["source"] == "file" or not text and layer["kind"] == "image" and layer["source"] == "fixed")
        self.text.setVisible(text and layer["source"] == "fixed" and getattr(self,'show_fixed_text',True))
        self.edit_sequence.setVisible(layer["source"] == "sequence")
        self.reset.setVisible(sequence)
        self.add_images.setVisible(layer["source"] == "sequence" and not text)
        self.choose_copy.setVisible(text and layer["source"] == "fixed" and getattr(self, "allow_copy_pick", True))
        self.choose_image_pool.setVisible(False)
        self.pool_field.setVisible(text and layer["source"] == "pool")
        task_pool = text and layer['source'] == 'pool' and not layer.get('pool_id')
        self.copy_binding.setVisible(task_pool)
        items = layer.get("sequence_items", [])
        if sequence:
            cursor = layer["sequence_cursor"] % len(items) if items else 0
            self.note.setText(("绑定“" + layer.get("pool_name", "图片池" if not text else "文案池") + "”，" if layer["source"] == "pool" else "")+
                (f"共{len(items)}项；下次使用第{cursor+1}项。仅导出成功才推进，预览不消耗。" if items else "没有可轮换内容，请先向资源池添加内容。"))
            self.reset.setEnabled(bool(items))
            if task_pool and layer.get('copy_binding', 'task') == 'task':
                self.note.setText('已搭配的任务／文案按同一条记录取标题和正文，不受轮换游标、重试或重新导入影响。未搭配的纯视频才按池顺序使用。')
                self.reset.setVisible(False)
        elif layer["source"] == "job":
            self.note.setText("读取每个任务自己的文案；旧的标题和正文配置保持兼容。")
        elif layer["source"] == "voice":
            self.note.setText("使用当前任务的人声与字幕；时间戳来源在下方设置。")
        else:
            self.note.setText("固定来源用于每个视频；原文件不会被修改。")

    def emit_change(self):
        self.show_mode()
        self.changed.emit(copy.deepcopy(self.layer))

    def source_changed(self):
        if self.loading or not self.layer:
            return
        selected = self.source.currentData()
        if not selected:
            return
        new_source = "pool" if selected.startswith("pool:") else selected
        new_pool = selected[5:] if selected.startswith("pool:") else ""
        if new_source == "pool" and (self.layer["source"] != "pool" or self.layer.get("pool_id", "") != new_pool):
            self.layer.update(sequence_items=[], sequence_cursor=0)
        self.layer["source"] = new_source
        self.layer["pool_id"] = new_pool
        if self.layer["kind"] == "caption":
            if self.layer["source"] == "voice":
                self.layer["caption_file"] = ""
                if self.layer["caption_timing"] not in {"auto", "model", "task", "uniform", "elevenlabs"}:
                    self.layer["caption_timing"] = "auto"
            elif self.layer["caption_timing"] != "uniform":
                self.layer["caption_timing"] = "file"
        if self.layer["source"] == "sequence" and not self.layer["sequence_items"]:
            field = CONTENT_FIELDS[self.layer["kind"]]
            if self.layer.get(field):
                self.layer["sequence_items"] = [{"id":uuid.uuid4().hex,field:self.layer[field],
                    **({"thumbnail":self.layer.get("thumbnail","")} if field == "path" else {})}]
        self.emit_change()

    def set_state(self, state):
        self.pool_state = state
        self.set_layer(self.layer)

    def choose_file(self):
        if not self.layer:
            return
        kind = self.layer["kind"]
        from .visual_media import VISUAL_FILTER
        pattern = VISUAL_FILTER if kind == "image" else "字幕 (*.srt)" if kind == "caption" else "文字 (*.txt *.md)"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择固定来源文件", "", pattern)
        if path:
            self.layer["source_file"] = path
            self.layer["source"] = "file"
            if kind == "image":
                self.layer["path"] = path
                self.layer["thumbnail"] = ""
            elif kind == "caption":
                self.layer["caption_file"] = path
                if self.layer["caption_timing"] != "uniform":
                    self.layer["caption_timing"] = "file"
            self.file_path.setText(path)
            self.emit_change()

    def text_changed(self):
        if not self.loading and self.layer:
            self.layer["text"] = self.text.toPlainText()
            self.emit_change()

    def pool_field_changed(self):
        if not self.loading and self.layer:
            self.layer["pool_field"] = self.pool_field.currentData()
            self.emit_change()

    def copy_binding_changed(self):
        if not self.loading and self.layer:
            self.layer['copy_binding'] = self.copy_binding.currentData()
            self.emit_change()

    def open_sequence(self):
        if not self.layer:
            return
        dialog = SequenceDialog(self.layer,self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        old = self.layer["sequence_items"]
        cursor = self.layer["sequence_cursor"]
        current_id = old[cursor % len(old)]["id"] if old else None
        self.layer["sequence_items"] = dialog.items
        self.layer["sequence_cursor"] = next((i for i,item in enumerate(dialog.items) if item["id"] == current_id),0)
        self.emit_change()

    def reset_cursor(self):
        if self.layer:
            self.layer["sequence_cursor"] = 0
            self.emit_change()


class TextBoxProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)
    paired_font_fields = True

    def __init__(self,parent=None):
        super().__init__(parent)
        ensure_bundled_fonts()
        QtGui.QGuiApplication.instance().fontDatabaseChanged.connect(self._invalidate_weight_status)
        self.layer,self.loading = None,False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        from .property_sections import PropertySections
        self.style_sections = PropertySections()
        layout.addWidget(self.style_sections)
        form = self._page("字体")
        outline = self._page("描边")
        shadow = self._page("阴影")
        background = self._page("底色")
        from .property_sections import PropertySection
        layout_widget = QtWidgets.QWidget()
        layout_form = QtWidgets.QFormLayout(layout_widget)
        layout_form.setRowWrapPolicy(layout_form.RowWrapPolicy.WrapLongRows)
        layout_form.setFieldGrowthPolicy(layout_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.controls = {}
        direction = Combo()
        for name, value in (("横排", "horizontal"), ("竖排 · 从右往左", "vertical_rl"),
                            ("竖排 · 从左往右", "vertical_lr")):
            direction.addItem(name, value)
        direction.setToolTip("竖排逐字自上而下，放不下时换列；换行主动开始新列。字母保持直立，组合字符不拆开。不是把整段文字旋转90度。")
        direction.currentIndexChanged.connect(self.update_values)
        self.controls["writing_mode"] = direction
        layout_form.addRow("文字方向", direction)
        for key,label,choices in (("align","水平对齐",(("左","left"),("中","center"),("右","right"))),
                                   ("vertical","垂直对齐",(("上","top"),("中","center"),("下","bottom")))):
            control = Combo()
            for text,value in choices:
                control.addItem(text,value)
            control.setToolTip("文字在当前文本框内的对齐方式；移动整个文本框请用“位置”页。")
            control.currentIndexChanged.connect(self.update_values)
            layout_form.addRow(label,control)
            self.controls[key] = control
        font = FontCombo()
        font.currentFontChanged.connect(self.update_values)
        form.addRow("字体",font)
        self.controls["font"] = font
        self.font_size_form = form
        self.font_size_section = self.style_sections.sections['font']
        self._number(form,'font_min','最小字号',8,200,0)
        self._number(form,'font_max','最大字号',8,200,0)
        color_form = form
        self._color(color_form,"color","基准文字颜色")
        mode = Combo()
        mode.addItem("固定颜色", "fixed"); mode.addItem("同色系随机 · 每条视频一个颜色", "family")
        mode.currentIndexChanged.connect(self.update_values)
        self.controls["color_mode"] = mode
        color_form.addRow("颜色方式", mode)
        random_widget = QtWidgets.QWidget()
        random_form = QtWidgets.QFormLayout(random_widget)
        random_form.setRowWrapPolicy(random_form.RowWrapPolicy.WrapLongRows)
        self.random_color_section = PropertySection('同色系随机范围',random_widget)
        color_form.addRow(self.random_color_section)
        self._number(random_form,"color_hue_variation","色相变化 ±度",0,30,1)
        self._number(random_form,"color_saturation_variation","饱和度变化 ±百分点",0,30,1)
        self._number(random_form,"color_value_variation","明暗变化 ±百分点",0,30,1)
        from .text_colors import ColorSamples
        self.color_samples = ColorSamples()
        random_form.addRow("范围示例", self.color_samples)
        self._number(random_form,"color_seed","颜色组编号",0,2147483647,0)
        self.controls["color_seed"].setDecimals(0)
        self.new_color_group = QtWidgets.QPushButton("换一组颜色")
        self.new_color_group.setAutoDefault(False)
        self.new_color_group.clicked.connect(lambda: self.controls["color_seed"].setValue(
            (int(self.controls["color_seed"].value())+1) % 2147483648))
        random_form.addRow(self.new_color_group)
        note = QtWidgets.QLabel("先选择基准色，再在这个色系内有限变化；白／灰只变化明暗。\n"
            "每条视频颜色固定，不逐帧闪色；预览、导出、重试保持一致。描边和阴影不随机。\n"
            "正文颜色在上方选择；动态字幕的逐组换色、读词变色和高亮框也在本页设置。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        random_form.addRow(note)
        weight = WeightCombo()
        for value,name in ((100,"极细"),(200,"纤细"),(300,"细体"),(400,"常规"),(500,"中等"),
                            (600,"半粗"),(700,"粗体"),(800,"特粗"),(900,"极粗")):
            weight.addItem(f"{name} · {value}",value)
        weight.setToolTip("可输入1–1000或选9档预设。内置Poppins九档100–900及Open Sans连续300–800正体/斜体；其他字体以本机实际加载字形为准。下方显示实际应用字重，超出字体范围不冒充生效。")
        weight.currentIndexChanged.connect(self.update_values)
        weight.editTextChanged.connect(self.update_values)
        weight.lineEdit().editingFinished.connect(self.finish_weight_edit)
        self.controls["font_weight"] = weight
        form.addRow("字体粗细",weight)
        self.weight_status = QtWidgets.QLabel()
        self.weight_status.setWordWrap(True)
        self.weight_status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.weight_status)
        flags = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(flags)
        grid.setContentsMargins(0,0,0,0)
        self.bold = QtWidgets.QPushButton("B 加粗")
        self.bold.setAutoDefault(False)
        self.bold.setCheckable(True)
        self.bold.clicked.connect(lambda checked:weight.setCurrentIndex(weight.findData(700 if checked else 400)))
        grid.addWidget(self.bold,0,0)
        for key,label,row,column in (("italic","斜体",0,1),("underline","下划线",1,0),("strikeout","删除线",1,1)):
            control = QtWidgets.QCheckBox(label)
            control.toggled.connect(self.update_values)
            self.controls[key] = control
            grid.addWidget(control,row,column)
        form.addRow("字体样式",flags)
        cases = Combo()
        for name,value in (("原样","normal"),("全部大写","uppercase"),("全部小写","lowercase"),("小型大写","smallcaps")):
            cases.addItem(name,value)
        cases.currentIndexChanged.connect(self.update_values)
        self.controls["capitalization"] = cases
        form.addRow("大小写",cases)
        for key,label,low,high in (("line_spacing","行间距",.8,3),("letter_spacing","字距 px",-5,30),
                                  ("word_spacing","附加词距 px",-5,60),("paragraph_spacing","段间距 px",0,120)):
            self._number(layout_form,key,label,low,high,
                         2 if key == "line_spacing" else 1 if key.endswith("spacing") else 0)
        self.layout_section=PropertySection('排版与间距',layout_widget)
        form.addRow(self.layout_section)
        self.controls['word_spacing'].setToolTip('这是在基本空格之外追加的距离，不是空格总宽度。0 不会删除空格；水印和达芬奇逐词柔弹以字体自然空格为基础。数值按1080宽画布缩放。')
        self._number(outline,"outline_width","描边厚度 px",0,15,1)
        self._outline_widths = {}
        self.outline_toggle = QtWidgets.QCheckBox('启用描边')
        self.style_sections.sections['outline'].set_enable_control(self.outline_toggle)
        self.outline_toggle.toggled.connect(self._toggle_outline)
        self.controls["outline_width"].setToolTip("0为关闭描边。像素按1080宽画布计算，导出时按实际画布缩放。")
        self._color(outline,"outline_color","描边颜色")
        self._number(outline,"outline_opacity","描边不透明度 %",0,100,2)
        self._number(outline,'outline_blur','描边模糊半径 px',0,150,1)
        self.controls['outline_blur'].setToolTip('0为清晰描边；仅柔化描边，文字主体保持清晰。')
        self._check(shadow,"shadow_enabled","启用文字阴影", section='shadow')
        self._color(shadow,"shadow_color","阴影颜色")
        for key,label,low,high in (("shadow_opacity","阴影不透明度 %",0,100),
                ("shadow_blur","模糊半径 px",0,150),
                ("shadow_x","阴影横偏移 px",-80,80),("shadow_y","阴影纵偏移 px",-80,80)):
            self._number(shadow,key,label,low,high,1)
        self.controls['shadow_blur'].setToolTip('0 = 硬边阴影；数值越大越柔和。只模糊阴影，不模糊文字和描边。按1080像素宽画布缩放。')
        self._check(background,"background_enabled","启用文本框底色", section='background')
        mode = Combo()
        mode.addItem("整框底色（原模式）","box")
        mode.addItem("随文字底色（字符模式）","text")
        mode.setToolTip("字符模式按每行实际文字宽度生成底色；空行不铺底色，自动换行和对齐会同步更新。不是给每个字单独套小方块。")
        self._background_mode_tip = mode.toolTip()
        mode.currentIndexChanged.connect(self.update_values)
        self.controls["background_mode"] = mode
        background.addRow("底色范围",mode)
        self._color(background,"background_color","底色")
        self._number(background,"background_opacity","底色不透明度 %",0,100,1)
        self._number(background,"background_radius","底色圆角 px",0,150,1)
        for side,label in (("left","向左扩展 px"),("right","向右扩展 px"),
                           ("top","向上扩展 px"),("bottom","向下扩展 px")):
            key = "background_extra_"+side
            self._number(background,key,label,0,300,1)
            self.controls[key].setToolTip("只扩展底色，不改变字号、换行或文本位置。按1080宽画布换算；四个方向独立。滚动控件超出文本框的部分仍会裁切。")
        self._check(background,"background_join_lines","连接相邻行底色")
        self.controls["background_join_lines"].setToolTip("让相邻行形成类似示例的连续圆角底色；关闭后每行分别显示。较大的段落间隔仍保持空隙。")
        self._number(background,"padding","内边距 px",0,100,1)
        self.controls["padding"].setToolTip("这个值会改变文字的可用排版空间。只想把底色加宽／加高，请使用上面的四方向扩展。")
        for key in ('outline', 'shadow', 'background'):
            section = self.style_sections.sections[key]
            self.style_sections.body.removeWidget(section)
            form.addRow(section)
        from .property_pairs import order_form_fields, pair_form_fields
        order_form_fields(form,[font,weight,self.weight_status,flags,cases,
            self.controls['font_min'],self.controls['font_max'],self.controls['color'],
            self.controls['color_mode'],self.random_color_section,self.layout_section]+
            [self.style_sections.sections[key] for key in ('outline','shadow','background')])
        self.font_size_pair=pair_form_fields(form,'字号',[
            ('min','最小',self.controls['font_min']),('max','最大',self.controls['font_max'])])

    def _page(self,title):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(4,4,4,4)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.style_sections.add_section({'字体':'font','描边':'outline','阴影':'shadow','底色':'background'}[title],
            title, page, expanded=title == '字体')
        return form

    def _color(self,form,key,label):
        control = QtWidgets.QPushButton()
        control.setAutoDefault(False)
        control.setToolTip("点击选择任意颜色；左侧色块表示当前颜色。")
        control.clicked.connect(lambda _checked=False,key=key:self.pick_color(key))
        self.controls[key] = control
        form.addRow(label,control)

    def _number(self,form,key,label,low,high,decimals):
        control = Spin()
        control.setRange(low,high)
        control.setDecimals(decimals)
        control.setSingleStep(.1 if decimals else 1)
        control.valueChanged.connect(self.update_values)
        self.controls[key] = control
        form.addRow(label,control)

    def _check(self,form,key,label,*,section=None):
        control = QtWidgets.QCheckBox(label)
        control.toggled.connect(self.update_values)
        self.controls[key] = control
        if section:
            self.style_sections.sections[section].set_enable_control(control)
        else:
            form.addRow(control)

    def _toggle_outline(self, enabled):
        if self.loading or not self.layer:
            return
        identifier = self.layer['id']
        width = self.controls['outline_width'].value()
        if width > 0:
            self._outline_widths[identifier] = width
        self.controls['outline_width'].setValue(self._outline_widths.get(identifier, 2) if enabled else 0)

    def pick_color(self,key="color"):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.controls[key].text()),self,"选择颜色")
        if color.isValid():
            self.controls[key].setText(color.name())
            self.update_values()

    def _invalidate_weight_status(self):
        self._weight_status_key = None

    def _show_effect_controls(self):
        caption=bool(self.layer and self.layer['kind']=='caption')
        for section in self.style_sections.sections.values():
            section.disable_content_when_off = not caption
            section.sync_enabled()
        width = self.controls['outline_width'].value()
        if self.layer and width > 0:
            self._outline_widths[self.layer['id']] = width
        blocker = QtCore.QSignalBlocker(self.outline_toggle)
        self.outline_toggle.setChecked(width > 0)
        del blocker
        self.style_sections.sections['outline'].content.setEnabled(caption or width > 0)
        random_allowed = bool(self.layer)
        self.controls["color_mode"].setEnabled(random_allowed)
        family = random_allowed and self.controls["color_mode"].currentData() == "family"
        self.random_color_section.setVisible(caption or family)
        for key in ("color_hue_variation", "color_saturation_variation", "color_value_variation", "color_seed"):
            self.controls[key].setEnabled(caption or family)
        self.new_color_group.setEnabled(caption or family)
        if self.layer:
            sample_layer = {**self.layer, **{key:self.controls[key].value() for key in (
                "color_hue_variation", "color_saturation_variation", "color_value_variation")},
                "color":self.controls["color"].text(), "color_mode":self.controls["color_mode"].currentData(),
                "color_seed":int(self.controls["color_seed"].value())}
            self.color_samples.set_layer(sample_layer)
        self.controls["writing_mode"].setEnabled(bool(self.layer and self.layer["kind"] != "caption"))
        weight = self.controls["font_weight"].currentData()
        self.bold.setChecked(weight is not None and weight >= 600)
        key = (self.controls["font"].currentFont().family(), weight, self.controls["italic"].isChecked())
        if weight is not None and getattr(self, '_weight_status_key', None) != key:
            summary, details = font_weight_status(*key)
            self.weight_status.setText(summary)
            self.weight_status.setToolTip(details)
            self._weight_status_key = key
        for key in ("color","outline_color","shadow_color","background_color"):
            swatch = QtGui.QPixmap(18,18)
            swatch.fill(QtGui.QColor(self.controls[key].text()))
            self.controls[key].setIcon(QtGui.QIcon(swatch))
        for group,keys in (("shadow_enabled",("shadow_color","shadow_opacity","shadow_blur","shadow_x","shadow_y")),
                            ("background_enabled",("background_color","background_opacity","background_radius"))):
            for key in keys:
                self.controls[key].setEnabled(caption or self.controls[group].isChecked())
        enabled = self.controls["background_enabled"].isChecked()
        allowed = bool(self.layer and self.layer["kind"] != "caption")
        self.controls["background_mode"].setEnabled(enabled and allowed)
        for side in ("left","right","top","bottom"):
            self.controls["background_extra_"+side].setEnabled(enabled and allowed)
        self.controls["background_join_lines"].setEnabled(enabled and allowed and self.controls["background_mode"].currentData() == "text")
        caption=bool(self.layer and self.layer['kind']=='caption')
        section=self.style_sections.sections['background']
        section.toggle.setText('整组字幕底框' if caption else '底色')
        section.enable_control.setAccessibleName('启用整组字幕底框' if caption else '启用文本框底色')
        section.enable_control.setToolTip('仅控制整组字幕底框；不会影响当前词高亮框' if caption else '启用文本框底色')
        form=section.content.layout()
        for key in ('background_mode','background_join_lines','padding',
                    'background_extra_left','background_extra_right','background_extra_top','background_extra_bottom'):
            control=self.controls[key]; control.setVisible(not caption)
            label=form.labelForField(control)
            if label: label.setVisible(not caption)
        self.controls['outline_blur'].setEnabled(caption or self.controls['outline_width'].value()>0)
        self.controls["background_mode"].setToolTip(self._background_mode_tip)

    def set_layer(self,layer):
        self.layer = copy.deepcopy(layer)
        self.setEnabled(bool(layer and layer["kind"] in {"title","body","text","caption"}))
        if not layer or layer["kind"] not in {"title","body","text","caption"}:
            return
        from .text_colors import normalize_color
        normalize_color(self.layer)
        from .text_background import normalize_background
        normalize_background(self.layer)
        layer = self.layer
        self.loading = True
        for key,control in self.controls.items():
            if isinstance(control,QtWidgets.QFontComboBox):
                control.setCurrentFont(QtGui.QFont(layer[key]))
            elif isinstance(control,WeightCombo):
                control.setValue(layer[key])
            elif isinstance(control,QtWidgets.QComboBox):
                control.setCurrentIndex(control.findData(layer.get(key, "horizontal") if key == "writing_mode" else layer[key]))
            elif isinstance(control,QtWidgets.QAbstractSpinBox):
                control.setValue(layer[key])
            elif isinstance(control,QtWidgets.QCheckBox):
                control.setChecked(layer[key])
            else:
                control.setText(str(layer[key]))
        self.loading = False
        self._show_effect_controls()

    def finish_weight_edit(self):
        if not self.loading and self.layer and self.controls["font_weight"].currentData() is None:
            self.controls["font_weight"].setValue(self.layer["font_weight"])

    def update_values(self):
        if self.loading or not self.layer:
            return
        values = {}
        for key,control in self.controls.items():
            values[key] = control.currentFont().family() if isinstance(control,QtWidgets.QFontComboBox) else control.currentData() if isinstance(control,QtWidgets.QComboBox) else (
                control.value() if isinstance(control,QtWidgets.QAbstractSpinBox) else control.isChecked() if isinstance(control,QtWidgets.QCheckBox) else control.text().strip())
        if values["font_weight"] is None:
            self.setToolTip("字重请输入1–1000的整数；当前无效输入尚未应用。")
            return
        values["color_seed"] = int(values["color_seed"])
        if values["font_min"] > values["font_max"] or not all(QtGui.QColor(values[key]).isValid()
                for key in ("color","outline_color","shadow_color","background_color")):
            self.setToolTip("请检查字号范围和颜色，当前无效值尚未应用。")
            return
        self.layer.update(values)
        self._show_effect_controls()
        self.setToolTip("")
        self.changed.emit(values)


def add_text_properties(panel):
    """The same font/color/effect UI is used by canvas and workflow components."""
    editor = panel.add_bound('text', '', TextBoxProperties(), section=False)
    panel.text_properties = editor
    panel.sections.update(editor.style_sections.sections)
    panel.sections['font_size'] = editor.font_size_section


def text_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    from .scrolling_text_ui import ScrollTextProperties
    panel = ComponentPropertyPanel(mode=mode)
    from .inspector_layout_contract import require_flat_layout
    require_flat_layout(panel)
    panel.add_source()
    panel.add_geometry()
    add_text_properties(panel)
    panel.scroll_properties = panel.add_bound('scroll', '文字滚动动画', ScrollTextProperties())
    scroll=panel.sections['scroll']
    panel.body.removeWidget(scroll); panel.body.insertWidget(2,scroll)
    if mode == 'batch':
        from .text_wrap_ui import TextWrapProperties
        panel.wrap_properties = panel.add_bound('text_wrap', '文字环绕／避让控件', TextWrapProperties())
        section = panel.sections['text_wrap']
        panel.body.removeWidget(section); panel.body.insertWidget(2, section)
    return panel

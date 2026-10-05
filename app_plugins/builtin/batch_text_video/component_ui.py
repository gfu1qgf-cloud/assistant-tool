"""Reusable content-source and text-box widgets, separated from the layer host."""
import copy
from pathlib import Path
import uuid
from qt_compat import QtCore, QtGui, QtWidgets
from .components import CONTENT_FIELDS, MAX_SEQUENCE_ITEMS


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
        return (item["text"].replace("\n"," ")[:160] or "（空白文字）") if self.field == "text" else item.get("name") or Path(item["path"]).name

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

    def __init__(self,parent=None):
        super().__init__(parent)
        self.layer,self.loading = None,False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.source = Combo()
        self.source.currentIndexChanged.connect(self.source_changed)
        layout.addWidget(self.source)
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
        self.pool_field = Combo()
        for label, field in (("轮换完整文案", "full"), ("只轮换标题", "title"), ("只轮换正文", "body")):
            self.pool_field.addItem(label, field)
        self.pool_field.currentIndexChanged.connect(self.pool_field_changed)
        layout.addWidget(self.pool_field)

    def set_layer(self,layer):
        self.loading = True
        self.layer = copy.deepcopy(layer)
        self.source.clear()
        if layer and layer["kind"] in {"title","body"}:
            self.source.addItem("使用每个任务的"+("标题" if layer["kind"] == "title" else "正文"),"job")
        for title,value in (("固定内容（手填 / 选择）","fixed"),
                            ("轮换内容（读取"+("图片池" if layer and layer["kind"] == "image" else "文案池")+"）", "pool"),
                            ("自定义轮换（兼容原序列）","sequence")):
            self.source.addItem(title,value)
        if layer:
            self.source.setCurrentIndex(self.source.findData(layer["source"]))
            self.text.setPlainText(layer.get("text",""))
            self.pool_field.setCurrentIndex(max(0, self.pool_field.findData(layer.get("pool_field", "full"))))
        self.loading = False
        self.show_mode()

    def show_mode(self):
        layer = self.layer
        if not layer:
            self.setEnabled(False)
            return
        self.setEnabled(True)
        sequence = layer["source"] in {"sequence", "pool"}
        text = layer["kind"] != "image"
        self.text.setVisible(text and layer["source"] == "fixed")
        self.edit_sequence.setVisible(layer["source"] == "sequence")
        self.reset.setVisible(sequence)
        self.add_images.setVisible(layer["source"] == "sequence" and not text)
        self.choose_copy.setVisible(text and layer["source"] == "fixed")
        self.pool_field.setVisible(text and layer["source"] == "pool")
        items = layer["sequence_items"]
        if sequence:
            cursor = layer["sequence_cursor"] % len(items) if items else 0
            self.note.setText(("自动读取资源池，" if layer["source"] == "pool" else "")+
                (f"共{len(items)}项；下次使用第{cursor+1}项。仅导出成功才推进，预览不消耗。" if items else "没有可轮换内容，请先向资源池添加内容。"))
            self.reset.setEnabled(bool(items))
        elif layer["source"] == "job":
            self.note.setText("读取每个任务自己的文案；旧的标题和正文配置保持兼容。")
        else:
            self.note.setText("固定内容用于每个视频。" if text else "固定图片用于每个视频。")

    def emit_change(self):
        self.show_mode()
        self.changed.emit(copy.deepcopy(self.layer))

    def source_changed(self):
        if self.loading or not self.layer:
            return
        self.layer["source"] = self.source.currentData()
        if self.layer["source"] == "sequence" and not self.layer["sequence_items"]:
            field = CONTENT_FIELDS[self.layer["kind"]]
            if self.layer.get(field):
                self.layer["sequence_items"] = [{"id":uuid.uuid4().hex,field:self.layer[field],
                    **({"thumbnail":self.layer.get("thumbnail","")} if field == "path" else {})}]
        self.emit_change()

    def text_changed(self):
        if not self.loading and self.layer:
            self.layer["text"] = self.text.toPlainText()
            self.emit_change()

    def pool_field_changed(self):
        if not self.loading and self.layer:
            self.layer["pool_field"] = self.pool_field.currentData()
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

    def __init__(self,parent=None):
        super().__init__(parent)
        self.layer,self.loading = None,False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.style_tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.style_tabs)
        form = self._page("字体")
        outline = self._page("描边")
        shadow = self._page("阴影")
        background = self._page("底色")
        self.controls = {}
        for key,label,choices in (("align","水平对齐",(("左","left"),("中","center"),("右","right"))),
                                   ("vertical","垂直对齐",(("上","top"),("中","center"),("下","bottom")))):
            control = Combo()
            for text,value in choices:
                control.addItem(text,value)
            control.setToolTip("文字在当前文本框内的对齐方式；移动整个文本框请用“位置”页。")
            control.currentIndexChanged.connect(self.update_values)
            form.addRow(label,control)
            self.controls[key] = control
        font = FontCombo()
        font.currentFontChanged.connect(self.update_values)
        form.addRow("字体",font)
        self.controls["font"] = font
        self._color(form,"color","文字颜色")
        weight = WeightCombo()
        for value,name in ((100,"极细"),(200,"纤细"),(300,"细体"),(400,"常规"),(500,"中等"),
                            (600,"半粗"),(700,"粗体"),(800,"特粗"),(900,"极粗")):
            weight.addItem(f"{name} · {value}",value)
        weight.setToolTip("可直接输入1–1000的整数，也可选择9档预设。400常规、700粗体；实际变化取决于字体支持的字重，不会自动取整到预设档位。")
        weight.currentIndexChanged.connect(self.update_values)
        weight.editTextChanged.connect(self.update_values)
        weight.lineEdit().editingFinished.connect(self.finish_weight_edit)
        self.controls["font_weight"] = weight
        form.addRow("字体粗细",weight)
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
        for key,label,low,high in (("font_min","最小字号",8,200),("font_max","最大字号",8,200),
                                  ("line_spacing","行间距",.8,3),("letter_spacing","字距 px",-5,30),
                                  ("word_spacing","词间距 px",-5,60),("paragraph_spacing","段间距 px",0,120)):
            self._number(form,key,label,low,high,2 if key == "line_spacing" else 1 if key.endswith("spacing") else 0)
        self._number(outline,"outline_width","描边厚度 px",0,15,1)
        self.controls["outline_width"].setToolTip("0为关闭描边。像素按1080宽画布计算，导出时按实际画布缩放。")
        self._color(outline,"outline_color","描边颜色")
        self._number(outline,"outline_opacity","描边不透明度 %",0,100,2)
        self._check(shadow,"shadow_enabled","启用文字阴影")
        self._color(shadow,"shadow_color","阴影颜色")
        for key,label,low,high in (("shadow_opacity","阴影不透明度 %",0,100),
                ("shadow_x","阴影横偏移 px",-80,80),("shadow_y","阴影纵偏移 px",-80,80)):
            self._number(shadow,key,label,low,high,1)
        self._check(background,"background_enabled","启用文本框底色")
        self._color(background,"background_color","底色")
        self._number(background,"background_opacity","底色不透明度 %",0,100,1)
        self._number(background,"background_radius","底色圆角 px",0,150,1)
        self._number(background,"padding","内边距 px",0,100,1)
        note = QtWidgets.QLabel("字号、字距和效果像素按1080宽画布计算；自动字号仍在设定范围内调整。每个文本框独立，不改变原文内容。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(note)

    def _page(self,title):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(4,4,4,4)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.style_tabs.addTab(page,title)
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

    def _check(self,form,key,label):
        control = QtWidgets.QCheckBox(label)
        control.toggled.connect(self.update_values)
        self.controls[key] = control
        form.addRow(control)

    def pick_color(self,key="color"):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.controls[key].text()),self,"选择颜色")
        if color.isValid():
            self.controls[key].setText(color.name())
            self.update_values()

    def _show_effect_controls(self):
        weight = self.controls["font_weight"].currentData()
        self.bold.setChecked(weight is not None and weight >= 600)
        for key in ("color","outline_color","shadow_color","background_color"):
            swatch = QtGui.QPixmap(18,18)
            swatch.fill(QtGui.QColor(self.controls[key].text()))
            self.controls[key].setIcon(QtGui.QIcon(swatch))
        for group,keys in (("shadow_enabled",("shadow_color","shadow_opacity","shadow_x","shadow_y")),
                            ("background_enabled",("background_color","background_opacity","background_radius"))):
            for key in keys:
                self.controls[key].setEnabled(self.controls[group].isChecked())

    def set_layer(self,layer):
        self.layer = copy.deepcopy(layer)
        self.setEnabled(bool(layer and layer["kind"] in {"title","body","text"}))
        if not layer or layer["kind"] not in {"title","body","text"}:
            return
        self.loading = True
        for key,control in self.controls.items():
            if isinstance(control,QtWidgets.QFontComboBox):
                control.setCurrentFont(QtGui.QFont(layer[key]))
            elif isinstance(control,WeightCombo):
                control.setValue(layer[key])
            elif isinstance(control,QtWidgets.QComboBox):
                control.setCurrentIndex(control.findData(layer[key]))
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
        if values["font_min"] > values["font_max"] or not all(QtGui.QColor(values[key]).isValid()
                for key in ("color","outline_color","shadow_color","background_color")):
            self.setToolTip("请检查字号范围和颜色，当前无效值尚未应用。")
            return
        self.layer.update(values)
        self._show_effect_controls()
        self.setToolTip("")
        self.changed.emit(values)

"""Basic caption appearance, shared unchanged by every preset and node editor."""
import copy
from qt_compat import QtCore, QtWidgets
from .property_sections import PropertySections
from .color_ui import EffectColorEditor
from .caption_styles import effects
from .caption_appearance import DEFAULTS, normalize_appearance


class CaptionAppearanceEditor(QtWidgets.QWidget):
    changed=QtCore.pyqtSignal(object)

    def __init__(self, *, background=False):
        super().__init__()
        self.layer=None; self.loading=False; self.controls={}; self.background=background
        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(0,0,0,0)
        self.sections=PropertySections(); root.addWidget(self.sections)
        if background:
            form=self.section('caption_background','字幕底框细节')
            self.check(form,'box_adaptive_width','随本组文字宽度')
            self.number(form,'box_padding_x','左右内边距 px',0,150)
            self.number(form,'box_padding_y','上下内边距 px',0,150)
            self.number(form,'box_blur','底框模糊 px',0,150)
            self.check(form,'bg_gradient_enabled','使用渐变底色')
            self.color(form,'bg_gradient_colors','渐变颜色')
            self.combo(form,'bg_gradient_type','渐变方向',[('横向','linear_h'),('纵向','linear_v')])
        else:
            form=self.section('word_highlight','当前词文字变色','karaoke_highlight')
            self.color(form,'color_high','读到的词颜色')
            self.combo(form,'high_gradient_direction','渐变方向',[('横向','horizontal'),('纵向','vertical')])
            form=self.section('word_highlight_box','当前词高亮框','dynamic_box')
            self.color(form,'color_high_bg','高亮框颜色')
            self.number(form,'opacity_high_bg','高亮框不透明度（0–255）',0,255,decimals=0)
            self.number(form,'high_padding','高亮框内边距 px',0,150)
            self.number(form,'dynamic_radius','高亮框圆角 px',0,150)
            self.number(form,'high_offset_y','高亮框纵向偏移 px',-150,150)
            self.check(form,'dynamic_box_stroke','只显示边线')
            self.number(form,'dynamic_box_stroke_width','边线宽度 px',0,30)
            form=self.section('caption_group_colors','按字幕组换色','caption_group_colors')
            self.color(form,'caption_group_palette','轮换颜色')
            self.controls['caption_group_palette'].add_button.setText('＋添加轮换色')
            self.controls['caption_group_palette'].setToolTip('每组使用一种颜色；下一组按色板轮换，相邻组不重复。不是逐帧闪色。')
            self.number(form,'caption_color_seed','配色种子',0,2147483647,decimals=0)
            note=QtWidgets.QLabel('同一色板、种子和文案会得到相同结果；预览、导出与重试一致。')
            note.setWordWrap(True); form.addRow(note)
        self.error=QtWidgets.QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet('color:#c33442;')
        root.addWidget(self.error); self.error.hide()

    def section(self,key,title,enable=None):
        widget=QtWidgets.QWidget(); form=QtWidgets.QFormLayout(widget)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        section=self.sections.add_section(key,title,widget)
        if enable:
            control=QtWidgets.QCheckBox('启用'+title); self.controls[enable]=control
            control.toggled.connect(self.edit); section.set_enable_control(control)
        return form

    def check(self,form,key,title):
        control=QtWidgets.QCheckBox(title); control.toggled.connect(self.edit)
        self.controls[key]=control; form.addRow(control)

    def number(self,form,key,title,low,high,decimals=1):
        control=QtWidgets.QDoubleSpinBox(); control.setRange(low,high); control.setDecimals(decimals)
        control.valueChanged.connect(self.edit); self.controls[key]=control; form.addRow(title,control)

    def combo(self,form,key,title,choices):
        control=QtWidgets.QComboBox()
        for label,value in choices: control.addItem(label,value)
        control.currentIndexChanged.connect(self.edit); self.controls[key]=control; form.addRow(title,control)

    def color(self,form,key,title):
        control=EffectColorEditor('#ffffff'); control.field.textChanged.connect(self.edit)
        self.controls[key]=control; form.addRow(title,control)

    def set_layer(self,layer):
        self.loading=True; self.layer=copy.deepcopy(layer)
        try:
            if not layer: self.setEnabled(False); return
            normalize_appearance(self.layer)
            style=effects(self.layer)
            for key,control in self.controls.items():
                value=self.layer[key] if key in DEFAULTS else style[key]
                if isinstance(control,EffectColorEditor):
                    control.setText(','.join(value) if isinstance(value,list) else str(value))
                elif isinstance(control,QtWidgets.QComboBox): control.setCurrentIndex(control.findData(value))
                elif isinstance(control,QtWidgets.QCheckBox): control.setChecked(value)
                else: control.setValue(value)
            self.setEnabled(not self.background or layer['background_enabled'])
            for section in self.sections.sections.values(): section.sync_enabled()
            self.error.hide()
        finally: self.loading=False

    def edit(self,*_args):
        if self.loading or not self.layer: return
        params={}; overrides=copy.deepcopy(self.layer['caption_effects'])
        for key,control in self.controls.items():
            value=(control.text() if isinstance(control,EffectColorEditor) else
                   control.currentData() if isinstance(control,QtWidgets.QComboBox) else
                   control.isChecked() if isinstance(control,QtWidgets.QCheckBox) else control.value())
            if key=='caption_color_seed': value=int(value)
            if key in DEFAULTS: params[key]=value
            else: overrides[key]=value
        params['caption_effects']=overrides
        candidate={**self.layer,**params}
        try:
            normalize_appearance(candidate); effects(candidate)
        except ValueError as error:
            self.error.setText(str(error)); self.error.show(); return
        params.update({key:copy.deepcopy(candidate[key]) for key in DEFAULTS if key in params})
        self.layer=candidate; self.error.hide()
        for section in self.sections.sections.values(): section.sync_enabled()
        self.changed.emit(params)

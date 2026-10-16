"""Compact per-component motion/timing controls, no model work on GUI thread."""
import copy
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo
from .caption_styles import preset_choices, apply_style, description, is_catalog, effects, ANIMATIONS, COLOR_KEYS, preset_values
from .color_ui import EffectColorEditor


class CaptionProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, *, allow_manual_file=True):
        super().__init__(parent)
        self.layer, self.loading = None, False
        self.allow_manual_file = allow_manual_file
        draft = QtWidgets.QWidget(self)
        form = QtWidgets.QFormLayout(draft)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.controls = {}
        for key, label, choices in (
            ("caption_preset", "动画预设", [("逐词跃动", "pop"), ("逐词出现（无弹跳）", "appear")]+preset_choices()),
            ("caption_timing", "时间戳来源", (("优先 ELE 逐词记录／任务 SRT，否则本地对齐", "auto"),
                ("仅使用任务已有逐词记录／SRT", "task"), ("手动指定 SRT", "file"), ("本地模型逐词对齐", "model"),
                ("匀速动画", "uniform"), ("ELE 云端逐词对齐（上传音频与文案）", "elevenlabs"))),
            ("caption_source", "无 SRT 时的文案", (("任务正文", "body"),
                ("标题＋正文", "full"), ("从人声识别（仅模型模式）", "asr"))),
            ("caption_language", "人声语言", (("斯洛伐克语 · sk", "sk"), ("英语 · en", "en"),
                ("意大利语 · it", "it"), ("捷克语 · cs", "cs"), ("波兰语 · pl", "pl"), ("中文 · zh", "zh")))):
            control = Combo()
            for title, value in choices:
                control.addItem(title, value)
            if key == 'caption_preset':
                control.setEditable(True)
                control.setInsertPolicy(control.InsertPolicy.NoInsert)
                control.completer().setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
                control.completer().setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
                control.currentIndexChanged.connect(self.choose_preset)
            else:
                control.currentIndexChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        consent = QtWidgets.QCheckBox('允许上传当前人声和文案并消耗 ELE 对齐额度', self)
        consent.setToolTip('只用主程序指定的对齐 Key；首次播放预览也会对齐，后续复用。原 task_audio.srt 不会改动。')
        consent.toggled.connect(self.edit)
        self.controls['caption_cloud_consent'] = consent
        form.addRow(consent)
        random_size = QtWidgets.QCheckBox('启用随机字号', self)
        random_size.setToolTip('关闭后字号使用当前范围的中间值，保留弹跳等动画；字幕框自适应仍可能缩小过长的一组。开启后恢复原对比度和种子。')
        random_size.toggled.connect(self.edit)
        self.controls['caption_random_size'] = random_size
        form.addRow(random_size)
        contrast = QtWidgets.QDoubleSpinBox()
        contrast.setRange(0, 4); contrast.setDecimals(2); contrast.setSingleStep(.25); contrast.setSuffix(' ×')
        contrast.setToolTip('1×保持原随机分布；大于1×让大小更靠近两端，0×固定为范围中点。不会超出随机范围；字幕框放不下时仍会自适应缩小。')
        contrast.valueChanged.connect(self.edit)
        self.controls['caption_size_contrast'] = contrast
        form.addRow('随机字号对比度', contrast)
        action_row = QtWidgets.QHBoxLayout()
        self.gallery_button = QtWidgets.QPushButton("样式图库…")
        self.gallery_button.clicked.connect(self.choose_gallery)
        form.addRow(self.gallery_button)
        self.effect_button = QtWidgets.QPushButton('高级效果…')
        self.effect_button.clicked.connect(self.edit_effects)
        reset = QtWidgets.QPushButton('重置当前样式')
        reset.setToolTip('重置这个组件的字体、位置和动画效果；保留人声语言、时间戳与文案来源。')
        reset.clicked.connect(self.reset_style)
        action_row.addWidget(self.effect_button); action_row.addWidget(reset)
        form.addRow(action_row)
        self.style_note = QtWidgets.QLabel()
        self.style_note.setWordWrap(True)
        self.style_note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.style_note)
        self.controls["caption_file"] = QtWidgets.QLineEdit(self)
        self.controls["caption_file"].setPlaceholderText("请选择 .srt 文件，支持 task_audio[动态].srt")
        self.controls["caption_file"].setToolTip("手动指定的 SRT 由该组件整批共用；不同任务请使用自动读取，或向任务提供 subtitle_path。")
        self.controls["caption_file"].editingFinished.connect(self.edit)
        self.file_row = QtWidgets.QWidget(self)
        file_layout = QtWidgets.QHBoxLayout(self.file_row); file_layout.setContentsMargins(0,0,0,0)
        file_layout.addWidget(self.controls['caption_file'],1)
        self.file_button = QtWidgets.QPushButton('选择 SRT…',self.file_row)
        self.file_button.setAutoDefault(False)
        self.file_button.clicked.connect(self.choose_subtitle)
        file_layout.addWidget(self.file_button)
        form.addRow('SRT 文件',self.file_row)
        self.source_note = QtWidgets.QLabel('字幕池或固定文件在“内容与来源”选择。')
        self.source_note.setWordWrap(True)
        form.addRow(self.source_note)
        for key, label, low, high, suffix in (
            ("caption_words", "每组最多词数", 1, 32, " 个"),
            ("caption_per_line", "每行最多词数", 1, 12, " 个"),
            ("caption_pop_ms", "原有模式弹入时长", 0, 600, " ms"),
            ("caption_hold_ms", "组末停留", 0, 1000, " ms"),
            ("caption_seed", "字号随机种子", 0, 999999, "")):
            control = QtWidgets.QSpinBox()
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setMinimumWidth(0)
            control.valueChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        self.controls['caption_timing'].setToolTip(
            '自动模式优先读取与当前人声绑定的 ELE 动态记录。词级记录按词播放；普通句级 SRT 按整块播放。\n'
            '匀速动画按字幕块或文案长度估算逐词时间；本地模型模式执行精确对齐。')
        self.controls['caption_words'].setToolTip('每组词数上限；逐词字幕按组累积出现，普通句级 SRT 保留原字幕块。')
        self.controls['caption_per_line'].setToolTip('每行词数上限。长词会提前换行，不切开单词；仍受字幕框宽度限制。')
        self._organize_sections(form)
        draft.deleteLater()

    def _organize_sections(self, original):
        from .property_sections import PropertySections
        self.sections = PropertySections()
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.addWidget(self.sections)
        self.forms = {}
        for key,title in (('caption','字幕样式与动画'),('timing','字幕时间'),('caption_layout','分组与节奏'),
                          ('random_size','随机字号（适用动画）')):
            widget = QtWidgets.QWidget(); form = QtWidgets.QFormLayout(widget)
            form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            self.forms[key] = form
            self.sections.add_section(key,title,widget,expanded=key == 'caption')
        timing = {self.controls[key] for key in ('caption_timing','caption_source','caption_language','caption_cloud_consent')}
        timing.add(self.file_row)
        random_size = {self.controls[key] for key in ('caption_random_size','caption_size_contrast','caption_seed')}
        rhythm = {self.controls[key] for key in ('caption_words','caption_per_line','caption_pop_ms','caption_hold_ms')}
        while original.rowCount():
            row = original.takeRow(0)
            field = row.fieldItem.widget() if row.fieldItem else None
            if field is self.controls['caption_random_size']:
                self.sections.sections['random_size'].set_enable_control(field)
                continue
            key = 'timing' if field in timing else 'random_size' if field in random_size else 'caption_layout' if field in rhythm else 'caption'
            if isinstance(field,QtWidgets.QLabel):
                if field is self.source_note: key = 'timing'
            form = self.forms[key]
            item = field if field is not None else row.fieldItem.layout()
            if row.labelItem: form.addRow(row.labelItem.widget(),item)
            else: form.addRow(item)

    def form_for(self,control):
        return next(form for form in self.forms.values() if form.labelForField(control) is not None)


    def set_layer(self, layer):
        self.layer = copy.deepcopy(layer)
        self.setEnabled(bool(layer))
        if not layer:
            return
        self.loading = True
        timing = self.controls["caption_timing"]
        for index in range(timing.count()):
            timing.model().item(index).setEnabled(True)
        for key, control in self.controls.items():
            if isinstance(control, QtWidgets.QComboBox):
                index = control.findData(layer[key])
                if index < 0:
                    control.addItem(layer[key], layer[key])
                    index = control.findData(layer[key])
                control.setCurrentIndex(index)
            elif isinstance(control, QtWidgets.QLineEdit):
                control.setText(layer[key])
            elif isinstance(control, QtWidgets.QCheckBox):
                control.setChecked(layer[key])
            else:
                control.setValue(layer[key])
        self.loading = False
        self.controls['caption_size_contrast'].setEnabled(layer['caption_random_size'])
        from .caption_random import supports_random_size
        self.sections.sections['random_size'].setVisible(supports_random_size(layer))
        self.update_timing_controls()
        self.effect_button.setEnabled(is_catalog(layer))
        self.style_note.setText('播放预览可查看逐词动画。')
        self.style_note.setToolTip(description(layer))
        self.controls['caption_preset'].setToolTip(description(layer))

    def choose_preset(self):
        if self.loading or not self.layer:
            return
        selected = self.controls['caption_preset'].currentData()
        if not selected:
            return
        updated = apply_style(self.layer,selected)
        self.set_layer(updated)
        self.changed.emit({**copy.deepcopy(updated),'_caption_style_apply':True})

    def choose_gallery(self):
        if not self.layer: return
        from .caption_gallery import CaptionGallery
        dialog = CaptionGallery(self.layer["caption_preset"], self)
        try:
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                combo = self.controls["caption_preset"]
                combo.setCurrentIndex(combo.findData(dialog.selected))
        finally:
            dialog.deleteLater()

    def reset_style(self):
        self.choose_preset()

    def edit_effects(self):
        if not self.layer or not is_catalog(self.layer):
            return
        dialog = EffectDialog(self.layer,self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.layer['caption_effects'] = dialog.value()
            self.set_layer(self.layer)
            self.changed.emit(copy.deepcopy(self.layer))

    def choose_subtitle(self):
        if not self.layer or not self.allow_manual_file: return
        path,_ = QtWidgets.QFileDialog.getOpenFileName(self,"选择 SRT（可选择动态逐词字幕）",self.layer.get('caption_file',''),"字幕 (*.srt)")
        if path:
            self.layer.update(source="file", source_file=path, caption_file=path)
            if self.layer['caption_timing'] != 'uniform': self.layer['caption_timing'] = 'file'
            self.set_layer(self.layer)
            self.changed.emit({**copy.deepcopy(self.layer), '_caption_source_apply':True})

    def update_timing_controls(self):
        mode = self.layer['caption_timing'] if self.layer else ''
        self.controls['caption_cloud_consent'].setVisible(mode == 'elevenlabs')
        visible = self.allow_manual_file and mode in {'file','uniform'} and self.layer.get('source') != 'pool'
        self.file_row.setVisible(visible)
        self.form_for(self.file_row).labelForField(self.file_row).setVisible(visible)

    def edit(self):
        if self.loading or not self.layer:
            return
        for key, control in self.controls.items():
            self.layer[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                               control.text().strip() if isinstance(control, QtWidgets.QLineEdit) else
                               control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else control.value())
        source_apply = False
        mode = self.layer['caption_timing']
        if self.allow_manual_file and mode in {'auto','task','model','elevenlabs'}:
            self.layer.update(source='voice',source_file='',caption_file='')
            self.controls['caption_file'].setText('')
            source_apply = True
        elif self.allow_manual_file and self.layer.get('source') != 'pool' and (
                mode == 'file' or mode == 'uniform' and self.layer['caption_file']):
            self.layer.update(source='file',source_file=self.layer['caption_file'])
            source_apply = True
        self.update_timing_controls()
        self.controls['caption_size_contrast'].setEnabled(self.layer['caption_random_size'])
        values = copy.deepcopy(self.layer)
        if source_apply: values['_caption_source_apply'] = True
        self.changed.emit(values)


EFFECT_LABELS = {
    'lzx_pop_scale':'柔弹峰值倍数（1–2）',
    'karaoke_highlight':'当前词文字变色',
    'color_high':'当前词高亮颜色', 'high_gradient_direction':'高亮渐变方向',
    'dynamic_box':'当前词高亮框', 'color_high_bg':'高亮框颜色', 'opacity_high_bg':'高亮框不透明度（0–255）',
    'dynamic_radius':'高亮框圆角', 'dynamic_box_stroke':'只显示高亮框边线',
    'dynamic_box_stroke_width':'高亮框线宽', 'high_padding':'高亮框内边距', 'high_offset_y':'高亮框纵向偏移',
    'dyn_box_anim':'高亮框回弹', 'dyn_box_anim_overshoot':'高亮框回弹峰值', 'dyn_box_anim_duration':'高亮框回弹秒数',
    'only_show_active_word':'只显示正在读的词',
    'anim_in_type':'入场动画', 'anim_out_type':'出场动画', 'anim_in_duration':'入场秒数',
    'anim_out_duration':'出场秒数', 'word_pop_random_min_scale':'随机字号最小倍数',
    'word_pop_random_max_scale':'随机字号最大倍数', 'word_pop_random_duration':'逐词弹入秒数',
    'word_pop_random_pulse_min_scale':'回弹最小峰值', 'word_pop_random_pulse_max_scale':'回弹最大峰值',
    'word_pop_random_pulse_duration':'逐词回弹秒数', 'letter_jump_scale':'放大峰值',
    'letter_jump_duration':'放大秒数', 'floating_amplitude':'漂浮振幅', 'floating_period':'漂浮周期（秒）',
    'char_bounce_height':'跳动高度', 'metronome_bpm':'视觉脉冲节拍（不改变任务时间戳）',
    'flash_color':'闪光颜色', 'flash_duration':'闪光秒数', 'holy_glow_radius':'圣光半径',
    'holy_glow_color':'圣光颜色', 'holy_glow_period':'圣光周期', 'blur_sharp_max':'入场模糊强度',
    'use_box':'整组底框', 'box_adaptive_width':'底框随文字宽度', 'box_padding_x':'底框横向内边距',
    'box_padding_y':'底框纵向内边距', 'box_radius':'底框圆角', 'box_blur':'底框柔化',
    'opacity_bg':'底框不透明度（0–255）', 'bg_gradient_enabled':'底框渐变',
    'bg_gradient_colors':'底框渐变颜色', 'bg_gradient_type':'底框渐变方向',
    'bg_gradient_highlight':'底框顶部柔光', 'shadow_blur':'阴影柔化',
    'stroke_expand_enabled':'多层描边', 'stroke_expand_layers':'描边层数', 'stroke_expand_step':'每层外扩',
    'stroke_expand_feather':'描边柔化', 'stroke_expand_colors':'多层描边颜色',
    'speed_trail_enabled':'速度幻影', 'speed_trail_layers':'幻影层数', 'speed_trail_step':'幻影间隔',
    'speed_trail_color':'幻影颜色', 'speed_trail_opacity':'幻影透明度（0–255）',
    'use_underline':'彩色下划线', 'color_underline':'下划线颜色',
    'global_mask_enabled':'全画面压暗', 'global_mask_color':'压暗颜色', 'global_mask_opacity':'压暗强度（0–1）',
    'ambient_glow_enabled':'金色环境柔光', 'ambient_glow_color':'环境光颜色', 'ambient_glow_radius':'环境光范围',
    'ambient_glow_opacity':'环境光强度（0–1）', 'ambient_lighting_enabled':'环境明暗',
    'ambient_dark_color':'环境暗部颜色', 'ambient_dark_opacity':'环境暗部强度（0–1）',
    'scrolling_mode':'歌词上下文', 'scrolling_visible_lines':'歌词上下文行数',
    'scrolling_opacity_context':'上下文透明度（0–1）', 'scatter_max_words':'随机卡片最多词数',
    'scatter_accum_prob':'词组累积概率（0–1）', 'scatter_min_scale':'随机定位最小字号倍数',
    'scatter_max_scale':'随机定位最大字号倍数', 'scatter_min_rotate':'最小旋转角度', 'scatter_max_rotate':'最大旋转角度',
    'fullpage_typewriter_cursor':'显示打字光标', 'fullpage_typewriter_cursor_color':'光标颜色',
    'fullpage_typewriter_first_line_bold':'首行加粗', 'fullpage_typewriter_first_line_scale':'首行大小倍数',
    'typewriter_reveal_type':'打字机出现单位', 'rotation':'整体旋转角度'}


class EffectDialog(QtWidgets.QDialog):
    """Human-editable effect controls; no JSON required, no model/network work."""
    def __init__(self,layer,parent=None):
        super().__init__(parent)
        self.setWindowTitle('字幕样式 · 效果参数')
        self.resize(560,640)
        self.layer = copy.deepcopy(layer)
        style = effects(layer)
        root = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget(); root.addWidget(tabs,1)
        forms = []
        for name in ('动画','发光与装饰','布局特效'):
            page = QtWidgets.QWidget(); form = QtWidgets.QFormLayout(page)
            form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
            scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
            tabs.addTab(scroll,name); forms.append(form)
        self.controls = {}
        preset = preset_values(layer)
        essential = {'color_high','karaoke_highlight','dynamic_box','anim_in_type','anim_out_type',
            'anim_in_duration','anim_out_duration','use_box','box_padding_x','box_padding_y','box_radius',
            'opacity_bg','bg_gradient_enabled','bg_gradient_colors'}
        from .caption_appearance import COMMON_EFFECT_KEYS
        keys = (set(preset)|set(layer.get('caption_effects',{}))|essential)-COMMON_EFFECT_KEYS
        if style['dynamic_box']:
            keys |= {'high_padding','color_high_bg','opacity_high_bg','dynamic_radius','dynamic_box_stroke',
                'dynamic_box_stroke_width','dyn_box_anim','dyn_box_anim_duration','dyn_box_anim_overshoot'}
        keys-=COMMON_EFFECT_KEYS
        for key in EFFECT_LABELS:
            if key not in keys: continue
            value = style[key]
            if key in {'anim_in_type','anim_out_type','typewriter_reveal_type','bg_gradient_type','high_gradient_direction'}:
                control = Combo()
                choices = ANIMATIONS if key.startswith('anim_') else ('word','char') if key=='typewriter_reveal_type' else ('horizontal','vertical') if key=='high_gradient_direction' else ('linear_h','linear_v')
                labels = {'none':'无','fade':'淡入／淡出','pop':'整体回弹','slide_up':'上滑','slide_down':'下滑',
                    'slide_left':'左滑','slide_right':'右滑','typewriter':'打字机','char_bounce':'跳动',
                    'letter_jump':'逐词放大','word_pop_random':'逐词随机大小','word_pop_random_pulse':'逐词随机回弹',
                    'metronome':'节奏脉冲','blur_sharp':'从模糊变清晰','holy_glow':'圣光','floating':'漂浮',
                    'bullet_reveal':'逐行出现','flash_highlight':'闪光','scatter_pop':'随机气泡卡片',
                    'word_random_position':'随机单词定位','word':'词','char':'字','horizontal':'横向','vertical':'纵向',
                    'linear_h':'横向','linear_v':'纵向','lzx_word_pop':'LZX 逐词柔弹（五段曲线）'}
                for choice in choices: control.addItem(labels.get(choice,choice),choice)
                index = control.findData(value)
                if index<0: control.addItem(str(value),value); index=control.findData(value)
                control.setCurrentIndex(index)
            elif key in COLOR_KEYS:
                control = EffectColorEditor(value)
            elif type(value) is bool:
                control = QtWidgets.QCheckBox(); control.setChecked(value)
            elif isinstance(value,(int,float)):
                control = QtWidgets.QDoubleSpinBox(); control.setDecimals(3); control.setRange(-10000,10000)
                control.setValue(value); control.setSingleStep(.05 if abs(value)<5 else 1)
            else:
                control = QtWidgets.QLineEdit(','.join(map(str,value)) if isinstance(value,list) else value)
            self.controls[key] = control
            section = 0 if key.startswith(('anim_','lzx_')) or any(part in key for part in ('word_pop','jump','floating','bounce','metronome','blur_sharp')) else (
                1 if any(part in key for part in ('ambient','glow','stroke_expand','speed_trail','underline','mask','shadow','flash')) else 2)
            forms[section].addRow(EFFECT_LABELS[key],control)
        note = QtWidgets.QLabel('文字变色、当前词高亮框、整组底框与逐组换色统一在“字体”中设置。这里仅调整特殊动画、发光与布局特效；时间戳不变。')
        note.setWordWrap(True); root.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); root.addWidget(buttons)

    def value(self):
        result = copy.deepcopy(self.layer.get('caption_effects',{}))
        for key,control in self.controls.items():
            result[key] = (control.currentData() if isinstance(control,QtWidgets.QComboBox) else
                control.isChecked() if isinstance(control,QtWidgets.QCheckBox) else
                control.value() if isinstance(control,QtWidgets.QDoubleSpinBox) else control.text().strip())
        return result

    def validate(self):
        try:
            effects({**self.layer,'caption_effects':self.value()})
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self,'字幕参数无效',str(error)); return
        self.accept()


def caption_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    from .component_ui import add_text_properties
    from .captions import DEFAULTS
    from .caption_styles import LAYER_KEYS
    panel = ComponentPropertyPanel(mode=mode)
    def patch(values):
        fields = set(DEFAULTS)
        if values.get('_caption_style_apply'): fields.update(LAYER_KEYS)
        if values.get('_caption_source_apply'): fields.update(('source','source_file'))
        return {key:copy.deepcopy(values[key]) for key in fields if key in values}
    if mode != 'node_caption':
        panel.add_source()
    panel.add_geometry()
    add_text_properties(panel)
    panel.caption_properties = panel.add_bound('caption', '', CaptionProperties(allow_manual_file=mode != 'node_caption'), patch=patch, section=False)
    panel.sections.update(panel.caption_properties.sections.sections)
    from .caption_appearance_ui import CaptionAppearanceEditor
    panel.caption_appearance=panel.add_bound('caption_appearance','',CaptionAppearanceEditor(),section=False)
    panel.body.removeWidget(panel.caption_appearance)
    panel.text_properties.style_sections.sections['font'].content.layout().addRow(panel.caption_appearance)
    panel.sections.update(panel.caption_appearance.sections.sections)
    panel.caption_background=panel.add_bound('caption_background','',CaptionAppearanceEditor(background=True),section=False)
    panel.body.removeWidget(panel.caption_background)
    panel.text_properties.style_sections.sections['background'].content.layout().addRow(panel.caption_background)
    panel.sections.update(panel.caption_background.sections.sections)
    return panel

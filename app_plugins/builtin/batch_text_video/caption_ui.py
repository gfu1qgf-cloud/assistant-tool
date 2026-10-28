"""Compact per-component motion/timing controls, no model work on GUI thread."""
import copy
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo
from .caption_styles import preset_choices, apply_style, description, effects
from .color_ui import EffectColorEditor
from .caption_display import DISPLAY_CHOICES, normalize_display
from .caption_follow import CHOICES as FOLLOW_CHOICES

DISPLAY_NOTES = {
    'group': '整组同时显示；切换到下一组时再更新。',
    'word': '按词依次出现；可与任意入场动画组合。',
    'char': '按阅读顺序逐字出现，可开启光标。逐词记录沿用词时间；整句 SRT 在每块时间内连续打字，字时刻为动画分配，不是逐字语音对齐。',
    'line': '按行依次出现；每行词数由“分组与节奏”控制。',
}
DURATION_NOTES = {
    'anim_in_duration': '常规入场',
    'word_pop_random_duration': '逐词弹入',
    'word_pop_random_pulse_duration': '逐词回弹',
    'letter_jump_duration': '逐词放大',
}


class CaptionProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, parent=None, *, allow_manual_file=True):
        super().__init__(parent)
        self.layer, self.loading = None, False
        self.parameter_window = None
        self.parameter_panel = None
        self.allow_manual_file = allow_manual_file
        draft = QtWidgets.QWidget(self)
        form = QtWidgets.QFormLayout(draft)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.controls = {}
        for key, label, choices in (
            ("caption_preset", "动画预设", [("逐词跃动", "pop"), ("逐词出现（无弹跳）", "appear")]+preset_choices()),
            ('caption_display_mode', '显示方式', DISPLAY_CHOICES),
            ('caption_follow_mode', '跟随方式', FOLLOW_CHOICES),
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
                control.setToolTip('切换外观、动画和显示参数；保留已有位置尺寸与分组节奏。需要完整默认排版时点击“还原预设默认值”。')
                control.setInsertPolicy(control.InsertPolicy.NoInsert)
                control.completer().setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
                control.completer().setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
                control.currentIndexChanged.connect(self.choose_preset)
            else:
                control.currentIndexChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        for key,title in (('caption_accumulate','全文累积显示（不按字幕组清屏）'),
                          ('caption_cursor','显示逐字光标'),
                          ('caption_first_line_emphasis','强调首行'),
                          ('caption_first_line_bold','首行加粗')):
            control=QtWidgets.QCheckBox(title,self)
            control.toggled.connect(self.edit)
            self.controls[key]=control
            form.addRow(control)
        self.controls['caption_accumulate'].setToolTip('保留全文直到最后；每组词数暂不参与分页。整篇放不下会提示，不会删字。')
        self.controls['caption_first_line_emphasis'].setToolTip('只作用于实际排版后的第一行，不是前几个单词；关闭后恢复基础字号和字重。')
        first_size=QtWidgets.QDoubleSpinBox(self); first_size.setRange(.5,3); first_size.setDecimals(2)
        first_size.setSingleStep(.05); first_size.setSuffix(' ×'); first_size.valueChanged.connect(self.edit)
        self.controls['caption_first_line_scale']=first_size
        form.addRow('首行字号倍数',first_size)
        cursor_color=EffectColorEditor('#FFD700',self)
        cursor_color.add_button.hide()
        cursor_color.field.textChanged.connect(self.edit)
        self.controls['caption_cursor_color']=cursor_color
        form.addRow('光标颜色',cursor_color)
        consent = QtWidgets.QCheckBox('允许上传当前人声和文案并消耗 ELE 对齐额度', self)
        consent.setToolTip('只用主程序指定的对齐 Key；首次播放预览也会对齐，后续复用。原 task_audio.srt 不会改动。')
        consent.toggled.connect(self.edit)
        self.controls['caption_cloud_consent'] = consent
        form.addRow(consent)
        random_size = QtWidgets.QCheckBox('启用随机字号', self)
        random_size.setAccessibleName('启用随机字号')
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
        action_row.addWidget(self.gallery_button)
        self.parameter_actions = action_row
        self.parameter_button = QtWidgets.QPushButton('参数一览…')
        self.parameter_button.setAutoDefault(False)
        self.parameter_button.setToolTip('比较预设与当前参数；双击展开、滚动定位并闪烁提示。')
        self.parameter_button.clicked.connect(self.show_parameters)
        action_row.addWidget(self.parameter_button)
        reset = QtWidgets.QPushButton('还原预设默认值')
        self.reset_button=reset
        reset.setToolTip('明确重置当前预设的外观、位置尺寸和分组节奏；保留任务文字、人声语言、时间戳与来源。普通切换预设不重置位置和分组。')
        reset.clicked.connect(self.reset_style)
        action_row.addWidget(reset)
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
        self.source_note = QtWidgets.QLabel('字幕池或固定文件在上方“来源”选择。')
        self.source_note.setWordWrap(True)
        form.addRow(self.source_note)
        for key, label, low, high, suffix in (
            ("caption_words", "每组最多词数", 1, 32, " 个"),
            ("caption_per_line", "每行最多词数", 1, 12, " 个"),
            ("caption_pop_ms", "入场动画时长", 0, 5000, " ms"),
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
            '逐词朗读跟随需要词级时间：自动模式遇到句级 SRT 会继续精确逐词对齐。\n'
            '匀速动画按字幕块或文案长度估算逐词时间；本地模型模式执行精确对齐。')
        self.controls['caption_words'].setToolTip('每组词数上限；逐词字幕按组累积出现，普通句级 SRT 保留原字幕块。')
        self.controls['caption_per_line'].setToolTip('每行词数上限。长词会提前换行，不切开单词；仍受字幕框宽度限制。')
        for key,label,low,high,suffix in (
                ('caption_focus_x','聚焦位置 X',0,100,' %'),
                ('caption_focus_y','聚焦位置 Y',0,100,' %'),
                ('caption_clip_feather','裁切边缘柔化',0,50,' %'),
                ('caption_context_opacity','未读不透明度',0,100,' %'),
                ('caption_active_opacity','正在读不透明度',0,100,' %'),
                ('caption_read_opacity','已读不透明度',0,100,' %'),
                ('caption_follow_ms','滚动平滑时间',0,2000,' ms')):
            control=QtWidgets.QSpinBox() if key=='caption_follow_ms' else QtWidgets.QDoubleSpinBox()
            control.setRange(low,high);control.setSuffix(suffix)
            control.valueChanged.connect(self.edit)
            self.controls[key]=control;form.addRow(label,control)
        self.controls['caption_focus_x'].setToolTip('相对字幕框宽度；逐词跟随时使用。50% 为字幕框水平中心。')
        self.controls['caption_focus_y'].setToolTip('相对字幕框高度；50% 为字幕框垂直中心。')
        self.controls['caption_follow_ms'].setToolTip('在下一个词开始前平滑移到聚焦位置；停顿时停住。0 ms 直接切换，绝不修改原时间戳。')
        self.controls['caption_context_opacity'].setToolTip('朗读开始前的不透明度，默认40%。0%隐藏，100%正常显示；相对于控件总不透明度。不会让被显示方式隐藏的未读文字提前出现。')
        self.controls['caption_active_opacity'].setToolTip('当前词从开始到结束时刻的不透明度，默认100%。与未读、已读分别控制；相对于控件总不透明度。')
        self.controls['caption_read_opacity'].setToolTip('当前词朗读结束后的不透明度，默认100%，恢复正常文字显示。可独立设为0%隐藏或降低亮度；相对于控件总不透明度。')
        self.controls['caption_clip_feather'].setToolTip('朗读跟随字幕框四边向内渐隐，数值相对框的短边。0%为硬裁切，可先试8%；作用于字幕图层及其描边、阴影和底色，不模糊字形、不影响背景视频。')
        self._organize_sections(form)
        from .caption_effect_ui import CaptionEffectEditor
        from .property_sections import PropertySection
        self.effects_editor = CaptionEffectEditor()
        self.effects_editor.changed.connect(self.apply_effects)
        self.effect_section = PropertySection('效果细节', self.effects_editor)
        self.forms['caption'].addRow(self.effect_section)
        draft.deleteLater()

    def _organize_sections(self, original):
        from .property_sections import PropertySection, PropertySections
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0,0,0,0)
        self.basic_widget=QtWidgets.QWidget()
        form=QtWidgets.QFormLayout(self.basic_widget); form.setContentsMargins(0,0,0,0)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms={'timing':form}
        root.addWidget(self.basic_widget)
        self.sections=PropertySections(); root.addWidget(self.sections)
        style_widget=QtWidgets.QWidget(); style_form=QtWidgets.QFormLayout(style_widget)
        style_form.setRowWrapPolicy(style_form.RowWrapPolicy.WrapLongRows)
        style_form.setFieldGrowthPolicy(style_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms['caption']=style_form
        self.sections.add_section('caption','字幕样式与动画',style_widget,expanded=True)
        rhythm_widget=QtWidgets.QWidget(); rhythm_form=QtWidgets.QFormLayout(rhythm_widget)
        rhythm_form.setRowWrapPolicy(rhythm_form.RowWrapPolicy.WrapLongRows)
        rhythm_form.setFieldGrowthPolicy(rhythm_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms['caption_layout']=rhythm_form
        self.rhythm_section=PropertySection('分组与节奏',rhythm_widget)
        self.sections.sections['caption_layout']=self.rhythm_section
        follow_widget=QtWidgets.QWidget();follow_form=QtWidgets.QFormLayout(follow_widget)
        follow_form.setFieldGrowthPolicy(follow_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms['caption_follow']=follow_form
        self.follow_section=PropertySection('朗读跟随',follow_widget)
        self.sections.sections['caption_follow']=self.follow_section
        self.typography_widget=QtWidgets.QWidget(self)
        typography=QtWidgets.QFormLayout(self.typography_widget)
        typography.setContentsMargins(0,0,0,0)
        typography.setRowWrapPolicy(typography.RowWrapPolicy.WrapLongRows)
        typography.setFieldGrowthPolicy(typography.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms['typography']=typography
        self.random_widget=QtWidgets.QWidget(self.typography_widget)
        random_form=QtWidgets.QFormLayout(self.random_widget)
        random_form.setContentsMargins(0,0,0,0)
        random_form.setRowWrapPolicy(random_form.RowWrapPolicy.WrapLongRows)
        random_form.setFieldGrowthPolicy(random_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forms['random_size']=random_form
        typography.addRow(self.random_widget)
        root.addWidget(self.typography_widget)
        timing={self.controls[key] for key in ('caption_timing','caption_source','caption_language','caption_cloud_consent','caption_clip_feather')}
        timing.update((self.file_row,self.source_note))
        rhythm={self.controls[key] for key in ('caption_words','caption_per_line','caption_accumulate','caption_hold_ms')}
        random_size = {self.controls[key] for key in ('caption_random_size','caption_size_contrast','caption_seed')}
        font_controls={self.controls[key] for key in ('caption_first_line_emphasis','caption_first_line_bold','caption_first_line_scale')}
        follow_controls={self.controls[key] for key in ('caption_follow_mode','caption_focus_x','caption_focus_y',
            'caption_context_opacity','caption_active_opacity','caption_read_opacity','caption_follow_ms')}
        while original.rowCount():
            row = original.takeRow(0)
            field = row.fieldItem.widget() if row.fieldItem else None
            key = ('caption_follow' if field in follow_controls else 'random_size' if field in random_size else 'typography' if field in font_controls else
                   'timing' if field in timing else 'caption_layout' if field in rhythm else 'caption')
            form = self.forms[key]
            item = field if field is not None else row.fieldItem.layout()
            if row.labelItem: form.addRow(row.labelItem.widget(),item)
            else: form.addRow(item)
        from .property_pairs import pair_form_fields, order_form_fields
        base=self.forms['caption']
        order_form_fields(base,[self.controls[key] for key in ('caption_preset','caption_display_mode',
            'caption_pop_ms','caption_cursor','caption_cursor_color')])
        self.display_note = QtWidgets.QLabel()
        self.display_note.setWordWrap(True)
        self.display_note.setStyleSheet('color: palette(mid);')
        base.insertRow(base.getWidgetPosition(self.controls['caption_display_mode'])[0]+1, self.display_note)
        order_form_fields(self.forms['timing'],[self.controls['caption_clip_feather'],self.controls['caption_timing'],self.file_row]+
            [self.controls[key] for key in ('caption_source','caption_language','caption_cloud_consent')])
        order_form_fields(rhythm_form,[self.controls[key] for key in
            ('caption_words','caption_per_line','caption_accumulate','caption_hold_ms')])
        self.group_pair=pair_form_fields(rhythm_form,'词数',[
            ('group','每组',self.controls['caption_words']),('line','每行',self.controls['caption_per_line'])])
        style_form.addRow(self.rhythm_section)
        style_form.addRow(self.follow_section)
        pair_form_fields(follow_form,'聚焦位置',[
            ('x','X',self.controls['caption_focus_x']),('y','Y',self.controls['caption_focus_y'])])
        note=QtWidgets.QLabel('按词跟随需要逐词时间戳；句级 SRT 可用按行／字幕块跟随。全文连续排版、超出字幕框裁切，'
            '不会因文案长而整篇缩小。选择“整组显示”可预先看到上下文。每行词数控制换行，每组词数仍可控制分组配色。'
            '未读、正在读、已读不透明度分别调节；词结束后转为已读，默认恢复100%。'
            '跟随模式优先连续排版，随机定位和歌词上下文参数保留，关闭跟随后恢复。')
        note.setWordWrap(True);follow_form.addRow(note)
        # These values affect glyph SIZE, so they live with the base font size.
        self.font_effect_controls={}
        for key,title,low,high in (
                ('block_typography_enabled','启用字词缩放排版',None,None),
                ('block_scale_min','排版缩放下限',.1,3),('block_scale_max','排版缩放上限',.1,3),
                ('word_pop_random_min_scale','随机字词缩放下限',.1,3),
                ('word_pop_random_max_scale','随机字词缩放上限',.1,3),
                ('scatter_min_scale','分散字词缩放下限',.1,3),('scatter_max_scale','分散字词缩放上限',.1,3)):
            if low is None:
                control=QtWidgets.QCheckBox(title); control.toggled.connect(self.edit_font_effects)
                typography.addRow(control)
            else:
                control=QtWidgets.QDoubleSpinBox(); control.setRange(low,high); control.setDecimals(2)
                control.setSuffix(' ×'); control.setSingleStep(.05); control.valueChanged.connect(self.edit_font_effects)
                typography.addRow(title,control)
            self.font_effect_controls[key]=control
        for low,high,title in (('block_scale_min','block_scale_max','排版缩放'),
                ('word_pop_random_min_scale','word_pop_random_max_scale','随机字词缩放'),
                ('scatter_min_scale','scatter_max_scale','分散字词缩放')):
            pair_form_fields(typography,title,[('min','最小',self.font_effect_controls[low]),
                                              ('max','最大',self.font_effect_controls[high])])

    def form_for(self,control):
        return next(form for form in self.forms.values() if form.labelForField(control) is not None)


    def set_layer(self, layer):
        self.layer = copy.deepcopy(layer)
        self.setEnabled(bool(layer))
        if self.parameter_window is not None:
            self.parameter_window.queue_refresh()
        if not layer:
            self.effects_editor.set_layer(None)
            return
        normalize_display(self.layer)
        layer=self.layer
        self.loading = True
        style=effects(layer)
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
            elif isinstance(control, (QtWidgets.QLineEdit,EffectColorEditor)):
                control.setText(layer[key])
            elif isinstance(control, QtWidgets.QCheckBox):
                control.setChecked(layer[key])
            else:
                if key == 'caption_pop_ms':
                    from .caption_styles import entrance_duration_key
                    control.setValue(round(style[entrance_duration_key(style)]*1000))
                else:
                    control.setValue(layer[key])
        for key,control in self.font_effect_controls.items():
            if isinstance(control,QtWidgets.QCheckBox): control.setChecked(style[key])
            else: control.setValue(style[key])
        self.loading = False
        self.effects_editor.set_layer(layer)
        self.controls['caption_size_contrast'].setEnabled(True)
        self.random_widget.show()
        self.update_display_controls()
        self.update_duration_label(style)
        self.update_timing_controls()
        self.style_note.setText('播放预览可查看逐词动画。')
        self.style_note.setToolTip(description(layer))
        self.controls['caption_preset'].setToolTip(description(layer))

    def update_display_controls(self):
        if not self.layer: return
        from .caption_follow import enabled as follow_enabled
        self.controls['caption_clip_feather'].setEnabled(follow_enabled(self.layer))
        self.display_note.setText(DISPLAY_NOTES.get(
            self.controls['caption_display_mode'].currentData(), '选择字幕显示方式。'))
        self.random_widget.show()
        self.controls['caption_cursor'].show()
        color=self.controls['caption_cursor_color']; color.show()
        self.form_for(color).labelForField(color).show()
        color.setEnabled(True)
        color.setToolTip('逐字显示并开启光标时生效；其他模式也可以预先设置，不会清除数值。')
        self.controls['caption_words'].setEnabled(True)
        self.controls['caption_size_contrast'].setEnabled(True)
        for key in ('caption_first_line_bold','caption_first_line_scale'):
            self.controls[key].setEnabled(True)
        for key,control in self.font_effect_controls.items():
            control.show()
            control.setEnabled(True)
            label=self.forms['typography'].labelForField(control)
            if label: label.show()
            if key.startswith('word_pop_random_'):
                control.setToolTip('逐词随机大小动画使用；启用字词缩放排版时使用排版范围。数值始终可编辑、保留。')
            elif key.startswith('scatter_'):
                control.setToolTip('随机定位动画使用；启用字词缩放排版时使用排版范围。数值始终可编辑、保留。')

    def update_duration_label(self, style):
        from .caption_styles import entrance_duration_key
        control = self.controls['caption_pop_ms']
        key = entrance_duration_key(style)
        self.form_for(control).labelForField(control).setText(
            f'入场时长（{DURATION_NOTES[key]}）')
        control.setToolTip(f'当前控制“{DURATION_NOTES[key]}”的持续时间；切换入场动画会自动对应到该动画。')

    def edit_font_effects(self,*_args):
        if self.loading or not self.layer: return
        overrides=copy.deepcopy(self.layer.get('caption_effects',{}))
        for key,control in self.font_effect_controls.items():
            overrides[key]=control.isChecked() if isinstance(control,QtWidgets.QCheckBox) else control.value()
        candidate={**self.layer,'caption_effects':overrides}
        try: effects(candidate)
        except ValueError as error:
            self.style_note.setText(str(error)); return
        self.layer=candidate; self.update_display_controls()
        self.effects_editor.set_layer(candidate)
        self.changed.emit(copy.deepcopy(candidate))

    def choose_preset(self):
        self._apply_preset(reset_layout=False)

    def _apply_preset(self, *, reset_layout):
        if self.loading or not self.layer:
            return
        selected = self.controls['caption_preset'].currentData()
        if not selected:
            return
        updated = apply_style(self.layer,selected,reset_layout=reset_layout)
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

    def show_parameters(self):
        if not self.layer: return
        if self.parameter_window is None:
            from .caption_parameter_window import CaptionParameterWindow
            self.parameter_window = CaptionParameterWindow(self)
        self.parameter_window.refresh()
        self.parameter_window.show()
        self.parameter_window.raise_()
        self.parameter_window.activateWindow()

    def reset_style(self):
        self._apply_preset(reset_layout=True)

    def apply_effects(self, values):
        if self.loading or not self.layer: return
        candidate={**self.layer,'caption_effects':copy.deepcopy(values['caption_effects'])}
        self.set_layer(candidate)
        self.changed.emit(copy.deepcopy(candidate))

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
        candidate=copy.deepcopy(self.layer)
        for key, control in self.controls.items():
            if key == 'caption_pop_ms':
                continue  # UI alias for the active animation's single duration.
            candidate[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                               control.text().strip() if isinstance(control, (QtWidgets.QLineEdit,EffectColorEditor)) else
                               control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else control.value())
        if self.sender() is self.controls['caption_pop_ms']:
            from .caption_styles import entrance_duration_key
            key=entrance_duration_key(effects(candidate))
            candidate['caption_effects']={**candidate.get('caption_effects',{}),
                key:self.controls['caption_pop_ms'].value()/1000}
        try: normalize_display(candidate)
        except ValueError as error:
            self.style_note.setText(str(error)); return
        self.layer=candidate
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
        self.controls['caption_size_contrast'].setEnabled(True)
        self.update_display_controls()
        self.effects_editor.set_layer(self.layer)
        values = copy.deepcopy(self.layer)
        if source_apply: values['_caption_source_apply'] = True
        self.changed.emit(values)




def caption_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    from .component_ui import add_text_properties, TextBoxProperties
    from .captions import DEFAULTS
    from .caption_display import DEFAULTS as DISPLAY_DEFAULTS
    from . import caption_styles
    if (not DISPLAY_DEFAULTS.keys() <= DEFAULTS.keys() or 'caption_effects_version' not in DEFAULTS or
            not callable(getattr(caption_styles,'freeze_effect_parameters',None)) or
            not getattr(TextBoxProperties,'paired_font_fields',False)):
        raise RuntimeError('字幕源码已更新，请在方便时重启程序后使用新的字幕设置；当前参数未修改。')
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
    typography=panel.caption_properties.typography_widget
    panel.caption_properties.layout().removeWidget(typography)
    font_form=panel.text_properties.font_size_form
    font_form.insertRow(font_form.getWidgetPosition(panel.text_properties.font_size_pair)[0]+1,typography)
    from .caption_appearance_ui import CaptionAppearanceEditor
    panel.caption_appearance=panel.add_bound('caption_appearance','',CaptionAppearanceEditor(),section=False)
    panel.body.removeWidget(panel.caption_appearance)
    font_form.insertRow(font_form.getWidgetPosition(panel.text_properties.random_color_section)[0],panel.caption_appearance)
    panel.sections.update(panel.caption_appearance.sections.sections)
    panel.caption_background=panel.add_bound('caption_background','',CaptionAppearanceEditor(background=True),section=False)
    panel.body.removeWidget(panel.caption_background)
    panel.text_properties.style_sections.sections['background'].content.layout().addRow(panel.caption_background)
    panel.sections.update(panel.caption_background.sections.sections)
    from .caption_basics_ui import compose_basics
    compose_basics(panel)
    return panel

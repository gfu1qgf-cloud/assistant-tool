"""Live subtitle effect inspector, shared by canvas and node properties."""
import copy
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo
from .color_ui import EffectColorEditor
from .caption_styles import effects, ANIMATIONS, COLOR_KEYS, colors
from .caption_effect_controls import GROUPS, number_range
from .property_sections import PropertySections

EFFECT_LABELS = {
    'pop_start':'回弹起始倍数', 'pop_peak':'回弹峰值倍数', 'lzx_pop_scale':'柔弹峰值倍数（1–2）',
    'anim_in_type':'入场动画', 'anim_out_type':'出场动画', 'anim_in_duration':'入场秒数',
    'anim_out_duration':'出场秒数', 'rotation':'整体旋转角度',
    'word_pop_random_duration':'逐词弹入秒数', 'word_pop_random_pulse_min_scale':'回弹最小峰值',
    'word_pop_random_pulse_max_scale':'回弹最大峰值', 'word_pop_random_pulse_duration':'逐词回弹秒数',
    'letter_jump_scale':'放大峰值', 'letter_jump_duration':'放大秒数',
    'floating_amplitude':'漂浮振幅', 'floating_period':'漂浮周期（秒）', 'char_bounce_height':'跳动高度',
    'metronome_bpm':'视觉脉冲节拍（不改时间戳）', 'flash_color':'闪光颜色', 'flash_duration':'闪光秒数',
    'holy_glow_radius':'圣光半径', 'holy_glow_color':'圣光颜色', 'blur_sharp_max':'入场模糊强度',
    'dyn_box_anim':'高亮框回弹', 'dyn_box_anim_overshoot':'高亮框回弹峰值', 'dyn_box_anim_duration':'高亮框回弹秒数',
    'use_underline':'彩色下划线', 'color_underline':'下划线颜色',
    'stroke_expand_enabled':'多层描边', 'stroke_expand_layers':'描边层数', 'stroke_expand_step':'每层外扩',
    'stroke_expand_feather':'描边柔化', 'stroke_expand_colors':'多层描边颜色',
    'speed_trail_enabled':'速度幻影', 'speed_trail_layers':'幻影层数', 'speed_trail_step':'幻影间隔',
    'speed_trail_color':'幻影颜色', 'speed_trail_opacity':'幻影透明度（0–255）',
    'global_mask_enabled':'全画面压暗', 'global_mask_color':'压暗颜色', 'global_mask_opacity':'压暗强度（0–1）',
    'ambient_glow_enabled':'环境柔光', 'ambient_glow_color':'环境光颜色', 'ambient_glow_radius':'环境光范围',
    'ambient_glow_opacity':'环境光强度（0–1）', 'ambient_lighting_enabled':'环境明暗',
    'ambient_dark_color':'环境暗部颜色', 'ambient_dark_opacity':'环境暗部强度（0–1）',
    'only_show_active_word':'只显示正在读的词', 'scrolling_mode':'歌词上下文',
    'scrolling_visible_lines':'上下文行数', 'scrolling_opacity_context':'上下文透明度（0–1）',
    'scatter_seed':'布局随机种子', 'scatter_min_rotate':'最小旋转角度', 'scatter_max_rotate':'最大旋转角度',
    'scatter_max_words':'随机卡片最多词数', 'scatter_accum_prob':'词组累积概率（0–1）',
}
ANIMATION_LABELS = {'none':'无', 'fade':'淡入／淡出', 'pop':'整体回弹', 'slide_up':'上滑',
    'slide_down':'下滑', 'slide_left':'左滑', 'slide_right':'右滑', 'char_bounce':'跳动',
    'letter_jump':'逐词放大', 'word_pop_random':'逐词随机大小', 'word_pop_random_pulse':'逐词随机回弹',
    'metronome':'节奏脉冲', 'blur_sharp':'从模糊变清晰', 'holy_glow':'圣光', 'floating':'漂浮',
    'flash_highlight':'闪光', 'scatter_pop':'随机气泡卡片', 'word_random_position':'随机单词定位',
    'lzx_word_pop':'LZX 逐词柔弹（五段曲线）'}


class CaptionEffectEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self, layer=None, parent=None):
        super().__init__(parent)
        self.layer = None
        self.loading = True
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.categories = PropertySections(); root.addWidget(self.categories)
        self.pages = []
        for key, title in (('animation','动画参数'), ('decoration','发光与装饰'), ('layout','布局特效')):
            page = PropertySections()
            self.categories.add_section(key, title, page, expanded=key == 'animation')
            self.pages.append(page)
        self.controls, self.groups, self.group_titles = {}, {}, {}
        from .captions import new_caption
        initial = effects(layer or new_caption())
        for identifier, title, tab, switch, fields, modes in GROUPS:
            widget = QtWidgets.QWidget(); form = QtWidgets.QFormLayout(widget)
            form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            section = self.pages[tab].add_section(identifier, title, widget, expanded=identifier == 'motion')
            self.groups[identifier] = section
            self.group_titles[identifier] = title
            if switch:
                section.set_enable_control(self._create(switch, initial[switch]), disable_content=False)
            for key in fields:
                form.addRow(EFFECT_LABELS[key], self._create(key, initial[key]))
        self.error = QtWidgets.QLabel(); self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#c33442;'); self.error.hide(); root.addWidget(self.error)
        self.set_layer(layer)

    def _create(self, key, value):
        if key in {'anim_in_type', 'anim_out_type'}:
            control = Combo()
            choices = tuple(mode for mode in ANIMATIONS if mode not in {'typewriter','bullet_reveal'})
            if key == 'anim_out_type': choices = ('none','fade','pop','slide_up','slide_down','slide_left','slide_right')
            for mode in choices: control.addItem(ANIMATION_LABELS[mode], mode)
            control.currentIndexChanged.connect(self.edit)
        elif key in COLOR_KEYS:
            control = EffectColorEditor(value); control.field.textChanged.connect(self.edit)
        elif type(value) is bool:
            control = QtWidgets.QCheckBox(); control.toggled.connect(self.edit)
        elif isinstance(value, (int, float)):
            low, high, decimals = number_range(key)
            control = QtWidgets.QDoubleSpinBox(); control.setDecimals(decimals); control.setRange(low, high)
            control.setKeyboardTracking(False); control.setMinimumWidth(0)
            control.setSingleStep(.05 if abs(value) < 5 else 1); control.valueChanged.connect(self.edit)
        else:
            control = QtWidgets.QLineEdit(); control.editingFinished.connect(self.edit)
        self.controls[key] = control
        return control

    def set_layer(self, layer):
        self.loading = True
        try:
            self.layer = copy.deepcopy(layer)
            self.setEnabled(bool(layer))
            if not layer: return
            style = effects(layer)
            for key, control in self.controls.items():
                value = style[key]
                if isinstance(control, QtWidgets.QComboBox): control.setCurrentIndex(control.findData(value))
                elif isinstance(control, QtWidgets.QCheckBox): control.setChecked(value)
                elif isinstance(control, QtWidgets.QDoubleSpinBox): control.setValue(value)
                else: control.setText(','.join(map(str, value)) if isinstance(value, list) else str(value))
            self.error.hide(); self.update_groups()
        finally:
            self.loading = False

    def update_groups(self, *_args):
        mode = self.controls['anim_in_type'].currentData()
        for identifier, _title, _tab, _switch, _fields, modes in GROUPS:
            section = self.groups[identifier]
            section.show(); section.setEnabled(True)
            active = bool(modes and mode in modes)
            section.toggle.setText(self.group_titles[identifier] + (' · 当前使用' if active else ''))
            if modes:
                section.setToolTip('当前入场动画使用此组参数。' if active else
                    '选择对应入场动画后生效；现在也可编辑并保留数值。')
        for control in self.controls.values(): control.setEnabled(True)
        self.groups['box_bounce'].setToolTip('在字体中开启当前词高亮框后生效；关闭不会清除参数。')
        self.groups['layout'].setToolTip('开启后，无特殊动画也保留特效样式的字距和回弹排布。关闭后普通效果使用经典排版；特殊动画仍会自动采用特效排版。')

    def value(self):
        result = copy.deepcopy(self.layer.get('caption_effects', {}))
        initial = effects(self.layer)
        for key, control in self.controls.items():
            value = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else
                control.value() if isinstance(control, QtWidgets.QDoubleSpinBox) else control.text().strip())
            same = value == initial[key]
            if key in COLOR_KEYS: same = colors(value) == colors(initial[key])
            elif isinstance(control, QtWidgets.QDoubleSpinBox): same = value == round(initial[key], control.decimals())
            if not same: result[key] = value
        return result

    def edit(self, *_args):
        if self.loading or not self.layer: return
        try:
            values = self.value()
            candidate = {**self.layer, 'caption_effects': values}
            effects(candidate)
        except ValueError as error:
            self.error.setText(str(error)); self.error.show(); return
        self.layer = copy.deepcopy(candidate)
        self.error.hide(); self.update_groups()
        self.changed.emit({'caption_effects': copy.deepcopy(values)})

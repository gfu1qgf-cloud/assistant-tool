"""Read-only parameter comparisons and navigation to the existing bound controls."""
import copy
import math

from qt_compat import QtCore, QtGui, QtWidgets
from .caption_styles import apply_style, effects, COLOR_KEYS, colors
from .caption_effect_ui import EFFECT_LABELS
from .color_ui import EffectColorEditor
from .property_sections import PropertySection

LABELS = {
    'font_min': '最小字号', 'font_max': '最大字号', 'font_weight': '字重',
    'caption_words': '每组最多词数', 'caption_per_line': '每行最多词数',
    'font': '字体', 'color': '文字颜色', 'outline_color': '描边颜色',
    'shadow_color': '阴影颜色', 'background_color': '底色颜色',
    'x': '横向中心（%）', 'y': '纵向中心（%）', 'width': '框宽度（%）', 'height': '框高度（%）',
    'block_scale_min': '排版缩放下限', 'block_scale_max': '排版缩放上限',
    'word_pop_random_min_scale': '随机字词缩放下限', 'word_pop_random_max_scale': '随机字词缩放上限',
    'scatter_min_scale': '分散字词缩放下限', 'scatter_max_scale': '分散字词缩放上限',
}
EXCLUDED = {'caption_preset', 'caption_timing', 'caption_source', 'caption_language',
            'caption_file', 'caption_cloud_consent', 'caption_pop_ms'}


def control_label(owner, control, key):
    if key in LABELS: return LABELS[key]
    if key in EFFECT_LABELS: return EFFECT_LABELS[key]
    if isinstance(control, QtWidgets.QCheckBox):
        return control.text() or control.accessibleName() or key
    child = control
    while child is not owner and child.parentWidget() is not None:
        parent = child.parentWidget()
        form = parent.layout()
        if isinstance(form, QtWidgets.QFormLayout):
            label = form.labelForField(child)
            if isinstance(label, QtWidgets.QLabel) and label.text(): return label.text()
        child = parent
    return control.accessibleName() or key


def control_path(control):
    titles = []
    parent = control.parentWidget()
    while parent is not None:
        if isinstance(parent, PropertySection): titles.append(parent.toggle.text())
        parent = parent.parentWidget()
    return ' › '.join(reversed(titles)) or '字幕参数'


def parameter_targets(owner):
    """Resolve widgets afresh: a row never retains another layer's draft/value."""
    targets = {}
    def add(editor, controls, domain):
        for key, control in controls.items():
            if key in EXCLUDED or control.isHidden(): continue
            targets[(domain, key)] = (control, control_label(editor, control, key), control_path(control))
    add(owner, owner.controls, 'layer')
    add(owner, owner.font_effect_controls, 'effect')
    add(owner.effects_editor, owner.effects_editor.controls, 'effect')
    panel = owner.parameter_panel
    if panel is not None:
        add(panel.text_properties, panel.text_properties.controls, 'layer')
        add(panel.geometry_editor, panel.geometry_editor.properties, 'layer')
        for editor in (panel.caption_appearance, panel.caption_background):
            from .caption_appearance import DEFAULTS
            for key, control in editor.controls.items():
                add(editor, {key: control}, 'layer' if key in DEFAULTS else 'effect')
    return targets


def comparison_layers(layer, mode):
    from .captions import new_caption
    from .layers import normalize_layers
    basic = normalize_layers([apply_style(new_caption(), 'appear', reset_layout=True)])[0]
    preset = normalize_layers([apply_style(new_caption(), layer['caption_preset'], reset_layout=True)])[0]
    if mode == 'preset': return preset, basic
    if mode == 'overrides': return copy.deepcopy(layer), preset
    return copy.deepcopy(layer), basic


def same_value(key, first, second):
    if key in COLOR_KEYS or (isinstance(first, str) and isinstance(second, str)
                            and QtGui.QColor(first).isValid() and QtGui.QColor(second).isValid()):
        return colors(first) == colors(second)
    if type(first) in (float, int) and type(second) in (float, int):
        return math.isclose(first, second, rel_tol=1e-9, abs_tol=1e-7)
    return first == second


def display_value(control, value):
    if type(value) is bool: return '开启' if value else '关闭'
    if isinstance(control, QtWidgets.QComboBox):
        index = control.findData(value)
        if index >= 0: return control.itemText(index)
    if isinstance(value, float): return format(value, '.8g')
    if isinstance(value, list): return '，'.join(map(str, value))
    return str(value)


class ParameterFlash(QtWidgets.QWidget):
    """A mouse-transparent overlay; never rewrites a control's palette or QSS."""
    def __init__(self, control):
        super().__init__(control)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setObjectName('caption_parameter_flash')
        self.phase = 0
        control.installEventFilter(self)
        self.setGeometry(control.rect())
        self.animation = QtCore.QVariantAnimation(self)
        self.animation.setStartValue(0.)
        self.animation.setEndValue(1.)
        self.animation.setDuration(550)
        self.animation.setLoopCount(4)
        self.animation.valueChanged.connect(self._phase)
        self.animation.finished.connect(self.deleteLater)
        self.show(); self.raise_(); self.animation.start()

    def _phase(self, value):
        self.phase = math.sin(math.pi * value)
        self.update()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Resize: self.setGeometry(watched.rect())
        return False

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        fill = QtGui.QColor('#ffbf47'); fill.setAlpha(round(110*self.phase))
        border = QtGui.QColor('#ff9800'); border.setAlpha(round(255*self.phase))
        painter.setBrush(fill); painter.setPen(QtGui.QPen(border, 3))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 4, 4)

    def stop(self):
        self.animation.stop(); self.hide(); self.deleteLater()


class CaptionParameterWindow(QtWidgets.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.layer_id = None
        self.pending = None
        self.flash = None
        self.setWindowTitle('字幕预设 · 参数一览')
        self.setModal(False)
        self.resize(980, 590)
        root = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel(); self.title.setWordWrap(True); root.addWidget(self.title)
        self.note = QtWidgets.QLabel('比较界面中可调参数的数值差异；保留的位置、字号也计入差异。双击后窗口收起，定位参数并闪烁4次。')
        self.note.setWordWrap(True); root.addWidget(self.note)
        row = QtWidgets.QHBoxLayout(); root.addLayout(row)
        self.mode = QtWidgets.QComboBox()
        for title, key in (('当前参数 ↔ 基础白字', 'current'), ('预设原值 ↔ 基础白字', 'preset'),
                           ('当前参数 ↔ 预设原值', 'overrides')):
            self.mode.addItem(title, key)
        row.addWidget(self.mode)
        self.search = QtWidgets.QLineEdit(); self.search.setPlaceholderText('搜索参数、位置或数值…')
        self.search.setClearButtonEnabled(True); row.addWidget(self.search, 1)
        self.only_changed = QtWidgets.QCheckBox('只看不同'); self.only_changed.setChecked(True)
        row.addWidget(self.only_changed)
        self.table = QtWidgets.QTreeWidget()
        self.table.setRootIsDecorated(False); self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(self.table.SelectionMode.SingleSelection)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.setHeaderLabels(['参数', '当前值', '对比值', '位置'])
        self.table.setColumnWidth(0, 185); self.table.setColumnWidth(1, 180); self.table.setColumnWidth(2, 180)
        self.table.header().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(3, QtCore.Qt.SortOrder.AscendingOrder)
        root.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True); root.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.hide); root.addWidget(buttons)
        self.refresh_timer = QtCore.QTimer(self); self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh)
        self.layout_timer = QtCore.QTimer(self); self.layout_timer.setSingleShot(True)
        self.layout_timer.timeout.connect(self._prepare_locate)
        self.locate_timer = QtCore.QTimer(self); self.locate_timer.setSingleShot(True)
        self.locate_timer.timeout.connect(self._finish_locate)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.only_changed.toggled.connect(self.refresh)
        self.search.textChanged.connect(self.filter_rows)
        self.table.itemDoubleClicked.connect(self.locate)
        self.table.itemActivated.connect(self.locate)

    def queue_refresh(self):
        self.refresh_timer.start(0)

    def refresh(self, *_args):
        self.refresh_timer.stop()
        selected = self.table.currentItem()
        selected_key = selected.data(0, QtCore.Qt.ItemDataRole.UserRole) if selected else None
        scroll = self.table.verticalScrollBar().value()
        self.table.clear()
        layer = self.owner.layer
        self.layer_id = layer.get('id') if layer else None
        self.table.setEnabled(bool(layer))
        if not layer:
            self.pending = None; self.layout_timer.stop(); self.locate_timer.stop()
            self.title.setText('请选择一个字幕控件。'); self.filter_rows(); return
        combo = self.owner.controls['caption_preset']
        self.title.setText('控件：'+layer['name']+'　　预设：'+combo.currentText())
        mode = self.mode.currentData()
        left, right = comparison_layers(layer, mode)
        values, reference = {'layer': left, 'effect': effects(left)}, {'layer': right, 'effect': effects(right)}
        self.table.headerItem().setText(1, '预设原值' if mode == 'preset' else '当前值')
        self.table.headerItem().setText(2, '预设原值' if mode == 'overrides' else '基础白字')
        self.table.setSortingEnabled(False)
        for key, (control, label, path) in parameter_targets(self.owner).items():
            domain, field = key
            if field not in values[domain] or field not in reference[domain]: continue
            value, before = values[domain][field], reference[domain][field]
            if self.only_changed.isChecked() and same_value(field, value, before): continue
            item = QtWidgets.QTreeWidgetItem([label, display_value(control, value), display_value(control, before), path])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, key)
            for column in range(4): item.setToolTip(column, item.text(column))
            self.table.addTopLevelItem(item)
            if key == selected_key: self.table.setCurrentItem(item)
        self.table.setSortingEnabled(True)
        self.table.verticalScrollBar().setValue(scroll)
        self.filter_rows()

    def filter_rows(self, *_args):
        query = self.search.text().strip().casefold()
        visible = 0
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            show = not query or query in ' '.join(item.text(i) for i in range(4)).casefold()
            item.setHidden(not show); visible += int(show)
        self.status.setText(f'显示 {visible} 项。双击一项即可定位。' if visible else '没有匹配项；可清空搜索或取消“只看不同”。')

    def locate(self, item, _column=0):
        layer = self.owner.layer
        if not layer or layer.get('id') != self.layer_id:
            self.refresh(); return
        key = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        target = parameter_targets(self.owner).get(key)
        if target is None: self.refresh(); return
        control = target[0]
        parent = control.parentWidget()
        while parent is not None:
            if isinstance(parent, PropertySection): parent.set_expanded(True)
            parent = parent.parentWidget()
        self.pending = (self.layer_id, key)
        self.hide()
        self.owner.window().raise_(); self.owner.window().activateWindow()
        self.layout_timer.start(0)

    def _prepare_locate(self):
        if not self.pending or not self.owner.layer or self.owner.layer.get('id') != self.pending[0]: return
        target = parameter_targets(self.owner).get(self.pending[1])
        if target is None: return
        parent = target[0].parentWidget()
        while parent is not None:
            if parent.layout() is not None: parent.layout().activate()
            parent = parent.parentWidget()
        # Let the scroll area's queued size/range update follow the opened
        # layouts before asking it to reveal a deeply nested parameter.
        self.locate_timer.start(0)

    def _finish_locate(self):
        pending, self.pending = self.pending, None
        if not pending or not self.owner.layer or self.owner.layer.get('id') != pending[0]: return
        target = parameter_targets(self.owner).get(pending[1])
        if target is None: return
        control = target[0]
        parent = control.parentWidget()
        while parent is not None:
            if parent.layout() is not None: parent.layout().activate()
            if isinstance(parent, QtWidgets.QScrollArea): parent.ensureWidgetVisible(control, 20, 40)
            parent = parent.parentWidget()
        focus = (control.buttons[0] if isinstance(control, EffectColorEditor) and control.buttons else control)
        focus.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        if self.flash is not None:
            try: self.flash.stop()
            except RuntimeError: pass  # The previous control may have been destroyed with its panel.
        self.flash = ParameterFlash(control)

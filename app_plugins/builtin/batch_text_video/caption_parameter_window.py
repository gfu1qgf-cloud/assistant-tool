"""Parameter comparisons, validated current-value editing and navigation."""
import copy
import math

from qt_compat import QtCore, QtGui, QtWidgets
from .caption_styles import apply_style, effects, COLOR_KEYS, colors
from .caption_effect_ui import EFFECT_LABELS
from .color_ui import EffectColorEditor
from .property_sections import PropertySection
from .caption_parameter_values import VALUE_ROLE, SEARCH_ROLE, FILTERS, describe, decorate, swatch, searchable
from .caption_parameter_edit import ValueDelegate, OverviewUndoKeys, editable, stale, history_host

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


def display_value(control, value, key='', label=''):
    return describe(control, value, key, label)['text']


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
        self._committing=False;self._refresh_after_edit=False
        self.setWindowTitle('字幕预设 · 参数一览')
        self.setModal(False)
        self.resize(1180, 690)
        root = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel(); self.title.setWordWrap(True)
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        heading_font=QtGui.QFont(self.font());heading_font.setPointSizeF(heading_font.pointSizeF()+2);heading_font.setBold(True)
        self.title.setFont(heading_font);root.addWidget(self.title)
        self.note = QtWidgets.QLabel('双击当前值直接修改；双击参数名或位置可定位并闪烁4次。回车应用，Esc取消输入；对比值和预设原值只读。')
        self.note.setWordWrap(True); root.addWidget(self.note)
        row = QtWidgets.QHBoxLayout(); root.addLayout(row)
        self.mode = QtWidgets.QComboBox()
        for title, key in (('当前参数 对比 基础白字', 'current'), ('预设原值 对比 基础白字', 'preset'),
                           ('当前参数 对比 预设原值', 'overrides')):
            self.mode.addItem(title, key)
        row.addWidget(self.mode)
        self.search = QtWidgets.QLineEdit(); self.search.setPlaceholderText('搜索参数、位置、颜色或 HEX…')
        self.search.setClearButtonEnabled(True); row.addWidget(self.search, 1)
        self.value_type=QtWidgets.QComboBox()
        for title,key in FILTERS:self.value_type.addItem(title,key)
        row.addWidget(self.value_type)
        self.only_changed = QtWidgets.QCheckBox('只看不同'); self.only_changed.setChecked(True)
        row.addWidget(self.only_changed)
        self.table = QtWidgets.QTreeWidget()
        self.table.setRootIsDecorated(False); self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(self.table.SelectionMode.SingleSelection)
        self.table.setEditTriggers(self.table.EditTrigger.DoubleClicked|self.table.EditTrigger.EditKeyPressed)
        self.table.setHeaderLabels(['参数', '当前值', '对比值', '位置'])
        self.table.setColumnWidth(0, 170); self.table.setColumnWidth(1, 230); self.table.setColumnWidth(2, 220)
        self.table.setIconSize(QtCore.QSize(84,20))
        self.table.setUniformRowHeights(True)
        self.table.header().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(3, QtCore.Qt.SortOrder.AscendingOrder)
        self.splitter=QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.table)
        self.details=QtWidgets.QWidget();self.details.setMinimumWidth(240)
        detail=QtWidgets.QVBoxLayout(self.details);detail.setContentsMargins(16,8,8,8)
        self.detail_title=QtWidgets.QLabel('选择一项参数');self.detail_title.setWordWrap(True)
        self.detail_title.setTextFormat(QtCore.Qt.TextFormat.PlainText);self.detail_title.setFont(heading_font)
        detail.addWidget(self.detail_title)
        self.detail_kind=QtWidgets.QLabel();detail.addWidget(self.detail_kind)
        self.detail_values=[]
        for title in ('当前值','对比值'):
            frame=QtWidgets.QFrame();frame.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            card=QtWidgets.QVBoxLayout(frame)
            caption=QtWidgets.QLabel(title);card.addWidget(caption)
            preview=QtWidgets.QLabel();card.addWidget(preview)
            value=QtWidgets.QLabel();value.setWordWrap(True);value.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            card.addWidget(value);detail.addWidget(frame)
            self.detail_values.append((caption,preview,value))
        detail.addWidget(QtWidgets.QLabel('参数位置'))
        self.detail_path=QtWidgets.QLabel();self.detail_path.setWordWrap(True)
        self.detail_path.setTextFormat(QtCore.Qt.TextFormat.PlainText);detail.addWidget(self.detail_path)
        self.edit_button=QtWidgets.QPushButton('修改当前值');self.edit_button.setAutoDefault(False)
        self.edit_button.clicked.connect(self.edit_current);detail.addWidget(self.edit_button)
        undo_row=QtWidgets.QHBoxLayout()
        self.undo_button=QtWidgets.QPushButton('撤销');self.redo_button=QtWidgets.QPushButton('重做')
        for button,redo in ((self.undo_button,False),(self.redo_button,True)):
            button.setAutoDefault(False);button.clicked.connect(lambda _checked=False,redo=redo:self.move_history(redo));undo_row.addWidget(button)
        detail.addLayout(undo_row)
        self.locate_button=QtWidgets.QPushButton('定位到这个参数');self.locate_button.setAutoDefault(False);self.locate_button.setEnabled(False)
        self.locate_button.clicked.connect(lambda:self.locate(self.table.currentItem()) if self.table.currentItem() else None)
        detail.addWidget(self.locate_button);detail.addStretch()
        self.splitter.addWidget(self.details);self.splitter.setSizes([840,290])
        self.splitter.setStretchFactor(0,1);self.splitter.setStretchFactor(1,0)
        root.addWidget(self.splitter,1)
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True); root.addWidget(self.status)
        self.edit_error=QtWidgets.QLabel();self.edit_error.setWordWrap(True)
        self.edit_error.setStyleSheet('color:#b91c1c;');self.edit_error.hide();root.addWidget(self.edit_error)
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
        self.value_type.currentIndexChanged.connect(self.filter_rows)
        self.table.currentItemChanged.connect(self.show_detail)
        self.undo_filter=OverviewUndoKeys(self)
        self.delegate=ValueDelegate(self);self.table.setItemDelegate(self.delegate)
        for child in [self,*self.findChildren(QtWidgets.QWidget)]:child.installEventFilter(self.undo_filter)
        self.table.itemDoubleClicked.connect(self.activate_item)
        self.table.itemActivated.connect(self.activate_item)

    def show_edit_error(self,message):
        self.edit_error.setText('未应用：'+message);self.edit_error.show()

    def activate_item(self,item,column=0):
        if column==1:self.edit_current()
        else:self.locate(item,column)

    def edit_current(self):
        item=self.table.currentItem()
        if item is None or item.isHidden() or self.delegate.editor is not None:return
        key=item.data(0,QtCore.Qt.ItemDataRole.UserRole)
        allowed,reason=editable(self,key)
        if not allowed:self.show_edit_error(reason);return
        self.edit_error.hide();self.table.setCurrentItem(item,1);self.table.editItem(item,1)

    def move_history(self,redo=False):
        self.delegate.cancel()
        host=history_host(self)
        if host is not None:
            (host.redo_edit if redo else host.undo_edit)()
            self.queue_refresh()

    def hideEvent(self,event):
        self.delegate.cancel()
        super().hideEvent(event)

    def queue_refresh(self):
        editor=self.delegate.editor
        if editor is not None and not self._committing:
            if stale(self,editor._parameter_token):
                self.delegate.cancel();self.show_edit_error('控件、任务或原参数已变化，未提交的输入已取消。')
            else:
                self._refresh_after_edit=True;return
        self.refresh_timer.start(0)

    def refresh(self, *_args):
        self.refresh_timer.stop()
        self.delegate.cancel()
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
        self.table.headerItem().setText(1, '预设原值（只读）' if mode == 'preset' else '当前值（可修改）')
        self.table.headerItem().setText(2, '预设原值（只读）' if mode == 'overrides' else '基础白字（只读）')
        self.table.setSortingEnabled(False)
        for key, (control, label, path) in parameter_targets(self.owner).items():
            domain, field = key
            if field not in values[domain] or field not in reference[domain]: continue
            value, before = values[domain][field], reference[domain][field]
            color_path='caption_effects/'+field if domain=='effect' else field
            random_rule=layer.get('color_rules',{}).get(color_path) if mode!='preset' else None
            if self.only_changed.isChecked() and same_value(field, value, before) and not random_rule: continue
            current=describe(control,value,field,label);reference_info=describe(control,before,field,label)
            if random_rule:
                current['random']=True
                current['text']=('套餐随机 · '+random_rule.get('name','自选颜色') if random_rule['mode']=='palette' else '同色系随机')
                if random_rule['mode']=='palette':
                    current['colors']=random_rule['colors'];current['gradient']=False
            changed=bool(random_rule) or not same_value(field,value,before)
            item = QtWidgets.QTreeWidgetItem([label,current['text'],reference_info['text'],path])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, key)
            if mode!='preset':item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsEditable)
            item.setData(0,SEARCH_ROLE,field+' '+searchable(current)+' '+searchable(reference_info))
            item.setSizeHint(0,QtCore.QSize(0,max(34,self.fontMetrics().height()+12)))
            for column in (0,3):item.setToolTip(column,item.text(column))
            decorate(item,1,current,self.palette(),changed)
            decorate(item,2,reference_info,self.palette())
            self.table.addTopLevelItem(item)
            if key == selected_key: self.table.setCurrentItem(item)
        self.table.setSortingEnabled(True)
        self.table.verticalScrollBar().setValue(scroll)
        self.filter_rows()
        if self.table.currentItem() is None:
            first=next((self.table.topLevelItem(i) for i in range(self.table.topLevelItemCount())
                        if not self.table.topLevelItem(i).isHidden()),None)
            self.table.setCurrentItem(first)
        self.show_detail(self.table.currentItem())

    def show_detail(self,item=None,*_args):
        host=history_host(self)
        for button,name in ((self.undo_button,'undo_action'),(self.redo_button,'redo_action')):
            action=getattr(host,name,None) if host else None
            button.setEnabled(bool(action and action.isEnabled() and not host.is_batch_busy()))
        allowed,reason=editable(self,item.data(0,QtCore.Qt.ItemDataRole.UserRole)) if item is not None else (False,'请选择参数。')
        self.edit_button.setEnabled(allowed and not item.isHidden() if item is not None else False)
        self.edit_button.setToolTip('双击当前值也可以编辑；回车应用、Esc取消。' if allowed else reason)
        self.locate_button.setEnabled(item is not None and not item.isHidden())
        if item is None:
            self.detail_title.setText('选择一项参数');self.detail_kind.clear();self.detail_path.clear()
            for _caption,preview,value in self.detail_values:preview.clear();preview.hide();value.clear()
            return
        self.detail_title.setText(item.text(0));self.detail_path.setText(item.text(3))
        info=item.data(1,VALUE_ROLE) or {}
        kinds={key:title for title,key in FILTERS}
        different=not same_value(item.data(0,QtCore.Qt.ItemDataRole.UserRole)[1],
                                 info.get('raw'),(item.data(2,VALUE_ROLE) or {}).get('raw'))
        different=different or info.get('random',False)
        self.detail_kind.setText(kinds.get(info.get('kind'),'参数')+(' · 与对比值不同' if different else ' · 与对比值相同'))
        for column,(caption,preview,value) in zip((1,2),self.detail_values):
            caption.setText(self.table.headerItem().text(column))
            data=item.data(column,VALUE_ROLE) or {'text':item.text(column),'kind':'text'}
            value.setText(data['text'])
            if data['kind'] in {'color','bool'}:
                preview.setPixmap(swatch(data,self.palette(),width=220,height=30));preview.show()
            else:preview.clear();preview.hide()
            if data.get('colors'):
                value.setText(data['text']+'\n'+'\n'.join(data['colors']))

    def filter_rows(self, *_args):
        query = self.search.text().strip().casefold()
        visible = 0
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            text=' '.join(item.text(i) for i in range(4))+' '+str(item.data(0,SEARCH_ROLE) or '')
            kind=(item.data(1,VALUE_ROLE) or {}).get('kind','text')
            show = (not query or query in text.casefold()) and (self.value_type.currentData() in ('all',kind))
            item.setHidden(not show); visible += int(show)
        self.status.setText(f'显示 {visible} / {self.table.topLevelItemCount()} 项 · 双击当前值修改，双击参数名定位。' if visible else '没有匹配项；可清空搜索、切换类型或取消“只看不同”。')
        current=self.table.currentItem()
        if current is None or current.isHidden():
            first=next((self.table.topLevelItem(i) for i in range(self.table.topLevelItemCount()) if not self.table.topLevelItem(i).isHidden()),None)
            self.table.setCurrentItem(first)
        self.show_detail(self.table.currentItem())

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
        if self.isVisible(): self.raise_()

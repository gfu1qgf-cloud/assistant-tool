"""Validated overview edits through the existing ID-bound inspector signal."""
import copy
from qt_compat import QtCore, QtGui, QtWidgets
from .component_ui import Spin, Combo, FontCombo, WeightCombo
from .color_ui import EffectColorEditor
from .caption_parameter_values import VALUE_ROLE, describe


def history_host(window):
    parent=window.owner
    while parent is not None:
        if all(callable(getattr(parent,name,None)) for name in
               ('flush_editor_history','capture_editor_change','undo_edit','redo_edit','is_batch_busy')):
            return parent
        parent=parent.parentWidget()
    return None


def context(window):
    host=history_host(window)
    return (id(window.owner.parameter_panel),host.state.get('active_profile'),host._editor_context()) if host else (id(window.owner.parameter_panel),)


def raw_value(window,key):
    from .caption_styles import effects
    layer=window.owner.layer
    return effects(layer)[key[1]] if key[0]=='effect' else layer[key[1]]


def editable(window,key):
    from .caption_parameter_window import parameter_targets
    if window.mode.currentData()=='preset':return False,'预设原值仅供比较；请切换到当前参数模式编辑。'
    if not window.owner.layer:return False,'请先选择字幕控件。'
    target=parameter_targets(window.owner).get(key)
    if target is None:return False,'此参数已不在当前控件中。'
    panel=window.owner.parameter_panel
    host=history_host(window)
    if host and host.is_batch_busy():return False,'正在渲染，暂不能修改字幕参数。'
    if window.owner.loading or panel is not None and panel.loading:return False,'控件正在更新，请稍后再试。'
    if not window.owner.isEnabled() or not target[0].isEnabled():return False,'此参数当前不可编辑，请先在原属性中启用。'
    return True,''


def token_for(window,key):
    return dict(layer=window.owner.layer['id'],context=copy.deepcopy(context(window)),
                key=tuple(key),before=copy.deepcopy(raw_value(window,key)))


def stale(window,token):
    from .caption_parameter_window import same_value
    return (not window.owner.layer or window.owner.layer['id']!=token['layer']
            or context(window)!=token['context']
            or not same_value(token['key'][1],raw_value(window,token['key']),token['before']))


_UNCHANGED=object()

def apply_value(window,token,value,rule=_UNCHANGED):
    from .caption_parameter_window import same_value
    from .layers import normalize_layers
    if stale(window,token):raise ValueError('控件、任务或原参数已变化，本次输入未应用。')
    allowed,reason=editable(window,token['key'])
    if not allowed:raise ValueError(reason)
    domain,key=token['key']
    path='caption_effects/'+key if domain=='effect' else key
    rule_changed=rule is not _UNCHANGED and window.owner.layer.get('color_rules',{}).get(path)!=rule
    if same_value(key,value,token['before']) and not rule_changed:return False
    candidate=copy.deepcopy(window.owner.layer)
    if domain=='effect':
        candidate['caption_effects']={**candidate.get('caption_effects',{}),key:value}
        fields=('caption_effects',)
    else:
        candidate[key]=value
        fields=(key,)
        if key in {'width','height'}:
            # Match the original geometry editor: resizing a side-anchored box
            # preserves that edge's distance, even in pixel display mode.
            from .geometry_anchors import anchor,center,distance
            axis='x' if key=='width' else 'y'
            if anchor(candidate,axis)!='center':
                candidate[axis]=center(candidate,axis,distance(window.owner.layer,axis))
                fields=(key,axis)
    if rule is not _UNCHANGED:
        from .palette_rules import set_rule
        set_rule(candidate,path,rule);fields=(*fields,'color_rules')
        if path=='color':candidate['color_mode']='fixed';fields=(*fields,'color_mode')
    elif domain=='layer' and key=='color_mode' and 'color' in candidate.get('color_rules',{}):
        from .palette_rules import set_rule
        set_rule(candidate,'color',None);fields=(*fields,'color_rules')
    candidate=normalize_layers([candidate])[0]
    patch={field:copy.deepcopy(candidate[field]) for field in fields}
    host=history_host(window)
    if host and not host.flush_editor_history():raise ValueError('请先完成其他参数输入，再修改这一项。')
    if stale(window,token):raise ValueError('原参数已变化，本次输入未应用。')
    window._committing=True
    try:
        panel=window.owner.parameter_panel
        if panel is not None:
            panel.edited.emit(token['layer'],patch)
        else:
            window.owner.set_layer(candidate)
            window.owner.changed.emit(patch)
        if not same_value(key,raw_value(window,token['key']),candidate['caption_effects'][key] if domain=='effect' else candidate[key]):
            raise ValueError('原控件未接受本次修改，请检查当前编辑状态。')
        if host:host.capture_editor_change()
    finally:window._committing=False
    window.queue_refresh()
    return True


class ColorDialog(QtWidgets.QDialog):
    def __init__(self,value,multiple,parent):
        super().__init__(parent)
        self.setWindowTitle('修改当前颜色／色板');self.resize(390,300)
        root=QtWidgets.QVBoxLayout(self)
        self.colors=EffectColorEditor(value);root.addWidget(self.colors)
        self.colors.add_button.setVisible(multiple)
        self.was_list=isinstance(value,list)
        self.error=QtWidgets.QLabel();self.error.setWordWrap(True);root.addWidget(self.error)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);root.addWidget(buttons)

    def accept(self):
        from .caption_styles import colors
        try:colors(self.colors.text())
        except ValueError as error:self.error.setText(str(error));return
        super().accept()

    def value(self):
        from .caption_styles import colors
        return colors(self.colors.text()) if self.was_list else self.colors.text()


class OverviewUndoKeys(QtCore.QObject):
    def eventFilter(self,watched,event):
        if event.type() not in (QtCore.QEvent.Type.KeyPress,QtCore.QEvent.Type.ShortcutOverride):return False
        if not event.modifiers()&QtCore.Qt.KeyboardModifier.ControlModifier or event.modifiers()&QtCore.Qt.KeyboardModifier.AltModifier:return False
        if event.key() not in (QtCore.Qt.Key.Key_Z,QtCore.Qt.Key.Key_Y):return False
        redo=event.key()==QtCore.Qt.Key.Key_Y or bool(event.modifiers()&QtCore.Qt.KeyboardModifier.ShiftModifier)
        if isinstance(watched,QtWidgets.QLineEdit) and (watched.isRedoAvailable() if redo else watched.isUndoAvailable()):return False
        window=self.parent()
        if history_host(window) is None:return False
        event.accept()
        if event.type()==QtCore.QEvent.Type.KeyPress:window.move_history(redo)
        return True


class ValueDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self,window):
        super().__init__(window.table);self.window=window;self.editor=None;self.color_dialog=None

    def cancel(self):
        if self.color_dialog is not None:self.color_dialog.reject()
        editor,self.editor=self.editor,None
        if editor is not None:
            editor._parameter_discard=True
            self.closeEditor.emit(editor,QtWidgets.QAbstractItemDelegate.EndEditHint.RevertModelCache)

    def createEditor(self,parent,option,index):
        if index.column()!=1:return None
        key=index.siblingAtColumn(0).data(QtCore.Qt.ItemDataRole.UserRole)
        allowed,reason=editable(self.window,key)
        if not allowed:self.window.show_edit_error(reason);return None
        from .caption_parameter_window import parameter_targets
        source,label,_path=parameter_targets(self.window.owner)[tuple(key)]
        value=raw_value(self.window,key);info=describe(source,value,key[1],label)
        if info['kind']=='color':
            editor=QtWidgets.QPushButton('修改颜色／色板…',parent);editor.setAutoDefault(False)
            editor.clicked.connect(lambda:self.choose_color(editor,source))
        elif isinstance(source,QtWidgets.QFontComboBox):
            editor=FontCombo(parent);editor.setCurrentFont(QtGui.QFont(value))
        elif isinstance(source,WeightCombo):
            editor=WeightCombo(parent)
            for i in range(source.count()):editor.addItem(source.itemText(i),source.itemData(i))
            editor.setValue(value)
        elif info['kind'] in {'choice','bool'}:
            editor=Combo(parent)
            choices=[('开启',True),('关闭',False)] if info['kind']=='bool' else [(source.itemText(i),source.itemData(i)) for i in range(source.count())]
            for text,data in choices:editor.addItem(text,data)
            editor.setCurrentIndex(editor.findData(value))
            editor.activated.connect(lambda _i:self.finish(editor))
        elif info['kind']=='number':
            editor=Spin(parent);editor.setKeyboardTracking(False)
            if key[0]=='layer' and key[1] in {'x','y','width','height'}:
                editor.setRange(1 if key[1] in {'width','height'} else 0,100);editor.setDecimals(3)
            else:
                editor.setRange(source.minimum(),source.maximum())
                editor.setDecimals(source.decimals() if hasattr(source,'decimals') else 0)
            if type(value) is float:
                fraction=format(value,'.8f').rstrip('0').partition('.')[2]
                editor.setDecimals(max(editor.decimals(),len(fraction)))
            editor.setSingleStep(source.singleStep());editor.setSuffix(' '+info.get('unit','') if info.get('unit') else '')
            editor.setValue(value)
        else:
            editor=QtWidgets.QLineEdit(parent);editor.setText(str(value));editor.setMaxLength(500)
        editor._parameter_token=token_for(self.window,key)
        editor._parameter_kind=info['kind'];editor._parameter_discard=False;editor._parameter_done=False
        editor._parameter_dirty=False
        def dirty(*_args):editor._parameter_dirty=True
        if isinstance(editor,QtWidgets.QFontComboBox):editor.currentFontChanged.connect(dirty)
        elif isinstance(editor,QtWidgets.QComboBox):
            editor.currentIndexChanged.connect(dirty)
            if editor.isEditable():editor.editTextChanged.connect(dirty)
        elif isinstance(editor,QtWidgets.QAbstractSpinBox):
            editor.valueChanged.connect(dirty);editor.lineEdit().textEdited.connect(dirty)
        elif isinstance(editor,QtWidgets.QLineEdit):editor.textChanged.connect(dirty)
        self.editor=editor
        for child in [editor,*editor.findChildren(QtWidgets.QWidget)]:
            child.installEventFilter(self);child.installEventFilter(self.window.undo_filter)
        if info['kind']=='color':QtCore.QTimer.singleShot(0,lambda:self.choose_color(editor,source) if self.editor is editor else None)
        return editor

    def setEditorData(self,editor,index):
        if isinstance(editor,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox)):editor.selectAll()

    def setModelData(self,editor,model,index):
        if editor._parameter_discard or editor._parameter_done:return
        if editor._parameter_kind=='color' and not hasattr(editor,'_parameter_value'):return
        editor._parameter_done=True
        if not editor._parameter_dirty:return
        try:
            if editor._parameter_kind=='color':value=editor._parameter_value
            elif isinstance(editor,QtWidgets.QFontComboBox):value=editor.currentFont().family()
            elif isinstance(editor,QtWidgets.QComboBox):value=editor.currentData()
            elif isinstance(editor,QtWidgets.QAbstractSpinBox):
                if not editor.hasAcceptableInput():raise ValueError('请输入允许范围内的有效数值。')
                editor.interpretText();value=editor.value()
                if type(editor._parameter_token['before']) is int and editor.decimals()==0:value=int(value)
            else:value=editor.text().strip()
            if value is None:raise ValueError('请输入有效的当前值。')
            apply_value(self.window,editor._parameter_token,value,getattr(editor,'_parameter_rule',_UNCHANGED))
            self.window.edit_error.hide()
        except (ValueError,TypeError,KeyError,RuntimeError) as error:self.window.show_edit_error(str(error))

    def destroyEditor(self,editor,index):
        if self.editor is editor:self.editor=None
        super().destroyEditor(editor,index)
        if self.window._refresh_after_edit:
            self.window._refresh_after_edit=False;self.window.refresh_timer.start(0)

    def finish(self,editor):
        if self.editor is not editor:return
        self.commitData.emit(editor)
        self.closeEditor.emit(editor,QtWidgets.QAbstractItemDelegate.EndEditHint.NoHint)

    def choose_color(self,editor,source):
        if self.editor is not editor or getattr(editor,'_choosing',False):return
        editor._choosing=True
        dialog=ColorDialog(editor._parameter_token['before'],isinstance(source,EffectColorEditor) and editor._parameter_token['key'][1]!='caption_cursor_color',self.window)
        self.color_dialog=dialog
        domain,key=editor._parameter_token['key']
        path='caption_effects/'+key if domain=='effect' else key
        dialog.colors.bind_choice(self.window.owner.layer,path)
        try:
            if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
                editor._parameter_rule=copy.deepcopy(dialog.colors.choice_rule)
                editor._parameter_value=dialog.value();editor._parameter_dirty=True;self.finish(editor)
            else:self.cancel()
        finally:
            self.color_dialog=None;dialog.deleteLater()

    def eventFilter(self,watched,event):
        editor=self.editor
        if editor is not None and event.type()==QtCore.QEvent.Type.KeyPress:
            if event.key()==QtCore.Qt.Key.Key_Escape:self.cancel();return True
            if event.key() in (QtCore.Qt.Key.Key_Return,QtCore.Qt.Key.Key_Enter):self.finish(editor);return True
        if editor is not None and editor._parameter_kind=='color' and event.type()==QtCore.QEvent.Type.FocusOut:return False
        return super().eventFilter(watched,event) if watched is editor else False

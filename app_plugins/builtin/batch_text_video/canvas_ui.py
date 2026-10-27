"""One canvas dialog reached from Settings and the project inspector."""
import copy
from qt_compat import QtCore, QtWidgets
from .canvas_settings import CHOICES, KEYS, normalized, summary
from .component_ui import Combo
from pathlib import Path


class CanvasDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from .numeric_wheel import protect_numeric_wheels
        protect_numeric_wheels(self)
        self.setWindowTitle('画布比例与尺寸')
        self.setMinimumWidth(440)
        values = normalized(settings)
        self.saved_custom = values
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel('用于当前项目；预览和最终导出使用相同画布。')
        note.setWordWrap(True); layout.addWidget(note)
        form = QtWidgets.QFormLayout(); layout.addLayout(form)
        self.aspect = Combo()
        for title, key in CHOICES: self.aspect.addItem(title, key)
        self.aspect.setCurrentIndex(self.aspect.findData(values['aspect']))
        form.addRow('比例', self.aspect)
        self.custom_group = QtWidgets.QGroupBox('自定义尺寸（像素）')
        custom = QtWidgets.QFormLayout(self.custom_group)
        self.width_spin, self.height_spin = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
        for control, key, label in ((self.width_spin, 'canvas_width', '宽度'), (self.height_spin, 'canvas_height', '高度')):
            control.setRange(64, 8192); control.setSingleStep(2); control.setSuffix(' px')
            control.setValue(values[key]); control.setAccessibleName(label)
            control.valueChanged.connect(self.refresh)
            custom.addRow(label, control)
        self.swap_button = QtWidgets.QPushButton('交换宽高')
        self.swap_button.setAutoDefault(False); self.swap_button.clicked.connect(self.swap)
        custom.addRow(self.swap_button); layout.addWidget(self.custom_group)
        self.description = QtWidgets.QLabel(); self.description.setWordWrap(True)
        self.description.setMinimumHeight(42); layout.addWidget(self.description)
        self.reference_label = QtWidgets.QLabel()
        self.reference_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.reference_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reference_label.setWordWrap(True); layout.addWidget(self.reference_label)
        self.reference_timer = QtCore.QTimer(self)
        self.reference_timer.setInterval(300)
        self.reference_timer.timeout.connect(self.refresh_reference)
        hint = QtWidgets.QLabel('更换比例后，请检查文字和图片布局；图层的相对位置与动画参数会保留。')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('应用')
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.aspect.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def values(self):
        return normalized(dict(aspect=self.aspect.currentData(),
            canvas_width=self.width_spin.value() if self.aspect.currentData() == 'custom' else self.saved_custom['canvas_width'],
            canvas_height=self.height_spin.value() if self.aspect.currentData() == 'custom' else self.saved_custom['canvas_height']))

    def refresh(self, *_):
        # During construction both spin boxes must exist before validation.
        if not hasattr(self, 'buttons'): return
        self.custom_group.setVisible(self.aspect.currentData() == 'custom')
        self.reference_label.setVisible(self.aspect.currentData() == 'source')
        if self.aspect.currentData() == 'source':
            self.reference_timer.start(); self.refresh_reference()
        else: self.reference_timer.stop()
        try:
            text = summary(self.values()); valid = True
            if self.aspect.currentData() == 'source': text += '；以底层背景素材为准。'
        except ValueError as error: text, valid = str(error), False
        self.description.setText(text)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def refresh_reference(self):
        parent = self.parentWidget()
        text = reference_text(parent) if parent is not None and hasattr(parent, 'active_entry') else (
            '参考对象：每条任务实际分配的底层背景视频或图片。\n无底层背景时使用1080×1920。叠加图层不参与尺寸取值。')
        self.reference_label.setText(text)

    def swap(self):
        width, height = self.width_spin.value(), self.height_spin.value()
        self.width_spin.setValue(height); self.height_spin.setValue(width)

    def accept(self):
        try: self.values()
        except ValueError: self.refresh(); return
        super().accept()


def install_controls(editor, form):
    values = normalized(editor.state['settings'])
    combo = Combo(editor)
    for title, key in CHOICES: combo.addItem(title, key)
    combo.setCurrentIndex(combo.findData(values['aspect'])); combo.hide()
    editor.controls['aspect'] = combo
    for key in ('canvas_width', 'canvas_height'):
        control = QtWidgets.QSpinBox(editor); control.setRange(64, 8192); control.setSingleStep(2)
        control.setValue(values[key]); control.hide(); editor.controls[key] = control
    editor.canvas_edit_button = QtWidgets.QPushButton()
    editor.canvas_edit_button.setAutoDefault(False)
    editor.canvas_edit_button.setToolTip('设置 → 画布比例与尺寸；常用比例、原素材规格及自定义宽高。')
    editor.canvas_edit_button.clicked.connect(lambda: edit_canvas(editor))
    form.addRow('画布', editor.canvas_edit_button)
    refresh_summary(editor)


def refresh_summary(editor):
    if hasattr(editor, 'canvas_edit_button'):
        values = {key: (editor.controls[key].currentData() if key == 'aspect' else editor.controls[key].value()) for key in KEYS}
        editor.canvas_edit_button.setText(summary(values)+'　设置…')


def reference_text(editor):
    """Describe the same bottom source as the preview, without probing on UI."""
    from .canvas_settings import output_size
    job = editor.active_entry()
    path = job.get('path', '')
    rule = '参考对象：每条任务实际分配的底层背景视频或图片；叠加图层不参与尺寸取值。'
    batch = '批量导出以各条任务实际分配的背景为准。背景尺寸不同，成品尺寸也会不同；统一尺寸请选择固定比例或自定义。'
    if not path: return rule+'\n当前预览无底层背景，使用默认1080×1920。\n'+batch
    request, media = getattr(editor, '_preview_path', None), getattr(editor, '_media', None)
    name = Path(path).name
    error = getattr(editor, '_background_error', '')
    if error: return rule+f'\n当前预览参考：{name}\n尺寸暂不可用：{error}\n'+batch
    if request and request[0] == path and media and media.get('video'):
        try:
            width, height = output_size({'aspect': 'source'}, media)
            spec = f'当前预览参考：{name}\n画布尺寸：{width}×{height}（计入旋转方向；奇数尺寸向下取偶数）'
        except (ValueError, TypeError, KeyError):
            spec = f'当前预览参考：{name}\n尺寸信息无效，请重新读取背景素材。'
    else:
        spec = f'当前预览参考：{name}\n尺寸尚未读取完成；不会改用其他素材的尺寸。'
    return rule+'\n'+spec+'\n路径：'+path+'\n'+batch


def apply_settings(editor, values):
    if editor.is_batch_busy():
        editor.status.setText('当前项目正在处理任务，请在结束后修改画布。'); return False
    values = normalized(values)
    if not editor.flush_editor_history(): return False
    if values == {key: editor.read_settings()[key] for key in KEYS}: return True
    # Publish before changing widgets/history; a revision conflict leaves the
    # current canvas and undo pointer untouched.
    target = copy.deepcopy(editor.state)
    target['settings'] = {**editor.read_settings(), **values}
    try: saved = editor.store.save(target, target['revision'])
    except Exception as error:
        editor.status.setText('画布未修改：'+str(error)); return False
    editor.state = saved
    for key in KEYS:
        control = editor.controls[key]
        with QtCore.QSignalBlocker(control):
            if key == 'aspect': control.setCurrentIndex(control.findData(values[key]))
            else: control.setValue(values[key])
    editor.capture_editor_change()
    editor.invalidate_playback()
    refresh_summary(editor)
    editor.update_preview()
    editor.request_background(editor.active_entry().get('path', ''))
    editor.status.setText('画布已保存：'+summary(values)+'；请检查文字和图片布局。')
    return True


def edit_canvas(editor):
    if editor.is_batch_busy():
        editor.status.setText('当前项目正在处理任务，请在结束后修改画布。'); return
    dialog = CanvasDialog(editor.read_settings(), editor)
    if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        apply_settings(editor, dialog.values())
    dialog.deleteLater()

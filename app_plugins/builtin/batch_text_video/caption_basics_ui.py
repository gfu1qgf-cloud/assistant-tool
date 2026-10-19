"""Caption-specific basic section composition, without another property store."""
import copy
from qt_compat import QtCore, QtWidgets


class CaptionIdentityEditor(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.layer, self.loading = None, False
        form = QtWidgets.QFormLayout(self); form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name = QtWidgets.QLineEdit(); self.name.setMaxLength(120)
        self.name.editingFinished.connect(self.edit); form.addRow('控件名称', self.name)
        self.opacity = QtWidgets.QDoubleSpinBox(); self.opacity.setRange(0, 100)
        self.opacity.setDecimals(1); self.opacity.setSuffix(' %'); self.opacity.setKeyboardTracking(False)
        self.opacity.valueChanged.connect(self.edit); form.addRow('不透明度', self.opacity)
        from .property_pairs import pair_form_fields
        self.identity_pair = pair_form_fields(form, '控件', [
            ('name', '名称', self.name), ('opacity', '透明度', self.opacity)])

    def set_layer(self, layer):
        self.loading = True
        try:
            self.layer = copy.deepcopy(layer)
            self.setEnabled(bool(layer))
            self.name.setText(layer['name'] if layer else '')
            self.opacity.setValue(layer['opacity'] if layer else 100)
        finally: self.loading = False

    def edit(self, *_args):
        if self.loading or not self.layer: return
        name = self.name.text().strip()
        if not name: return
        patch = {'name': name, 'opacity': self.opacity.value()}
        self.layer.update(patch); self.changed.emit(patch)


def compose_basics(panel):
    from .inspector_layout_contract import require_flat_layout
    require_flat_layout(panel)
    identity = panel.add_bound('caption_identity', '', CaptionIdentityEditor(), fields={'name','opacity'}, section=False)
    panel.caption_identity = identity
    content = QtWidgets.QWidget(); body = QtWidgets.QVBoxLayout(content)
    body.setContentsMargins(3, 3, 3, 3); body.setSpacing(7)
    basic = panel.add_section('basic', '基础设置', content, expanded=True)
    for widget in (identity, panel.caption_properties, panel.geometry_editor):
        panel.body.removeWidget(widget)
    body.addWidget(identity)
    body.addWidget(panel.geometry_editor)
    panel.geometry_editor.show()
    body.addWidget(panel.caption_properties)
    panel.targets['timing']=panel.caption_properties.basic_widget
    panel.sections['caption_effects']=panel.caption_properties.effect_section
    panel.body.removeWidget(basic)
    panel.body.insertWidget(1 if hasattr(panel,'content_editor') else 0,basic)
    panel.sections['font'].set_expanded(False)

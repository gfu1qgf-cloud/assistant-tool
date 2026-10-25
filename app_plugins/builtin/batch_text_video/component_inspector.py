"""Lazy component-owned property UIs. The layer model alone owns committed values."""
import copy
import importlib
import sys
from qt_compat import QtCore, QtWidgets
from .property_sections import PropertySections

EDITOR_FACTORIES = {
    'title': ('.component_ui', 'text_property_panel'),
    'body': ('.component_ui', 'text_property_panel'),
    'text': ('.component_ui', 'text_property_panel'),
    'caption': ('.caption_ui', 'caption_property_panel'),
    'image': ('.visual_media', 'visual_property_panel'),
    'background': ('.background_visual', 'background_property_panel'),
    'watermark': ('.watermarks', 'watermark_property_panel'),
    'node_effect': ('.node_effect_ui', 'node_property_panel'),
}


def require_editor_api(panel_type):
    # Source installation does not hot-reload existing Python classes. A panel
    # first imported later in that session must not mix incompatible UI APIs.
    providers_ready = all(
        name not in sys.modules or callable(getattr(sys.modules[name],function,None))
        for module,function in EDITOR_FACTORIES.values() for name in (__package__+module,))
    if not callable(getattr(panel_type,'_inspector_edited',None)) or not providers_ready:
        raise RuntimeError('控件属性界面已更新，当前会话仍载有旧界面。请在下次正常重启后使用；不会修改当前参数。')


class ComponentPropertyPanel(PropertySections):
    flat_sources = True
    edited = QtCore.pyqtSignal(str, object)
    unitChanged = QtCore.pyqtSignal(str)
    addImages = QtCore.pyqtSignal(str)
    selectCopy = QtCore.pyqtSignal(str)
    selectImagePool = QtCore.pyqtSignal(str)
    mattingRequested = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, *, mode='batch', parent=None):
        super().__init__(parent)
        if not hasattr(self,'targets'):
            raise RuntimeError('属性布局源码已更新，当前进程仍使用旧界面；请在下次正常重启后使用，当前参数未修改。')
        self.mode, self.layer, self.loading = mode, None, False
        self.bindings = []
        self.geometry_editor = None

    def add_bound(self, key, title, editor, *, fields=None, expanded=False, patch=None, section=True):
        self.bindings.append((key, editor))
        if section:
            group = self.add_section(key, title, editor, expanded=expanded)
            if getattr(editor, 'enable_control', None) is not None:
                group.set_enable_control(editor.enable_control)
        else: self.body.addWidget(editor)
        def edited(values):
            if self.loading or not self.layer: return
            source = getattr(editor, 'layer', None)
            if source is not None and source.get('id') != self.layer['id']: return
            if values.get('id', self.layer['id']) != self.layer['id']: return
            result = patch(values) if patch else {k:copy.deepcopy(v) for k,v in values.items()
                if k not in {'id','kind'} and not k.startswith('_') and (fields is None or k in fields)}
            self.edited.emit(self.layer['id'], result)
        editor.changed.connect(edited)
        return editor

    def add_source(self):
        from .component_ui import ContentSourceEditor
        fields = {'source','source_file','text','path','thumbnail','caption_file','caption_timing',
                  'sequence_items','sequence_cursor','pool_field','pool_id','copy_binding'}
        editor = self.add_bound('source', '', ContentSourceEditor(), fields=fields, section=False)
        self.targets['source'] = editor
        self.body.removeWidget(editor); self.body.insertWidget(0,editor)
        editor.addImages.connect(self.addImages); editor.selectCopy.connect(self.selectCopy)
        editor.selectImagePool.connect(self.selectImagePool)
        self.content_editor = editor
        if self.mode != 'batch':
            editor.allow_copy_pick = False
        return editor

    def add_geometry(self):
        from .geometry_ui import GeometryEditor
        self.geometry_editor = self.add_bound('position', '', GeometryEditor(), section=False)
        self.targets['position'] = self.geometry_editor
        self.geometry_editor.unitChanged.connect(self.unitChanged)
        self.geometry_editor.message.connect(self.message)

    def bind(self, layer, canvas_size, state, unit):
        self.loading = True
        self.layer = copy.deepcopy(layer) if layer else None
        try:
            for key, editor in self.bindings:
                if key == 'position':
                    editor.bind(layer, canvas_size, unit)
                else:
                    if hasattr(editor, 'set_state'): editor.set_state(state)
                    if hasattr(editor, 'set_pool_state'): editor.set_pool_state(state)
                    if hasattr(editor, 'set_canvas_size'): editor.set_canvas_size(canvas_size.width(), canvas_size.height())
                    if key == 'scroll': editor.set_layer(layer, canvas_size.height())
                    else: editor.set_layer(layer)
            if self.mode == 'node_layer' and hasattr(self, 'content_editor'):
                editor = self.content_editor
                blocker = QtCore.QSignalBlocker(editor.source)
                for index in range(editor.source.count()-1, -1, -1):
                    if editor.source.itemData(index) not in {'fixed','file'}: editor.source.removeItem(index)
                del blocker
                editor.show_mode()
            self.setEnabled(bool(layer))
            for section in self.sections.values():
                section.sync_enabled()
        finally:
            self.loading = False

    def sync_geometry(self, layer, canvas_size, unit):
        self.layer = copy.deepcopy(layer) if layer else None
        if self.geometry_editor: self.geometry_editor.bind(layer, canvas_size, unit)
        if hasattr(self, 'scroll_properties'): self.scroll_properties.set_layer(layer, canvas_size.height())


class ComponentInspector(QtWidgets.QWidget):
    edited = QtCore.pyqtSignal(str, object)
    unitChanged = QtCore.pyqtSignal(str)
    addImages = QtCore.pyqtSignal(str)
    selectCopy = QtCore.pyqtSignal(str)
    selectImagePool = QtCore.pyqtSignal(str)
    mattingRequested = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, services=None, *, mode='batch', parent=None):
        super().__init__(parent)
        self.services, self.mode, self.active = services, mode, None
        self.editors = {}
        self.matting_available = False
        self.cache_parent = QtWidgets.QWidget(self); self.cache_parent.hide()
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(0,0,0,0)
        self.body.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.empty = QtWidgets.QLabel('在画布或图层列表中选择一个控件，便可在这里修改。')
        self.empty.setWordWrap(True); self.body.addWidget(self.empty)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)

    def show_layer(self, layer, canvas_size, state, unit, *, layers=None):
        target = None
        if layer:
            kind = layer['kind']
            if kind not in self.editors:
                module, function = EDITOR_FACTORIES[kind]
                factory = getattr(importlib.import_module(module, __package__), function)
                editor = factory(self.services, mode=self.mode)
                for name in ('edited','unitChanged','addImages','selectCopy','selectImagePool','mattingRequested','message'):
                    getattr(editor, name).connect(getattr(self, name))
                self.editors[kind] = editor
            target = self.editors[kind]
        if target is not self.active:
            if self.active:
                self.body.removeWidget(self.active)
                self.active.bind(None, canvas_size, state, unit)
                self.active.hide(); self.active.setParent(self.cache_parent)
            self.active = target
            if target:
                self.body.addWidget(target); target.show()
        self.empty.setVisible(target is None)
        if target:
            if hasattr(target, 'wrap_properties'):
                target.wrap_properties.set_layers(layers if layers is not None else state.get('settings', {}).get('layers', []))
            target.bind(layer, canvas_size, state, unit)
            if hasattr(target, 'visual_properties'):
                target.visual_properties.matting_button.setVisible(self.matting_available and layer['kind'] != 'background')

    def reveal(self, key):
        if self.active: self.active.reveal(key)

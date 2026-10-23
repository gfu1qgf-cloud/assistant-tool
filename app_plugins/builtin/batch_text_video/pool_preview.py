"""Session-only visual pool choices; never change the committed layer cursor."""
import copy
from pathlib import Path
from qt_compat import QtCore, QtWidgets


def pool_key(layer):
    return layer['id'], layer.get('pool_id', '')


class PoolPreview(QtWidgets.QWidget):
    selectionChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layers, self.choices, self.loading, self.scope = [], {}, False, None
        self.embedded, self.source_attached = False, False
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('资源池试看'))
        self.layer_combo = self.combo(); row.addWidget(self.layer_combo, 1)
        root.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.previous = QtWidgets.QToolButton(); self.previous.setText('上一张')
        self.next = QtWidgets.QToolButton(); self.next.setText('下一张')
        self.item_combo = self.combo()
        self.reset = QtWidgets.QPushButton('恢复生成顺序'); self.reset.setAutoDefault(False)
        for widget in (self.previous, self.item_combo, self.next, self.reset):
            row.addWidget(widget, 1 if widget is self.item_combo else 0)
        root.addLayout(row)
        self.note = QtWidgets.QLabel('只切换设计视图和播放预览；不改变生成顺序。快速导出前请恢复生成顺序。')
        self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        root.addWidget(self.note)
        self.layer_combo.currentIndexChanged.connect(self._layer_changed)
        self.item_combo.currentIndexChanged.connect(self._item_changed)
        self.previous.clicked.connect(lambda: self.step(-1)); self.next.clicked.connect(lambda: self.step(1))
        self.reset.clicked.connect(self.restore)
        self.hide()

    @staticmethod
    def combo():
        control = QtWidgets.QComboBox()
        control.setSizeAdjustPolicy(control.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        control.setMinimumContentsLength(8); control.setMinimumWidth(0)
        control.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        return control

    def current_layer(self):
        return next((layer for layer in self.layers if layer['id'] == self.layer_combo.currentData()), None)

    def attach_to_source(self, source):
        """Keep the session state while moving the chooser beside its source."""
        self.embedded = True
        self.source_attached = source is not None
        self.layer_combo.hide()
        if source is None:
            self.hide()
        elif self.parentWidget() is not source:
            self.setParent(source)
            source.layout().insertWidget(1, self)

    def sync(self, layers, selected='', *, scope=None):
        self.loading = True
        if scope is not None and scope != self.scope:
            self.choices.clear()
            self.scope = scope
        old = self.layer_combo.currentData()
        self.layers = copy.deepcopy([layer for layer in layers
            if layer.get('kind') == 'image' and layer.get('source') == 'pool'])
        valid = {pool_key(layer): {item['id'] for item in layer.get('sequence_items', [])} for layer in self.layers}
        self.choices = {key: value for key, value in self.choices.items() if value in valid.get(key, set())}
        ids = [layer['id'] for layer in self.layers]
        target = selected if selected in ids else old if old in ids else ids[0] if ids else ''
        self.layer_combo.clear()
        for layer in self.layers:
            self.layer_combo.addItem(layer.get('name', '图片')+' · '+layer.get('pool_name', '图片池'), layer['id'])
        self.layer_combo.setCurrentIndex(self.layer_combo.findData(target))
        self.loading = False
        self._populate_items()
        self.setVisible(self.source_attached and selected in ids if self.embedded else bool(self.layers))

    def _populate_items(self):
        self.loading = True
        self.item_combo.clear()
        layer = self.current_layer()
        items = layer.get('sequence_items', []) if layer else []
        cursor = layer.get('sequence_cursor', 0) % len(items) if items else 0
        self.item_combo.addItem(f'按生成顺序（下次第{cursor+1}项）' if items else '资源池为空', '')
        for index, item in enumerate(items):
            self.item_combo.addItem(f"{index+1}/{len(items)} · {item.get('name') or Path(item['path']).name}", item['id'])
            self.item_combo.setItemData(index+1, item['path'], QtCore.Qt.ItemDataRole.ToolTipRole)
        selected = self.choices.get(pool_key(layer), '') if layer else ''
        self.item_combo.setCurrentIndex(max(0, self.item_combo.findData(selected)))
        self.item_combo.setEnabled(bool(items))
        for button in (self.previous, self.next): button.setEnabled(len(items) > 1)
        self.reset.setEnabled(bool(self.choices))
        self.loading = False

    def _layer_changed(self, *_):
        if not self.loading: self._populate_items()

    def _item_changed(self, *_):
        layer = self.current_layer()
        if self.loading or layer is None: return
        value = self.item_combo.currentData()
        if value: self.choices[pool_key(layer)] = value
        else: self.choices.pop(pool_key(layer), None)
        self.reset.setEnabled(bool(self.choices))
        self.selectionChanged.emit()

    def step(self, offset):
        layer = self.current_layer()
        items = layer.get('sequence_items', []) if layer else []
        if not items: return
        index = next((i for i, item in enumerate(items) if item['id'] == self.item_combo.currentData()),
                     layer.get('sequence_cursor', 0) % len(items))
        self.item_combo.setCurrentIndex((index+offset) % len(items)+1)

    def restore(self):
        if not self.choices: return
        self.choices.clear(); self._populate_items(); self.selectionChanged.emit()

    def apply(self, settings):
        result = copy.deepcopy(settings)
        for layer in result['layers']:
            if layer.get('kind') != 'image' or layer.get('source') != 'pool': continue
            selected = self.choices.get(pool_key(layer))
            index = next((i for i, item in enumerate(layer.get('sequence_items', [])) if item['id'] == selected), None)
            if index is not None: layer['sequence_cursor'] = index
        return result

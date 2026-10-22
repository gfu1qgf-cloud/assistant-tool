"""Small, reusable inspector sections. No persistence or rendering in this module."""
from qt_compat import QtCore, QtWidgets


class PropertySection(QtWidgets.QWidget):
    def __init__(self, title, content, *, expanded=False, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        self.content = content
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.header = QtWidgets.QWidget(self)
        self.header.setObjectName('property_section_header')
        self.header.setStyleSheet('QWidget#property_section_header {border:1px solid palette(mid); border-radius:4px; background:palette(button);}')
        self.header_layout = QtWidgets.QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(0, 0, 6, 0)
        self.header_layout.setSpacing(6)
        self.enable_control = None
        self.disable_content_when_off = True
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.toggle.setStyleSheet('QToolButton {text-align:left; padding:7px; border:none; background:transparent;}')
        self.toggle.toggled.connect(self._expanded)
        self.header_layout.addWidget(self.toggle, 1)
        layout.addWidget(self.header)
        layout.addWidget(content)
        self.set_expanded(expanded)

    def _expanded(self, opened):
        self.toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if opened else QtCore.Qt.ArrowType.RightArrow)
        self.content.setVisible(opened)

    def set_expanded(self, opened):
        self.toggle.setChecked(opened)
        self._expanded(opened)

    def set_enable_control(self, control, *, disable_content=True):
        """Use the feature's existing checkbox; the title only folds details."""
        self.enable_control = control
        self.disable_content_when_off = disable_content
        label = control.text()
        control.setText('')
        control.setAccessibleName(label or '启用' + self.toggle.text())
        control.setToolTip(label or '启用' + self.toggle.text())
        self.header_layout.addWidget(control)
        control.toggled.connect(self.sync_enabled)
        self.sync_enabled()

    def sync_enabled(self, *_args):
        if self.enable_control is not None:
            self.content.setEnabled(not self.disable_content_when_off or self.enable_control.isChecked())


class PropertySections(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sections = {}
        self.targets = {}  # Flat fields remain revealable without fake sections.
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(7)
        self.body.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self._pending_reveal = None
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._ensure_revealed)

    def add_section(self, key, title, content, *, expanded=False):
        section = PropertySection(title, content, expanded=expanded, parent=self)
        self.sections[key] = section
        self.body.addWidget(section)
        return section

    def reveal(self, key):
        section = self.sections.get(key) or self.targets.get(key)
        if section:
            parent = section.parentWidget()
            while parent is not None:
                if isinstance(parent, PropertySection):
                    parent.set_expanded(True)
                parent = parent.parentWidget()
            if isinstance(section, PropertySection): section.set_expanded(True)
            self._pending_reveal = key
            self._scroll_timer.start(0)

    def _ensure_revealed(self):
        section = self.sections.get(self._pending_reveal) or self.targets.get(self._pending_reveal)
        if not section or not section.isVisible(): return
        parent = self.parentWidget()
        while parent and not isinstance(parent, QtWidgets.QScrollArea): parent = parent.parentWidget()
        if parent: parent.ensureWidgetVisible(section)


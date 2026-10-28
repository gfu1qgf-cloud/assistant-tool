"""Persist the prompt editor's logical-pixel width independently of its text."""
from qt_compat import QtCore, QtWidgets


class EditorWidthLock(QtCore.QObject):
    def __init__(self, panel, layout, *, prefix='manual_editor', companions=()):
        super().__init__(panel)
        self.panel, self.editor = panel, panel.prompt
        self.prefix,self.companions=prefix,companions
        self.minimum, self.maximum = self.editor.minimumWidth(), self.editor.maximumWidth()
        self.width = None
        self.pending = None
        self.checkbox = QtWidgets.QCheckBox('锁定编辑框宽度', panel)
        self.checkbox.setObjectName('flow_lock_editor_width')
        self.checkbox.setToolTip('勾选时锁定当前宽度；取消后可调整窗口，再勾选记住新宽度。宽度和开关自动保存。')
        self.checkbox.setChecked(True)
        layout.addWidget(self.checkbox)
        self.checkbox.toggled.connect(self.toggle)
        self.editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() in {QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Resize}:
            if self.checkbox.isChecked() and self.width is None:
                QtCore.QTimer.singleShot(0, self.capture_if_ready)
        return False

    def capture_if_ready(self):
        if self.checkbox.isChecked() and self.width is None and self.editor.isVisible() and self.editor.width() >= 100:
            self.width = self.editor.width()
            self.editor.setFixedWidth(self.width)
            for widget in self.companions:widget.setFixedWidth(self.width)
            self.save()

    def save(self):
        values = {self.prefix+'_width_locked':self.checkbox.isChecked()}
        if self.width is not None:
            values[self.prefix+'_width'] = self.width
        self.pending = values
        self.panel.dialog.send('preferences', value=values)

    def toggle(self, checked):
        if checked:
            self.width = self.editor.width() if self.editor.width() >= 100 else None
            if self.width is not None:self.editor.setFixedWidth(self.width)
            if self.width is not None:
                for widget in self.companions:widget.setFixedWidth(self.width)
        else:
            self.width = None
            self.editor.setMinimumWidth(self.minimum)
            self.editor.setMaximumWidth(self.maximum)
            for widget in self.companions:widget.setMinimumWidth(0);widget.setMaximumWidth(16777215)
        self.save()

    def apply(self, preferences):
        if self.pending:
            if any(preferences.get(key) != value for key,value in self.pending.items()):
                return
            self.pending = None
        locked = preferences.get(self.prefix+'_width_locked', True)
        width = preferences.get(self.prefix+'_width')
        with QtCore.QSignalBlocker(self.checkbox):self.checkbox.setChecked(locked)
        if locked:
            if type(width) is int and 100 <= width <= 8000:
                self.width = width
                self.editor.setFixedWidth(width)
                for widget in self.companions:widget.setFixedWidth(width)
            elif self.width is None:
                QtCore.QTimer.singleShot(0, self.capture_if_ready)
        else:
            self.width = None
            self.editor.setMinimumWidth(self.minimum)
            self.editor.setMaximumWidth(self.maximum)
            for widget in self.companions:widget.setMinimumWidth(0);widget.setMaximumWidth(16777215)

"""Compact token input and a multi-select popup for music search tags."""

from qt_compat import QtCore, QtGui, QtWidgets

from .encoder import MOOD_TAGS, SOUND_TAGS


class _BackspaceLineEdit(QtWidgets.QLineEdit):
    emptyBackspace = QtCore.pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Backspace and not self.text():
            self.emptyBackspace.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _TagPopup(QtWidgets.QFrame):
    changed = QtCore.pyqtSignal(str, str, bool)
    clearRequested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self.setObjectName("music_tag_popup")
        self.setStyleSheet("""
            QFrame#music_tag_popup { background:#ffffff; border:1px solid #cbd9e8;
                border-radius:12px; }
            QLabel { color:#263e58; border:0; }
            QToolButton { background:#f5f8fc; color:#35516c; border:1px solid #dae4ef;
                border-radius:14px; padding:6px 10px; }
            QToolButton:hover { background:#e9f2ff; border-color:#8db9e9; }
            QToolButton:checked { background:#dcecff; color:#165a9e;
                border-color:#5a9cdb; font-weight:600; }
            QPushButton { background:transparent; border:0; color:#537eae; }
        """)
        self.buttons = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(15, 13, 15, 14)
        layout.setSpacing(8)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("挑选音乐线索")
        title.setStyleSheet("font-size:14px; font-weight:700")
        heading.addWidget(title)
        heading.addStretch(1)
        clear = QtWidgets.QPushButton("清空")
        clear.clicked.connect(self.clearRequested)
        heading.addWidget(clear)
        layout.addLayout(heading)
        note = QtWidgets.QLabel("可多选；标签只描述声音，不读取歌曲文件名")
        note.setStyleSheet("color:#718298; font-size:11px")
        layout.addWidget(note)
        self._add_group(layout, "情绪 / 氛围", "mood", MOOD_TAGS, 4)
        self._add_group(layout, "声音 / 编制", "sound", SOUND_TAGS, 4)
        self.setFixedWidth(430)

    def _add_group(self, layout, title, group, tags, columns):
        label = QtWidgets.QLabel(title)
        label.setStyleSheet("font-weight:600; margin-top:4px")
        layout.addWidget(label)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(7)
        for number, (name, english) in enumerate(tags.items()):
            button = QtWidgets.QToolButton()
            button.setText(name)
            button.setCheckable(True)
            button.setToolTip(f"用于模型检索：{english}")
            button.toggled.connect(
                lambda checked, g=group, n=name: self.changed.emit(g, n, checked)
            )
            grid.addWidget(button, number // columns, number % columns)
            self.buttons[(group, name)] = button
        layout.addLayout(grid)

    def set_selected(self, selected):
        selected = set(selected)
        for key, button in self.buttons.items():
            with QtCore.QSignalBlocker(button):
                button.setChecked(key in selected)


class MusicTagInput(QtWidgets.QWidget):
    """A single-line query with removable chips, plus an anchored tag picker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = []
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        self.field = QtWidgets.QFrame()
        self.field.setObjectName("music_query_field")
        self.field.setStyleSheet("""
            QFrame#music_query_field { background:#ffffff; border:1px solid #b9cce0;
                border-radius:9px; }
            QLineEdit#music_query_text { background:transparent; color:#23384e;
                border:0; padding:3px 5px; }
        """)
        field_layout = QtWidgets.QHBoxLayout(self.field)
        field_layout.setContentsMargins(8, 3, 8, 3)
        field_layout.setSpacing(4)
        self.chip_host = QtWidgets.QWidget()
        self.chip_layout = QtWidgets.QHBoxLayout(self.chip_host)
        self.chip_layout.setContentsMargins(0, 0, 0, 0)
        self.chip_layout.setSpacing(4)
        self.chip_area = QtWidgets.QScrollArea()
        self.chip_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.chip_area.setWidgetResizable(False)
        self.chip_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chip_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chip_area.setFixedHeight(30)
        self.chip_area.setWidget(self.chip_host)
        self.chip_area.hide()
        field_layout.addWidget(self.chip_area)
        self.query = _BackspaceLineEdit()
        self.query.setObjectName("music_query_text")
        self.query.setFrame(False)
        self.query.setPlaceholderText("输入情绪、场景或歌名…")
        self.query.emptyBackspace.connect(self.remove_last)
        field_layout.addWidget(self.query, 1)
        layout.addWidget(self.field, 1)

        self.tag_button = QtWidgets.QToolButton()
        self.tag_button.setText("＋ 标签")
        self.tag_button.setToolTip("选择情绪、氛围和声音标签；支持多选")
        self.tag_button.setStyleSheet("""
            QToolButton { background:#e9f2ff; color:#185a9a; border:1px solid #9cc6ef;
                border-radius:8px; padding:7px 11px; font-weight:600; }
            QToolButton:hover { background:#d9eaff; border-color:#5b9fdf; }
        """)
        self.tag_button.clicked.connect(self.show_picker)
        layout.addWidget(self.tag_button)

        self.picker = _TagPopup(self)
        self.picker.changed.connect(self._picker_changed)
        self.picker.clearRequested.connect(self.clear_tags)
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_end)

    def selected_tags(self):
        return (tuple(name for group, name in self._selected if group == "mood"),
                tuple(name for group, name in self._selected if group == "sound"))

    def show_picker(self):
        self.picker.set_selected(self._selected)
        size = self.picker.sizeHint()
        point = self.tag_button.mapToGlobal(QtCore.QPoint(self.tag_button.width() - size.width(),
                                                         self.tag_button.height() + 5))
        screen = QtGui.QGuiApplication.screenAt(point) or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            point.setX(max(available.left(), min(point.x(), available.right() - size.width())))
            if point.y() + size.height() > available.bottom():
                point.setY(self.tag_button.mapToGlobal(QtCore.QPoint(0, 0)).y() - size.height() - 5)
        self.picker.move(point)
        self.picker.show()
        self.picker.raise_()

    def _picker_changed(self, group, name, checked):
        if checked:
            self.add_tag(group, name)
        else:
            self.remove_tag(group, name)

    def add_tag(self, group, name):
        if group not in {"mood", "sound"} or name not in (
                MOOD_TAGS if group == "mood" else SOUND_TAGS):
            return
        key = (group, name)
        if key not in self._selected:
            self._selected.append(key)
            self._render_chips()
        self.picker.set_selected(self._selected)

    def remove_tag(self, group, name):
        key = (group, name)
        if key in self._selected:
            self._selected.remove(key)
            self._render_chips()
        self.picker.set_selected(self._selected)
        if not self.picker.isVisible():
            self.query.setFocus()

    def remove_last(self):
        if self._selected:
            self.remove_tag(*self._selected[-1])

    def clear_tags(self):
        self._selected.clear()
        self._render_chips()
        self.picker.set_selected(())
        if not self.picker.isVisible():
            self.query.setFocus()

    def _render_chips(self):
        while self.chip_layout.count():
            item = self.chip_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for group, name in self._selected:
            chip = QtWidgets.QToolButton()
            chip.setText(name + "  ×")
            chip.setToolTip(f"移除“{name}”标签")
            chip.setStyleSheet("""
                QToolButton { background:#e7f1ff; color:#174e8e;
                    border:1px solid #a8c7ed; border-radius:11px; padding:3px 7px; }
                QToolButton:hover { background:#d0e5ff; border-color:#5d9ddd; }
            """)
            chip.clicked.connect(lambda _checked=False, g=group, n=name: self.remove_tag(g, n))
            self.chip_layout.addWidget(chip)
        self.chip_area.setVisible(bool(self._selected))
        self.query.setPlaceholderText("补充文字（可选）" if self._selected else "输入情绪、场景或歌名…")
        self._resize_chip_area()

    def _resize_chip_area(self):
        if not self._selected:
            return
        chips = [self.chip_layout.itemAt(index).widget()
                 for index in range(self.chip_layout.count())]
        natural = sum(chip.sizeHint().width() for chip in chips)
        natural += self.chip_layout.spacing() * max(0, len(chips) - 1)
        width = min(natural, max(110, self.field.width() - 230))
        self.chip_area.setFixedWidth(width)
        self.chip_host.resize(natural, self.chip_area.height())
        self._scroll_timer.start(0)

    def _scroll_to_end(self):
        bar = self.chip_area.horizontalScrollBar()
        bar.setValue(bar.maximum())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_chip_area()

    def closeEvent(self, event):
        self.picker.close()
        super().closeEvent(event)

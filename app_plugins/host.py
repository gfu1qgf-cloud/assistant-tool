import logging
import hashlib
from collections import OrderedDict

from qt_compat import QtCore, QtGui, QtWidgets

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginMainWidget,
    PluginSettingsPage,
)


PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY = "plugin_main_widget_order"
PLUGIN_MAIN_WIDGET_MIME = "application/x-lzx-plugin-main-widget"

def _card_color_indices(full_ids):
    """Generate separated hues for any registered cards, independent of drag order."""
    assigned = {}
    used = []
    for full_id in sorted(set(full_ids)):
        seed = int.from_bytes(
            hashlib.blake2s(full_id.encode("utf-8"), digest_size=2).digest(), "big"
        ) % 360
        if not used:
            hue = seed
        else:
            def distance(first, second):
                gap = abs(first - second)
                return min(gap, 360 - gap)

            hue = max(
                range(360),
                key=lambda candidate: (
                    min(distance(candidate, existing) for existing in used),
                    -distance(candidate, seed),
                    -candidate,
                ),
            )
        assigned[full_id] = hue
        used.append(hue)
    return assigned


class _PluginWidgetDragHandle(QtWidgets.QToolButton):
    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self._press_position = None
        self.setText("⠿")
        self.setAutoRaise(True)
        self.setFixedWidth(24)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.setToolTip("按住并拖动，可调整插件卡片顺序")

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._press_position = event.position().toPoint()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_position = None
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._press_position is None
            or not event.buttons() & QtCore.Qt.MouseButton.LeftButton
        ):
            super().mouseMoveEvent(event)
            return
        distance = (
            event.position().toPoint() - self._press_position
        ).manhattanLength()
        if distance < QtWidgets.QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        mime = QtCore.QMimeData()
        mime.setData(
            PLUGIN_MAIN_WIDGET_MIME,
            QtCore.QByteArray(self.card.full_id.encode("utf-8")),
        )
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        pixmap = self.card.grab()
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
        drag.exec(QtCore.Qt.DropAction.MoveAction)
        self._press_position = None
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)


class PluginMainWidgetFrame(QtWidgets.QFrame):
    """Host-owned draggable frame around one plugin-owned widget."""

    reorderRequested = QtCore.pyqtSignal(str, str, bool)

    def __init__(self, full_id, title, content, parent=None, color_index=0):
        super().__init__(parent)
        self.full_id = str(full_id)
        self.content = content
        self.color_index = int(color_index) % 360
        self.setObjectName("plugin_card_" + self.full_id.replace(".", "_"))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Plain)
        self.setAcceptDrops(True)
        self.setLineWidth(1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 7)
        layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.drag_handle = _PluginWidgetDragHandle(self)
        self.title_label = QtWidgets.QLabel(str(title or full_id), self)
        self.title_label.setStyleSheet("font-weight:600;")
        header.addWidget(self.drag_handle)
        header.addWidget(self.title_label)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addWidget(content)

    def card_colors(self):
        window = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window)
        if window.lightness() < 128:
            return (QtGui.QColor.fromHsv(self.color_index, 65, 74),
                    QtGui.QColor.fromHsv(self.color_index, 100, 185))
        return (QtGui.QColor.fromHsv(self.color_index, 42, 246),
                QtGui.QColor.fromHsv(self.color_index, 110, 170))

    def paintEvent(self, event):
        super().paintEvent(event)
        background, outline = self.card_colors()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(outline, max(1, self.lineWidth())))
        painter.setBrush(background)
        painter.drawRoundedRect(
            QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7
        )

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    @staticmethod
    def _point(event):
        position = getattr(event, "position", None)
        if callable(position):
            return position().toPoint()
        return event.pos()

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(PLUGIN_MAIN_WIDGET_MIME):
            self.setLineWidth(2)
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(PLUGIN_MAIN_WIDGET_MIME):
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setLineWidth(1)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setLineWidth(1)
        try:
            source_id = bytes(
                event.mimeData().data(PLUGIN_MAIN_WIDGET_MIME)
            ).decode("utf-8")
        except (UnicodeError, ValueError):
            event.ignore()
            return
        if not source_id or source_id == self.full_id:
            event.ignore()
            return
        after = self._point(event).y() >= self.height() / 2
        self.reorderRequested.emit(source_id, self.full_id, after)
        event.setDropAction(QtCore.Qt.DropAction.MoveAction)
        event.accept()


class PluginContext:
    """Stable services offered by the main application to one plugin."""

    def __init__(self, host, plugin_id):
        self._host = host
        self.plugin_id = plugin_id

    @property
    def parent_widget(self):
        return self._host.main_window

    def register_command(self, command):
        self._host.register_command(self.plugin_id, command)

    def register_settings_page(self, page):
        self._host.register_settings_page(self.plugin_id, page)

    def register_main_widget(self, widget):
        self._host.register_main_widget(self.plugin_id, widget)

    def load_config(self):
        return self._host.main_window.load_config()

    def save_config(self):
        return self._host.main_window.saveCurrentConfig()

    def log(self, message, level=logging.INFO):
        self._host.main_window.appendLog(
            f"[插件/{self.plugin_id}] {message}",
            end="",
            level=level,
        )

    def notify(self, title, message, critical=False):
        self._host.main_window.showDesktopNotification(
            title,
            message,
            critical=critical,
        )

    def selected_task_rows(self):
        return self._host.main_window.selectedTaskRows()

    def task_targets(self, rows, require_loaded=True):
        return self._host.main_window.taskTargetsForRows(
            rows,
            require_loaded=require_loaded,
        )

    def task_language(self, task):
        """Return the host's normalized language for one task."""
        return self._host.main_window.detectTaskLanguage(task)

    def subtitle_generation_settings(self):
        """Expose the live subtitle controls without leaking widget details."""
        window = self._host.main_window
        return {
            "srt_include_line_breaks": (
                window.subtitle_line_break_checkbox.isChecked()
            ),
            "srt_max_words_per_block": window.subtitle_max_words_spinbox.value(),
            "srt_block_gap_ms": window.subtitle_gap_ms_spinbox.value(),
        }

    def set_subtitle_generation_settings(self, settings):
        """Update the host's global subtitle controls from a plugin editor."""
        window = self._host.main_window
        values = settings if isinstance(settings, dict) else {}
        mappings = (
            (
                window.subtitle_line_break_checkbox,
                "setChecked",
                bool(values.get("srt_include_line_breaks", False)),
            ),
            (
                window.subtitle_max_words_spinbox,
                "setValue",
                int(values.get("srt_max_words_per_block", 0) or 0),
            ),
            (
                window.subtitle_gap_ms_spinbox,
                "setValue",
                int(values.get("srt_block_gap_ms", -1)),
            ),
        )
        for widget, method, value in mappings:
            getattr(widget, method)(value)

    def whisper_model(self, model_name=None):
        """Return the application's shared, lazily initialized Whisper model."""
        from globalValue import globalValue

        return globalValue.get_whisper_model(model_name)

    def loaded_whisper_model_name(self):
        from globalValue import globalValue

        return globalValue.loaded_whisper_model_name()

    def update_command(
        self,
        command_id,
        title=None,
        tooltip=None,
        enabled=None,
        checked=None,
    ):
        self._host.update_command(
            self.plugin_id,
            command_id,
            title=title,
            tooltip=tooltip,
            enabled=enabled,
            checked=checked,
        )


class PluginHost:
    """Registers built-in plugins and connects them to the host UI."""

    api_version = 1

    def __init__(self, main_window):
        self.main_window = main_window
        self._plugins = OrderedDict()
        self._contexts = {}
        self._commands = OrderedDict()
        self._settings_pages = OrderedDict()
        self._settings_controllers = []
        self._main_widgets = OrderedDict()
        self._main_widget_controllers = OrderedDict()
        self._main_widget_frames = OrderedDict()
        self._main_widget_order = []
        self._main_widget_layout = None
        self._main_actions = {}
        self._tool_actions = {}
        self._tools_separator = None
        self.menu = None
        self.tools_menu = None

    def install(self, plugin):
        plugin_id = str(getattr(plugin, "plugin_id", "") or "").strip()
        if not plugin_id:
            raise ValueError("插件缺少 plugin_id。")
        if plugin_id in self._plugins:
            raise ValueError(f"插件 ID 重复：{plugin_id}")
        required_api = int(getattr(plugin, "required_api_version", 1))
        if required_api > self.api_version:
            raise RuntimeError(
                f"插件 {plugin_id} 需要接口版本 {required_api}，"
                f"当前仅支持 {self.api_version}。"
            )
        context = PluginContext(self, plugin_id)
        existing_commands = set(self._commands)
        existing_pages = set(self._settings_pages)
        existing_widgets = set(self._main_widgets)
        self._plugins[plugin_id] = plugin
        self._contexts[plugin_id] = context
        try:
            plugin.register(context)
        except Exception:
            self._plugins.pop(plugin_id, None)
            self._contexts.pop(plugin_id, None)
            for command_id in set(self._commands) - existing_commands:
                self._commands.pop(command_id, None)
            for page_id in set(self._settings_pages) - existing_pages:
                self._settings_pages.pop(page_id, None)
            for widget_id in set(self._main_widgets) - existing_widgets:
                self._main_widgets.pop(widget_id, None)
            raise
        if self.menu is not None:
            self._rebuild_main_menu()
        if self.tools_menu is not None:
            self._rebuild_tools_menu()
        return plugin

    def plugin(self, plugin_id):
        return self._plugins.get(plugin_id)

    def register_command(self, plugin_id, command):
        if not isinstance(command, PluginCommand):
            raise TypeError("register_command 只接受 PluginCommand。")
        command_id = str(command.command_id or "").strip()
        if not command_id:
            raise ValueError("插件命令缺少 command_id。")
        full_id = f"{plugin_id}.{command_id}"
        if full_id in self._commands:
            raise ValueError(f"插件命令 ID 重复：{full_id}")
        invalid_locations = set(command.locations) - {
            MAIN_MENU,
            TOOLS_MENU,
            TASK_CONTEXT_MENU,
        }
        if invalid_locations:
            raise ValueError(f"插件命令位置无效：{sorted(invalid_locations)}")
        self._commands[full_id] = (plugin_id, command)

    def register_settings_page(self, plugin_id, page):
        if not isinstance(page, PluginSettingsPage):
            raise TypeError("register_settings_page 只接受 PluginSettingsPage。")
        page_id = str(page.page_id or "").strip()
        if not page_id:
            raise ValueError("插件设置页缺少 page_id。")
        full_id = f"{plugin_id}.{page_id}"
        if full_id in self._settings_pages:
            raise ValueError(f"插件设置页 ID 重复：{full_id}")
        self._settings_pages[full_id] = (plugin_id, page)

    def register_main_widget(self, plugin_id, widget):
        if not isinstance(widget, PluginMainWidget):
            raise TypeError("register_main_widget 只接受 PluginMainWidget。")
        widget_id = str(widget.widget_id or "").strip()
        if not widget_id:
            raise ValueError("插件主界面控件缺少 widget_id。")
        full_id = f"{plugin_id}.{widget_id}"
        if full_id in self._main_widgets:
            raise ValueError(f"插件主界面控件 ID 重复：{full_id}")
        self._main_widgets[full_id] = (plugin_id, widget)

    def attach_main_menu(self, menu_bar):
        if self.menu is None:
            self.menu = menu_bar.addMenu("插件")
            self.menu.setObjectName("plugins_menu")
        self._rebuild_main_menu()
        return self.menu

    def attach_tools_menu(self, tools_menu):
        self.tools_menu = tools_menu
        self._rebuild_tools_menu()
        return self.tools_menu

    def attach_main_widget_area(self, container, layout=None):
        """Mount plugin widgets while keeping placement under host control."""
        if not isinstance(container, QtWidgets.QWidget):
            raise TypeError("插件主界面区域必须是 QWidget。")
        target_layout = layout or container.layout()
        if target_layout is None:
            target_layout = QtWidgets.QVBoxLayout(container)
            target_layout.setContentsMargins(0, 0, 0, 0)
        self._main_widget_layout = target_layout
        default_entries = sorted(
            self._main_widgets.items(),
            key=lambda item: (item[1][1].order, item[0]),
        )
        config = self.main_window.load_config()
        saved_order = config.get(PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY, [])
        if not isinstance(saved_order, list):
            saved_order = []
        known_ids = {full_id for full_id, _entry in default_entries}
        ordered_ids = [
            str(full_id) for full_id in saved_order
            if str(full_id) in known_ids
        ]
        ordered_ids.extend(
            full_id for full_id, _entry in default_entries
            if full_id not in ordered_ids
        )
        entries_by_id = dict(default_entries)
        entries = [(full_id, entries_by_id[full_id]) for full_id in ordered_ids]
        color_indices = _card_color_indices(ordered_ids)
        for full_id, (plugin_id, descriptor) in entries:
            if full_id in self._main_widget_controllers:
                continue
            try:
                controller = descriptor.factory(container)
                widget = getattr(controller, "widget", controller)
                if not isinstance(widget, QtWidgets.QWidget):
                    raise TypeError(
                        "主界面控件工厂必须返回 QWidget 或含 widget 的控制器。"
                    )
                plugin = self._plugins.get(plugin_id)
                title = (
                    str(descriptor.title or "").strip()
                    or str(getattr(plugin, "display_name", "") or "").strip()
                    or plugin_id
                )
                frame = PluginMainWidgetFrame(
                    full_id, title, widget, container,
                    color_index=color_indices[full_id],
                )
                frame.reorderRequested.connect(self.move_main_widget)
                target_layout.addWidget(frame)
                self._main_widget_controllers[full_id] = controller
                self._main_widget_frames[full_id] = frame
            except Exception as error:
                self._report_error(plugin_id, descriptor.widget_id, error)
        self._main_widget_order = [
            full_id for full_id in ordered_ids
            if full_id in self._main_widget_frames
        ]
        return list(self._main_widget_controllers.items())

    def main_widget_controller(self, plugin_id, widget_id):
        return self._main_widget_controllers.get(f"{plugin_id}.{widget_id}")

    def main_widget_frame(self, plugin_id, widget_id):
        return self._main_widget_frames.get(f"{plugin_id}.{widget_id}")

    def main_widget_order(self):
        return list(self._main_widget_order)

    def move_main_widget(self, source_id, target_id, after=False, persist=True):
        source_id = str(source_id or "")
        target_id = str(target_id or "")
        if (
            source_id == target_id
            or source_id not in self._main_widget_order
            or target_id not in self._main_widget_order
        ):
            return False
        order = [item for item in self._main_widget_order if item != source_id]
        target_index = order.index(target_id) + (1 if after else 0)
        order.insert(target_index, source_id)
        self._main_widget_order = order
        if self._main_widget_layout is not None:
            for full_id in order:
                frame = self._main_widget_frames.get(full_id)
                if frame is not None:
                    self._main_widget_layout.removeWidget(frame)
                    self._main_widget_layout.addWidget(frame)
        if persist:
            self.main_window.saveCurrentConfig()
        return True

    def _sorted_commands(self, location):
        entries = [
            (full_id, plugin_id, command)
            for full_id, (plugin_id, command) in self._commands.items()
            if location in command.locations
        ]
        return sorted(entries, key=lambda item: (item[2].order, item[2].title, item[0]))

    def _rebuild_main_menu(self):
        if self.menu is None:
            return
        self.menu.clear()
        self._main_actions.clear()
        submenus = {}
        for full_id, plugin_id, command in self._sorted_commands(MAIN_MENU):
            submenu_title = str(command.submenu or "").strip()
            target_menu = self.menu
            if submenu_title:
                target_menu = submenus.get(submenu_title)
                if target_menu is None:
                    target_menu = self.menu.addMenu(submenu_title)
                    target_menu.setObjectName(
                        "plugin_main_submenu_" + "_".join(submenu_title.split())
                    )
                    submenus[submenu_title] = target_menu
            action = target_menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            action.setCheckable(bool(command.checkable))
            if command.checkable:
                checked = command.checked() if callable(command.checked) else command.checked
                action.setChecked(bool(checked))
            action.triggered.connect(
                lambda checked=False, fid=full_id: self.invoke(fid, (), checked)
            )
            self._main_actions[full_id] = action
        if not self._main_actions:
            empty = self.menu.addAction("暂无可用插件")
            empty.setEnabled(False)

    def _rebuild_tools_menu(self):
        if self.tools_menu is None:
            return
        for action in self._tool_actions.values():
            self.tools_menu.removeAction(action)
            action.deleteLater()
        self._tool_actions.clear()
        if self._tools_separator is not None:
            self.tools_menu.removeAction(self._tools_separator)
            self._tools_separator.deleteLater()
            self._tools_separator = None

        entries = self._sorted_commands(TOOLS_MENU)
        if not entries:
            return
        if self.tools_menu.actions():
            self._tools_separator = self.tools_menu.addSeparator()
        for full_id, _plugin_id, command in entries:
            action = self.tools_menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            action.setCheckable(bool(command.checkable))
            if command.checkable:
                checked = command.checked() if callable(command.checked) else command.checked
                action.setChecked(bool(checked))
            action.triggered.connect(
                lambda checked=False, fid=full_id: self.invoke(fid, (), checked)
            )
            self._tool_actions[full_id] = action

    def populate_task_context_menu(self, menu, rows):
        actions = []
        context_rows = tuple(rows or ())
        submenus = {}
        for full_id, plugin_id, command in self._sorted_commands(TASK_CONTEXT_MENU):
            if command.visible is not None:
                try:
                    if not bool(command.visible(context_rows)):
                        continue
                except Exception as error:
                    self._report_error(plugin_id, command.title, error)
                    continue
            submenu_title = str(command.submenu or "").strip()
            target_menu = menu
            if submenu_title:
                target_menu = submenus.get(submenu_title)
                if target_menu is None:
                    target_menu = menu.addMenu(submenu_title)
                    target_menu.setObjectName(
                        "plugin_submenu_" + "_".join(submenu_title.split())
                    )
                    submenus[submenu_title] = target_menu
            action = target_menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            if command.enabled is not None:
                try:
                    action.setEnabled(bool(command.enabled(context_rows)))
                except Exception as error:
                    action.setEnabled(False)
                    self._report_error(plugin_id, command.title, error)
            action.triggered.connect(
                lambda _checked=False, fid=full_id, selected=context_rows: self.invoke(
                    fid,
                    selected,
                )
            )
            actions.append(action)
        return actions

    def invoke(self, full_id, rows=(), checked=None):
        entry = self._commands.get(full_id)
        if entry is None:
            return None
        plugin_id, command = entry
        try:
            if command.checkable:
                return command.callback(tuple(rows or ()), bool(checked))
            return command.callback(tuple(rows or ()))
        except Exception as error:
            self._report_error(plugin_id, command.title, error)
            QtWidgets.QMessageBox.critical(
                self.main_window,
                "插件执行失败",
                f"{command.title} 执行失败：{error}\n\n详细信息已写入程序日志。",
            )
            return None

    def update_command(
        self,
        plugin_id,
        command_id,
        title=None,
        tooltip=None,
        enabled=None,
        checked=None,
    ):
        full_id = f"{plugin_id}.{command_id}"
        action = self._main_actions.get(full_id) or self._tool_actions.get(full_id)
        if action is None:
            return
        if title is not None:
            action.setText(str(title))
        if tooltip is not None:
            action.setToolTip(str(tooltip))
        if enabled is not None:
            action.setEnabled(bool(enabled))
        if checked is not None and action.isCheckable():
            action.blockSignals(True)
            action.setChecked(bool(checked))
            action.blockSignals(False)

    def create_settings_pages(self, dialog, tab_widget):
        controllers = []
        entries = sorted(
            self._settings_pages.items(),
            key=lambda item: (item[1][1].order, item[1][1].title, item[0]),
        )
        for full_id, (plugin_id, page) in entries:
            try:
                controller = page.factory(dialog)
                widget = getattr(controller, "widget", controller)
                if not isinstance(widget, QtWidgets.QWidget):
                    raise TypeError("设置页工厂必须返回 QWidget 或含 widget 的控制器。")
                tab_widget.addTab(widget, page.title)
                controllers.append((plugin_id, full_id, controller))
            except Exception as error:
                self._report_error(plugin_id, page.title, error)
        self._settings_controllers = controllers
        return list(controllers)

    def load_settings_pages(self, config):
        for plugin_id, full_id, controller in self._settings_controllers:
            loader = getattr(controller, "load_config", None)
            if loader is not None:
                try:
                    loader(config)
                except Exception as error:
                    self._report_error(plugin_id, full_id, error)

    def settings_hotkey_fields(self):
        fields = []
        for _plugin_id, _full_id, controller in self._settings_controllers:
            provider = getattr(controller, "hotkey_fields", None)
            if provider is not None:
                fields.extend(provider() or ())
        return fields

    def validate_settings_pages(self):
        for plugin_id, full_id, controller in self._settings_controllers:
            validator = getattr(controller, "validate", None)
            if validator is not None:
                try:
                    validator()
                except Exception as error:
                    widget = getattr(controller, "widget", None)
                    return plugin_id, full_id, widget, str(error)
        return None

    def update_settings_config(self, config):
        for plugin_id, full_id, controller in self._settings_controllers:
            updater = getattr(controller, "update_config", None)
            if updater is not None:
                try:
                    updater(config)
                except Exception as error:
                    self._report_error(plugin_id, full_id, error)
                    raise
        return config

    def start_all(self):
        for plugin_id, plugin in self._plugins.items():
            try:
                starter = getattr(plugin, "start", None)
                if starter is not None:
                    starter()
            except Exception as error:
                self._report_error(plugin_id, "启动", error)

    def apply_settings(self, config):
        results = []
        for plugin_id, plugin in self._plugins.items():
            try:
                callback = getattr(plugin, "apply_settings", None)
                result = True if callback is None else bool(callback(config))
            except Exception as error:
                self._report_error(plugin_id, "应用设置", error)
                result = False
            results.append((plugin_id, result))
        return results

    def update_runtime_config(self, config):
        config[PLUGIN_MAIN_WIDGET_ORDER_CONFIG_KEY] = self.main_widget_order()
        for plugin_id, plugin in self._plugins.items():
            try:
                callback = getattr(plugin, "update_config", None)
                if callback is not None:
                    callback(config)
            except Exception as error:
                self._report_error(plugin_id, "保存配置", error)
                raise
        return config

    def can_close_all(self):
        for plugin_id, plugin in self._plugins.items():
            checker = getattr(plugin, "can_close", None)
            if checker is None:
                continue
            try:
                allowed, message = checker()
            except Exception as error:
                self._report_error(plugin_id, "退出检查", error)
                return False, "插件退出检查失败，请查看程序日志。"
            if not allowed:
                return False, str(message or "插件仍有任务正在运行。")
        return True, ""

    def stop_all(self):
        for plugin_id, plugin in reversed(self._plugins.items()):
            try:
                stopper = getattr(plugin, "stop", None)
                if stopper is not None:
                    stopper()
            except Exception as error:
                self._report_error(plugin_id, "停止", error)

    def _report_error(self, plugin_id, operation, error):
        self.main_window.appendLog(
            f"[插件/{plugin_id}] {operation}失败：{error}",
            end="",
            level=logging.ERROR,
        )

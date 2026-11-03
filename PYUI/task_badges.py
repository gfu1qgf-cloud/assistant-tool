"""Host-owned task chips. No disk/network reads while painting or filtering."""
import html
import os

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.api import TaskBadge

DIRECTORY_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 310
COLORS = {'success': ('#DCF6E6', '#15603A'), 'warning': ('#FFF0C7', '#805300'),
          'error': ('#FCE2E2', '#A22020'), 'info': ('#DFEEFF', '#184F8A'), 'muted': ('#ECEFF3', '#475569')}


def task_badge_key(path): return os.path.normcase(os.path.abspath(str(path)))


class TaskBadgeDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, controller): super().__init__(controller.table); self.controller = controller
    def badges(self, index): return self.controller.badges(index.data(DIRECTORY_ROLE))
    def widths(self, option, badges): return [option.fontMetrics.horizontalAdvance(badge.text) + 16 for badge in badges]
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index); badges = self.badges(index)
        if badges: size.setWidth(size.width() + sum(self.widths(option, badges)) + 6 * len(badges) + 6)
        return size
    def paint(self, painter, option, index):
        badges = self.badges(index)
        if not badges: return super().paint(painter, option, index)
        widths = self.widths(option, badges); total = sum(widths) + 6 * len(badges)
        base = QtWidgets.QStyleOptionViewItem(option)
        base.rect.setRight(max(base.rect.left(), base.rect.right() - total - 4))
        # Paint the full original row/selection background first, then the
        # ordinary text and separate opaque chips. Never recolor item data.
        background = QtWidgets.QStyleOptionViewItem(option); self.initStyleOption(background, index); background.text = ''
        (option.widget.style() if option.widget else QtWidgets.QApplication.style()).drawControl(
            QtWidgets.QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget)
        super().paint(painter, base, index)
        painter.save(); painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        x = max(option.rect.left(), option.rect.right() - total)
        height = min(option.rect.height() - 4, option.fontMetrics.height() + 6)
        for badge, width in zip(badges, widths):
            fill, text = COLORS[badge.tone]
            rect = QtCore.QRectF(x, option.rect.center().y() - height / 2, width, height)
            painter.setPen(QtCore.Qt.PenStyle.NoPen); painter.setBrush(QtGui.QColor(fill)); painter.drawRoundedRect(rect, 5, 5)
            painter.setPen(QtGui.QColor(text)); painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, badge.text)
            x += width + 6
        painter.restore()
    def helpEvent(self, event, view, option, index):
        badges = self.badges(index)
        if not badges: return super().helpEvent(event, view, option, index)
        parts = [str(index.data(QtCore.Qt.ItemDataRole.ToolTipRole) or ''), *[badge.tooltip for badge in badges]]
        QtWidgets.QToolTip.showText(event.globalPos(), '<br>'.join(html.escape(text).replace('\n', '<br>') for text in parts if text), view)
        return True


class TaskBadgeController(QtCore.QObject):
    def __init__(self, window, enabled):
        super().__init__(window)
        self.window, self.table, self.enabled = window, window.task_table_widget, enabled
        self.layers, self.filters, self.hidden_rows, self.base_hidden = {}, set(), set(), {}
        self.original_delegate = self.table.itemDelegateForColumn(0)
        self.delegate = TaskBadgeDelegate(self); self.table.setItemDelegateForColumn(0, self.delegate)
        self.table.horizontalHeader().sortIndicatorChanged.connect(lambda *_args: self.refresh())
        self.bind_project(getattr(window, 'loaded_project_dir', None))

    def publish(self, owner, mapping):
        checked = {}
        for directory, badge in dict(mapping).items():
            if (not isinstance(directory, str) or not os.path.isabs(directory) or not isinstance(badge, TaskBadge)
                    or not badge.text or len(badge.text) > 40 or badge.tone not in COLORS
                    or len(badge.tooltip) > 3000 or type(badge.unread) is not bool):
                raise ValueError('任务标记必须使用绝对任务目录、TaskBadge和有效的短文字／颜色。')
            checked[task_badge_key(directory)] = badge
        if self.layers.get(owner) != checked:
            self.layers[owner] = checked; self.refresh()

    def bind_project(self, project):
        from model.TaskReferenceDownloader import task_directory_for
        for row in self.hidden_rows:
            if row < self.table.rowCount(): self.table.setRowHidden(row, False)
        self.hidden_rows.clear(); self.base_hidden.clear()
        tasks = getattr(self.window, 'task_list', [])
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None: continue
            key = ''
            if project is not None and row < len(tasks):
                try: key = task_badge_key(task_directory_for(project, tasks[row].task_type, tasks[row].task_id))
                except (ValueError, AttributeError): pass
            item.setData(DIRECTORY_ROLE, key)
        self.refresh()

    def badges(self, key):
        return [layer[key] for owner, layer in sorted(self.layers.items()) if self.enabled(owner) and key in layer][:4]

    def keys(self, rows):
        return [str(self.table.item(row, 0).data(DIRECTORY_ROLE) or '') for row in rows
                if 0 <= row < self.table.rowCount() and self.table.item(row, 0)]

    def set_filter(self, owner, active):
        if active: self.filters.add(owner)
        else: self.filters.discard(owner)
        self.refresh()

    def clear(self, owner):
        self.layers.pop(owner, None); self.filters.discard(owner); self.refresh()

    def refresh(self):
        active = {owner for owner in self.filters if self.enabled(owner)}
        for row in self.hidden_rows:
            if row < self.table.rowCount(): self.table.setRowHidden(row, False)
        self.hidden_rows.clear()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None: continue
            key = item.data(DIRECTORY_ROLE)
            if active and key not in self.base_hidden: self.base_hidden[key] = self.table.isRowHidden(row)
            eligible = any(self.layers.get(owner, {}).get(key, TaskBadge('')).unread for owner in active)
            if active:
                self.table.setRowHidden(row, bool(self.base_hidden.get(key, False)) or not eligible)
            elif key in self.base_hidden:
                self.table.setRowHidden(row, self.base_hidden[key])
            if active and not eligible:
                self.hidden_rows.add(row)
                self.table.selectionModel().select(self.table.model().index(row, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.Deselect | QtCore.QItemSelectionModel.SelectionFlag.Rows)
        if not active: self.base_hidden.clear()
        self.table.resizeColumnToContents(0); self.table.viewport().update()

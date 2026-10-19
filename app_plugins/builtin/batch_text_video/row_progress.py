"""Progress painted across a table row without embedded progress widgets."""
import math
from qt_compat import QtCore, QtGui, QtWidgets


PROGRESS_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 41


class RowProgressDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter, option, index):
        value = index.siblingAtColumn(0).data(PROGRESS_ROLE)
        view = self.parent()
        item = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        selected = bool(item.state & QtWidgets.QStyle.StateFlag.State_Selected)
        base = item.palette.color(QtGui.QPalette.ColorRole.Base)
        accent = item.palette.color(QtGui.QPalette.ColorRole.Highlight)
        fill = QtGui.QColor('#dcecf8' if value is None or value < 100 else '#e0f1e7')
        if base.lightnessF() < .5:
            fill = QtGui.QColor('#294657' if value is None or value < 100 else '#294c40')
        painter.save()
        painter.fillRect(option.rect, base)
        progress = QtCore.QRect(0, option.rect.top(),
            round(view.viewport().width() * (value or 0) / 100), option.rect.height())
        painter.fillRect(option.rect.intersected(progress), fill)
        if selected:
            accent.setAlpha(40)
            painter.fillRect(option.rect, accent)
        text_rect = option.rect.adjusted(6, 0, -6, 0)
        if item.features & QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            style=view.style()
            check=QtWidgets.QStyleOptionButton()
            check.rect=style.subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemCheckIndicator,item,view)
            check.state=item.state & QtWidgets.QStyle.StateFlag.State_Enabled
            check.state|={QtCore.Qt.CheckState.Checked:QtWidgets.QStyle.StateFlag.State_On,
                          QtCore.Qt.CheckState.Unchecked:QtWidgets.QStyle.StateFlag.State_Off,
                          QtCore.Qt.CheckState.PartiallyChecked:QtWidgets.QStyle.StateFlag.State_NoChange}[item.checkState]
            style.drawPrimitive(QtWidgets.QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,check,painter,view)
            text_rect.setLeft(check.rect.right()+5)
        painter.setFont(item.font)
        painter.setPen(item.palette.color(QtGui.QPalette.ColorRole.Text))
        text = item.fontMetrics.elidedText(item.text, QtCore.Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, item.displayAlignment | QtCore.Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


def install_row_progress(table):
    table.setItemDelegate(RowProgressDelegate(table))
    table.verticalHeader().setDefaultSectionSize(28)
    table.setAlternatingRowColors(False)


def set_row_progress(table, row, percent, *, column=2):
    item = table.item(row, 0)
    if item is None:
        return
    value = None if percent is None else max(0, min(100, float(percent)))
    item.setData(PROGRESS_ROLE, value)
    label = table.item(row, column)
    if label is None:
        label = QtWidgets.QTableWidgetItem()
        table.setItem(row, column, label)
    # Integer labels; keep precise fill, and reserve 100% for actual completion.
    display = None if value is None else 100 if value >= 100 else min(99, math.ceil(value))
    label.setText('—' if display is None else f'{display}%')
    label.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    rect = table.visualItemRect(item)
    table.viewport().update(QtCore.QRect(0, rect.top(), table.viewport().width(), rect.height()))

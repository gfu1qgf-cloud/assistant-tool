"""Scrollable status text with a stable footprint in its surrounding layout."""
from qt_compat import QtCore, QtWidgets


def status_area(label, lines=2):
    area = QtWidgets.QScrollArea(label.parentWidget())
    area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
    area.setFixedHeight(label.fontMetrics().lineSpacing() * lines + 4)
    label.setMinimumWidth(0)
    label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)
    label.setWordWrap(True)
    label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
    label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    area.setWidget(label)
    return area

"""Compact paired fields; presentation only, no second property model."""
from qt_compat import QtWidgets


class FieldPair(QtWidgets.QWidget):
    def __init__(self, fields):
        super().__init__()
        self.labels = {}
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(6)
        for key, title, control in fields:
            label = QtWidgets.QLabel(title); label.setBuddy(control)
            self.labels[key] = label
            control.setMinimumWidth(0)
            control.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            layout.addWidget(label); layout.addWidget(control, 1)


def pair_form_fields(form, title, fields):
    """Move existing bound controls together, retaining their original signals."""
    index = min(form.getWidgetPosition(control)[0] for _, _, control in fields)
    for _, _, control in fields:
        row = form.takeRow(control)
        if row.labelItem:
            label = row.labelItem.widget(); label.hide(); label.deleteLater()
    pair = FieldPair(fields)
    form.insertRow(index, title, pair)
    return pair


def order_form_fields(form, preferred):
    rows = [form.takeRow(0) for _ in range(form.rowCount())]
    ordered = []
    for field in preferred:
        for row in rows:
            actual = row.fieldItem.widget() or row.fieldItem.layout()
            if actual is field:
                ordered.append(row); rows.remove(row); break
    for row in ordered + rows:
        field = row.fieldItem.widget() or row.fieldItem.layout()
        if row.labelItem: form.addRow(row.labelItem.widget(), field)
        else: form.addRow(field)

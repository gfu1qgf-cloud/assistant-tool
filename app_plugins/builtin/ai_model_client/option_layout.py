"""One aligned, responsive grid for all chat controls; never recreate values."""
from qt_compat import QtCore,QtWidgets


def inline_field(parent,*widgets,width=260):
    container=QtWidgets.QWidget(parent)
    layout=QtWidgets.QHBoxLayout(container);layout.setContentsMargins(0,0,0,0);layout.setSpacing(8)
    layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter)
    for widget in widgets:
        if isinstance(widget,QtWidgets.QCheckBox):
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum,QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(widget)
    if width is not None:container.setFixedWidth(width)
    return container


class OptionsGrid(QtWidgets.QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.fields=[];self.pairs=1
        self.viewport=None
        self.grid=QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(4,4,8,4)
        self.grid.setHorizontalSpacing(10);self.grid.setVerticalSpacing(8)
        self.grid.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.grid.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)

    def bind_viewport(self,viewport):
        self.viewport=viewport;viewport.installEventFilter(self);self.reflow()

    def minimumSizeHint(self):
        hint=super().minimumSizeHint()
        # A two-column layout must not force a horizontal scrollbar before it
        # gets the opportunity to reflow to the viewport's narrower width.
        return QtCore.QSize(0,hint.height())

    def add_field(self,title,field,*,wide=False,top=False):
        label=QtWidgets.QLabel(title,self)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight|
                           (QtCore.Qt.AlignmentFlag.AlignTop if top else QtCore.Qt.AlignmentFlag.AlignVCenter))
        label.setBuddy(field)
        self.fields.append((label,field,wide,top))
        self.reflow()

    def reflow(self):
        label_width=max([80,*[label.fontMetrics().horizontalAdvance(label.text())+4
                              for label,_field,_wide,_top in self.fields]])
        width=self.viewport.width() if self.viewport is not None else self.width()
        self.pairs=2 if width>=max(650,2*(label_width+260)+40) else 1
        while self.grid.count():self.grid.takeAt(0)
        for column in range(4):
            self.grid.setColumnStretch(column,1 if column in range(1,self.pairs*2,2) else 0)
            self.grid.setColumnMinimumWidth(column,0)
        row=pair=0
        for label,field,wide,top in self.fields:
            label.setFixedWidth(label_width)
            if wide:
                if pair:row+=1;pair=0
                self.grid.addWidget(label,row,0,alignment=QtCore.Qt.AlignmentFlag.AlignTop if top else QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.grid.addWidget(field,row,1,1,self.pairs*2-1)
                row+=1
            else:
                self.grid.addWidget(label,row,pair*2)
                self.grid.addWidget(field,row,pair*2+1,alignment=QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter)
                pair+=1
                if pair==self.pairs:row+=1;pair=0

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'grid'):
            old=self.pairs
            label_width=max([80,*[label.width() for label,_field,_wide,_top in self.fields]])
            width=self.viewport.width() if self.viewport is not None else self.width()
            target=2 if width>=max(650,2*(label_width+260)+40) else 1
            if target!=old:self.reflow()

    def eventFilter(self,watched,event):
        if watched is self.viewport and event.type()==QtCore.QEvent.Type.Resize:
            self.reflow()
        return super().eventFilter(watched,event)

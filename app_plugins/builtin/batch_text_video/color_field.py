"""One native split colour control; layout, focus and popup belong together."""
from qt_compat import QtCore,QtGui,QtWidgets


class ColorField(QtWidgets.QToolButton):
    textChanged=QtCore.pyqtSignal(str)

    def __init__(self,text='',parent=None):
        super().__init__(parent)
        self.rule_source=lambda:None
        self.setText(text)
        self.setAutoRaise(False)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setIconSize(QtCore.QSize(24,18))
        self.setMinimumHeight(28)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def setAutoDefault(self,value):
        pass  # Common field API; tool buttons never submit a containing dialog.

    def autoDefault(self):
        return False

    def setText(self,text):
        changed=text!=self.text()
        super().setText(text)
        color=QtGui.QColor(text)
        if color.isValid():
            swatch=QtGui.QPixmap(24,18);swatch.fill(color);self.setIcon(QtGui.QIcon(swatch))
        if changed:self.textChanged.emit(text)

    def display_text(self):
        rule=self.rule_source()
        if not rule:return self.text()
        return ('套餐 · '+rule.get('name','自选颜色')) if rule['mode']=='palette' else '同色系随机'

    def paintEvent(self,event):
        option=QtWidgets.QStyleOptionToolButton();self.initStyleOption(option)
        rule=self.rule_source()
        if rule and rule.get('colors'):
            from .palette_ui import swatches
            option.icon=swatches(rule['colors']);option.iconSize=QtCore.QSize(32,18)
        available=max(10,self.width()-option.iconSize.width()-44)
        option.text=self.fontMetrics().elidedText(self.display_text(),QtCore.Qt.TextElideMode.ElideRight,available)
        painter=QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ToolButton,option)

    def sizeHint(self):
        hint=super().sizeHint();hint.setWidth(max(140,min(240,hint.width()+28)));return hint

    def minimumSizeHint(self):
        return QtCore.QSize(110,28)

    def set_color_menu(self,fixed,family,palette,manage):
        menu=QtWidgets.QMenu(self)
        for title,slot in (('选择固定颜色…',fixed),('同色系随机…',family),('从颜色套餐中随机…',palette)):
            menu.addAction(title,slot)
        choices=menu.actions()
        for action in choices:action.setCheckable(True)
        def show_mode():
            mode=(self.rule_source() or {}).get('mode','fixed')
            for action,key in zip(choices,('fixed','family','palette')):action.setChecked(mode==key)
        menu.aboutToShow.connect(show_mode)
        menu.addSeparator();menu.addAction('管理我的颜色套餐…',manage)
        self.setMenu(menu)
        self.setToolTip('点击色块选择固定颜色；点击右侧箭头设置随机配色或颜色套餐。')

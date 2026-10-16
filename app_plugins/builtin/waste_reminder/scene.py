"""Quiet local weather scenery and one readable theme for the life dashboard."""
from qt_compat import QtCore, QtGui, QtWidgets
from .weather import local_time, location_key


def forecast_for_day(data, location, day):
    if not data or not location or location_key(data["location"]) != location_key(location):
        return None
    selected = day if isinstance(day, QtCore.QDate) else QtCore.QDate(day.year, day.month, day.day)
    return next((row for row in data["daily"]
                 if local_time(row["time"], location["timezone"]).date() == selected), None)


def weather_scene(code):
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "cloud_sun"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    return "neutral"


def life_style(night):
    text, muted = ("#e8eff5", "#a8bbca") if night else ("#233d50", "#5c7788")
    surface, field, border = ("rgba(22,40,56,235)", "#1d3548", "#405b6e") if night else (
        "rgba(255,255,255,232)", "#ffffff", "#d7e4ec")
    active, hover = ("#345f74", "#284a60") if night else ("#dceff3", "#edf6f9")
    hero = "rgba(22,40,56,208)" if night else "rgba(255,255,255,211)"
    return f"""
        QDialog#lifeAssistant {{ color:{text}; background:{'#142936' if night else '#eef4f6'}; }}
        QDialog#lifeAssistant QLabel, QDialog#lifeAssistant QCheckBox,
        QDialog#lifeAssistant QGroupBox {{ color:{text}; }}
        QFrame#lifeHeader {{ background:{hero}; border:1px solid {border}; border-radius:18px; }}
        QFrame#lifeBody {{ background:{surface}; border:1px solid {border}; border-radius:18px; }}
        QLabel#lifeTitle {{ font-size:24px; font-weight:700; }}
        QLabel#lifeSubtitle, QLabel#lifeFooter {{ color:{muted}; }}
        QLabel#selectedWeather {{ font-size:16px; font-weight:600; }}
        QTabWidget#lifePages::pane {{ background:{surface}; border:1px solid {border}; border-radius:15px; top:3px; }}
        QTabWidget#lifePages > QTabBar::tab {{ background:{surface}; color:{muted}; border:0;
            border-radius:9px; padding:10px 15px; margin-right:5px; margin-bottom:8px; }}
        QTabWidget#lifePages > QTabBar::tab:selected {{ background:{active}; color:{text}; font-weight:600; }}
        QTabWidget#lifePages > QTabBar::tab:hover {{ background:{hover}; color:{text}; }}
        QDialog#lifeAssistant QTabWidget::pane {{ background:{field}; border:1px solid {border}; border-radius:10px; }}
        QDialog#lifeAssistant QTabBar::tab {{ background:{field}; color:{muted}; border:0; padding:7px 12px; }}
        QDialog#lifeAssistant QTabBar::tab:selected {{ background:{active}; color:{text}; }}
        QDialog#lifeAssistant QPushButton, QDialog#lifeAssistant QToolButton {{
            color:{text}; background:{field}; border:1px solid {border}; border-radius:8px;
            padding:6px 11px; min-height:20px; }}
        QDialog#lifeAssistant QPushButton:hover, QDialog#lifeAssistant QToolButton:hover {{ background:{hover}; border-color:#87b2c3; }}
        QDialog#lifeAssistant QPushButton:disabled, QDialog#lifeAssistant QToolButton:disabled {{ color:{muted}; }}
        QPushButton#lifePrimary {{ background:{active}; border-color:#94bbc6; font-weight:600; }}
        QDialog#lifeAssistant QLineEdit, QDialog#lifeAssistant QDateEdit,
        QDialog#lifeAssistant QSpinBox, QDialog#lifeAssistant QComboBox {{
            color:{text}; background:{field}; border:1px solid {border}; border-radius:7px; padding:5px; min-height:20px; }}
        QDialog#lifeAssistant QPlainTextEdit, QDialog#lifeAssistant QTextBrowser,
        QDialog#lifeAssistant QListWidget, QDialog#lifeAssistant QTableWidget {{
            color:{text}; background:{field}; border:1px solid {border}; border-radius:10px;
            selection-background-color:{active}; selection-color:{text}; }}
        QDialog#lifeAssistant QHeaderView::section {{ background:{hover}; color:{muted};
            padding:8px 5px; border:0; border-bottom:1px solid {border}; font-weight:600; }}
        QDialog#lifeAssistant QScrollArea {{ border:0; background:transparent; }}
        QDialog#lifeAssistant QScrollArea > QWidget > QWidget {{ background:transparent; }}
    """


class ThemeWatcher(QtCore.QObject):
    changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent):
        super().__init__(parent)
        self.night = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
        QtWidgets.QApplication.instance().installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            night = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
            if night != self.night:
                self.night = night
                self.changed.emit(night)
        return False


class WeatherSceneDialog(QtWidgets.QDialog):
    """Single cached backdrop, repainted only on size/theme/weather changes.

    No animation timers, assets, network requests, or changes to forecast data.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("lifeAssistant")
        self.scene = "neutral"
        self._night = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
        self._scene_cache = None
        self.apply_life_style()
        self._theme_watcher = ThemeWatcher(self)
        # Never rebuild styles inside Qt's native palette propagation. The
        # queued QObject receiver is automatically disconnected on destruction.
        self._theme_watcher.changed.connect(self._theme_changed, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot(bool)
    def _theme_changed(self, night):
        if night != self._night:
            self.apply_life_style()
            self.update()

    def apply_life_style(self):
        if getattr(self, "_styling", False):
            return
        self._styling = True
        try:
            self._night = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
            self.setStyleSheet(life_style(self._night))
            self._scene_cache = None
        finally:
            self._styling = False

    def set_weather_scene(self, code):
        scene = weather_scene(code)
        if scene != self.scene:
            self.scene = scene
            self._scene_cache = None
            self.update()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange) and hasattr(self, "scene"):
            self._scene_cache = None
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        ratio = self.devicePixelRatioF()
        key = self.width(), self.height(), ratio, self.scene, self._night
        if self._scene_cache is None or self._scene_cache[0] != key:
            pixmap = QtGui.QPixmap(max(1, round(self.width()*ratio)), max(1, round(self.height()*ratio)))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pixmap)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            self._draw_scene(painter, self.width(), self.height(), self._night)
            painter.end()
            self._scene_cache = key, pixmap
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, self._scene_cache[1])

    def _draw_scene(self, painter, width, height, night):
        colors = {
            "sun": ("#cdebf7", "#fff0ce"), "cloud_sun": ("#d7e9f2", "#f6f0df"),
            "cloud": ("#cfdee9", "#ebf0f2"), "rain": ("#b9d4e4", "#d9e9ed"),
            "snow": ("#cfdeed", "#f3f7fb"), "storm": ("#b4c7d7", "#dce5ed"),
            "fog": ("#d5dfe3", "#eef1ec"), "neutral": ("#dce9ed", "#f0eee5"),
        }
        upper, lower = colors[self.scene]
        if night:
            upper = "#18364c" if self.scene not in ("storm", "cloud") else "#26364d"
            lower = "#142936"
        gradient = QtGui.QLinearGradient(0, 0, width*.45, height)
        gradient.setColorAt(0, QtGui.QColor(upper))
        gradient.setColorAt(1, QtGui.QColor(lower))
        painter.fillRect(QtCore.QRectF(0, 0, width, height), gradient)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        if self.scene in ("sun", "cloud_sun"):
            center = QtCore.QPointF(width*.86, height*.12)
            glow = QtGui.QRadialGradient(center, width*.23)
            glow.setColorAt(0, QtGui.QColor(255, 221, 145, 95 if night else 170))
            glow.setColorAt(1, QtGui.QColor(255, 221, 145, 0))
            painter.setBrush(glow)
            painter.drawEllipse(center, width*.23, width*.23)
            painter.setBrush(QtGui.QColor(255, 226, 164, 80 if night else 200))
            painter.drawEllipse(center, 47, 47)
        if self.scene not in ("sun", "neutral"):
            for x, y, scale in ((.08, .16, 1.1), (.79, .24, 1.7), (.36, .05, .9)):
                painter.save()
                painter.translate(width*x, height*y)
                painter.scale(scale, scale)
                painter.setBrush(QtGui.QColor(210, 225, 235, 28 if night else 115))
                cloud = QtGui.QPainterPath()
                cloud.moveTo(-90, 28)
                cloud.cubicTo(-130, 18, -109, -35, -65, -21)
                cloud.cubicTo(-48, -75, 20, -70, 37, -28)
                cloud.cubicTo(83, -48, 125, -2, 98, 27)
                cloud.closeSubpath()
                painter.drawPath(cloud)
                painter.restore()
        if self.scene in ("rain", "storm"):
            painter.setPen(QtGui.QPen(QtGui.QColor(69, 126, 158, 90 if night else 65), 1.8))
            for i in range(90):
                x, y = (i*137 % max(1, width)), (i*193 % max(1, height))
                painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x-7, y+20))
        elif self.scene == "snow":
            painter.setBrush(QtGui.QColor(255, 255, 255, 130))
            for i in range(65):
                center = QtCore.QPointF(i*131 % max(1, width), i*179 % max(1, height))
                painter.drawEllipse(center, 2+i%3, 2+i%3)
        elif self.scene == "fog":
            for i in range(4):
                painter.setBrush(QtGui.QColor(230, 240, 242, 25 if night else 65))
                painter.drawRoundedRect(QtCore.QRectF(-20, height*(.30+i*.13), width+40, 43), 21, 21)
        if self.scene == "storm":
            painter.setBrush(QtGui.QColor(255, 216, 137, 75 if night else 125))
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(width*.92, height*.20),
                QtCore.QPointF(width*.88, height*.28), QtCore.QPointF(width*.915, height*.27),
                QtCore.QPointF(width*.89, height*.35), QtCore.QPointF(width*.96, height*.24),
                QtCore.QPointF(width*.925, height*.245)]))
        # Soft landscape anchors the scene without competing with the controls.
        for level in (0, 1):
            hill = QtGui.QPainterPath(QtCore.QPointF(0, height*(.84+level*.08)))
            hill.cubicTo(width*.25, height*(.73+level*.12), width*.6, height*.99,
                         width, height*(.81+level*.09))
            hill.lineTo(width, height)
            hill.lineTo(0, height)
            hill.closeSubpath()
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(84, 135, 138, (26 if night else 28)-level*5))
            painter.drawPath(hill)

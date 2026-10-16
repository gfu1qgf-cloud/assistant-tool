"""Locally drawn weather icons and charts: no web images or extra libraries."""
import math
from qt_compat import QtCore, QtGui, QtWidgets
from .weather import CHART_SPANS, local_time, number
from .weather_physics import BallWorld


def dark(widget):
    return widget.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128


def card(parent=None, tone="blue"):
    frame = QtWidgets.QFrame(parent)
    frame.setObjectName("weatherCard")
    backgrounds = {"blue": ("#edf6ff", "#172d43"), "green": ("#eef9f3", "#18372c")}
    bg = backgrounds[tone][int(dark(frame))]
    edge = "#3d596c" if dark(frame) else "#d0e0ec"
    frame.setStyleSheet(f"QFrame#weatherCard {{background:{bg}; border:1px solid {edge}; border-radius:12px;}}")
    return frame


class WeatherGlyph(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.code = None
        self.setFixedSize(80, 74)

    def set_code(self, code):
        self.code = code
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.scale(self.width() / 80, self.height() / 74)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        if self.code is None:
            painter.setBrush(QtGui.QColor("#abc0ce"))
            painter.drawEllipse(QtCore.QRectF(10, 30, 24, 24))
            painter.drawEllipse(QtCore.QRectF(25, 16, 32, 34))
            painter.drawEllipse(QtCore.QRectF(46, 29, 25, 25))
            painter.drawRoundedRect(QtCore.QRectF(18, 34, 46, 20), 10, 10)
            return
        if self.code in {0, 1, 2}:
            center = QtCore.QPointF(35, 28)
            painter.setPen(QtGui.QPen(QtGui.QColor("#eaa83c"), 3, QtCore.Qt.PenStyle.SolidLine,
                                     QtCore.Qt.PenCapStyle.RoundCap))
            for angle in range(0, 360, 45):
                dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
                painter.drawLine(center + QtCore.QPointF(dx * 20, dy * 20), center + QtCore.QPointF(dx * 25, dy * 25))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#f8c65a"))
            painter.drawEllipse(center, 15, 15)
            if self.code == 0:
                return
        cloud = QtGui.QPainterPath()
        cloud.moveTo(20, 48)
        cloud.cubicTo(3, 48, 4, 29, 19, 29)
        cloud.cubicTo(22, 10, 48, 10, 52, 30)
        cloud.cubicTo(71, 27, 76, 48, 59, 49)
        cloud.closeSubpath()
        painter.setBrush(QtGui.QColor("#87aec8" if dark(self) else "#a6c4d9"))
        painter.drawPath(cloud)
        if self.code in {45, 48}:
            painter.setPen(QtGui.QPen(QtGui.QColor("#87a2b4"), 3, QtCore.Qt.PenStyle.SolidLine,
                                     QtCore.Qt.PenCapStyle.RoundCap))
            painter.drawLine(QtCore.QPointF(17, 57), QtCore.QPointF(64, 57))
            painter.drawLine(QtCore.QPointF(23, 65), QtCore.QPointF(58, 65))
        elif self.code in {71, 73, 75, 77, 85, 86}:
            painter.setPen(QtGui.QColor("#5e9bbb"))
            for x in (23, 40, 57):
                painter.drawText(QtCore.QRectF(x-6, 50, 14, 20), QtCore.Qt.AlignmentFlag.AlignCenter, "✦")
        elif self.code >= 51:
            painter.setPen(QtGui.QPen(QtGui.QColor("#4897cf"), 3, QtCore.Qt.PenStyle.SolidLine,
                                     QtCore.Qt.PenCapStyle.RoundCap))
            for x in (23, 39, 55):
                painter.drawLine(QtCore.QPointF(x, 55), QtCore.QPointF(x-3, 63))
            if self.code >= 95:
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor("#f1b647"))
                painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(44, 43), QtCore.QPointF(34, 57),
                    QtCore.QPointF(43, 56), QtCore.QPointF(36, 71), QtCore.QPointF(56, 51), QtCore.QPointF(46, 51)]))


def weather_icon(code, palette):
    """Use the same local vector glyph in the daily list, on a transparent tile."""
    glyph = WeatherGlyph()
    glyph.setPalette(palette)
    glyph.setFixedSize(32, 30)
    glyph.set_code(code)
    pixmap = QtGui.QPixmap(glyph.size())
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    glyph.render(pixmap, flags=QtWidgets.QWidget.RenderFlag.DrawChildren)
    glyph.deleteLater()
    return QtGui.QIcon(pixmap)


class ForecastChart(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.zone, self.start = [], "Europe/Rome", 0
        self._all_rows, self.span_hours = [], 24
        self.world = BallWorld()
        self._clock = QtCore.QElapsedTimer()
        self._animation = QtCore.QTimer(self)
        self._animation.setInterval(16)
        self._animation.timeout.connect(self._animate)
        self.setMinimumHeight(215)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def set_forecast(self, data, now):
        self.start = int(now.timestamp()) // 3600 * 3600
        self.zone = data["location"]["timezone"] if data else "Europe/Rome"
        self._all_rows = data["hourly"] if data else []
        self._select_rows()

    def set_span(self, hours):
        hours = int(hours)
        if hours not in dict(CHART_SPANS):
            raise ValueError("不支持的图表时间跨度")
        if hours != self.span_hours:
            self.span_hours = hours
            self._select_rows()

    def _select_rows(self):
        self.rows = sorted((row for row in self._all_rows
                            if self.start <= row["time"] < self.start + self.span_hours * 3600),
                           key=lambda row: row["time"])
        self._clear_animation()
        self.update()

    def coverage_text(self):
        if not self.rows:
            return "当前缓存没有所选范围的数据，请刷新天气。"
        last = max(row["time"] for row in self.rows)
        message = "逐小时预报；悬停查看温度与降水。"
        if last < self.start + (self.span_hours - 1) * 3600:
            message += " 数据仅到 " + local_time(last, self.zone).toString("MM-dd HH:mm") + "，灰色区域暂无预报。"
        if self.span_hours >= 336:
            message += " 远期预报不确定性较高，仅供参考。"
        return message

    def _plot(self):
        return QtCore.QRectF(42, 32, max(1, self.width() - 88), max(1, self.height() - 78))

    def _x(self, row):
        plot = self._plot()
        return plot.left() + (row["time"] - self.start) / ((self.span_hours - 1) * 3600) * plot.width()

    def _temperature_bounds(self):
        values = [row["temperature_2m"] for row in self.rows if row["temperature_2m"] is not None]
        low = math.floor((min(values) - 1) / 5) * 5 if values else 0
        high = math.ceil((max(values) + 1) / 5) * 5 if values else 20
        return low, max(low + 5, high)

    def _curve_runs(self):
        plot = self._plot()
        low, high = self._temperature_bounds()
        runs, run, previous = [], [], None
        for row in self.rows:
            value = row["temperature_2m"]
            if value is None or (previous is not None and row["time"] - previous > 3600):
                if run:
                    runs.append(run)
                run = []
            if value is not None:
                run.append((self._x(row), plot.bottom() - (value - low) / (high - low) * plot.height()))
            previous = row["time"]
        if run:
            runs.append(run)
        return runs

    def _bounds(self):
        plot = self._plot()
        return plot.left(), plot.top(), plot.right(), plot.bottom()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        text = self.palette().color(QtGui.QPalette.ColorRole.WindowText)
        grid = QtGui.QColor("#395366" if dark(self) else "#d5e2ec")
        temperature = QtGui.QColor("#f59a72" if dark(self) else "#bd542f")
        rain = QtGui.QColor("#73b9e9" if dark(self) else "#3587bb")
        painter.setPen(temperature)
        painter.drawText(10, 18, "● 温度（左轴）")
        painter.setPen(rain)
        painter.drawText(150, 18, "● 降雨概率（右轴）")
        if not self.rows:
            painter.setPen(text)
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "暂无所选范围预报；旧缓存不足时请刷新。")
            return
        plot = self._plot()
        low, high = self._temperature_bounds()
        last = max(row["time"] for row in self.rows)
        if last < self.start + (self.span_hours - 1) * 3600:
            begin = min(plot.right(), self._x({"time": last + 3600}))
            missing = QtCore.QRectF(begin, plot.top(), plot.right() - begin, plot.height())
            fill = QtGui.QColor(text)
            fill.setAlpha(22)
            painter.fillRect(missing, fill)
            painter.setPen(text)
            if missing.width() > 70:
                painter.drawText(missing, QtCore.Qt.AlignmentFlag.AlignCenter, "暂无预报")
        for fraction in (0, .5, 1):
            y = plot.bottom() - fraction * plot.height()
            painter.setPen(QtGui.QPen(grid, 1, QtCore.Qt.PenStyle.DashLine))
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(text)
            painter.drawText(QtCore.QRectF(0, y-8, 38, 18), QtCore.Qt.AlignmentFlag.AlignRight,
                             f"{low + fraction * (high-low):g}°")
            painter.drawText(QtCore.QRectF(plot.right()+5, y-8, 42, 18), QtCore.Qt.AlignmentFlag.AlignLeft,
                             f"{fraction*100:g}%")
        painter.save()
        painter.setClipRect(plot.adjusted(-1, -4, 1, 1))
        for row in self.rows:
            chance = row["precipitation_probability"]
            if chance is None:
                continue
            height = max(0, min(100, chance)) / 100 * plot.height()
            fill = QtGui.QColor(rain)
            fill.setAlpha(80)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            width = max(.6, min(20, plot.width() / max(28, self.span_hours) * .85))
            painter.drawRoundedRect(QtCore.QRectF(self._x(row)-width/2, plot.bottom()-height, width, height), 2, 2)
        # Draw separate segments: missing temperatures must never be invented.
        path = QtGui.QPainterPath()
        runs = self._curve_runs()
        for run in runs:
            path.moveTo(QtCore.QPointF(*run[0]))
            for point in run[1:]:
                path.lineTo(QtCore.QPointF(*point))
        painter.setPen(QtGui.QPen(temperature, 2.5))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setBrush(temperature)
        if self.span_hours <= 72:
            for run in runs:
                for point in run:
                    painter.drawEllipse(QtCore.QPointF(*point), 2, 2)
        self._paint_toy(painter)
        painter.restore()
        painter.setPen(text)
        ticks = (0, 6, 12, 18, 23) if self.span_hours == 24 else (
            0, (self.span_hours - 1) // 4, (self.span_hours - 1) // 2,
            (self.span_hours - 1) * 3 // 4, self.span_hours - 1)
        for hour in ticks:
            x = plot.left() + hour / (self.span_hours - 1) * plot.width()
            stamp = self.start + hour*3600
            painter.drawText(QtCore.QRectF(x-38, plot.bottom()+5, 76, 38), QtCore.Qt.AlignmentFlag.AlignHCenter,
                             local_time(stamp, self.zone).toString("HH:mm" if self.span_hours == 24 else "MM-dd\nHH:mm"))

    def mouseMoveEvent(self, event):
        if not self.rows or not self._plot().contains(event.position()):
            QtWidgets.QToolTip.hideText()
            return
        row = min(self.rows, key=lambda value: abs(self._x(value) - event.position().x()))
        stamp = self.start + (event.position().x() - self._plot().left()) / self._plot().width() * (self.span_hours - 1) * 3600
        if abs(row["time"] - stamp) > 3600:
            QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), "这个时段暂无预报数据；请刷新或缩短时间跨度。", self)
            return
        text = (local_time(row["time"], self.zone).toString("MM-dd HH:mm")
                + f"\n温度 {number(row['temperature_2m'], '℃')}"
                + f"\n降雨概率 {number(row['precipitation_probability'], '%')}"
                + f"\n降水 {number(row['precipitation'], 'mm')}")
        QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton and self.rows and self._plot().contains(event.position()):
            if self.world.spawn(event.position().x(), event.position().y(), self._bounds()):
                if not self._animation.isActive():
                    self._clock.start()
                    self._animation.start()
                self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def _animate(self):
        elapsed = self._clock.restart() / 1000 if self._clock.isValid() else .016
        segments = [pair for run in self._curve_runs() for pair in zip(run, run[1:])]
        self.world.advance(elapsed, segments, self._bounds())
        if not self.world.active():
            self._animation.stop()
        self.update()

    def _paint_toy(self, painter):
        for spark in self.world.sparks:
            color = QtGui.QColor("#ffbe58" if dark(self) else "#df6d36")
            color.setAlphaF(max(0, min(1, spark.life / spark.total)))
            painter.setPen(QtGui.QPen(color, 2, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap))
            painter.drawLine(QtCore.QPointF(spark.x, spark.y), QtCore.QPointF(spark.x - spark.vx * .025, spark.y - spark.vy * .025))
        for ball in self.world.balls:
            gradient = QtGui.QRadialGradient(QtCore.QPointF(ball.x - 2, ball.y - 3), ball.radius * 1.8)
            gradient.setColorAt(0, QtGui.QColor("#fff8df"))
            gradient.setColorAt(.5, QtGui.QColor("#ffc85d"))
            gradient.setColorAt(1, QtGui.QColor("#d78032"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#ae6930"), 1))
            painter.setBrush(QtGui.QBrush(gradient))
            painter.drawEllipse(QtCore.QPointF(ball.x, ball.y), ball.radius, ball.radius)
            painter.setPen(QtGui.QPen(QtGui.QColor("#a66a38"), 1))
            painter.drawLine(QtCore.QPointF(ball.x, ball.y), QtCore.QPointF(
                ball.x + math.cos(ball.angle) * ball.radius * .7,
                ball.y + math.sin(ball.angle) * ball.radius * .7))

    def _clear_animation(self):
        self._animation.stop()
        self.world.clear()

    def resizeEvent(self, event):
        self._clear_animation()
        super().resizeEvent(event)

    def hideEvent(self, event):
        self._clear_animation()
        super().hideEvent(event)

    def leaveEvent(self, event):
        QtWidgets.QToolTip.hideText()
        super().leaveEvent(event)

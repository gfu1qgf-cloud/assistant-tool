import functools
import logging
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from model.OfflineMode import is_offline
from .weather import CHART_SPANS, PRIVACY, LABELS, number, local_time, location_key, tonight_summary
from .weather_visuals import WeatherGlyph, ForecastChart, card, weather_icon, dark


def safe_action(function):
    @functools.wraps(function)
    def wrapped(self, *args):
        try:
            return function(self, *args)
        except Exception as error:
            self.message.setText(str(error))
            self.plugin.context.log("天气面板操作失败：\n" + traceback.format_exc(), level=logging.ERROR)
    return wrapped


class WeatherTab(QtWidgets.QWidget):
    daySelected = QtCore.pyqtSignal(object)
    forecastChanged = QtCore.pyqtSignal()

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin, self.loading = plugin, False
        self._weather_icons = {}
        self.selected_day = QtCore.QDate()
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setSpacing(10)
        heading = QtWidgets.QHBoxLayout()
        heading_icon = WeatherGlyph()
        heading_icon.set_code(0)
        heading_icon.setFixedSize(30, 28)
        heading.addWidget(heading_icon)
        title = QtWidgets.QLabel("天气预报")
        title.setStyleSheet("font-size:19px; font-weight:600;")
        heading.addWidget(title)
        heading.addStretch(1)
        self.update_button = QtWidgets.QPushButton("刷新天气")
        self.update_button.clicked.connect(lambda: plugin.refresh_weather(manual=True))
        heading.addWidget(self.update_button)
        layout.addLayout(heading)
        self.enabled = QtWidgets.QCheckBox("启用城市天气（不上传我的素材；需联网）")
        self.enabled.setToolTip(PRIVACY)
        layout.addWidget(self.enabled)
        setting_toggle = QtWidgets.QToolButton()
        setting_toggle.setText("城市 / 自动刷新 / 隐私说明")
        setting_toggle.setCheckable(True)
        setting_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        setting_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        layout.addWidget(setting_toggle)
        settings = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings)
        settings_layout.setContentsMargins(6, 0, 6, 0)
        settings.setVisible(False)
        setting_toggle.toggled.connect(settings.setVisible)
        setting_toggle.toggled.connect(lambda shown: setting_toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if shown else QtCore.Qt.ArrowType.RightArrow))
        layout.addWidget(settings)
        privacy = QtWidgets.QLabel(PRIVACY)
        privacy.setWordWrap(True)
        settings_layout.addWidget(privacy)
        search = QtWidgets.QHBoxLayout()
        self.city = QtWidgets.QLineEdit()
        self.city.setMaxLength(80)
        self.city.setPlaceholderText("意大利城市名，不要输入详细住址")
        self.city.setText((plugin.settings["weather"].get("location") or {}).get("name", ""))
        self.search_button = QtWidgets.QPushButton("搜索城市")
        self.search_button.clicked.connect(self.search)
        self.city.returnPressed.connect(self.search)
        search.addWidget(self.city, 1)
        search.addWidget(self.search_button)
        settings_layout.addLayout(search)
        results = QtWidgets.QHBoxLayout()
        self.cities = QtWidgets.QComboBox()
        self.cities.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.use_button = QtWidgets.QPushButton("使用该城市")
        self.use_button.clicked.connect(self.use_city)
        results.addWidget(self.cities, 1)
        results.addWidget(self.use_button)
        settings_layout.addLayout(results)
        self.location = QtWidgets.QLabel()
        self.location.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.location.setWordWrap(True)
        layout.addWidget(self.location)
        controls = QtWidgets.QHBoxLayout()
        self.auto = QtWidgets.QCheckBox("自动刷新")
        self.interval = QtWidgets.QSpinBox()
        self.interval.setRange(30, 720)
        self.interval.setSuffix(" 分钟")
        controls.addWidget(self.auto)
        controls.addWidget(self.interval)
        controls.addStretch(1)
        settings_layout.addLayout(controls)
        self.message = QtWidgets.QLabel()
        self.message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        current_card = card()
        current_layout = QtWidgets.QHBoxLayout(current_card)
        current_layout.setContentsMargins(14, 10, 14, 10)
        self.glyph = WeatherGlyph()
        current_layout.addWidget(self.glyph)
        self.current = QtWidgets.QLabel()
        self.current.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.current.setWordWrap(True)
        self.current.setStyleSheet("font-size:16px;")
        current_layout.addWidget(self.current, 1)
        layout.addWidget(current_card)
        tonight_card = card(tone="green")
        tonight_layout = QtWidgets.QVBoxLayout(tonight_card)
        tonight_layout.setContentsMargins(14, 10, 14, 10)
        tonight_title = QtWidgets.QLabel("🌙 今晚出门投放")
        tonight_title.setStyleSheet("font-weight:600;")
        tonight_layout.addWidget(tonight_title)
        self.tonight = QtWidgets.QLabel()
        self.tonight.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.tonight.setWordWrap(True)
        tonight_layout.addWidget(self.tonight)
        layout.addWidget(tonight_card)
        self.chart = ForecastChart()
        chart_heading = QtWidgets.QHBoxLayout()
        chart_title = QtWidgets.QLabel("📈 天气走势")
        chart_title.setStyleSheet("font-weight:600;")
        chart_heading.addWidget(chart_title)
        chart_heading.addStretch(1)
        self.chart_range = QtWidgets.QComboBox()
        for hours, title in CHART_SPANS:
            self.chart_range.addItem(title, hours)
        self.chart_range.setToolTip("普通天气接口最多16天预报；1个月视图的其余日期明确留空，不外推。中键点击图表有小彩蛋。")
        self.chart_range.currentIndexChanged.connect(self.change_span)
        chart_heading.addWidget(self.chart_range)
        layout.addLayout(chart_heading)
        self.chart_note = QtWidgets.QLabel()
        self.chart_note.setWordWrap(True)
        layout.addWidget(self.chart_note)
        layout.addWidget(self.chart)
        self.week_title = QtWidgets.QLabel("🗓 逐日预报")
        self.week_title.setStyleSheet("font-weight:600;")
        layout.addWidget(self.week_title)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["日期", "天气", "温度℃", "降水概率", "降水mm", "风速km/h"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellClicked.connect(self.choose_day)
        self.table.setToolTip("点击任意日期：切换整个生活助手的天气背景，并联动查询该日垃圾收运。")
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(43)
        self.table.setIconSize(QtCore.QSize(32, 30))
        self.table.setStyleSheet("QTableWidget {border:1px solid palette(mid); border-radius:10px;} "
                                 "QHeaderView::section {padding:7px 3px; border:0; "
                                 "border-bottom:1px solid palette(mid); background:palette(button); font-weight:600;}")
        self.table.setMinimumHeight(310)
        self.table.setMaximumHeight(365)
        layout.addWidget(self.table)
        source = QtWidgets.QLabel("来源：Open-Meteo · CC BY 4.0 · 最多16天预报；1个月视图超出部分无数据，不是一个月的准确预测。\n远期预报不确定性较高；非官方灾害警报，不改变垃圾日历或投放时间。")
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addStretch(1)
        self.enabled.toggled.connect(self.change_options)
        self.auto.toggled.connect(self.change_options)
        self.interval.valueChanged.connect(self.change_options)
        self.refresh()

    @safe_action
    def change_span(self, *_args):
        if self.loading:
            return
        try:
            self.plugin.change_weather_options(chart_hours=self.chart_range.currentData())
        except Exception:
            self.refresh()
            raise

    @safe_action
    def change_options(self, *_args):
        if self.loading:
            return
        try:
            self.plugin.change_weather_options(enabled=self.enabled.isChecked(), auto_refresh=self.auto.isChecked(),
                                               refresh_minutes=self.interval.value())
        except Exception:
            self.refresh()  # reflect the saved state if persistence failed
            raise
        if self.enabled.isChecked():
            self.plugin.refresh_weather()

    @safe_action
    def search(self, *_args):
        if not self.enabled.isChecked():
            self.message.setText("先勾选上方联网同意选项，再搜索城市。")
            return
        self.plugin.search_weather_city(self.city.text())

    @safe_action
    def use_city(self, *_args):
        selected = self.cities.currentData()
        if selected:
            self.plugin.change_weather_options(location=selected)
            self.plugin.refresh_weather(manual=True)

    def show_cities(self, cities):
        self.cities.clear()
        for city in cities:
            self.cities.addItem(" · ".join(filter(None, (city["name"], city["admin2"], city["admin1"]))), city)
        self.use_button.setEnabled(bool(cities))
        if not cities:
            self.message.setText("没有找到城市，请尝试意大利语城市名。")

    def choose_day(self, row, _column):
        item = self.table.item(row, 0)
        if item:
            day = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(day, QtCore.QDate) and day.isValid():
                self.daySelected.emit(day)

    def select_day(self, day):
        self.selected_day = day
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == day:
                self.table.setCurrentCell(row, 0)
                break

    @safe_action
    def refresh(self):
        self.loading = True
        try:
            options = self.plugin.settings["weather"]
            self.enabled.setChecked(options["enabled"])
            self.auto.setChecked(options["auto_refresh"])
            self.interval.setValue(options["refresh_minutes"])
            self.chart_range.setCurrentIndex(max(0, self.chart_range.findData(options.get("chart_hours", 24))))
            self.chart.set_span(options.get("chart_hours", 24))
        finally:
            self.loading = False
        busy = self.plugin.is_weather_updating()
        self.search_button.setEnabled(options["enabled"] and not busy and not is_offline())
        self.update_button.setEnabled(options["enabled"] and not busy and not is_offline())
        self.use_button.setEnabled(self.cities.count() > 0 and not busy)
        selected = options.get("location")
        city_name = " · ".join(filter(None, (selected["name"], selected["admin2"], selected["admin1"]))) if selected else "尚未选择"
        self.location.setText("当前城市：" + city_name)
        self.message.setText(self.plugin.weather_status)
        data = self.plugin.weather_data
        if not data or not selected or location_key(selected) != location_key(data["location"]):
            self.current.setText("尚无当前城市的天气缓存。启用后可立即刷新。")
            self.glyph.set_code(None)
            self.chart.set_forecast(None, self.plugin.now())
            self.chart_note.setText(self.chart.coverage_text())
            self.week_title.setText("🗓 逐日预报")
            self.tonight.setText("暂无降水预报；垃圾类别和投放时间请看“查询与本周”。")
            self.table.setRowCount(0)
            self.forecastChanged.emit()
            return
        now = self.plugin.now()
        current = data["current"]
        self.glyph.set_code(current["weather_code"])
        self.chart.set_forecast(data, now)
        self.chart_note.setText(self.chart.coverage_text())
        self.current.setText(f"{LABELS.get(current['weather_code'], '未知天气')} · {number(current['temperature_2m'], '℃')}"
                             f" · 体感 {number(current['apparent_temperature'], '℃')} · 风速 {number(current['wind_speed_10m'], 'km/h')}\n"
                             f"预报时刻：{local_time(current['time'], selected['timezone']).toString('MM-dd HH:mm')}"
                             f"；成功获取：{local_time(data['checked_at'], selected['timezone']).toString('MM-dd HH:mm')}")
        self.tonight.setText(tonight_summary(data, now))
        self.table.setRowCount(len(data["daily"]))
        self.week_title.setText(f"🗓 逐日预报 · {len(data['daily'])}天")
        today = local_time(now.timestamp(), selected["timezone"]).date()
        palette_key = self.palette().cacheKey()
        # Drop obsolete palette entries; no remote icons or unbounded image cache.
        self._weather_icons = {key: icon for key, icon in self._weather_icons.items() if key[1] == palette_key}
        for index, row in enumerate(data["daily"]):
            day = local_time(row["time"], selected["timezone"]).date()
            relative = "今天" if day == today else "明天" if day == today.addDays(1) else day.toString("MM-dd")
            cells = (relative + " · 周" + "一二三四五六日"[day.dayOfWeek() - 1], LABELS.get(row["weather_code"], "未知"),
                     number(row["temperature_2m_min"])+"～"+number(row["temperature_2m_max"]),
                     number(row["precipitation_probability_max"], "%"), number(row["precipitation_sum"]), number(row["wind_speed_10m_max"]))
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(day.toString("yyyy-MM-dd") + "\n" + " · ".join(cells[1:]))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, day)
                if column == 1:
                    key = row["weather_code"], palette_key
                    if key not in self._weather_icons:
                        self._weather_icons[key] = weather_icon(row["weather_code"], self.palette())
                    item.setIcon(self._weather_icons[key])
                elif column == 2:
                    item.setForeground(QtGui.QColor("#f6b589" if dark(self) else "#a24d2b"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif column == 3 and (row["precipitation_probability_max"] or 0) >= 40:
                    item.setForeground(QtGui.QColor("#7bc2f3" if dark(self) else "#237ba9"))
                self.table.setItem(index, column, item)
        self.select_day(self.selected_day)
        self.forecastChanged.emit()

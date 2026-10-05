from datetime import timedelta
import logging
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text
from .recipes import generate_recipes, recipe_text, request_id
from .settings import CONDIMENTS
from .store import eligible_items, stock_status, today

SAFETY_TEXT = """先看包装说明和食材状态，AI 不能判断你的食物是否真的安全。

• “食用截止日 / use-by”关系到安全，过了日期不要当作普通临期食材使用；储存不当也可能提前变质。
• “最佳赏味期 / best before”主要关系到品质，不等于过了就一定坏。软件会先要求你核查，不擅自推荐。
• 自己设的“检查日”只用于提醒，不是软件推算的保质期。发霉、变质或储存不当的食材不要靠多煮一会儿补救。
• 生熟食材分开处理。按包装和烹饪说明把需要熟透的食品彻底做熟。
• 普通剩菜尽快分装冷却，2 小时内冷藏，48 小时内吃完；更久应及时冷冻。复热应彻底热透，只复热一次。
• 米饭更要注意：尽快冷却，最好 1 小时内；冷藏后 24 小时内食用，只复热一次。两顿不是让饭菜一直放在桌上。

以上为一般家庭食品安全提示；特殊食品、包装要求和过敏情况优先。
参考：英国食品标准局（FSA），核对日期 2026-10-01。
"""
SOURCES = (
    ("烹饪和剩菜保存", "https://www.gov.uk/government/publications/cooking-your-food/cooking-your-food"),
    ("米饭保存", "https://www.gov.uk/government/publications/home-food-fact-checker/home-food-fact-checker"),
    ("食用截止日与最佳赏味期", "https://www.gov.uk/understanding-food-labelling/best-before-and-use-by-dates"),
)


class FoodEditDialog(QtWidgets.QDialog):
    def __init__(self, item=None, parent=None):
        super().__init__(parent)
        self.original = item or {}
        self.setWindowTitle("编辑食材" if item else "添加食材")
        self.resize(450, 400)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(self.original.get("name", ""))
        self.quantity = QtWidgets.QDoubleSpinBox()
        self.quantity.setRange(0, 1000000)
        self.quantity.setDecimals(3)
        self.quantity.setValue(self.original.get("quantity", 1))
        self.unit = QtWidgets.QComboBox()
        self.unit.setEditable(True)
        self.unit.addItems(["个", "g", "kg", "ml", "份", "包", "把"])
        self.unit.setCurrentText(self.original.get("unit", "个"))
        amount = QtWidgets.QHBoxLayout()
        amount.addWidget(self.quantity)
        amount.addWidget(self.unit)
        self.storage = QtWidgets.QComboBox()
        self.storage.addItems(["冷藏", "冷冻", "常温"])
        self.storage.setCurrentText(self.original.get("storage", "冷藏"))
        self.purchased = QtWidgets.QDateEdit()
        self.purchased.setCalendarPopup(True)
        self.purchased.setDisplayFormat("yyyy-MM-dd")
        self.purchased.setDate(QtCore.QDate.fromString(self.original.get("purchased", today().isoformat()), "yyyy-MM-dd"))
        self.has_due = QtWidgets.QCheckBox("设置检查／包装日期")
        self.has_due.setChecked(bool(self.original.get("due")))
        self.due = QtWidgets.QDateEdit()
        self.due.setCalendarPopup(True)
        self.due.setDisplayFormat("yyyy-MM-dd")
        self.due.setDate(QtCore.QDate.fromString(self.original.get("due") or (today()+timedelta(days=3)).isoformat(), "yyyy-MM-dd"))
        self.kind = QtWidgets.QComboBox()
        for title, value in (("自己设定的检查日", "check"), ("包装食用截止日（use-by）", "use_by"),
                             ("最佳赏味期（best before）", "best_before")):
            self.kind.addItem(title, value)
        self.kind.setCurrentIndex(max(0, self.kind.findData(self.original.get("date_kind", "check"))))
        self.spoiled = QtWidgets.QCheckBox("已变质／不可食用（不用于生成菜谱）")
        self.spoiled.setChecked(self.original.get("spoiled", False))
        self.note = QtWidgets.QLineEdit(self.original.get("note", ""))
        form.addRow("食材名称", self.name)
        form.addRow("现有数量", amount)
        form.addRow("放在哪里", self.storage)
        form.addRow("购买日期", self.purchased)
        form.addRow(self.has_due)
        form.addRow("日期", self.due)
        form.addRow("日期含义", self.kind)
        form.addRow(self.spoiled)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        hint = QtWidgets.QLabel("检查日只是提醒，不保证安全。包装食用截止日不要擅自延长。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.has_due.toggled.connect(self._enable_date)
        self._enable_date(self.has_due.isChecked())

    def _enable_date(self, enabled):
        self.due.setEnabled(enabled)
        self.kind.setEnabled(enabled)

    def value(self):
        return dict(self.original, name=self.name.text().strip(), quantity=self.quantity.value(),
            unit=self.unit.currentText().strip(), storage=self.storage.currentText(),
            purchased=self.purchased.date().toString("yyyy-MM-dd"),
            due=self.due.date().toString("yyyy-MM-dd") if self.has_due.isChecked() else "",
            date_kind=self.kind.currentData(), spoiled=self.spoiled.isChecked(), note=self.note.text())

    def accept(self):
        if not self.name.text().strip():
            QtWidgets.QMessageBox.information(self, "缺少名称", "请填写食材名称。")
            return
        super().accept()


class RecipeThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    succeeded = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, payload, model, manager, parent=None):
        super().__init__(parent)
        self.payload, self.model, self.manager = payload, model, manager

    def run(self):
        try:
            recipes = generate_recipes(self.payload, self.model, self.manager, self.progress.emit)
            self.succeeded.emit({"recipes": recipes, "people": self.payload["people"],
                "meals": self.payload["meals"], "model": self.model, "date": self.payload["date"]})
        except Exception as error:
            self.failed.emit(self.manager.redact(f"{type(error).__name__}: {error}"),
                             self.manager.redact(traceback.format_exc()))


class CookingDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin, self.worker = plugin, None
        self.current_result, self.pending_key = None, ""
        self.setWindowTitle("做饭小助手")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMinMaxButtonsHint)
        self.resize(1050, 760)
        layout = QtWidgets.QVBoxLayout(self)
        self.stock_summary = QtWidgets.QLabel()
        self.stock_summary.setWordWrap(True)
        layout.addWidget(self.stock_summary)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_recipe_tab()
        self._build_stock_tab()
        self.status = QtWidgets.QLabel("库存本地保存；点击生成时才把选中食材和偏好发送给 Gemini。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh_stock()
        preferences = self.plugin.store.get("preferences", {})
        self.people.setValue(preferences.get("people", plugin.settings["people"]))
        self.meals.setValue(preferences.get("meals", plugin.settings["meals"]))
        for name, box in self.condiments.items():
            box.setChecked(name in preferences.get("condiments", ["盐"]))
        self.extra.setPlainText(preferences.get("extra", ""))
        self.notes.setText(preferences.get("notes", ""))
        previous = self.plugin.store.get("last_recipes")
        if previous:
            self.show_recipes(previous)
            self.status.setText("已恢复上次方案。做法仅供参考，请按当前食材状态判断。")

    def _build_recipe_tab(self):
        tab = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(tab)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        outer.addWidget(splitter)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        controls = QtWidgets.QWidget()
        form = QtWidgets.QVBoxLayout(controls)
        form.addWidget(QtWidgets.QLabel("从库存选食材（快到检查日的默认勾选）"))
        self.ingredients = QtWidgets.QListWidget()
        self.ingredients.setMinimumHeight(140)
        form.addWidget(self.ingredients)
        select = QtWidgets.QHBoxLayout()
        for title, check in (("全选", True), ("清空选择", False)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda _checked=False, state=check: self.select_ingredients(state))
            select.addWidget(button)
        stock = QtWidgets.QPushButton("添加食材…")
        stock.clicked.connect(lambda: self.edit_stock())
        select.addWidget(stock)
        form.addLayout(select)
        form.addWidget(QtWidgets.QLabel("手头还有什么（可直接写数量，不必先入库）"))
        self.extra = QtWidgets.QPlainTextEdit()
        self.extra.setPlaceholderText("例如：西红柿 2 个、鸡蛋 3 个、土豆 500g")
        self.extra.setMaximumHeight(80)
        form.addWidget(self.extra)
        servings = QtWidgets.QHBoxLayout()
        self.people, self.meals = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
        self.people.setRange(1, 20)
        self.meals.setRange(1, 6)
        servings.addWidget(QtWidgets.QLabel("几人吃"))
        servings.addWidget(self.people)
        servings.addWidget(QtWidgets.QLabel("做几顿"))
        servings.addWidget(self.meals)
        form.addLayout(servings)
        form.addWidget(QtWidgets.QLabel("有哪些调料（没勾选就不假设你有）"))
        grid = QtWidgets.QGridLayout()
        self.condiments = {}
        for i, name in enumerate(CONDIMENTS):
            box = QtWidgets.QCheckBox(name)
            self.condiments[name] = box
            grid.addWidget(box, i//4, i%4)
        form.addLayout(grid)
        self.notes = QtWidgets.QLineEdit()
        self.notes.setPlaceholderText("忌口、过敏、没有烤箱等（可不填）")
        form.addWidget(self.notes)
        self.generate_button = QtWidgets.QPushButton("生成做饭方案")
        self.generate_button.setToolTip("把当前选中的食材、调料、人数与偏好发送给 Gemini；不会扣减库存。")
        self.generate_button.clicked.connect(lambda: self.generate())
        form.addWidget(self.generate_button)
        self.change_button = QtWidgets.QPushButton("换一组方案")
        self.change_button.setToolTip("重新请求 Gemini，不复用相同食材的本地缓存。")
        self.change_button.clicked.connect(lambda: self.generate(force=True))
        form.addWidget(self.change_button)
        keys = QtWidgets.QPushButton("管理 Gemini 密钥…")
        keys.clicked.connect(lambda: self.plugin.context.open_gemini_key_manager())
        form.addWidget(keys)
        safety = QtWidgets.QPushButton("两顿饭怎么安全保存？")
        safety.clicked.connect(self.show_safety)
        form.addWidget(safety)
        form.addStretch()
        scroll.setWidget(controls)
        scroll.setMinimumWidth(330)
        splitter.addWidget(scroll)
        right = QtWidgets.QWidget()
        content = QtWidgets.QVBoxLayout(right)
        self.recipe_list = QtWidgets.QComboBox()
        self.recipe_list.currentIndexChanged.connect(self.display_recipe)
        content.addWidget(self.recipe_list)
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("选好食材后点击生成，这里会显示总用量和一步一步的做法。")
        content.addWidget(self.output, 1)
        copy = QtWidgets.QPushButton("复制当前做法")
        copy.clicked.connect(lambda: set_internal_clipboard_text(self.output.toPlainText()))
        content.addWidget(copy)
        splitter.addWidget(right)
        splitter.setSizes([380, 650])
        self.tabs.addTab(tab, "今天做什么")

    def _build_stock_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        row = QtWidgets.QHBoxLayout()
        for title, callback in (("添加", lambda: self.edit_stock()), ("编辑", self.edit_selected),
                                ("用掉／减量", self.consume_selected), ("删除", self.delete_selected),
                                ("刷新", self.refresh_stock)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.stock_table = QtWidgets.QTableWidget(0, 7)
        self.stock_table.setHorizontalHeaderLabels(["食材", "数量", "存放", "购买日期", "检查／包装日期", "状态", "更新日期"])
        self.stock_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.stock_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.stock_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stock_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.stock_table.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.stock_table.cellDoubleClicked.connect(lambda *_args: self.edit_selected())
        self.stock_table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.stock_table.customContextMenuRequested.connect(self.stock_menu)
        layout.addWidget(self.stock_table, 1)
        note = QtWidgets.QLabel("双击编辑；可以记录不同批次的同一种食材。红色需要先处理，橙色优先检查，未设置日期的不会被软件猜测保质期。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs.addTab(tab, "食材库存")

    def refresh_stock(self):
        checked = {self.ingredients.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                   for i in range(self.ingredients.count())
                   if self.ingredients.item(i).checkState() == QtCore.Qt.CheckState.Checked}
        known = {self.ingredients.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.ingredients.count())}
        self.stock_items = self.plugin.store.items()
        self.stock_table.setSortingEnabled(False)
        self.stock_table.setRowCount(len(self.stock_items))
        self.ingredients.clear()
        counts = {"soon": 0, "overdue": 0, "spoiled": 0, "unknown": 0}
        for row, item in enumerate(self.stock_items):
            state, description = stock_status(item, warn_days=self.plugin.settings["warn_days"])
            if state in counts:
                counts[state] += 1
            values = [item["name"], f"{item['quantity']:g} {item['unit']}", item["storage"], item["purchased"],
                      item["due"] or "未设置", description,
                      QtCore.QDateTime.fromString(item["updated_at"], QtCore.Qt.DateFormat.ISODateWithMs)
                          .toTimeZone(QtCore.QTimeZone(b"Europe/Rome")).toString("yyyy-MM-dd HH:mm")]
            for column, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(value)
                cell.setData(QtCore.Qt.ItemDataRole.UserRole, item["id"])
                cell.setToolTip(item["note"] + "\n日期含义：" + {"check": "自己设的检查日", "use_by": "食用截止日", "best_before": "最佳赏味期"}[item["date_kind"]])
                if state in {"soon", "overdue", "spoiled"}:
                    cell.setBackground(QtGui.QColor("#fff0cc" if state == "soon" else "#ffdce0"))
                    cell.setForeground(QtGui.QColor("#302825"))
                self.stock_table.setItem(row, column, cell)
            if state not in {"empty", "overdue", "spoiled"}:
                entry = QtWidgets.QListWidgetItem(f"{item['name']} · {item['quantity']:g} {item['unit']} · {description}")
                entry.setData(QtCore.Qt.ItemDataRole.UserRole, item["id"])
                entry.setFlags(entry.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                selected = item["id"] in checked or (item["id"] not in known and state == "soon")
                entry.setCheckState(QtCore.Qt.CheckState.Checked if selected else QtCore.Qt.CheckState.Unchecked)
                self.ingredients.addItem(entry)
        self.stock_table.setSortingEnabled(True)
        self.stock_summary.setText(f"库存 {len(self.stock_items)} 条 · 优先检查 {counts['soon']} · 待处理 {counts['overdue'] + counts['spoiled']}"
                                  f" · 未设检查日 {counts['unknown']}（可双击补充）")

    def selected_stock(self):
        row = self.stock_table.currentRow()
        if row < 0 or self.stock_table.item(row, 0) is None:
            return None
        identifier = self.stock_table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        return next((x for x in self.plugin.store.items() if x["id"] == identifier), None)

    def edit_selected(self):
        item = self.selected_stock()
        if item:
            self.edit_stock(item)

    def edit_stock(self, item=None):
        dialog = FoodEditDialog(item, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                self.plugin.store.save_item(dialog.value())
                self.refresh_stock()
            except Exception as error:
                self.show_error("保存食材失败", error)

    def consume_selected(self):
        item = self.selected_stock()
        if not item or item["quantity"] <= 0:
            return
        amount, ok = QtWidgets.QInputDialog.getDouble(self, "用掉多少", f"{item['name']}，单位：{item['unit']}",
            min(1, item["quantity"]), 0.001, item["quantity"], 3)
        if ok:
            try:
                self.plugin.store.consume(item["id"], amount)
                self.refresh_stock()
            except Exception as error:
                self.show_error("扣减失败", error)

    def delete_selected(self):
        item = self.selected_stock()
        if item and QtWidgets.QMessageBox.question(self, "删除库存条目", f"删除“{item['name']}”这条记录？") == QtWidgets.QMessageBox.StandardButton.Yes:
            try:
                self.plugin.store.remove(item["id"])
                self.refresh_stock()
            except Exception as error:
                self.show_error("删除失败", error)

    def stock_menu(self, position):
        index = self.stock_table.indexAt(position)
        if index.isValid():
            self.stock_table.selectRow(index.row())
        menu = QtWidgets.QMenu(self)
        for title, callback in (("编辑食材／日期…", self.edit_selected), ("用掉／减量…", self.consume_selected),
                                ("删除条目", self.delete_selected)):
            menu.addAction(title).triggered.connect(callback)
        menu.exec(self.stock_table.viewport().mapToGlobal(position))

    def select_ingredients(self, checked):
        for i in range(self.ingredients.count()):
            self.ingredients.item(i).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def payload(self):
        selected = {self.ingredients.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.ingredients.count())
                    if self.ingredients.item(i).checkState() == QtCore.Qt.CheckState.Checked}
        items = eligible_items(self.plugin.store.items(), warn_days=self.plugin.settings["warn_days"])
        ingredients = [dict(name=x["name"], quantity=x["quantity"], unit=x["unit"], storage=x["storage"],
                            priority=stock_status(x, warn_days=self.plugin.settings["warn_days"])[0] == "soon")
                       for x in items if x["id"] in selected]
        extra = self.extra.toPlainText().strip()
        if extra:
            ingredients.append({"手动补充": extra[:4000]})
        return {"ingredients": ingredients, "condiments": [name for name, box in self.condiments.items() if box.isChecked()],
                "people": self.people.value(), "meals": self.meals.value(), "notes": self.notes.text()[:2000],
                "date": today().isoformat()}

    def save_preferences(self):
        self.plugin.store.put("preferences", {"people": self.people.value(), "meals": self.meals.value(),
            "condiments": [name for name, box in self.condiments.items() if box.isChecked()],
            "extra": self.extra.toPlainText()[:4000], "notes": self.notes.text()[:2000]})

    def is_busy(self):
        # Keep ownership until finished is processed on the GUI thread.
        # isRunning() can become false before its queued finished signal arrives.
        return self.worker is not None

    def generate(self, force=False):
        if self.is_busy():
            return
        payload = self.payload()
        if not payload["ingredients"]:
            self.status.setText("请先勾选库存食材或输入手头的食材。")
            return
        model = self.plugin.settings["model"]
        self.pending_key = request_id(payload, model)
        try:
            self.save_preferences()
            cached = None if force else self.plugin.store.recipe_cache(self.pending_key)
        except Exception as error:
            self.show_error("读取菜谱缓存失败", error)
            return
        if cached:
            self.show_recipes(cached)
            self.status.setText("已复用今天相同食材的方案，未发送网络请求。想换一种做法可点“换一组方案”。")
            return
        manager = self.plugin.context.gemini_keys
        if not manager.request_keys(model):
            self.status.setText(manager.unavailable_message(model))
            return
        self.generate_button.setEnabled(False)
        self.change_button.setEnabled(False)
        self.status.setText("正在生成…窗口可以继续使用，关闭只是隐藏，不会销毁后台任务。")
        self.worker = RecipeThread(payload, model, manager, self)
        self.worker.progress.connect(self.generation_progress)
        self.worker.succeeded.connect(self.generated)
        self.worker.failed.connect(self.generation_failed)
        self.worker.finished.connect(self.generation_finished)
        self.worker.start()

    def generated(self, result):
        self.show_recipes(result)
        try:
            self.plugin.store.save_recipes(self.pending_key, result)
            self.status.setText("方案已生成并保存。库存没有扣减；做完可到“食材库存”记录使用量。")
        except Exception as error:
            self.show_error("菜谱已生成，但本地保存失败", error)

    @QtCore.pyqtSlot(str)
    def generation_progress(self, message):
        # The worker only emits signals; widgets and logging stay on the GUI thread.
        message = self.plugin.context.gemini_keys.redact(message)
        self.status.setText(message)
        self.plugin.context.log(message)

    def generation_failed(self, message, detail):
        self.status.setText(message)
        self.plugin.context.log(detail, logging.ERROR)

    def generation_finished(self):
        self.generate_button.setEnabled(True)
        self.change_button.setEnabled(True)
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()

    def show_recipes(self, result):
        self.current_result = result
        self.recipe_list.blockSignals(True)
        self.recipe_list.clear()
        self.recipe_list.addItems([x["title"] for x in result["recipes"]])
        self.recipe_list.blockSignals(False)
        self.display_recipe()

    def display_recipe(self, *_args):
        index = self.recipe_list.currentIndex()
        if self.current_result and index >= 0:
            self.output.setPlainText(recipe_text(self.current_result["recipes"][index],
                self.current_result["people"], self.current_result["meals"]))

    def show_safety(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("两顿饭的保存与食材检查")
        dialog.resize(650, 550)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit(SAFETY_TEXT)
        text.setReadOnly(True)
        layout.addWidget(text)
        for name, url in SOURCES:
            button = QtWidgets.QPushButton("查看官方说明：" + name)
            button.clicked.connect(lambda _checked=False, link=url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)))
            layout.addWidget(button)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def show_error(self, title, error):
        self.plugin.context.log(f"{title}：{type(error).__name__}: {error}\n{traceback.format_exc()}", logging.ERROR)
        self.status.setText(f"{title}：{error}")
        QtWidgets.QMessageBox.warning(self, title, str(error))

    def closeEvent(self, event):
        try:
            self.save_preferences()
        except Exception as error:
            self.plugin.context.log(f"做饭偏好保存失败：{error}", logging.ERROR)
        self.hide()
        event.ignore()

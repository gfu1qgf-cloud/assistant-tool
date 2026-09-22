from __future__ import annotations

import copy

from qt_compat import QtCore, QtWidgets

from .classifier import (
    DEFAULT_CATEGORY_TREE,
    IMAGE_CLASSIFIER_CONFIG_KEY,
    normalize_category_tree,
    normalize_image_classifier_settings,
)


class _SubcategoryDialog(QtWidgets.QDialog):
    def __init__(self, name="", descriptions=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("子类别")
        self.resize(520, 360)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(str(name or ""), self)
        form.addRow("类别名称：", self.name_edit)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel(
            "英文描述词（每行一个；应描述画面内容，而不是文件用途）：", self
        ))
        self.descriptions_edit = QtWidgets.QPlainTextEdit(self)
        self.descriptions_edit.setPlainText("\n".join(descriptions or []))
        layout.addWidget(self.descriptions_edit, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            self,
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        if not self.name_edit.text().strip():
            QtWidgets.QMessageBox.warning(self, "子类别", "类别名称不能为空。")
            return
        if not self.descriptions():
            QtWidgets.QMessageBox.warning(
                self, "子类别", "请至少填写一条英文画面描述。"
            )
            return
        self.accept()

    def descriptions(self):
        values = []
        seen = set()
        for line in self.descriptions_edit.toPlainText().splitlines():
            text = line.strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                values.append(text)
        return values


class ImageClassifierSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("图片智能分类", self.widget)
        title.setStyleSheet("font-size:14px;font-weight:600;")
        layout.addWidget(title)
        description = QtWidgets.QLabel(
            "CLIP 模型只在开始分析后后台加载。分类先生成预览结果，"
            "不会直接改动图片；低相似度或前两类难以区分的图片会进入待人工确认。",
            self.widget,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QtWidgets.QFormLayout()
        self.model_combo = QtWidgets.QComboBox(self.widget)
        self.model_combo.addItem("CLIP Large（更准、较慢）", "large")
        self.model_combo.addItem("CLIP Base（更快、精度略低）", "base")
        self.device_combo = QtWidgets.QComboBox(self.widget)
        self.device_combo.addItem("自动选择", "auto")
        self.device_combo.addItem("仅 CPU", "cpu")
        self.device_combo.addItem("CUDA 显卡", "cuda")
        self.batch_spin = QtWidgets.QSpinBox(self.widget)
        self.batch_spin.setRange(1, 64)
        self.batch_spin.setToolTip("显存或内存不足时调低；CPU 建议 4～12。")
        self.similarity_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.similarity_spin.setRange(-1.0, 1.0)
        self.similarity_spin.setDecimals(3)
        self.similarity_spin.setSingleStep(0.01)
        self.similarity_spin.setToolTip(
            "最高类别低于此余弦相似度时进入待人工确认。"
        )
        self.margin_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.margin_spin.setRange(0.0, 0.5)
        self.margin_spin.setDecimals(3)
        self.margin_spin.setSingleStep(0.005)
        self.margin_spin.setToolTip(
            "第一名与第二名的差距低于此值时，认为难以区分。"
        )
        self.parent_weight_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.parent_weight_spin.setRange(0.0, 0.5)
        self.parent_weight_spin.setDecimals(2)
        self.parent_weight_spin.setSingleStep(0.05)
        self.parent_weight_spin.setToolTip(
            "用主类别综合特征辅助子类别判断，可减少跨大类误判。"
        )
        self.recursive_checkbox = QtWidgets.QCheckBox("递归扫描子文件夹", self.widget)
        self.operation_combo = QtWidgets.QComboBox(self.widget)
        self.operation_combo.addItem("复制原图片（推荐）", "copy")
        self.operation_combo.addItem("移动原图片", "move")
        form.addRow("模型：", self.model_combo)
        form.addRow("运行设备：", self.device_combo)
        form.addRow("批量大小：", self.batch_spin)
        form.addRow("最低相似度：", self.similarity_spin)
        form.addRow("最低领先差距：", self.margin_spin)
        form.addRow("父类别辅助权重：", self.parent_weight_spin)
        form.addRow("扫描方式：", self.recursive_checkbox)
        form.addRow("默认整理方式：", self.operation_combo)
        layout.addLayout(form)

        category_header = QtWidgets.QHBoxLayout()
        category_title = QtWidgets.QLabel("分类树与画面描述", self.widget)
        category_title.setStyleSheet("font-weight:600;")
        category_header.addWidget(category_title)
        category_header.addStretch(1)
        self.add_main_button = QtWidgets.QPushButton("新增主类", self.widget)
        self.add_sub_button = QtWidgets.QPushButton("新增子类", self.widget)
        self.edit_button = QtWidgets.QPushButton("编辑", self.widget)
        self.delete_button = QtWidgets.QPushButton("删除", self.widget)
        self.reset_button = QtWidgets.QPushButton("恢复扩展默认分类", self.widget)
        for button in (
            self.add_main_button, self.add_sub_button, self.edit_button,
            self.delete_button, self.reset_button,
        ):
            category_header.addWidget(button)
        layout.addLayout(category_header)

        self.category_tree = QtWidgets.QTreeWidget(self.widget)
        self.category_tree.setHeaderLabels(["类别", "描述数量 / 示例"])
        self.category_tree.setAlternatingRowColors(True)
        self.category_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.category_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        layout.addWidget(self.category_tree, 1)
        hint = QtWidgets.QLabel(
            "分类不是越多越准：相似类别的描述应写出视觉差异。"
            "描述建议使用英文，每行只描述一种典型画面。",
            self.widget,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)

        self.add_main_button.clicked.connect(self._add_main)
        self.add_sub_button.clicked.connect(self._add_sub)
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.reset_button.clicked.connect(self._reset_categories)
        self.category_tree.itemDoubleClicked.connect(
            lambda _item, _column: self._edit_selected()
        )

    def _populate_categories(self, categories):
        self.category_tree.clear()
        for main, children in normalize_category_tree(categories).items():
            parent = QtWidgets.QTreeWidgetItem([main, f"{len(children)} 个子类别"])
            parent.setData(0, QtCore.Qt.UserRole, None)
            self.category_tree.addTopLevelItem(parent)
            for sub, descriptions in children.items():
                child = QtWidgets.QTreeWidgetItem([
                    sub,
                    f"{len(descriptions)} 条 · {descriptions[0]}",
                ])
                child.setData(0, QtCore.Qt.UserRole, list(descriptions))
                child.setToolTip(1, "\n".join(descriptions))
                parent.addChild(child)
            parent.setExpanded(True)

    def categories(self):
        result = {}
        for parent_index in range(self.category_tree.topLevelItemCount()):
            parent = self.category_tree.topLevelItem(parent_index)
            children = {}
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                children[child.text(0)] = list(
                    child.data(0, QtCore.Qt.UserRole) or []
                )
            if children:
                result[parent.text(0)] = children
        return normalize_category_tree(result)

    def _selected_parent(self):
        item = self.category_tree.currentItem()
        if item is None:
            return None
        return item.parent() or item

    def _add_main(self):
        name, accepted = QtWidgets.QInputDialog.getText(
            self.widget, "新增主类别", "主类别名称："
        )
        name = name.strip()
        if not accepted or not name:
            return
        existing = {
            self.category_tree.topLevelItem(index).text(0)
            for index in range(self.category_tree.topLevelItemCount())
        }
        if name in existing:
            QtWidgets.QMessageBox.warning(self.widget, "新增主类别", "名称已存在。")
            return
        item = QtWidgets.QTreeWidgetItem([name, "0 个子类别"])
        item.setData(0, QtCore.Qt.UserRole, None)
        self.category_tree.addTopLevelItem(item)
        self.category_tree.setCurrentItem(item)

    def _add_sub(self):
        parent = self._selected_parent()
        if parent is None:
            QtWidgets.QMessageBox.information(
                self.widget, "新增子类别", "请先选择或新增一个主类别。"
            )
            return
        dialog = _SubcategoryDialog(parent=self.widget)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        name = dialog.name_edit.text().strip()
        if any(parent.child(i).text(0) == name for i in range(parent.childCount())):
            QtWidgets.QMessageBox.warning(self.widget, "新增子类别", "名称已存在。")
            return
        descriptions = dialog.descriptions()
        child = QtWidgets.QTreeWidgetItem([
            name, f"{len(descriptions)} 条 · {descriptions[0]}"
        ])
        child.setData(0, QtCore.Qt.UserRole, descriptions)
        child.setToolTip(1, "\n".join(descriptions))
        parent.addChild(child)
        parent.setText(1, f"{parent.childCount()} 个子类别")
        parent.setExpanded(True)
        self.category_tree.setCurrentItem(child)

    def _edit_selected(self):
        item = self.category_tree.currentItem()
        if item is None:
            return
        if item.parent() is None:
            name, accepted = QtWidgets.QInputDialog.getText(
                self.widget, "编辑主类别", "主类别名称：", text=item.text(0)
            )
            if accepted and name.strip():
                item.setText(0, name.strip())
            return
        descriptions = list(item.data(0, QtCore.Qt.UserRole) or [])
        dialog = _SubcategoryDialog(item.text(0), descriptions, self.widget)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        descriptions = dialog.descriptions()
        item.setText(0, dialog.name_edit.text().strip())
        item.setText(1, f"{len(descriptions)} 条 · {descriptions[0]}")
        item.setData(0, QtCore.Qt.UserRole, descriptions)
        item.setToolTip(1, "\n".join(descriptions))

    def _delete_selected(self):
        item = self.category_tree.currentItem()
        if item is None:
            return
        name = item.text(0)
        if QtWidgets.QMessageBox.question(
            self.widget, "删除分类", f"确定删除“{name}”吗？"
        ) != QtWidgets.QMessageBox.Yes:
            return
        parent = item.parent()
        if parent is None:
            self.category_tree.takeTopLevelItem(
                self.category_tree.indexOfTopLevelItem(item)
            )
        else:
            parent.removeChild(item)
            parent.setText(1, f"{parent.childCount()} 个子类别")

    def _reset_categories(self):
        if QtWidgets.QMessageBox.question(
            self.widget,
            "恢复分类",
            "将丢弃当前分类修改并恢复扩展默认分类，是否继续？",
        ) == QtWidgets.QMessageBox.Yes:
            self._populate_categories(copy.deepcopy(DEFAULT_CATEGORY_TREE))

    def load_config(self, config):
        settings = normalize_image_classifier_settings(
            (config or {}).get(IMAGE_CLASSIFIER_CONFIG_KEY)
        )
        self.model_combo.setCurrentIndex(
            max(0, self.model_combo.findData(settings["model"]))
        )
        self.device_combo.setCurrentIndex(
            max(0, self.device_combo.findData(settings["device"]))
        )
        self.batch_spin.setValue(settings["batch_size"])
        self.similarity_spin.setValue(settings["minimum_similarity"])
        self.margin_spin.setValue(settings["minimum_margin"])
        self.parent_weight_spin.setValue(settings["parent_weight"])
        self.recursive_checkbox.setChecked(settings["recursive"])
        self.operation_combo.setCurrentIndex(
            max(0, self.operation_combo.findData(settings["operation"]))
        )
        self._populate_categories(settings["categories"])

    def validate(self):
        categories = self.categories()
        if not categories:
            raise ValueError("图片智能分类至少需要一个主类别和一个子类别。")

    def update_config(self, config):
        self.validate()
        previous = normalize_image_classifier_settings(
            (config or {}).get(IMAGE_CLASSIFIER_CONFIG_KEY)
        )
        config[IMAGE_CLASSIFIER_CONFIG_KEY] = normalize_image_classifier_settings({
            **previous,
            "model": self.model_combo.currentData(),
            "device": self.device_combo.currentData(),
            "batch_size": self.batch_spin.value(),
            "minimum_similarity": self.similarity_spin.value(),
            "minimum_margin": self.margin_spin.value(),
            "parent_weight": self.parent_weight_spin.value(),
            "recursive": self.recursive_checkbox.isChecked(),
            "operation": self.operation_combo.currentData(),
            "categories": self.categories(),
        })

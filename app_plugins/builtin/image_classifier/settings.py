from __future__ import annotations

import copy
from pathlib import Path

from qt_compat import QtCore, QtWidgets

from .classifier import (
    DEFAULT_CATEGORY_TREE,
    IMAGE_CLASSIFIER_CONFIG_KEY,
    MEDIA_SUFFIXES,
    normalize_category_tree,
    normalize_image_classifier_settings,
)


EXAMPLES_ROLE = int(QtCore.Qt.UserRole) + 1


class _SubcategoryDialog(QtWidgets.QDialog):
    def __init__(self, name="", descriptions=None, examples=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("子类别")
        self.resize(620, 540)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(str(name or ""), self)
        form.addRow("类别名称：", self.name_edit)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel(
            "高级英文描述（可选；每行一个，不会写可以留空）：", self
        ))
        self.descriptions_edit = QtWidgets.QPlainTextEdit(self)
        self.descriptions_edit.setPlainText("\n".join(descriptions or []))
        layout.addWidget(self.descriptions_edit, 1)
        layout.addWidget(QtWidgets.QLabel(
            "参考素材（推荐；添加几张典型图片、视频或已有分类文件夹即可）：",
            self,
        ))
        self.examples_list = QtWidgets.QListWidget(self)
        self.examples_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        for path in examples or []:
            self.examples_list.addItem(str(path))
        layout.addWidget(self.examples_list, 1)
        example_actions = QtWidgets.QHBoxLayout()
        add_files = QtWidgets.QPushButton("添加图片/视频…", self)
        add_folder = QtWidgets.QPushButton("添加参考文件夹…", self)
        remove = QtWidgets.QPushButton("移除选中", self)
        example_actions.addWidget(add_files)
        example_actions.addWidget(add_folder)
        example_actions.addWidget(remove)
        example_actions.addStretch(1)
        layout.addLayout(example_actions)
        add_files.clicked.connect(self._add_example_files)
        add_folder.clicked.connect(self._add_example_folder)
        remove.clicked.connect(self._remove_examples)
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
        self.accept()

    def _add_example_files(self):
        files, _selected = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择参考图片或视频",
            "",
            "图片与视频 (*.jpg *.jpeg *.jfif *.png *.webp *.bmp *.gif *.tif *.tiff *.avif *.mp4 *.mov *.m4v *.avi *.mkv *.webm *.mts *.m2ts);;所有文件 (*)",
        )
        self._append_examples(files)

    def _add_example_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择参考素材文件夹"
        )
        if folder:
            self._append_examples([folder])

    def _append_examples(self, paths):
        existing = {
            self.examples_list.item(index).text()
            for index in range(self.examples_list.count())
        }
        for path in paths:
            if path and path not in existing:
                existing.add(path)
                self.examples_list.addItem(path)

    def _remove_examples(self):
        for item in self.examples_list.selectedItems():
            self.examples_list.takeItem(self.examples_list.row(item))

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

    def examples(self):
        return [
            self.examples_list.item(index).text()
            for index in range(self.examples_list.count())
        ]


class ImageClassifierSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("图片/视频素材分类", self.widget)
        title.setStyleSheet("font-size:14px;font-weight:600;")
        layout.addWidget(title)
        description = QtWidgets.QLabel(
            "CLIP 模型只在开始分析后后台加载。分类先生成预览结果，"
            "不会直接改动素材；视频会跳过片头黑屏并抽取多张代表画面。"
            "低相似度或前两类难以区分的素材会进入待人工确认。",
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
        self.video_frames_spin = QtWidgets.QSpinBox(self.widget)
        self.video_frames_spin.setRange(1, 8)
        self.video_frames_spin.setSuffix(" 帧")
        self.video_frames_spin.setToolTip(
            "1 表示只取第一张非黑屏；建议 3 帧，兼顾片头、中段和后段。"
        )
        self.maximum_examples_spin = QtWidgets.QSpinBox(self.widget)
        self.maximum_examples_spin.setRange(1, 100)
        self.maximum_examples_spin.setSuffix(" 个/类")
        self.recursive_checkbox = QtWidgets.QCheckBox("递归扫描子文件夹", self.widget)
        self.operation_combo = QtWidgets.QComboBox(self.widget)
        self.operation_combo.addItem("复制原素材（推荐）", "copy")
        self.operation_combo.addItem("移动原素材", "move")
        form.addRow("模型：", self.model_combo)
        form.addRow("运行设备：", self.device_combo)
        form.addRow("批量大小：", self.batch_spin)
        form.addRow("最低相似度：", self.similarity_spin)
        form.addRow("最低领先差距：", self.margin_spin)
        form.addRow("父类别辅助权重：", self.parent_weight_spin)
        form.addRow("每个视频抽取：", self.video_frames_spin)
        form.addRow("每类最多参考素材：", self.maximum_examples_spin)
        form.addRow("扫描方式：", self.recursive_checkbox)
        form.addRow("默认整理方式：", self.operation_combo)
        layout.addLayout(form)

        category_header = QtWidgets.QHBoxLayout()
        category_title = QtWidgets.QLabel("用户分类与参考素材", self.widget)
        category_title.setStyleSheet("font-weight:600;")
        category_header.addWidget(category_title)
        category_header.addStretch(1)
        self.add_main_button = QtWidgets.QPushButton("新增主类", self.widget)
        self.add_sub_button = QtWidgets.QPushButton("新增子类", self.widget)
        self.edit_button = QtWidgets.QPushButton("编辑", self.widget)
        self.delete_button = QtWidgets.QPushButton("删除", self.widget)
        self.import_button = QtWidgets.QPushButton("从已整理目录导入…", self.widget)
        self.reset_button = QtWidgets.QPushButton("恢复扩展默认分类", self.widget)
        for button in (
            self.add_main_button, self.add_sub_button, self.edit_button,
            self.delete_button, self.import_button, self.reset_button,
        ):
            category_header.addWidget(button)
        layout.addLayout(category_header)

        self.category_tree = QtWidgets.QTreeWidget(self.widget)
        self.category_tree.setHeaderLabels(["类别", "参考素材 / 高级描述"])
        self.category_tree.setAlternatingRowColors(True)
        self.category_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.category_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        layout.addWidget(self.category_tree, 1)
        hint = QtWidgets.QLabel(
            "最省事的用法：从已经整理好的目录导入，或为子类别添加几张典型"
            "图片/视频。英文描述只是可选的高级补充，不要求用户自己写提示词。",
            self.widget,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)

        self.add_main_button.clicked.connect(self._add_main)
        self.add_sub_button.clicked.connect(self._add_sub)
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.import_button.clicked.connect(self._import_directory)
        self.reset_button.clicked.connect(self._reset_categories)
        self.category_tree.itemDoubleClicked.connect(
            lambda _item, _column: self._edit_selected()
        )

    @staticmethod
    def _summary(descriptions, examples):
        return f"参考 {len(examples)} 项 · 描述 {len(descriptions)} 条"

    def _populate_categories(self, categories, examples=None):
        examples = examples if isinstance(examples, dict) else {}
        self.category_tree.clear()
        for main, children in normalize_category_tree(categories).items():
            parent = QtWidgets.QTreeWidgetItem([main, f"{len(children)} 个子类别"])
            parent.setData(0, QtCore.Qt.UserRole, None)
            self.category_tree.addTopLevelItem(parent)
            for sub, descriptions in children.items():
                child = QtWidgets.QTreeWidgetItem([
                    sub,
                    self._summary(
                        descriptions, examples.get(f"{main}/{sub}", [])
                    ),
                ])
                child.setData(0, QtCore.Qt.UserRole, list(descriptions))
                child.setData(
                    0, EXAMPLES_ROLE, list(examples.get(f"{main}/{sub}", []))
                )
                child.setToolTip(1, "\n".join(
                    list(examples.get(f"{main}/{sub}", [])) + list(descriptions)
                ))
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

    def category_examples(self):
        result = {}
        for parent_index in range(self.category_tree.topLevelItemCount()):
            parent = self.category_tree.topLevelItem(parent_index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                paths = list(child.data(0, EXAMPLES_ROLE) or [])
                if paths:
                    result[f"{parent.text(0)}/{child.text(0)}"] = paths
        return result

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
        examples = dialog.examples()
        child = QtWidgets.QTreeWidgetItem([
            name, self._summary(descriptions, examples)
        ])
        child.setData(0, QtCore.Qt.UserRole, descriptions)
        child.setData(0, EXAMPLES_ROLE, examples)
        child.setToolTip(1, "\n".join(examples + descriptions))
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
        examples = list(item.data(0, EXAMPLES_ROLE) or [])
        dialog = _SubcategoryDialog(
            item.text(0), descriptions, examples, self.widget
        )
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        descriptions = dialog.descriptions()
        examples = dialog.examples()
        item.setText(0, dialog.name_edit.text().strip())
        item.setText(1, self._summary(descriptions, examples))
        item.setData(0, QtCore.Qt.UserRole, descriptions)
        item.setData(0, EXAMPLES_ROLE, examples)
        item.setToolTip(1, "\n".join(examples + descriptions))

    def _find_or_add_main(self, name):
        for index in range(self.category_tree.topLevelItemCount()):
            item = self.category_tree.topLevelItem(index)
            if item.text(0) == name:
                return item
        item = QtWidgets.QTreeWidgetItem([name, "0 个子类别"])
        item.setData(0, QtCore.Qt.UserRole, None)
        self.category_tree.addTopLevelItem(item)
        return item

    def _import_leaf(self, main_name, sub_name, folder):
        parent = self._find_or_add_main(main_name)
        child = next(
            (
                parent.child(index) for index in range(parent.childCount())
                if parent.child(index).text(0) == sub_name
            ),
            None,
        )
        if child is None:
            child = QtWidgets.QTreeWidgetItem([sub_name, ""])
            child.setData(0, QtCore.Qt.UserRole, [])
            child.setData(0, EXAMPLES_ROLE, [])
            parent.addChild(child)
        examples = list(child.data(0, EXAMPLES_ROLE) or [])
        folder = str(folder)
        if folder not in examples:
            examples.append(folder)
        descriptions = list(child.data(0, QtCore.Qt.UserRole) or [])
        child.setData(0, EXAMPLES_ROLE, examples)
        child.setText(1, self._summary(descriptions, examples))
        child.setToolTip(1, "\n".join(examples + descriptions))
        parent.setText(1, f"{parent.childCount()} 个子类别")
        parent.setExpanded(True)

    def _import_directory(self):
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self.widget, "选择已整理素材的根目录"
        )
        if not selected:
            return
        root = Path(selected)
        imported = 0
        for first in sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        ):
            subdirectories = [path for path in first.iterdir() if path.is_dir()]
            if subdirectories:
                for sub in sorted(subdirectories, key=lambda path: path.name):
                    self._import_leaf(first.name, sub.name, sub)
                    imported += 1
            elif any(
                path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
                for path in first.iterdir()
            ):
                self._import_leaf(root.name, first.name, first)
                imported += 1
        QtWidgets.QMessageBox.information(
            self.widget,
            "导入分类",
            f"已导入或更新 {imported} 个子类别。目录中的素材将作为参考，"
            "不会被移动。",
        )

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
            self._populate_categories(copy.deepcopy(DEFAULT_CATEGORY_TREE), {})

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
        self.video_frames_spin.setValue(settings["video_sample_frames"])
        self.maximum_examples_spin.setValue(
            settings["maximum_examples_per_category"]
        )
        self.recursive_checkbox.setChecked(settings["recursive"])
        self.operation_combo.setCurrentIndex(
            max(0, self.operation_combo.findData(settings["operation"]))
        )
        self._populate_categories(
            settings["categories"], settings["category_examples"]
        )

    def validate(self):
        categories = self.categories()
        if not categories:
            raise ValueError("素材智能分类至少需要一个主类别和一个子类别。")

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
            "video_sample_frames": self.video_frames_spin.value(),
            "maximum_examples_per_category": self.maximum_examples_spin.value(),
            "recursive": self.recursive_checkbox.isChecked(),
            "operation": self.operation_combo.currentData(),
            "categories": self.categories(),
            "category_examples": self.category_examples(),
        })

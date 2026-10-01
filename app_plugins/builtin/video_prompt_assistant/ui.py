from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text

from .cases import PromptCaseStore
from .templates import SCENE_LABELS, build_suggestions
from .vision import analyze_image


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".jfif"}


class ImageDropLabel(QtWidgets.QLabel):
    dropped = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("拖入图片，或点击“选择图片”", parent)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumSize(220, 260)
        self.setStyleSheet("QLabel { border: 1px dashed #8898a6; border-radius: 8px; padding: 12px; }")

    def _image_path(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in IMAGE_EXTENSIONS:
                return url.toLocalFile()
        return ""

    def dragEnterEvent(self, event):
        if self._image_path(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        path = self._image_path(event)
        if path:
            self.dropped.emit(path)
            event.acceptProposedAction()


class AnalyzeThread(QtCore.QThread):
    succeeded = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, image, keys, model, parent=None, gemini_keys=None):
        super().__init__(parent)
        self.image, self.keys, self.model = image, list(keys), model
        self.gemini_keys = gemini_keys

    def run(self):
        try:
            self.succeeded.emit(analyze_image(self.image, self.keys, self.model,
                                             gemini_keys=self.gemini_keys))
        except Exception as error:
            self.failed.emit(str(error))


class VideoPromptDialog(QtWidgets.QDialog):
    def __init__(self, context, parent=None, store=None):
        super().__init__(parent)
        self.context = context
        self.store = store or PromptCaseStore()
        self.image_path = ""
        self.image_jpeg = b""
        self.worker = None
        self.cloud_approved = False
        self.image_expression = "unknown"
        self.image_full_body = False
        self.case_id = None
        self.setWindowTitle("视频提示词助手")
        self.resize(1080, 700)
        self.setMinimumSize(800, 540)
        self._build_ui()
        self.refresh_cases()
        self.refresh_suggestions()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel("看图只负责判断画面；提示词沿用你常用的说法。生成前可自己改。")
        heading.setWordWrap(True)
        root.addWidget(heading)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        self.preview = ImageDropLabel()
        self.preview.dropped.connect(self.load_image)
        left_layout.addWidget(self.preview, 1)
        choose = QtWidgets.QPushButton("选择图片…")
        choose.clicked.connect(self.choose_image)
        left_layout.addWidget(choose)
        left_layout.addWidget(QtWidgets.QLabel("画面类型（识别不准时可手动改）"))
        self.scene = QtWidgets.QComboBox()
        for key, label in SCENE_LABELS.items():
            self.scene.addItem(label, key)
        self.scene.setCurrentIndex(self.scene.findData("unknown"))
        self.scene.currentIndexChanged.connect(self.refresh_suggestions)
        left_layout.addWidget(self.scene)
        left_layout.addWidget(QtWidgets.QLabel("主体称呼"))
        self.subject = QtWidgets.QLineEdit()
        self.subject.setPlaceholderText("例如：画面中的人物；不确定时留空")
        self.subject.textChanged.connect(self.refresh_suggestions)
        left_layout.addWidget(self.subject)
        self.has_text = QtWidgets.QCheckBox("画面有文字，必须保持可读")
        self.has_text.toggled.connect(self.refresh_suggestions)
        left_layout.addWidget(self.has_text)
        self.analyze_button = QtWidgets.QPushButton("用 Gemini 看图并刷新方案")
        self.analyze_button.clicked.connect(self.analyze)
        left_layout.addWidget(self.analyze_button)
        self.status = QtWidgets.QLabel("不使用看图服务时，仍可手动选类型生成通用方案。")
        self.status.setWordWrap(True)
        left_layout.addWidget(self.status)
        splitter.addWidget(left)

        self.tabs = QtWidgets.QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        self._build_suggestions_tab()
        self._build_cases_tab()

    def _build_suggestions_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(QtWidgets.QLabel("选择一个方案后直接修改文字；不会自动提交给视频生成网站。"))
        self.suggestion_list = QtWidgets.QListWidget()
        self.suggestion_list.setMaximumHeight(125)
        self.suggestion_list.currentRowChanged.connect(self.select_suggestion)
        layout.addWidget(self.suggestion_list)
        self.prompt_editor = QtWidgets.QPlainTextEdit()
        self.prompt_editor.setPlaceholderText("可在这里修改方案，也可以粘贴自己的提示词。")
        layout.addWidget(self.prompt_editor, 1)
        actions = QtWidgets.QHBoxLayout()
        copy = QtWidgets.QPushButton("复制提示词")
        copy.clicked.connect(lambda: set_internal_clipboard_text(self.prompt_editor.toPlainText()))
        save = QtWidgets.QPushButton("保存为用户建议文案…")
        save.clicked.connect(self.save_from_suggestion)
        actions.addWidget(copy)
        actions.addWidget(save)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(QtWidgets.QLabel("带文字的画面仍需人工检查；提示词不能保证生成模型逐字保留文字。"))
        self.tabs.addTab(page, "提示词方案")

    def _build_cases_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索标题、成功提示词或失败文案")
        self.search.textChanged.connect(self.refresh_cases)
        layout.addWidget(self.search)
        inner = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        layout.addWidget(inner, 1)
        self.case_list = QtWidgets.QListWidget()
        self.case_list.currentItemChanged.connect(self.select_case)
        inner.addWidget(self.case_list)
        editor = QtWidgets.QWidget()
        fields = QtWidgets.QVBoxLayout(editor)
        fields.addWidget(QtWidgets.QLabel("标题 *"))
        self.case_title = QtWidgets.QLineEdit()
        fields.addWidget(self.case_title)
        fields.addWidget(QtWidgets.QLabel("成功的提示词 *"))
        self.case_prompt = QtWidgets.QPlainTextEdit()
        fields.addWidget(self.case_prompt, 3)
        fields.addWidget(QtWidgets.QLabel("原来失败的文案（可选）"))
        self.case_failed = QtWidgets.QPlainTextEdit()
        fields.addWidget(self.case_failed, 2)
        fields.addWidget(QtWidgets.QLabel("经验备注（可选）"))
        self.case_note = QtWidgets.QPlainTextEdit()
        fields.addWidget(self.case_note, 1)
        inner.addWidget(editor)
        inner.setStretchFactor(0, 2)
        inner.setStretchFactor(1, 4)
        buttons = QtWidgets.QHBoxLayout()
        for label, slot in (("新建", self.new_case), ("保存案例", self.save_case),
                            ("复制成功文案", self.copy_case), ("用作当前方案", self.use_case),
                            ("删除选中", self.delete_case)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, "用户建议文案")

    def choose_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.jfif)")
        if path:
            self.load_image(path)

    def load_image(self, path):
        if self.is_busy():
            self.status.setText("当前图片还在分析，请稍候。")
            return
        image = QtGui.QImage(path)
        if image.isNull():
            QtWidgets.QMessageBox.warning(self, "无法读取图片", "请选择受支持的图片文件。")
            return
        small = image.scaled(1024, 1024, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                             QtCore.Qt.TransformationMode.SmoothTransformation)
        buffer = QtCore.QBuffer()
        if not buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly) or not small.save(buffer, "JPEG", 85):
            QtWidgets.QMessageBox.warning(self, "图片处理失败", "无法准备图片分析数据。")
            return
        self.image_jpeg = bytes(buffer.data())
        self.image_path = path
        self.image_expression = "unknown"
        self.image_full_body = False
        self.preview.setPixmap(QtGui.QPixmap.fromImage(image).scaled(
            260, 400, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation))
        self.preview.setToolTip(path)
        self.scene.setCurrentIndex(self.scene.findData("unknown"))
        self.subject.clear()
        self.has_text.setChecked(False)
        self.status.setText("图片已加载。已有通用方案；配置 Gemini Key 后可自动看图。")
        self.refresh_suggestions()
        self.tabs.setCurrentIndex(0)
        # The first image asks once before any upload; later drops in this
        # window can use the already-granted session consent automatically.
        if self._api_keys():
            QtCore.QTimer.singleShot(0, self.analyze)

    def refresh_suggestions(self, *_args):
        if not hasattr(self, "suggestion_list"):
            return
        suggestions = build_suggestions(
            self.scene.currentData() or "unknown", self.subject.text(),
            self.has_text.isChecked(), self.image_expression,
            self.image_full_body)
        self.suggestion_list.blockSignals(True)
        self.suggestion_list.clear()
        for title, prompt in suggestions:
            item = QtWidgets.QListWidgetItem(title)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, prompt)
            self.suggestion_list.addItem(item)
        self.suggestion_list.blockSignals(False)
        self.suggestion_list.setCurrentRow(0)

    def select_suggestion(self, row):
        item = self.suggestion_list.item(row)
        if item:
            self.prompt_editor.setPlainText(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def is_busy(self):
        return self.worker is not None and self.worker.isRunning()

    def _api_keys(self):
        # Also allow offline-only hosts that do not offer cloud services.
        manager = getattr(self.context, "gemini_keys", None)
        model = self.context.load_config().get("gemini_model") or "gemini-2.5-flash"
        return manager.request_keys(model) if manager is not None else []

    def analyze(self):
        if not self.image_jpeg or self.is_busy():
            self.status.setText("请先加载一张图片。" if not self.image_jpeg else "正在分析，请稍候。")
            return
        config = self.context.load_config()
        manager = self.context.gemini_keys
        model = config.get("gemini_model") or "gemini-2.5-flash"
        keys = manager.request_keys(model)
        if not keys:
            QtWidgets.QMessageBox.information(self, "Gemini Key 不可用", manager.unavailable_message(model))
            return
        if not self.cloud_approved:
            response = QtWidgets.QMessageBox.question(
                self, "发送图片进行分析",
                "看图会把当前图片的缩小版发送给 Google Gemini。是否继续？\n本工具只生成提示词，不会自动生成视频。",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if response != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.cloud_approved = True
        self.analyze_button.setEnabled(False)
        self.status.setText("正在分析图片…")
        self.worker = AnalyzeThread(self.image_jpeg, keys, model, self, gemini_keys=manager)
        self.worker.succeeded.connect(self._analysis_done)
        self.worker.failed.connect(self._analysis_failed)
        self.worker.finished.connect(self._analysis_finished)
        self.worker.start()

    def _analysis_done(self, result):
        self.image_expression = result["expression"]
        self.image_full_body = result["full_body"]
        self.scene.setCurrentIndex(self.scene.findData(result["scene"]))
        self.subject.setText(result["subject"])
        self.has_text.setChecked(result["has_text"])
        self.status.setText("识别参考：" + (result["observation"] or "完成。请检查画面类型和主体称呼。"))
        self.refresh_suggestions()

    def _analysis_failed(self, message):
        self.status.setText(message)
        self.context.log("图片分析失败；可继续使用手动方案。")

    def _analysis_finished(self):
        self.analyze_button.setEnabled(True)
        self.worker = None

    def refresh_cases(self, *_args):
        if not hasattr(self, "case_list"):
            return
        try:
            cases = self.store.load()
        except (OSError, ValueError) as error:
            self.status.setText(f"读取成功案例失败：{error}")
            return
        query = self.search.text().strip().casefold()
        self.case_list.blockSignals(True)
        self.case_list.clear()
        for case in cases:
            if query and query not in " ".join(str(case.get(key, "")) for key in
                                          ("title", "prompt", "failed_prompt", "note")).casefold():
                continue
            item = QtWidgets.QListWidgetItem(case.get("title") or "未命名")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, case)
            self.case_list.addItem(item)
        self.case_list.blockSignals(False)

    def select_case(self, current, _previous=None):
        if current is None:
            return
        case = current.data(QtCore.Qt.ItemDataRole.UserRole)
        self.case_id = case.get("id")
        self.case_title.setText(case.get("title", ""))
        self.case_prompt.setPlainText(case.get("prompt", ""))
        self.case_failed.setPlainText(case.get("failed_prompt", ""))
        self.case_note.setPlainText(case.get("note", ""))

    def new_case(self):
        self.case_list.clearSelection()
        self.case_list.setCurrentRow(-1)
        self.case_id = None
        self.case_title.clear()
        self.case_prompt.clear()
        self.case_failed.clear()
        self.case_note.clear()
        self.case_title.setFocus()

    def save_from_suggestion(self):
        prompt = self.prompt_editor.toPlainText().strip()
        if not prompt:
            return
        self.new_case()
        self.case_prompt.setPlainText(prompt)
        self.case_failed.setPlainText("")
        self.tabs.setCurrentIndex(1)

    def save_case(self):
        try:
            entry = self.store.upsert(
                self.case_title.text(), self.case_prompt.toPlainText(),
                self.case_failed.toPlainText(), self.case_note.toPlainText(), self.case_id)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "保存失败", str(error))
            return
        self.case_id = entry["id"]
        self.refresh_cases()
        for index in range(self.case_list.count()):
            item = self.case_list.item(index)
            if item.data(QtCore.Qt.ItemDataRole.UserRole).get("id") == self.case_id:
                self.case_list.setCurrentItem(item)
                break
        self.status.setText("成功案例已保存。")

    def copy_case(self):
        set_internal_clipboard_text(self.case_prompt.toPlainText())

    def use_case(self):
        self.prompt_editor.setPlainText(self.case_prompt.toPlainText())
        self.tabs.setCurrentIndex(0)

    def delete_case(self):
        if not self.case_id:
            return
        if QtWidgets.QMessageBox.question(self, "删除案例", "确定删除这个成功案例？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(self.case_id)
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "删除失败", str(error))
            return
        self.new_case()
        self.refresh_cases()

    def closeEvent(self, event):
        if self.is_busy():
            self.status.setText("正在分析图片，请等分析结束后关闭。")
            event.ignore()
        else:
            super().closeEvent(event)

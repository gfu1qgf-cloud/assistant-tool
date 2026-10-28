"""Per-export opt-in and access to the existing, independent node panel."""
from qt_compat import QtCore, QtWidgets
from .postprocess_workflow import TYPE_ID, ENABLED, WORKFLOW


def exec_review(dialog):
    """Wait for a decision while allowing the shared editor to become usable.

    QDialog.exec() forces application modality and quits when the dialog is
    hidden. A local loop survives the hide/show required by Qt to change an
    already visible window's modality, preserving all unsaved review edits.
    """
    loop = QtCore.QEventLoop(dialog)
    dialog.finished.connect(loop.quit)
    dialog.setModal(True)
    dialog.show()
    try:
        loop.exec()
        return dialog.result()
    finally:
        dialog.finished.disconnect(loop.quit)
        loop.deleteLater()


def _set_modal(dialog, modal):
    visible = dialog.isVisible()
    if visible:
        dialog.hide()
    dialog.setModal(modal)
    if visible:
        dialog.show()


class WorkflowChoice(QtWidgets.QComboBox):
    def __init__(self, services, selected="", parent=None):
        super().__init__(parent)
        self.services, self.selected = services, str(selected or "")
        self.setMinimumContentsLength(10)
        self.setSizeAdjustPolicy(self.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setToolTip("只显示“智能剪辑 · 导出后处理”工作流。保存到节点工作流库后，展开此列表即可刷新。")
        self.refresh()

    def refresh(self):
        selected = str(self.currentData() or self.selected or "")
        try:
            rows = self.services.workflow_library.list(TYPE_ID) if self.services is not None else []
        except Exception as error:
            self.setToolTip("无法刷新工作流列表，原选择已保留：" + str(error))
            if self.count():
                return
            rows = []
        self.blockSignals(True)
        self.clear()
        self.addItem("请选择节点工作流", "")
        for row in rows:
            self.addItem(row["name"], row["id"])
        index = self.findData(selected)
        if selected and index < 0:
            self.addItem("所选工作流不可用 · 请重新选择", selected)
            index = self.count() - 1
        self.setCurrentIndex(max(0, index))
        self.blockSignals(False)

    def showPopup(self):
        self.selected = str(self.currentData() or "")
        self.refresh()
        super().showPopup()


class PostprocessControls(QtWidgets.QWidget):
    def __init__(self, settings, services=None, parent=None):
        super().__init__(parent)
        self.services = services
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.enabled_box = QtWidgets.QCheckBox("导出后处理", self)
        self.enabled_box.setChecked(bool(settings.get(ENABLED, False)))
        self.enabled_box.setToolTip("默认关闭。开启后，对本次成片或各个分段运行所选节点流，原成片保留。")
        self.choice = WorkflowChoice(services, settings.get(WORKFLOW, ""), self)
        self.editor_button = QtWidgets.QPushButton("节点面板…", self)
        self.editor_button.setToolTip("编辑所选流程；未选择时新建“智能剪辑 · 导出后处理”，保存后展开列表选择。")
        self.editor_button.setEnabled(callable(getattr(services, "open_workflow_editor", None)))
        layout.addWidget(self.enabled_box)
        layout.addWidget(self.choice, 1)
        layout.addWidget(self.editor_button)
        self.choice.setEnabled(self.enabled_box.isChecked())
        self.enabled_box.toggled.connect(self.choice.setEnabled)
        self.editor_button.clicked.connect(self.open_editor)

    def selection(self, *, validate=False):
        enabled = self.enabled_box.isChecked()
        key = str(self.choice.currentData() or "")
        if validate and enabled and not key:
            raise ValueError("已开启导出后处理，请先选择或创建一个节点工作流。")
        return {ENABLED: enabled, WORKFLOW: key}

    def open_editor(self):
        owner = self.window()
        was_modal = isinstance(owner, QtWidgets.QDialog) and owner.isModal()
        try:
            # The shared editor is an independent window. Keep the review's
            # event loop/data, while allowing this explicitly opened editor.
            if was_modal:
                _set_modal(owner, False)
            opened = self.services.open_workflow_editor(workflow_id=str(self.choice.currentData() or ""), workflow_type=TYPE_ID)
            if opened is False and was_modal:
                _set_modal(owner, True)
        except Exception as error:
            if was_modal:
                _set_modal(owner, True)
            QtWidgets.QMessageBox.warning(self, "节点面板未打开", str(error))

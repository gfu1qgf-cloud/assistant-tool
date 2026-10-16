"""A plugin-specific, live refreshed choice of saved workflow contracts."""
import logging
from qt_compat import QtWidgets
from model.node_workflow.library import WorkflowLibrary
from .task_delivery_workflow import CONFIG_KEY, TYPE_ID


class PostprocessChoice(QtWidgets.QComboBox):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.setObjectName('task_delivery_postprocess_workflow')
        self.setMinimumContentsLength(8)
        self.setSizeAdjustPolicy(self.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setToolTip('只显示任务交付·视频后处理类型。到工作流面板：新建工作流 → 选择类型 → 保存到工作流库。\n'
                        '自定义节点流替换原压缩阶段，保存／上传及审核去重仍由插件负责。打开下拉会刷新列表。')
        self.refresh()
        self.currentIndexChanged.connect(self.persist_choice)

    def refresh(self):
        try:
            selected = str(self.context.load_config().get(CONFIG_KEY) or '') if self.context else ''
            rows = WorkflowLibrary().list(TYPE_ID)
        except Exception as error:
            logging.getLogger(__name__).exception('后续处理列表刷新失败')
            if not self.count():
                self.blockSignals(True)
                self.addItem('内置处理（现有压缩规则）', '')
                self.blockSignals(False)
            self.setToolTip('无法读取配置或工作流列表；原选择未修改。\n' + str(error))
            return False
        self.blockSignals(True)
        self.clear()
        self.addItem('内置处理（现有压缩规则）', '')
        for row in rows:
            self.addItem(row['name'], row['id'])
        index = self.findData(selected)
        if index < 0:
            self.addItem('所选工作流不可用 · 请重新选择', selected)
            index = self.count() - 1
        self.setCurrentIndex(index)
        self.blockSignals(False)
        return True

    def showPopup(self):
        self.refresh()
        super().showPopup()

    def persist_choice(self, _index):
        if self.context is None:
            return
        try:
            config = self.context.load_config()
            value = self.currentData()
            if value == str(config.get(CONFIG_KEY) or ''):
                return
            if not self.context.patch_config({CONFIG_KEY: value}, expected=config):
                raise RuntimeError('配置未保存，原选择已保留。')
        except Exception as error:
            self.refresh()
            QtWidgets.QMessageBox.warning(self, '后续处理', str(error))

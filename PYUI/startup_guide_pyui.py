"""Reopenable guide owned by the existing pre-storage startup chooser."""
import copy
from pathlib import Path

from qt_compat import QtWidgets
from model.FirstRun import first_use_state
from .first_run_pyui import FirstRunDialog


class StartupGuideDialog(QtWidgets.QDialog):
    def __init__(self, program_root, *, bound=False, initial_config=None, parent=None):
        super().__init__(parent)
        self.program_root = Path(program_root)
        self.state = 'bound' if bound else first_use_state(self.program_root)
        self.initial_config = copy.deepcopy(initial_config) if self.state == 'fresh' else None
        self.setWindowTitle('首次使用／配置向导')
        self.resize(650, 490)
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel('在启动窗口随时打开，不需要删除配置或重新安装。', self)
        title.setWordWrap(True)
        layout.addWidget(title)
        self.state_note = QtWidgets.QLabel(self)
        self.state_note.setWordWrap(True)
        if self.state == 'bound':
            text = '当前已绑定加密数据。继续使用原来的密盘即可；此向导不会读取旧明文配置，也不会重建、覆盖账号和历史。'
        elif self.state == 'existing':
            text = '检测到已有普通配置。可以迁移到密盘，或者选择卷内已有的完整数据；不会用新用户默认值重置现有设置。'
        elif self.state == 'recovery':
            text = '检测到旧数据但主配置缺失。不能创建空配置顶替；请选用原来的完整加密数据，或先恢复配置备份。'
        elif self.initial_config is not None:
            text = '首次配置已有草稿，尚未写入密盘。可以修改草稿，或直接返回选择密盘。'
        else:
            text = '尚未发现配置。新用户可以填写首次配置；如果你有旧密盘，请直接返回并选择已有数据。'
        self.state_note.setText(text)
        layout.addWidget(self.state_note)
        steps = (
            ('1. 挂载密盘', '先在 VeraCrypt 中手动挂载。程序不接收密码，不会替你挂载或卸载。'),
            ('2. 选择数据', '返回启动窗口后，选择已挂载的卷。已有数据点击“使用所选加密数据”；新用户先在下方填写首次配置，再在启动窗口确认新建。普通配置迁移需先关闭其他实例并明确确认。'),
            ('3. 进入程序后完善设置', '任务目录在主界面选择；账号、API Key、表格地址和插件开关在“程序设置”中修改。已有设置保持不变，缺失配置或错误卷不会被空配置替代。'),
        )
        for heading, description in steps:
            group = QtWidgets.QGroupBox(heading, self)
            box = QtWidgets.QVBoxLayout(group)
            label = QtWidgets.QLabel(description, group)
            label.setWordWrap(True)
            box.addWidget(label)
            layout.addWidget(group)
        self.create_button = QtWidgets.QPushButton(
            '修改首次配置草稿…' if self.initial_config is not None else '新用户：填写首次配置…', self)
        self.create_button.setEnabled(self.state == 'fresh')
        self.create_button.setToolTip('只适用于没有旧配置或旧数据的新用户；不在程序目录生成明文配置。')
        self.create_button.clicked.connect(self.edit_initial_config)
        layout.addWidget(self.create_button)
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.return_button = QtWidgets.QPushButton('返回选择密盘', self)
        self.return_button.setDefault(True)
        self.return_button.clicked.connect(self.accept)
        layout.addWidget(self.return_button)

    def edit_initial_config(self):
        if self.state != 'fresh' or first_use_state(self.program_root) != 'fresh':
            self.status.setText('检测到已有配置或旧数据，不能新建空配置；原数据未改变。')
            self.create_button.setEnabled(False)
            return
        dialog = FirstRunDialog(self.program_root, force_vc=True, parent=self,
                                initial_config=self.initial_config, draft_only=True)
        dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('保存草稿并返回选盘')
        dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('返回向导')
        try:
            result = dialog.exec()
            decision = dialog.decision
        finally:
            dialog.deleteLater()
        if result != QtWidgets.QDialog.DialogCode.Accepted:
            return
        if decision is None or decision.mode != 'veracrypt' or not isinstance(decision.initial_config, dict):
            self.status.setText('未取得首次密盘配置，请保留原数据并重新检查。')
            return
        self.initial_config = copy.deepcopy(decision.initial_config)
        self.accept()

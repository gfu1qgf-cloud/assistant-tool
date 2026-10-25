"""First-use UI; no model, plugin, credential or private store initialization."""
from pathlib import Path
from qt_compat import QtWidgets
from model.FirstRun import (FirstUseDecision, first_use_state, initial_settings,
                            create_normal_config, read_config_object)


class FirstRunDialog(QtWidgets.QDialog):
    def __init__(self, program_root, *, force_vc=False, parent=None):
        super().__init__(parent)
        self.program_root = Path(program_root)
        self.force_vc = bool(force_vc)
        self.state = first_use_state(self.program_root)
        self.decision = None
        self.setWindowTitle('欢迎使用 · 首次配置')
        self.resize(660, 520)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel('先建立配置，再初始化字幕模型。账号、API Key、表格地址都可以进入程序设置后再填写。', self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        storage_group = QtWidgets.QGroupBox('1. 选择配置保存方式', self)
        storage_layout = QtWidgets.QVBoxLayout(storage_group)
        self.normal = QtWidgets.QRadioButton('普通模式：配置保存到程序目录', storage_group)
        self.vc = QtWidgets.QRadioButton('VeraCrypt 模式：下一步选择已挂载的密盘', storage_group)
        self.normal.setChecked(not force_vc)
        self.vc.setChecked(force_vc)
        self.normal.setEnabled(not force_vc)
        storage_layout.addWidget(self.normal)
        storage_layout.addWidget(self.vc)
        path = QtWidgets.QLabel(str(self.program_root / 'config.json'), storage_group)
        path.setWordWrap(True)
        storage_layout.addWidget(path)
        hint = QtWidgets.QLabel('密盘模式直接在卷内新建配置，不在外部生成明文副本；也可以选择卷内已有数据。程序不接收密盘密码。', storage_group)
        hint.setWordWrap(True)
        storage_layout.addWidget(hint)
        layout.addWidget(storage_group)
        options = QtWidgets.QGroupBox('2. 基础选项（以后仍可修改）', self)
        form = QtWidgets.QFormLayout(options)
        task_row = QtWidgets.QWidget(options)
        row = QtWidgets.QHBoxLayout(task_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.task_path = QtWidgets.QLineEdit(task_row)
        self.task_path.setPlaceholderText('可留空，进入主界面后再选择任务目录')
        browse = QtWidgets.QPushButton('选择…', task_row)
        browse.clicked.connect(self.choose_task_path)
        row.addWidget(self.task_path)
        row.addWidget(browse)
        form.addRow('任务目录', task_row)
        self.model = QtWidgets.QComboBox(options)
        for name, label in [('base', 'base（默认，下载较小）'), ('small', 'small'), ('medium', 'medium'), ('large-v3', 'large-v3（下载和内存较大）')]:
            self.model.addItem(label, name)
        form.addRow('字幕模型', self.model)
        self.offline = QtWidgets.QCheckBox('离线安全模式：不联网，不下载模型', options)
        from model.OfflineMode import is_offline
        self.offline.setChecked(is_offline())
        if is_offline():
            self.offline.setEnabled(False)
            self.offline.setToolTip('本次启动已强制离线，不能在向导中取消。')
        form.addRow(self.offline)
        note = QtWidgets.QLabel('首次在线启动可能需要下载模型。模型加载失败会明确提示；已保存的配置不会因此删除。离线模式需事先准备本地模型。', options)
        note.setWordWrap(True)
        form.addRow(note)
        layout.addWidget(options)
        plugins = QtWidgets.QLabel('新配置默认关闭：账号切换、生活提醒、AI模型试用台、文档自动格式化、视频提示词助手。'
                                  '可在“程序设置 → 插件管理”中启用，重启生效；Flow 保持原默认状态。', self)
        plugins.setWordWrap(True)
        layout.addWidget(plugins)
        self.restore = QtWidgets.QPushButton('已有配置？从备份恢复…', self)
        self.restore.clicked.connect(self.restore_config)
        layout.addWidget(self.restore)
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, self)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setText('创建配置并继续')
        self.buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('退出')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if self.state == 'recovery':
            storage_group.setEnabled(False)
            options.setEnabled(False)
            self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(False)
            self.status.setText('检测到旧私有数据，但 config.json 缺失。为避免看似丢失账号或历史，本次不创建空配置。请从备份恢复，或退出后恢复原数据。恢复到程序目录后可再启用密盘迁移。')

    def choose_task_path(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '选择任务目录', self.task_path.text())
        if path:
            self.task_path.setText(path)

    def accept(self):
        if self.state not in {'fresh'}:
            return
        try:
            seed = initial_settings(self.program_root, task_path=self.task_path.text(),
                                    offline=self.offline.isChecked(), model=self.model.currentData())
            if self.vc.isChecked():
                self.decision = FirstUseDecision('veracrypt', seed)
            else:
                create_normal_config(self.program_root, seed)
                self.decision = FirstUseDecision('normal', created=True)
        except (OSError, ValueError) as error:
            self.status.setText('配置未创建：' + str(error) + '\n若程序目录不可写，请将完整程序目录移到可写位置后重试。')
            return
        super().accept()

    def restore_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择已有配置备份', '', 'JSON 配置 (*.json);;所有文件 (*)')
        if not path:
            return
        if QtWidgets.QMessageBox.question(self, '恢复已有配置',
                '将所选备份恢复为程序目录中的 config.json；不会覆盖已有配置。备份可能含密钥，请确认保存位置可接受。继续吗？',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        try:
            create_normal_config(self.program_root, read_config_object(path), restoring=True)
        except (OSError, ValueError) as error:
            self.status.setText('未恢复配置：' + str(error))
            return
        self.decision = FirstUseDecision('existing', created=True)
        super().accept()


def configure_first_use(program_root, *, force_vc=False):
    if first_use_state(program_root) in {'bound', 'existing'}:
        return FirstUseDecision()
    dialog = FirstRunDialog(program_root, force_vc=force_vc)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    return dialog.decision

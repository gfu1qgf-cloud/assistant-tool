"""Explicit project output destination, shared by tasks and plain media."""
from qt_compat import QtWidgets
from .component_ui import Combo


def build(host):
    widget = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 0)
    row = QtWidgets.QHBoxLayout(); layout.addLayout(row)
    row.addWidget(QtWidgets.QLabel('当前项目导出到'))
    mode = Combo(); mode.setObjectName('exportDestination'); mode.setMinimumWidth(220)
    mode.setAccessibleName('导出位置')
    for text, value in (('原任务目录／默认位置', 'auto'), ('指定文件夹', 'directory'),
                        ('指定文件夹，保留相对目录', 'relative')):
        mode.addItem(text, value)
    mode.setCurrentIndex(max(0, mode.findData(host.state['settings'].get('output_destination', 'auto'))))
    host.controls['output_destination'] = mode
    row.addWidget(mode)
    host.output = QtWidgets.QLineEdit(host.state['settings']['output_dir'])
    row.addWidget(host.output, 1)
    browse = QtWidgets.QPushButton('选择…'); browse.clicked.connect(host.choose_output); row.addWidget(browse)
    host.open_directory_button = QtWidgets.QPushButton('打开目录'); host.open_directory_button.setAutoDefault(False)
    host.open_directory_button.clicked.connect(host.open_output_directory); row.addWidget(host.open_directory_button)
    relative = QtWidgets.QWidget(); sub = QtWidgets.QHBoxLayout(relative); sub.setContentsMargins(0, 0, 0, 0)
    sub.addWidget(QtWidgets.QLabel('相对目录基准'))
    root = QtWidgets.QLineEdit(host.state['settings'].get('output_relative_root', ''))
    root.setPlaceholderText('留空：自动使用任务目录的公共上级')
    root.setToolTip('例如基准 D:/项目，任务 D:/项目/reels/001，导出到指定文件夹下的 reels/001。')
    host.controls['output_relative_root'] = root; sub.addWidget(root, 1)
    root.textChanged.connect(host.schedule_edit)
    choose = QtWidgets.QPushButton('选择基准…'); sub.addWidget(choose)
    def choose_root():
        path = QtWidgets.QFileDialog.getExistingDirectory(host, '选择相对目录基准', root.text())
        if path: root.setText(path)
    choose.clicked.connect(choose_root); layout.addWidget(relative)
    def update(*_):
        value = mode.currentData()
        relative.setVisible(value == 'relative')
        host.output.setPlaceholderText('普通视频的默认文件夹；任务使用原任务目录' if value == 'auto' else '所有成品导出到此文件夹')
        host.output.setToolTip('指定文件夹对表格、人声任务和普通视频均生效；仅改变成品位置。')
    mode.currentIndexChanged.connect(update)
    host.output.textEdited.connect(lambda _: mode.setCurrentIndex(mode.findData('directory')) if mode.currentData() == 'auto' else None)
    naming = QtWidgets.QHBoxLayout()
    project_prefix = QtWidgets.QCheckBox('文件名前加项目名（全部项目）')
    project_prefix.setObjectName('exportProjectPrefix')
    project_prefix.setChecked(host.state['settings'].get('output_project_prefix', False))
    project_prefix.setToolTip('项目名_原文件名.mp4；按全局导出偏好保存。关闭后恢复原有文件名。任务若按固定候选名称整理，请对应调整候选文件名。')
    host.controls['output_project_prefix'] = project_prefix
    naming.addWidget(project_prefix)
    example = QtWidgets.QLabel('项目名_原文件名.mp4')
    project_prefix.toggled.connect(example.setVisible)
    project_prefix.toggled.connect(lambda _: host.render_queue_panel.flat.refresh_names())
    example.setVisible(project_prefix.isChecked())
    naming.addWidget(example); naming.addStretch()
    layout.addLayout(naming)
    update()
    host.export_destination_widget = widget
    return widget

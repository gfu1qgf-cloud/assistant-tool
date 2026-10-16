"""Core settings page: scheduling limits, not encoder/model parameters."""
from copy import deepcopy
from qt_compat import QtWidgets
from model.BackgroundTaskSettings import DEFAULTS,VIDEO_PRIORITY,MAX_PARALLEL,normalize_settings


class BackgroundTaskSettingsPage(QtWidgets.QScrollArea):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        body=QtWidgets.QWidget();self.setWidget(body)
        root=QtWidgets.QVBoxLayout(body)
        note=QtWidgets.QLabel('这些数字决定后台任务可同时运行的数量，不是整个程序的线程总数。'
            '总上限和分类上限同时生效，超过上限的任务继续排队。')
        note.setWordWrap(True);root.addWidget(note)
        group=QtWidgets.QGroupBox('可调整的并发上限')
        form=QtWidgets.QFormLayout(group)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        self.controls={}
        for key,label in (('max_parallel','后台总并发'),('media','视频／媒体处理'),('network','联网／上传／下载')):
            spin=QtWidgets.QSpinBox(group);spin.setRange(1,MAX_PARALLEL);spin.setSuffix(' 个')
            spin.valueChanged.connect(self.update_effective_limits)
            self.controls[key]=spin;form.addRow(label,spin)
        self.effective=QtWidgets.QLabel();self.effective.setWordWrap(True)
        form.addRow(self.effective);root.addWidget(group)
        buttons=QtWidgets.QHBoxLayout()
        defaults=QtWidgets.QPushButton('恢复默认：3／2／2')
        defaults.setToolTip('后台总并发3，媒体2，联网2')
        video=QtWidgets.QPushButton('视频优先：6／4／2')
        video.setToolTip('后台总并发6，媒体4，联网2')
        defaults.clicked.connect(lambda:self.set_settings(DEFAULTS))
        video.clicked.connect(lambda:self.set_settings(VIDEO_PRIORITY))
        buttons.addWidget(defaults);buttons.addWidget(video);buttons.addStretch(1)
        root.addLayout(buttons)
        fixed=QtWidgets.QGroupBox('保留的安全限制')
        policy=QtWidgets.QFormLayout(fixed)
        policy.setRowWrapPolicy(policy.RowWrapPolicy.WrapLongRows)
        for label,text in (('共享模型任务','固定 1 个；本地字幕识别／对齐也保留推理锁'),
                           ('CPU类通用脚本','固定 1 个；不放开未经并发验证的通用脚本'),
                           ('资源／目录互斥','始终保留；不能同时覆盖同一输出或私有状态')):
            field=QtWidgets.QLabel(text);field.setWordWrap(True);policy.addRow(label,field)
        root.addWidget(fixed)
        help_label=QtWidgets.QLabel('点击“确定”并保存成功后，等待中的任务即可使用新上限，无需重启。'
            '降低上限不会中断正在运行的任务，会等运行数量降下来后再启动新任务。\n\n'
            '视频并发过高可能占满内存／显存、降低单条速度；联网过高可能触发服务端限流。'
            '文案视频的最终编码路数仍在其右侧“渲染队列”设置，两层上限并非同一个数字。')
        help_label.setWordWrap(True);root.addWidget(help_label)
        self.warning=QtWidgets.QLabel();self.warning.setWordWrap(True);self.warning.setStyleSheet('color:#b94d28')
        root.addWidget(self.warning);root.addStretch(1)
        self.invalid_original=None;self.invalid=False
        self.set_settings(DEFAULTS)

    def set_settings(self,value):
        self.invalid=False;self.invalid_original=None
        try:settings=normalize_settings(value)
        except ValueError as error:
            self.invalid=True;self.invalid_original=deepcopy(value);settings=deepcopy(DEFAULTS)
            self.warning.setText(str(error)+' 已显示安全默认值；点击“确定”可修复并保存。')
        else:self.warning.clear()
        self.warning.setVisible(self.invalid)
        self.controls['max_parallel'].setValue(settings['max_parallel'])
        for key in ('media','network'):self.controls[key].setValue(settings['kind_limits'][key])
        self.update_effective_limits()

    def settings(self):
        return normalize_settings({'max_parallel':self.controls['max_parallel'].value(),
            'kind_limits':{key:self.controls[key].value() for key in ('media','network')}})

    def update_effective_limits(self,*_args):
        total=self.controls['max_parallel'].value()
        self.effective.setText(f'按当前总上限：媒体最多 {min(total,self.controls["media"].value())} 个，'
            f'联网最多 {min(total,self.controls["network"].value())} 个；所有类别合计最多 {total} 个。')

"""A compact encoder inspector; presets are editable values, never UI locks."""
import copy
from dataclasses import asdict
from qt_compat import QtCore, QtWidgets
from model.node_workflow.encoding import PARAMETERS, from_parameters, video_encoder_args, audio_encoder_args, summary


class CompressionInspector(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        self.changed = changed
        self.values = asdict(from_parameters(params))
        self.controls, self.rows, self.loading = {}, {}, True
        layout = QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        preset = QtWidgets.QPushButton('应用截图配置 · H.264 30＋MP3 128',self)
        preset.setToolTip('仅设置参数，不锁定任何控件。你的其他节点和连线不会改变。')
        preset.clicked.connect(self.apply_screenshot); layout.addWidget(preset)
        self.tabs = QtWidgets.QTabWidget(self); layout.addWidget(self.tabs)
        pages = {}
        for key, name in [('video','编码'),('picture','画面'),('audio','音频'),('extra','其他')]:
            page=QtWidgets.QWidget(); form=QtWidgets.QFormLayout(page)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setContentsMargins(6,8,6,8); pages[key]=form; self.tabs.addTab(page,name)
        pages['video'].addRow('文件格式',QtWidgets.QLabel('MP4（由导出节点／主程序保存）'))
        self.add(pages['video'],'codec'); self.add(pages['video'],'rate_control')
        for key in ('crf','qp','video_kbps','preset'): self.add(pages['video'],key)
        note=QtWidgets.QLabel('质量数值越大，压缩越强、细节越少。\n恒定质量不保证体积变小；需要控体积请选择“目标码率”。')
        note.setWordWrap(True); pages['video'].addRow(note)
        self.add(pages['picture'],'frame_rate',switch='frame_rate_enabled')
        self.add(pages['picture'],'cfr')
        self.add(pages['picture'],'keyframe_seconds',switch='keyframe_enabled')
        self.add(pages['picture'],'resize_enabled')
        for key in ('width','height','resize_mode'): self.add(pages['picture'],key)
        note=QtWidgets.QLabel('完整画面：宽高作为最大边界。补边、裁切、拉伸：输出精确宽高。默认不改尺寸，不裁画面。')
        note.setWordWrap(True); pages['picture'].addRow(note)
        for key in ('audio','audio_mode','audio_kbps'): self.add(pages['audio'],key)
        self.choice(pages['audio'],'channels','声道',[(0,'原版'),(1,'单声道'),(2,'立体声')])
        self.choice(pages['audio'],'sample_rate','采样率',[(0,'保留原采样率')]+[(n,f'{n} Hz') for n in (8000,11025,12000,16000,22050,24000,32000,44100,48000)])
        note=QtWidgets.QLabel('Auto 使用指定码率；CBR／ABR 为 MP3 模式。\n保留原编码时，码率、采样率和声道不修改。拼接音轨无法直接复制时会明确提示改用AAC。')
        note.setWordWrap(True); pages['audio'].addRow(note)
        for key in ('profile','tune','faststart','strip_metadata'): self.add(pages['extra'],key)
        note=QtWidgets.QLabel('字幕、抠像、LUT、叠加继续使用对应节点。此处统一最终编码，不调用Shana、不生成中间视频。')
        note.setWordWrap(True); pages['extra'].addRow(note)
        self.info=QtWidgets.QLabel(); self.info.setWordWrap(True); self.info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)
        self.loading=False; self.update_groups(); self.update_summary()

    def choice(self, form, key, title, choices):
        widget=QtWidgets.QComboBox(self)
        for value,label in choices: widget.addItem(label,value)
        widget.setCurrentIndex(widget.findData(self.values[key]))
        widget.currentIndexChanged.connect(lambda _index,k=key,w=widget:self.edit(k,w.currentData()))
        self.controls[key]=widget; form.addRow(title,widget); self.rows[key]=(form.labelForField(widget),widget)

    def add(self, form, key, switch=None):
        param=next(p for p in PARAMETERS if p.key==key)
        if param.kind=='choice':
            self.choice(form,key,param.title,param.choices); return
        if param.kind=='boolean':
            widget=QtWidgets.QCheckBox(param.title,self); widget.setChecked(self.values[key])
            widget.toggled.connect(lambda checked,k=key:self.edit(k,checked)); form.addRow(widget)
            self.controls[key]=widget; return
        widget=QtWidgets.QDoubleSpinBox(self) if param.kind=='number' else QtWidgets.QSpinBox(self)
        widget.setRange(param.minimum,param.maximum)
        if isinstance(widget,QtWidgets.QDoubleSpinBox): widget.setDecimals(1 if key=='crf' else 3)
        widget.setKeyboardTracking(False); widget.setValue(self.values[key])
        widget.valueChanged.connect(lambda value,k=key:self.edit(k,value))
        label=param.title
        if switch:
            checkbox=QtWidgets.QCheckBox(param.title,self); checkbox.setChecked(self.values[switch])
            checkbox.toggled.connect(lambda checked,k=switch:self.edit(k,checked))
            self.controls[switch]=checkbox; label=checkbox
        form.addRow(label,widget); self.controls[key]=widget
        self.rows[key]=(form.labelForField(widget),widget)

    def edit(self, key, value):
        if self.loading: return
        previous=copy.deepcopy(self.values)
        self.values[key]=value
        if key=='codec' and value=='libx265':
            if self.values['profile'] not in {'auto','main'}: self.values['profile']='auto'
            if self.values['tune'] in {'film','stillimage'}: self.values['tune']='none'
            self.load_controls()
        if key=='audio' and value=='mp3' and self.values['audio_kbps']>320:
            self.values['audio_kbps']=320; self.load_controls()
        try:
            from_parameters(self.values)
            if self.changed(copy.deepcopy(self.values)) is False:
                raise ValueError('参数未通过节点校验；请检查输入数值。')
        except Exception as error:
            self.values=previous; self.load_controls(); self.info.setText(str(error)); return
        self.update_groups(); self.update_summary()

    def load_controls(self):
        self.loading=True
        try:
            for key,widget in self.controls.items():
                with QtCore.QSignalBlocker(widget):
                    if isinstance(widget,QtWidgets.QComboBox): widget.setCurrentIndex(widget.findData(self.values[key]))
                    elif isinstance(widget,QtWidgets.QCheckBox): widget.setChecked(self.values[key])
                    else: widget.setValue(self.values[key])
        finally: self.loading=False

    def apply_screenshot(self):
        previous=copy.deepcopy(self.values)
        self.values=asdict(from_parameters({}))
        try:
            if self.changed(copy.deepcopy(self.values)) is False: raise ValueError('截图配置未保存，请检查工作流。')
        except Exception as error:
            self.values=previous; self.info.setText(str(error)); return
        self.load_controls(); self.update_groups(); self.update_summary()

    def update_groups(self):
        active={'crf':'crf','qp':'qp','video_kbps':'bitrate'}
        for key,mode in active.items():
            for widget in self.rows[key]:
                if widget is not None: widget.setVisible(self.values['rate_control']==mode)
        for field,switch in [('frame_rate','frame_rate_enabled'),('keyframe_seconds','keyframe_enabled')]:
            self.controls[field].setEnabled(self.values[switch])
        for key in ('width','height','resize_mode'): self.controls[key].setEnabled(self.values['resize_enabled'])
        for key in ('audio_kbps','sample_rate','channels'): self.controls[key].setEnabled(self.values['audio']!='copy')
        self.controls['audio_mode'].setEnabled(self.values['audio']=='mp3')

    def update_summary(self):
        settings=from_parameters(self.values)
        self.info.setText(summary(settings)+'\n\n参数：'+' '.join(video_encoder_args(settings)+audio_encoder_args(settings)))

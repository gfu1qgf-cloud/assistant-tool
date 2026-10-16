"""Source inspector reuses the batch content picker, not a JSON/hex textbox."""
import copy
from qt_compat import QtWidgets
from .component_ui import ContentSourceEditor
from .workflow_component_source import normalized


class ComponentSourceEditor(QtWidgets.QWidget):
    def __init__(self, params, changed, parent=None):
        super().__init__(parent)
        self.services = getattr(parent,'workflow_services',None)
        self.params, self.callback = copy.deepcopy(params), changed
        self.value = normalized(params['source'])
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        note = QtWidgets.QLabel('任务标题／正文：直接从“文案视频输入”连接“添加文字”的文字输入，无需经过本节点。'
            '这里只处理独立轮换来源／字幕时间。字号、位置、动画和颜色在后面的图层节点中设置。'
            '轮换只在最终导出成功后推进，预览不消耗素材。')
        note.setWordWrap(True); root.addWidget(note)
        self.error = QtWidgets.QLabel(); self.error.setWordWrap(True); root.addWidget(self.error)
        self.content = ContentSourceEditor(self)
        self.content.allow_copy_pick = False
        self.content.set_layer(self.value)
        root.addWidget(self.content)
        self.content.changed.connect(self.edit)
        self.content.addImages.connect(self.append_files)
        if self.value['kind'] == 'caption':
            form = QtWidgets.QFormLayout()
            self.timing = QtWidgets.QComboBox()
            for title, key in (('自动：ELE逐词记录／任务SRT，否则本地对齐', 'auto'), ('直接使用任务字幕', 'task'),
                    ('使用来源中的固定／轮换SRT', 'file'), ('本地模型对齐任务文案', 'model'), ('匀速动画（近似）', 'uniform'),
                    ('ELE 云端对齐（上传音频与文案）', 'elevenlabs')):
                self.timing.addItem(title, key)
            self.timing.setCurrentIndex(self.timing.findData(self.value['caption_timing']))
            self.timing.currentIndexChanged.connect(self.timing_changed)
            form.addRow('时间戳策略', self.timing)
            self.text_source = QtWidgets.QComboBox()
            for label, key in (('正文', 'body'), ('标题＋正文', 'full'), ('识别人声', 'asr')):
                self.text_source.addItem(label, key)
            self.text_source.setCurrentIndex(self.text_source.findData(self.value['caption_source']))
            self.text_source.currentIndexChanged.connect(self.timing_changed)
            form.addRow('对齐文案', self.text_source)
            self.language = QtWidgets.QLineEdit(self.value['caption_language'])
            self.language.editingFinished.connect(self.timing_changed)
            form.addRow('语言代码', self.language)
            self.consent = QtWidgets.QCheckBox('允许上传人声和文案并消耗 ELE 对齐额度')
            self.consent.setChecked(self.value.get('caption_cloud_consent',False))
            self.consent.toggled.connect(self.timing_changed)
            form.addRow(self.consent)
            root.addLayout(form)
        form = QtWidgets.QFormLayout()
        self.project = QtWidgets.QLabel('由本次文案视频运行传入，不保存路径或配置编号。')
        self.project.setWordWrap(True)
        form.addRow('运行时来源',self.project)
        root.addLayout(form)
        refresh = QtWidgets.QPushButton('刷新来源项目中的资源池')
        refresh.clicked.connect(self.reload); root.addWidget(refresh)
        self.reload()

    def append_files(self, _identifier):
        from pathlib import Path
        from uuid import uuid4
        from .components import CONTENT_FIELDS, MAX_SEQUENCE_ITEMS
        from .visual_media import VISUAL_FILTER
        caption = self.value['kind'] == 'caption'
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '追加轮换素材', '',
            '字幕 (*.srt)' if caption else VISUAL_FILTER)
        if not paths: return
        value = copy.deepcopy(self.value)
        field = CONTENT_FIELDS[value['kind']]
        if len(value['sequence_items']) + len(paths) > MAX_SEQUENCE_ITEMS:
            self.error.setText('每个轮换来源最多1000项，未追加。'); return
        value['sequence_items'].extend({'id':uuid4().hex, field:str(Path(p).resolve()),
            'path':str(Path(p).resolve()), 'name':Path(p).name} for p in paths)
        self.content.set_layer(value)
        self.edit(value)

    def reload(self):
        from .project_runtime import project_runtime
        try:
            project = project_runtime(self.services)
            data = project['data']
            self.project.setText('当前文案视频项目 · '+project.get('name',''))
            self.content.set_state(data)
            if self.value.get('source') == 'pool':
                from .pools import bind_pool_layers
                displayed = bind_pool_layers([self.value], data)[0]
                items = displayed['sequence_items']
                displayed['sequence_cursor'] = next((i for i, item in enumerate(items)
                    if item['id'] == self.value.get('pool_next_id')),
                    self.value.get('pool_cursor', 0) % len(items) if items else 0)
                self.content.set_layer(displayed)
            self.error.clear()
        except Exception as error:
            self.error.setText('资源池暂不可用；原来源已保留：'+str(error))

    def edit(self, value):
        if value.get('pool_id', '') != self.value.get('pool_id', ''):
            value = dict(value, pool_cursor=0, pool_next_id='')
        self.value = normalized(value)
        if hasattr(self, 'timing'):
            self.timing.blockSignals(True)
            self.timing.setCurrentIndex(self.timing.findData(value['caption_timing']))
            self.timing.blockSignals(False)
        self.notify()

    def timing_changed(self, *_):
        self.value.update(caption_timing=self.timing.currentData(), caption_source=self.text_source.currentData(),
                          caption_language=self.language.text().strip(), caption_cloud_consent=self.consent.isChecked())
        mode = self.value['caption_timing']
        if mode in {'auto','task','model','elevenlabs'}:
            self.value.update(source='voice',source_file='',caption_file='')
        elif mode == 'file' and self.value.get('source') == 'voice':
            self.value.update(source='file',source_file=self.value.get('caption_file',''))
        self.content.set_layer(self.value)
        self.notify()

    def notify(self):
        try:
            value = normalized(self.value)
            self.params['source'] = copy.deepcopy(value)
            self.callback({'source': value})
            self.error.clear()
        except Exception as error:
            self.error.setText('来源设置未应用：'+str(error))


def source_inspector(params, changed, parent=None):
    return ComponentSourceEditor(params, changed, parent)

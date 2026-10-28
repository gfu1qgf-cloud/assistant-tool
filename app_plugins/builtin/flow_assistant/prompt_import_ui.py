"""Paste, inspect and explicitly accept segments for one chosen reference image."""
from qt_compat import QtCore,QtWidgets
from .prompt_segments import parse_segments


class PromptImportDialog(QtWidgets.QDialog):
    def __init__(self,image,parent=None):
        super().__init__(parent);self.setWindowTitle('识别多段提示词');self.resize(1080,740);self.segments=[]
        layout=QtWidgets.QVBoxLayout(self)
        note=QtWidgets.QLabel(f'全部段落使用：{image}\n每段默认10秒；标题秒数仅作参考，加入清单后可逐条修改。',self)
        note.setTextFormat(QtCore.Qt.TextFormat.PlainText);note.setWordWrap(True);layout.addWidget(note)
        split=QtWidgets.QSplitter(self)
        self.source=QtWidgets.QPlainTextEdit(self);self.source.setPlaceholderText('在这里粘贴整份提示词。支持“第1段｜8秒”、Segment 1、重复的视频描述／台词标签等格式。')
        split.addWidget(self.source)
        right=QtWidgets.QWidget(self);side=QtWidgets.QVBoxLayout(right);side.setContentsMargins(0,0,0,0)
        self.list=QtWidgets.QListWidget(self);side.addWidget(self.list,1)
        self.preview=QtWidgets.QPlainTextEdit(self);self.preview.setReadOnly(True);side.addWidget(self.preview,2)
        split.addWidget(right);layout.addWidget(split,1)
        self.status=QtWidgets.QLabel('识别只在本机拆分文本，不调用模型、不提交生成。',self)
        self.status.setWordWrap(True);self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText);layout.addWidget(self.status)
        row=QtWidgets.QHBoxLayout();parse=QtWidgets.QPushButton('识别／重新识别',self);parse.clicked.connect(self.parse)
        row.addWidget(parse);row.addStretch()
        self.add=QtWidgets.QPushButton('加入清单',self);self.add.setEnabled(False);self.add.clicked.connect(self.accept)
        cancel=QtWidgets.QPushButton('取消',self);cancel.clicked.connect(self.reject)
        row.addWidget(self.add);row.addWidget(cancel);layout.addLayout(row)
        self.list.currentRowChanged.connect(self.show_segment)
        self.source.textChanged.connect(self.invalidate)

    def invalidate(self):
        self.segments=[];self.list.clear();self.preview.clear();self.add.setEnabled(False)

    def parse(self):
        try:self.segments=parse_segments(self.source.toPlainText())
        except ValueError as error:self.status.setText(str(error));self.add.setEnabled(False);return
        self.list.clear()
        for n,segment in enumerate(self.segments,1):
            source=f' · 标注{segment.detected_duration}秒' if segment.detected_duration else ''
            item=QtWidgets.QListWidgetItem(f'{n}. {segment.title}{source} → 使用10秒'+(' · 请核对' if segment.warning else ''))
            item.setToolTip(segment.warning);self.list.addItem(item)
        self.list.setCurrentRow(0)
        valid=all(item.prompt.strip() for item in self.segments)
        self.add.setText(f'加入清单（{len(self.segments)}条）');self.add.setEnabled(valid)
        self.status.setText(f'识别到{len(self.segments)}段，{sum(bool(item.warning) for item in self.segments)}段有核对提示。'
                            +('只有标题的段落需先在左侧补正文，再重新识别。' if not valid else '请检查右侧完整内容，再加入清单。'))

    def show_segment(self,index):
        if 0<=index<len(self.segments):self.preview.setPlainText(self.segments[index].prompt)

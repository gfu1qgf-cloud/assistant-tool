"""Visible dialogue and required speaking rate; no promises about voice speed."""
from qt_compat import QtWidgets
from .prompt_segments import speech_metrics
from .status_ui import status_area


class SpeechReview(QtWidgets.QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.label=QtWidgets.QLabel('台词速查（随上方提示词更新）',self);layout.addWidget(self.label)
        self.text=QtWidgets.QPlainTextEdit(self);self.text.setReadOnly(True);self.text.setMaximumHeight(90)
        self.text.setPlaceholderText('识别到“台词／对白／旁白”后，在这里单独显示，方便校对。')
        layout.addWidget(self.text)
        self.metrics=QtWidgets.QLabel(self);layout.addWidget(status_area(self.metrics,2))

    def update_prompt(self,prompt,duration):
        text,metrics=speech_metrics(prompt,duration)
        if self.text.toPlainText()!=text:self.text.setPlainText(text)
        self.metrics.setText(metrics)
        self.metrics.setToolTip(metrics)
        if '（' in metrics:self.metrics.setText(metrics.split('（',1)[0].replace('内完整说完需',' · 所需'))

"""Model-aware chat controls. Disabled values are retained, never transmitted."""
from qt_compat import QtCore, QtWidgets
from model.AIChatCapabilities import capabilities
from .option_layout import inline_field


class AdvancedOptions(QtCore.QObject):
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.widget_parent=parent
        self.reasoning = self.combo((('default','模型默认'),('none','不思考'),('minimal','极少'),('low','低'),('medium','中'),('high','高'),('xhigh','很高'),('max','最高')))
        self.mode = self.combo((('default','模型默认'),('standard','标准'),('pro','Pro · 更多时间与费用')))
        self.verbosity = self.combo((('default','模型默认'),('low','简短'),('medium','适中'),('high','详细')))
        self.budget_enabled = QtWidgets.QCheckBox('自定义',parent); self.budget = QtWidgets.QSpinBox(parent)
        self.budget.setRange(-1,32768); self.budget.setValue(-1); self.budget.setSpecialValueText('动态（-1）')
        self.top_p_enabled = QtWidgets.QCheckBox('自定义',parent); self.top_p = QtWidgets.QDoubleSpinBox(parent)
        self.top_p.setRange(.01,1); self.top_p.setSingleStep(.05); self.top_p.setValue(1)
        self.web = QtWidgets.QCheckBox('开启',parent)
        self.web.setToolTip('默认关闭；开启可能另计费，按所选模式决定是否必须搜索。')
        self.search_limit=QtWidgets.QSpinBox(parent); self.search_limit.setRange(1,20); self.search_limit.setValue(3)
        self.search_limit.setFixedWidth(160)
        self.search_mode=self.combo((('auto','按需联网'),('required','必须搜索')))
        self.search_mode.setFixedWidth(190)
        self.note = QtWidgets.QLabel(parent); self.note.setWordWrap(True)
        parent.add_field('Top P',inline_field(parent,self.top_p_enabled,self.top_p))
        parent.add_field('思考等级',self.reasoning)
        parent.add_field('推理模式',self.mode)
        parent.add_field('思考预算',inline_field(parent,self.budget_enabled,self.budget))
        parent.add_field('回答详略',self.verbosity)
        parent.add_field('联网搜索',inline_field(parent,self.web,self.search_mode))
        parent.add_field('工具调用上限',self.search_limit)
        parent.add_field('',self.note,wide=True)
        for widget in (self.budget,self.top_p,self.search_limit):
            widget.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter)
        for widget in (self.reasoning,self.mode,self.verbosity,self.search_mode): widget.currentIndexChanged.connect(self.changed)
        for widget in (self.budget_enabled,self.top_p_enabled,self.web): widget.toggled.connect(self.changed)
        self.budget.valueChanged.connect(self.changed); self.top_p.valueChanged.connect(self.changed)
        self.search_limit.valueChanged.connect(self.changed)

    def combo(self, choices):
        widget = QtWidgets.QComboBox(self.widget_parent)
        for value,title in choices: widget.addItem(title,value)
        widget.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(8);widget.setFixedWidth(260)
        return widget

    def values(self):
        return {'reasoning_effort':self.reasoning.currentData(),'reasoning_mode':self.mode.currentData(),
                'verbosity':self.verbosity.currentData(), 'thinking_budget':self.budget.value() if self.budget_enabled.isChecked() else None,
                'top_p':self.top_p.value() if self.top_p_enabled.isChecked() else None,'web_search':self.web.isChecked(),
                'search_limit':self.search_limit.value(),'search_mode':self.search_mode.currentData()}

    def load(self, options):
        for widget,key in ((self.reasoning,'reasoning_effort'),(self.mode,'reasoning_mode'),(self.verbosity,'verbosity')):
            widget.setCurrentIndex(max(0,widget.findData(options.get(key) or 'default')))
        self.budget_enabled.setChecked(options.get('thinking_budget') is not None)
        self.budget.setValue(options.get('thinking_budget') if options.get('thinking_budget') is not None else -1)
        self.top_p_enabled.setChecked(options.get('top_p') is not None)
        self.top_p.setValue(options.get('top_p') if options.get('top_p') is not None else 1)
        self.web.setChecked(bool(options.get('web_search',False)))
        self.search_limit.setValue(options.get('search_limit',3))
        self.search_mode.setCurrentIndex(max(0,self.search_mode.findData(options.get('search_mode','auto'))))

    def sync(self, provider, model, row, options):
        caps = capabilities(provider,model,row,options)
        for index in range(self.reasoning.count()):
            self.reasoning.model().item(index).setEnabled(self.reasoning.itemData(index)=='default' or self.reasoning.itemData(index) in caps['efforts'])
        self.reasoning.setEnabled(bool(caps['efforts'])); self.mode.setEnabled(caps['pro'])
        self.verbosity.setEnabled(caps['verbosity']); self.web.setEnabled(caps['web'])
        self.search_limit.setEnabled(provider=='openai' and caps['web'] and self.web.isChecked())
        self.search_mode.setEnabled(provider=='openai' and caps['web'] and self.web.isChecked())
        self.budget_enabled.setEnabled(bool(caps['budget']))
        self.budget.setEnabled(bool(caps['budget']) and self.budget_enabled.isChecked())
        if caps['budget']:
            low,high,zero=caps['budget']
            self.budget.setToolTip(f'允许动态 -1，或 {low}–{high} Token。'+('0可关闭思考。' if zero else '此模型不能用0关闭思考。'))
        self.top_p_enabled.setEnabled(caps['sampling']); self.top_p.setEnabled(caps['sampling'] and self.top_p_enabled.isChecked())
        for widget in (self.reasoning,self.mode,self.verbosity,self.web,self.budget_enabled,self.top_p_enabled):
            widget.setToolTip('当前模型支持；按所选值发送。' if widget.isEnabled() else '当前模型／接口或模式不支持：保留原设置，但不发送。')
        self.web.setToolTip(self.web.toolTip()+' 默认关闭；开启联网可能另计费。')
        text=caps['note']+' 切换后灰色选项保留但不发送；联网由模型按需决定，不保证每条都搜索。'
        if caps['efforts'] and options.get('max_tokens',4096)<8192:
            text+=' 思考也占输出额度，当前上限较小，复杂问题可提高输出上限。'
        self.note.setText(text)
        return caps

    def effective(self, provider, model, row, options):
        values=self.values(); caps=capabilities(provider,model,row,options)
        if values['reasoning_effort'] not in caps['efforts']: values['reasoning_effort']='default'
        if not caps['pro']: values['reasoning_mode']='default'
        if not caps['verbosity']: values['verbosity']='default'
        if not caps['budget']: values['thinking_budget']=None
        if not caps['sampling']: values['top_p']=None
        if not caps['web']: values['web_search']=False
        return values

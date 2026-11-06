"""Global palette management and the same random-colour chooser at every field."""
import copy
from qt_compat import QtCore,QtGui,QtWidgets
from .palette_library import PaletteLibrary,canonical,new_palette
from .palette_rules import KEY,normalize,set_rule

def swatches(colors):
    pixmap=QtGui.QPixmap(140,24);pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter=QtGui.QPainter(pixmap)
    for i,color in enumerate(colors):painter.fillRect(round(i*140/len(colors)),0,round(140/len(colors))+1,24,QtGui.QColor(color))
    painter.end();return QtGui.QIcon(pixmap)

def parse_colors(text):
    values=[canonical(c.strip()) for c in text.replace('，',',').split(',') if c.strip()]
    if not 1<=len(values)<=32:raise ValueError('请选1–32种颜色，用逗号分隔。')
    return list(dict.fromkeys(values))

class PaletteManager(QtWidgets.QDialog):
    def __init__(self,parent=None,library=None):
        super().__init__(parent);self.setWindowTitle('我的颜色套餐 · 全局共用');self.resize(650,450)
        self.library=library or PaletteLibrary();state=self.library.load();self.revision=state['revision'];self.items=state['palettes'];self.index=-1;self.loading=False
        root=QtWidgets.QVBoxLayout(self);self.list=QtWidgets.QListWidget();self.list.setIconSize(QtCore.QSize(140,24));root.addWidget(self.list,1)
        form=QtWidgets.QFormLayout();root.addLayout(form);self.name=QtWidgets.QLineEdit();self.colors=QtWidgets.QLineEdit();form.addRow('套餐名称',self.name);form.addRow('套餐颜色',self.colors)
        self.colors.setPlaceholderText('#ffcc66,#ffffff,#66ccff（支持透明色）')
        row=QtWidgets.QHBoxLayout();root.addLayout(row)
        for label,slot in (('＋选择一种颜色',self.add_color),('新建套餐',self.add),('复制套餐',self.duplicate),('删除套餐',self.remove)):
            button=QtWidgets.QPushButton(label);button.setAutoDefault(False);button.clicked.connect(slot);row.addWidget(button)
        note=QtWidgets.QLabel('颜色套餐跨项目共用。选用时把颜色保存到该参数，导出和重试不依赖套餐库。\n修改或删除套餐后，原参数保留选用时的颜色；需要更新时重新选择套餐。');note.setWordWrap(True);root.addWidget(note)
        self.error=QtWidgets.QLabel();self.error.setWordWrap(True);root.addWidget(self.error)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('保存套餐')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.save);buttons.rejected.connect(self.reject);root.addWidget(buttons)
        self.list.currentRowChanged.connect(self.select);self.rebuild(0)

    def capture(self):
        if self.index<0:return True
        try:
            from .palette_library import validate
            self.items[self.index]=validate([{**self.items[self.index],'name':self.name.text(),'colors':parse_colors(self.colors.text())}])[0]
            item=self.list.item(self.index)
            if item:item.setText(self.items[self.index]['name']);item.setIcon(swatches(self.items[self.index]['colors']))
            self.error.clear();return True
        except ValueError as error:self.error.setText(str(error));return False

    def rebuild(self,index):
        self.loading=True;self.list.clear()
        for item in self.items:self.list.addItem(QtWidgets.QListWidgetItem(swatches(item['colors']),item['name']))
        self.index=-1;self.loading=False;self.list.setCurrentRow(min(index,len(self.items)-1))
        if not self.items:self.name.clear();self.colors.clear()

    def select(self,index):
        if self.loading:return
        if not self.capture():
            with QtCore.QSignalBlocker(self.list):self.list.setCurrentRow(self.index)
            return
        self.index=index
        if index>=0:
            item=self.items[index];self.name.setText(item['name']);self.colors.setText(','.join(item['colors']))

    def add_color(self):
        color=QtWidgets.QColorDialog.getColor(QtGui.QColor('#ffffff'),self,'添加到套餐',QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():self.colors.setText(self.colors.text()+(',' if self.colors.text().strip() else '')+canonical(color.name(color.NameFormat.HexArgb)))

    def add(self):
        if not self.capture():return
        if len(self.items)>=64:self.error.setText('最多64个套餐。');return
        self.items.append(new_palette('我的配色', ['#ffffff','#ffd166','#90e0ef']));self.rebuild(len(self.items)-1)

    def duplicate(self):
        if self.index<0 or not self.capture():return
        if len(self.items)>=64:self.error.setText('最多64个套餐。');return
        item=self.items[self.index];self.items.append(new_palette(item['name'][:55]+' 副本',item['colors']));self.rebuild(len(self.items)-1)

    def remove(self):
        if self.index<0:return
        index=self.index;self.items.pop(index);self.rebuild(max(0,index-1))

    def save(self):
        if not self.capture():return
        try:self.library.save(self.items,self.revision)
        except (ValueError,OSError) as error:self.error.setText(str(error));return
        self.accept()

class PaletteChoice(QtWidgets.QDialog):
    def __init__(self,initial,rule=None,parent=None,alpha=False,library=None):
        super().__init__(parent);self.setWindowTitle('颜色随机与套餐');self.resize(540,420)
        self.initial=initial;self.alpha=alpha;self.library=library or PaletteLibrary();self.rule=copy.deepcopy(rule)
        root=QtWidgets.QVBoxLayout(self);form=QtWidgets.QFormLayout();root.addLayout(form)
        self.mode=QtWidgets.QComboBox()
        for label,key in (('固定颜色（关闭随机）','fixed'),('同色系随机 · 基于当前颜色','family'),('套餐内随机 · 只使用我选的颜色','palette')):self.mode.addItem(label,key)
        form.addRow('颜色方式',self.mode)
        self.palette=QtWidgets.QComboBox();self.palette.setIconSize(QtCore.QSize(140,24));form.addRow('选择套餐',self.palette)
        self.colors=QtWidgets.QLineEdit();form.addRow('自选颜色',self.colors)
        self.colors.setToolTip('可直接填几种喜欢的颜色；临时使用也会随项目保存。')
        self.samples=QtWidgets.QLabel();root.addWidget(self.samples)
        self.seed=QtWidgets.QSpinBox();self.seed.setRange(0,2147483647);self.seed.installEventFilter(self);form.addRow('随机组编号',self.seed)
        self.ranges={}
        for key,title,default in (('hue','色相变化（度）',8.),('saturation','饱和度变化（%）',8.),('value','明暗变化（%）',10.)):
            spin=QtWidgets.QDoubleSpinBox();spin.setRange(0,30);spin.setValue((rule or {}).get(key,default));spin.installEventFilter(self);self.ranges[key]=spin;form.addRow(title,spin)
        row=QtWidgets.QHBoxLayout();root.addLayout(row)
        add=QtWidgets.QPushButton('＋选一种颜色');add.setAutoDefault(False);add.clicked.connect(self.add_color);row.addWidget(add)
        manage=QtWidgets.QPushButton('管理我的颜色套餐…');manage.setAutoDefault(False);manage.clicked.connect(self.manage);row.addWidget(manage)
        note=QtWidgets.QLabel('每个参数、每条视频固定取色，整条视频不闪色；预览、导出及失败重试使用同一结果。\n渐变／多层色板保留原色位数量，从套餐依次取色，套餐用完前不重复。');note.setWordWrap(True);root.addWidget(note)
        self.error=QtWidgets.QLabel();self.error.setWordWrap(True);root.addWidget(self.error)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('应用')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.confirm);buttons.rejected.connect(self.reject);root.addWidget(buttons)
        self.reload();self.mode.setCurrentIndex(self.mode.findData(rule['mode'] if rule else 'palette'))
        self.seed.setValue((rule or {}).get('seed',0))
        self.colors.setText(','.join((rule or {}).get('colors',self.items[0]['colors'] if self.items and rule is None else [initial.split(',')[0]])))
        if rule:
            index=self.palette.findData(rule.get('palette_id',''));self.palette.setCurrentIndex(max(0,index))
        elif self.items:self.palette.setCurrentIndex(1)
        self.palette.currentIndexChanged.connect(self.select);self.colors.textChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh);self.refresh()

    def eventFilter(self,widget,event):
        if event.type()==QtCore.QEvent.Type.Wheel:event.ignore();return True
        return super().eventFilter(widget,event)

    def reload(self):
        self.items=self.library.load()['palettes'];self.palette.clear();self.palette.addItem('临时自选颜色','')
        for item in self.items:self.palette.addItem(swatches(item['colors']),item['name'],item['id'])

    def select(self,index):
        identifier=self.palette.currentData();item=next((p for p in self.items if p['id']==identifier),None)
        if item:self.colors.setText(','.join(item['colors']))

    def refresh(self):
        palette=self.mode.currentData()=='palette';family=self.mode.currentData()=='family'
        self.palette.setEnabled(palette);self.colors.setEnabled(palette);self.seed.setEnabled(palette or family)
        for control in self.ranges.values():control.setEnabled(family)
        try:self.samples.setPixmap(swatches(parse_colors(self.colors.text())).pixmap(420,28))
        except ValueError:self.samples.clear()

    def add_color(self):
        color=QtWidgets.QColorDialog.getColor(QtGui.QColor('white'),self,'选择套餐颜色',QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel if self.alpha else QtWidgets.QColorDialog.ColorDialogOption(0))
        if color.isValid():self.colors.setText(self.colors.text()+','+canonical(color.name(color.NameFormat.HexArgb),self.alpha))

    def manage(self):
        try:
            dialog=PaletteManager(self,self.library)
            if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
                with QtCore.QSignalBlocker(self.palette):self.reload()
            dialog.deleteLater()
        except (ValueError,OSError) as error:self.error.setText(str(error))

    def confirm(self):
        mode=self.mode.currentData()
        try:
            if mode=='fixed':self.rule=None
            else:
                rule=dict(mode=mode,seed=self.seed.value(),alpha=self.alpha)
                if mode=='palette':rule.update(colors=parse_colors(self.colors.text()),palette_id=self.palette.currentData(),name=self.palette.currentText() if self.palette.currentData() else '自选颜色')
                else:rule.update({k:v.value() for k,v in self.ranges.items()})
                self.rule=normalize({'color':rule})['color']
        except ValueError as error:self.error.setText(str(error));return
        self.accept()

def edit_library(parent):
    try:dialog=PaletteManager(parent);dialog.exec();dialog.deleteLater()
    except (ValueError,OSError) as error:QtWidgets.QMessageBox.warning(parent,'颜色套餐',str(error))

class ButtonPalette(QtCore.QObject):
    def __init__(self,button,get_layer,path,commit):
        super().__init__(button);self.button=button;self.get_layer=get_layer;self.path=path;self.commit=commit
        self.tool=QtWidgets.QToolButton(button);self.tool.setAutoRaise(False);self.tool.setText('随机／套餐…')
        self.tool.setToolTip('同色系随机，或从我的颜色套餐中选择。');self.tool.clicked.connect(self.choose)
        button.setMinimumWidth(max(button.minimumWidth(),190));button.installEventFilter(self);self.position()
        button.setStyleSheet(button.styleSheet()+' QPushButton {text-align:left;padding-left:4px;padding-right:125px;}')
        if isinstance(button,QtWidgets.QLineEdit):button.setTextMargins(0,0,125,0)

    def position(self):
        width=min(125,max(90,self.button.width()//2));self.tool.setGeometry(self.button.width()-width-2,2,width,max(20,self.button.height()-4))

    def eventFilter(self,obj,event):
        if event.type() in (QtCore.QEvent.Type.Resize,QtCore.QEvent.Type.Show):self.position()
        if event.type() in (QtCore.QEvent.Type.Paint,QtCore.QEvent.Type.Show):
            rule=(self.get_layer() or {}).get(KEY,{}).get(self.path)
            title=('套餐：'+rule.get('name','自选') if rule['mode']=='palette' else '同色系随机') if rule else '随机／套餐…'
            if self.tool.text()!=title:self.tool.setText(title);self.tool.setToolTip(title+'；每条视频固定取色，点击修改。')
        return False

    def choose(self):
        layer=self.get_layer()
        if layer is None:return
        try:
            initial=layer.get(self.path,self.button.text())
            dialog=PaletteChoice(initial,layer.get(KEY,{}).get(self.path),self.button)
            if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
                set_rule(layer,self.path,dialog.rule);self.commit()
            dialog.deleteLater()
        except (ValueError,OSError) as error:QtWidgets.QMessageBox.warning(self.button,'颜色随机',str(error))

def attach_button(button,get_layer,path,commit):
    button.palette_choice=ButtonPalette(button,get_layer,path,commit)

"""A small style list and the existing caption inspector editing a private draft."""
import copy
from uuid import uuid4
from qt_compat import QtCore, QtGui, QtWidgets
from . import caption_rotation as rotation


class StyleDialog(QtWidgets.QDialog):
    def __init__(self, layer, entry, state, canvas_size, services=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('编辑字幕样式');self.resize(1000,760)
        self.state=state;self.canvas_size=canvas_size;self.result_entry=None
        self.entry=copy.deepcopy(entry)
        self.layer=rotation.single(layer);self.layer.update(copy.deepcopy(entry['settings']))
        self.layer.update(name=entry['name'],opacity=entry['settings'].get('opacity',100),enabled=True)
        layout=QtWidgets.QVBoxLayout(self);row=QtWidgets.QHBoxLayout();layout.addLayout(row,1)
        left=QtWidgets.QVBoxLayout();row.addLayout(left)
        self.preview=QtWidgets.QLabel();self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(300,534);self.preview.setStyleSheet('background:#242830;color:white;')
        left.addWidget(self.preview)
        self.sample=QtWidgets.QLineEdit('字幕样式预览 · This is a caption preview')
        self.sample.setPlaceholderText('预览文字');left.addWidget(self.sample)
        self.clock=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal);self.clock.setRange(0,1000);self.clock.setValue(500)
        left.addWidget(self.clock);left.addStretch()
        from .caption_ui import caption_property_panel
        self.editor=caption_property_panel(services,mode='variant')
        self.editor.edited.connect(self.edit)
        scroll=QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(self.editor);row.addWidget(scroll,1)
        self.note=QtWidgets.QLabel();self.note.setWordWrap(True);layout.addWidget(self.note)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('保存')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(180);self.timer.timeout.connect(self.refresh)
        self.clock.valueChanged.connect(lambda:self.timer.start());self.sample.textChanged.connect(lambda:self.timer.start())
        self.editor.bind(self.layer,canvas_size,state,'percent');self.timer.start()

    def edit(self,_identifier,patch):
        from .layers import normalize_layers
        try:
            value=copy.deepcopy(self.layer);value.update(patch)
            self.layer=normalize_layers([value])[0]
            self.editor.bind(self.layer,self.canvas_size,self.state,'percent')
            self.note.clear();self.timer.start()
        except (ValueError,TypeError) as error:self.note.setText(str(error))

    def refresh(self):
        try:
            from .cursor_pool import bind,resolve
            from .caption_style_render import render_design
            value=copy.deepcopy(self.layer);value['text']=self.sample.text().strip() or '字幕预览'
            bind(value,self.state);resolve(value)
            width,height=self.canvas_size.width(),self.canvas_size.height()
            scale=min(1.,520/max(width,height));width=max(2,round(width*scale));height=max(2,round(height*scale))
            duration=4.
            image,_=render_design(value,width,height,duration=duration,time=duration*self.clock.value()/1000)
            self.preview.setPixmap(QtGui.QPixmap.fromImage(image).scaled(self.preview.size(),QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation));self.note.clear()
        except Exception as error:self.note.setText('预览：'+str(error))

    def accept(self):
        name=self.layer['name'].strip()
        if not name or len(name)>80:self.note.setText('样式名称请填写1到80个字符。');return
        self.result_entry=dict(id=self.entry['id'],name=name,settings=rotation.snapshot(self.layer))
        self.timer.stop();super().accept()

    def done(self,result):
        self.timer.stop();super().done(result)


class RotationEditor(QtWidgets.QWidget):
    changed=QtCore.pyqtSignal(object)

    def __init__(self,panel,services=None):
        super().__init__();self.panel=panel;self.services=services;self.layer=None;self.state={};self.loading=False
        self.canvas_size=QtCore.QSize(1080,1920)
        body=QtWidgets.QVBoxLayout(self);body.setContentsMargins(0,0,0,0)
        self.enable=QtWidgets.QCheckBox('每条成片轮换一套样式');body.addWidget(self.enable)
        self.enable.toggled.connect(self.toggle)
        self.styles=QtWidgets.QComboBox();self.styles.setToolTip('选择后点击编辑；不消耗轮换顺序。');body.addWidget(self.styles)
        self.next_label=QtWidgets.QLabel();body.addWidget(self.next_label)
        row=QtWidgets.QHBoxLayout();body.addLayout(row);self.buttons={}
        for key,title,callback in (('add','添加',self.add),('edit','编辑…',self.edit),('copy','复制',self.duplicate),
                                   ('remove','删除',self.remove),('up','↑',lambda:self.move(-1)),('down','↓',lambda:self.move(1))):
            button=QtWidgets.QPushButton(title);button.setAutoDefault(False);button.clicked.connect(callback)
            if key in {'up','down'}:button.setFixedWidth(32)
            row.addWidget(button);self.buttons[key]=button
        self.styles.currentIndexChanged.connect(self.sync_buttons)

    def set_pool_state(self,state):self.state=state
    def set_canvas_size(self,width,height):self.canvas_size=QtCore.QSize(width,height)

    def set_layer(self,layer):
        selected=self.styles.currentData() if self.layer and layer and self.layer['id']==layer['id'] else None
        self.loading=True;self.layer=copy.deepcopy(layer)
        try:
            self.enable.setChecked(bool(layer and layer.get('caption_rotation_enabled')))
            self.styles.clear()
            entries=layer.get('caption_variants',[]) if layer else []
            for i,entry in enumerate(entries):self.styles.addItem(f'{i+1}. {entry["name"]}',entry['id'])
            if selected:self.styles.setCurrentIndex(max(0,self.styles.findData(selected)))
            next_index=layer.get('caption_variant_cursor',0)%len(entries) if entries else 0
            self.next_label.setText('下次：'+entries[next_index]['name'] if entries else '添加样式后即可轮换')
            self.sync_buttons();self.sync_appearance()
        finally:self.loading=False

    def sync_buttons(self,*_args):
        index=self.styles.currentIndex();count=self.styles.count()
        for key,button in self.buttons.items():button.setEnabled(bool(self.layer) and
            (count<rotation.MAX_STYLES if key=='add' else index>0 if key=='up' else 0<=index<count-1 if key=='down' else index>=0))

    def sync_appearance(self):
        enabled=not self.enable.isChecked()
        cp=self.panel.caption_properties
        for widget in (self.panel.text_properties,cp.sections,cp.typography_widget,cp.rhythm_section,cp.follow_section,
                       self.panel.caption_appearance,self.panel.caption_background,cp.parameter_button):
            widget.setEnabled(enabled);widget.setVisible(enabled)
        for key in ('caption_clip_feather','caption_clip_axis'):
            field=cp.controls[key];field.setEnabled(enabled);field.setVisible(enabled)
            label=cp.form_for(field).labelForField(field)
            if label:label.setVisible(enabled)
        if enabled:cp.update_display_controls()
        cp.parameter_button.setToolTip('请在上方轮换列表中编辑具体样式。' if not enabled else '查看并调整当前字幕参数。')

    def publish(self,entries,cursor=None):
        if not self.layer:return
        candidate=copy.deepcopy(self.layer);old=candidate.get('caption_variants',[])
        next_id=old[candidate.get('caption_variant_cursor',0)%len(old)]['id'] if old else None
        candidate['caption_variants']=entries
        candidate['caption_variant_cursor']=cursor if cursor is not None else next((i for i,e in enumerate(entries) if e['id']==next_id),0)
        try:rotation.normalize(candidate)
        except ValueError as error:QtWidgets.QMessageBox.warning(self,'未修改样式',str(error));return
        self.set_layer(candidate)
        self.changed.emit({k:copy.deepcopy(candidate[k]) for k in rotation.DEFAULTS})

    def toggle(self,enabled):
        if self.loading or not self.layer:return
        self.layer['caption_rotation_enabled']=enabled;self.publish(copy.deepcopy(self.layer.get('caption_variants',[])))

    def dialog(self,entry):
        window=StyleDialog(self.layer,entry,self.state,self.canvas_size,self.services,self)
        try:return window.result_entry if window.exec()==QtWidgets.QDialog.DialogCode.Accepted else None
        finally:window.deleteLater()

    def unique_name(self,base):
        names={e['name'] for e in self.layer.get('caption_variants',[])};name=base;i=2
        while name in names:name=base[:70]+' '+str(i);i+=1
        return name

    def add(self):
        if not self.layer or self.styles.count()>=rotation.MAX_STYLES:return
        entry=dict(id=uuid4().hex,name=self.unique_name('样式 '+str(self.styles.count()+1)),settings=rotation.snapshot(self.layer))
        entry['settings']['opacity']=100
        value=self.dialog(entry)
        if value:
            self.publish(copy.deepcopy(self.layer['caption_variants'])+[value]);self.styles.setCurrentIndex(self.styles.count()-1)

    def edit(self):
        index=self.styles.currentIndex()
        if index<0:return
        entries=copy.deepcopy(self.layer['caption_variants']);value=self.dialog(entries[index])
        if value:entries[index]=value;self.publish(entries)

    def duplicate(self):
        index=self.styles.currentIndex()
        if index<0 or self.styles.count()>=rotation.MAX_STYLES:return
        entries=copy.deepcopy(self.layer['caption_variants']);value=copy.deepcopy(entries[index])
        value.update(id=uuid4().hex,name=self.unique_name(value['name']+' 副本'));entries.insert(index+1,value)
        self.publish(entries);self.styles.setCurrentIndex(index+1)

    def remove(self):
        index=self.styles.currentIndex()
        if index<0:return
        entries=copy.deepcopy(self.layer['caption_variants']);entries.pop(index);self.publish(entries)

    def move(self,direction):
        index=self.styles.currentIndex();target=index+direction
        if index<0 or not 0<=target<self.styles.count():return
        entries=copy.deepcopy(self.layer['caption_variants']);entries[index],entries[target]=entries[target],entries[index]
        self.publish(entries);self.styles.setCurrentIndex(target)


def install(panel,services,mode):
    if mode=='variant':
        panel.geometry_editor.hide();panel.timing_editor.hide()
        cp=panel.caption_properties
        original=cp.update_timing_controls
        def timing():
            original()
            for key in ('caption_timing','caption_source','caption_language','caption_cloud_consent'):
                field=cp.controls[key];field.hide()
                label=next((form.labelForField(field) for form in cp.forms.values() if form.labelForField(field)),None)
                if label:label.hide()
            cp.file_row.hide();label=cp.form_for(cp.file_row).labelForField(cp.file_row)
            if label:label.hide()
        cp.update_timing_controls=timing;timing()
        return
    manager=panel.add_bound('rotation','样式轮换',RotationEditor(panel,services),fields=set(rotation.DEFAULTS),expanded=True)
    panel.rotation_editor=manager
    section=panel.sections['rotation'];panel.body.removeWidget(section)
    panel.body.insertWidget(1 if hasattr(panel,'content_editor') else 0,section)
